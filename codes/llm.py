"""llm.py — 轻量级 LLM 调用层（无 litellm，纯 requests + 标准库）。

设计目标
--------
替换原基于 litellm 的实现：litellm 导入慢（秒级）、依赖重，而本项目实际
只使用两种 API 调用范式，直接用 requests（已在 requirements.txt 的 [BASE]
依赖中）实现即可在毫秒级导入、零新增依赖。

支持的两种范式（由 provider.config 的 type 字段决定）：
  1. openai   — OpenAI 兼容范式（OpenAI 官方 / DeepSeek / 各类中转站）
                POST /chat/completions + SSE 流式
  2. anthropic— Anthropic 原生范式（Claude 官方）
                POST /messages + SSE 事件流（message_start / content_block_*）

对外接口与原实现完全一致（agent.py / main.py 无需改动）：
  - complete(...)          非流式调用（同步收集器，补回 reasoning_content）
  - complete_stream(...)   流式调用（yield text/reasoning/tool_call_chunk/usage，
                           迭代结束后 StopIteration.value = (content, extras)）
  - start_prewarm()        no-op，保留签名兼容（requests 无需预热）

与 litellm 版的行为对齐点：
  - 中断机制：interrupt_event 置位后返回已积累内容，extras["interrupted"]=True，
    tool_calls 置空（不返回不完整调用）
  - 流式聚合：text / reasoning_content / tool_calls 增量拼接，usage 序列化
  - 损坏 tool_calls 检测：arguments 非法 JSON 时记入 extras["tool_calls_invalid"]
  - OpenAI 兼容端点在 400 且报错含 temperature 时去掉该参数重试一次
    （对齐原 litellm drop_params=True 对 gpt-5 系不支持 temperature!=1 的兜底）
  - API 不稳定自愈（对齐 litellm 重试能力）：
      · 429/5xx → HTTP 层指数退避重试（_MAX_HTTP_ATTEMPTS=3）
      · 连接类错误（DNS/握手/SSL/重置）→ HTTP 层指数退避重试
      · 流式超时 / 连接中断 / 空响应（均未产生内容时）→ 流级重试
        （_MAX_STREAM_RETRIES=2，未产生 chunk 无重复计费风险）
  - Anthropic 分段上报的 usage（message_start + message_delta）自动合并
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Generator, Optional

import requests

from codes import config as _config
from codes import provider_config
from codes._log import logger

# ────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────

# 价格单位为 USD / 1M tokens；集中维护，避免 REPL 与 Agent 价格表漂移。
MODEL_PRICING = {
    "deepseek-flash": {"input": 0.5, "output": 2.0},  # 2026-09-10：官方统一名 deepseek-flash（旧名 deepseek-v4-flash 已下线），沿用 Flash 价
    "deepseek-v4-flash": {"input": 0.5, "output": 2.0},
    "deepseek-v4-pro": {"input": 2.0, "output": 8.0},
    "gpt-5.4": {"input": 10.0, "output": 30.0},
}

def get_model_pricing(model_name: str | None) -> tuple[float, float]:
    """按模型名返回输入/输出单价；未知模型返回零价格。"""
    if not model_name:
        return 0.0, 0.0
    for key, price in MODEL_PRICING.items():
        if key in model_name:
            return price["input"], price["output"]
    return 0.0, 0.0

def estimate_cost(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    """按 token 数估算 USD 成本；未知模型不估价。"""
    in_price, out_price = get_model_pricing(model)
    return prompt_tokens / 1_000_000 * in_price + completion_tokens / 1_000_000 * out_price

# 流式中断轮询间隔（秒）。通过 select 非阻塞等待 socket 可读，
# 每次唤醒后检查 interrupt_event / 总超时，保证 Ctrl+C 与 web 场景
# 中断延迟可控（无 worker 线程也能及时响应，对齐原 50ms 轮询）。
_POLL_INTERVAL = 0.05

def _stream_read_timeout(timeout: int) -> int:
    """流式 urllib3 读超时（防御性，秒）。

    为什么必须晚于外层空闲超时触发：
      select 可读时才 read（通常立即返回），读超时几乎不触发；但 select 与
      read 之间存在竞态（数据在 select 后被对端关闭等），read 可能阻塞。
      若 urllib3 读超时早于空闲超时（调用方可传超大值）触发，ReadTimeoutError
      会被当作"轮询点"continue，可能损坏底层 _fp 导致整个流中断。
      因此读超时 = timeout + 30（下限 60），保证空闲判定优先。

    修复（2026-08-10）：旧实现固定 300s 下限导致【响应头等待阶段】无有效
    超时——服务端接受 TCP 连接但永不返回响应头时，session.post() 挂起最长
    300s+，且 requests 的 ReadTimeout 不被 complete_stream 捕获（仅捕获内置
    TimeoutError），卡死线程残留导致"无报错不执行"。去掉 300s 下限后，
    POST 阶段读超时受控为 timeout+30，配合 ReadTimeout→TimeoutError 转换，
    连接建立阶段也能进入空闲超时/重试/兜底路径。
    """
    return max(timeout + 30, 60)

# Anthropic 官方 API 要求 max_tokens 必填；取 litellm 默认值保持行为一致。
_ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

# 各范式默认端点（对齐 litellm 内置默认；自定义端点由配置 base_url 覆盖）。
_DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

# 连接超时（秒）：建立 TCP/TLS 连接的上限，独立于流式读超时。
_CONNECT_TIMEOUT = 30


# ────────────────────────────────────────────────────────────────
# 统一异常 + 重试策略 + 连接复用（健壮性增强，对齐 litellm 能力）
# ────────────────────────────────────────────────────────────────

class LLMError(RuntimeError):
    """LLM 调用统一异常（RuntimeError 子类，兼容现有 except RuntimeError 捕获）。

    携带 provider / model / status_code / stage 上下文，便于上层日志定位与重试决策；
    覆盖 API 错误、SSE 读失败等非超时类错误；超时仍为 TimeoutError（语义特殊）。

    stage 语义（决定 complete_stream 是否可安全重试）：
      - "api"     : HTTP 非 200（HTTP 层已指数退避重试 3 次，流层不再重试）
      - "connect" : POST 阶段连接失败（HTTP 层已指数退避重试 3 次，流层不再重试）
      - "stream"  : SSE 流中途中断（HTTP 已成功；未产生内容时流层可重试）
    """
    def __init__(self, message, *, provider: str = "", model: str = "",
                 status_code: Optional[int] = None, stage: str = "api"):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.stage = stage


# 可自动重试的 HTTP 状态码：限流 / 服务端瞬时抖动（幂等，未开始流式无重复计费风险）。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 总尝试次数（含首次）：最多重试 2 次，共 3 次尝试。
_MAX_HTTP_ATTEMPTS = 3
# 重试基础退避（秒），指数增长：0.5 / 1.0 / 2.0。
_RETRY_BACKOFF = 0.5


def _retryable_hint(status: int) -> str:
    """可重试状态码 → 日志提示文案。"""
    if status == 429:
        return "限流"
    return "服务端瞬时错误"


# 线程级复用 Session：TCP/TLS keep-alive，避免每次请求重建握手；
# thread-local 隔离保证多线程（web 多 session）并发安全。
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """返回当前线程的复用 Session（首次创建并缓存）。"""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        _thread_local.session = s
    return s


# ────────────────────────────────────────────────────────────────
# 基础工具
# ────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────
# OpenCode Go 兼容：x-opencode-session / User-Agent
# ────────────────────────────────────────────────────────────────
_OPENCODE_GO_MARK = "opencode.ai"

# 进程级 fallback session id：无会话上下文时使用（进程内稳定）。
_FALLBACK_SESSION_ID = "xkagent-" + uuid.uuid4().hex[:16]


def _opencode_session_id() -> str:
    """当前会话的稳定 session id（供 OpenCode Go 的 x-opencode-session 头）。

    优先取激活的会话上下文名（SessionContext.name，会话级稳定）；
    无会话上下文（如启动自检）时回退到进程级 fallback。
    """
    ctx = _config.get_active_session_context()
    if ctx is not None and getattr(ctx, "name", None):
        return str(ctx.name)
    return _FALLBACK_SESSION_ID


def _opencode_extra_headers(base_url: str) -> dict:
    """OpenCode Go 要求的附加请求头（base_url 含 opencode.ai 时）。

    官方文档要求（opencode.ai/docs/go/#where-can-i-use-it）：
      1. 使用自定义 User-Agent（而非通用 HTTP 库名）
      2. 每个会话发送稳定 x-opencode-session 头
    缺失 x-opencode-session 时服务端拒绝请求（400 MissingSessionID）。
    """
    if not base_url or _OPENCODE_GO_MARK not in base_url:
        return {}
    return {
        "User-Agent": "xkagent/1.0",
        "x-opencode-session": _opencode_session_id(),
    }


def _resolve_provider(provider: str) -> dict:
    """解析 provider 配置：配置文件 > 环境变量 > 内置默认。"""
    return provider_config.get_provider(provider)


def _serialize_usage(usage) -> dict:
    """统一 usage 序列化（OpenAI 解析后为 dict，防御性兼容对象）。

    除 prompt/completion/total 外，额外提取 reasoning_tokens（思考 token）：
      - OpenAI 兼容格式：usage.reasoning_tokens 或 usage.completion_tokens_details.reasoning_tokens
      - Anthropic 等无该字段的模型返回 0（由上层走"长度比估算"降级）
    """
    def _reasoning(d):
        rt = d.get("reasoning_tokens", 0) or 0
        if not rt:
            _det = d.get("completion_tokens_details") or {}
            rt = (_det.get("reasoning_tokens", 0) if isinstance(_det, dict) else 0) or 0
        return rt

    if isinstance(usage, dict):
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "reasoning_tokens": _reasoning(usage),
        }
    _rt = getattr(usage, "reasoning_tokens", 0) or 0
    if not _rt:
        _det = getattr(usage, "completion_tokens_details", None) or {}
        _rt = (_det.get("reasoning_tokens", 0) if isinstance(_det, dict) else 0) or 0
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
        "reasoning_tokens": _rt,
    }


def split_reasoning_tokens(usage: dict, reasoning_len: int = 0, content_len: int = 0) -> tuple:
    """拆分 thinking（reasoning）token，返回 (reasoning_tokens, content_tokens)。

    双轨策略（对齐 opencode）：
      轨道 A：服务器返回了 reasoning_tokens → 直接采用（精确）。
      轨道 B：服务器未返回（DeepSeek/Anthropic 等）→ 用 reasoning 与正文的
             字符长度比估算：reasoning_tokens ≈ completion × len(reasoning)/(len(reasoning)+len(content))。
    约束：reasoning_tokens ∈ [0, completion_tokens]，content_tokens = completion - reasoning。
    """
    completion = usage.get("completion_tokens", 0) or 0
    rt = usage.get("reasoning_tokens", 0) or 0
    if rt <= 0:
        # 轨道 B：纯长度比估算
        total_len = (reasoning_len or 0) + (content_len or 0)
        if completion > 0 and total_len > 0:
            rt = round(completion * (reasoning_len or 0) / total_len)
    rt = max(0, min(rt, completion))
    return rt, completion - rt


def _get_stream_socket(resp: requests.Response):
    """从流式响应中取出底层 socket（用于 select 非阻塞轮询）。

    为什么需要底层 socket：
      直接 resp.raw.read() 在无数据时阻塞，无法插入中断检查点；而把它
      当作"轮询粒度"设置短读超时（如 0.5s）会在模型 thinking 停顿
      （可达 30s+）时触发 urllib3 ReadTimeoutError 并关闭内部 _fp，
      导致整个流中断（真实网络实测）。正确做法是 select 等待可读，
      可读时才 read（此时立即返回，不会超时）。

    访问路径：urllib3 2.x 的 HTTPResponse.connection.sock（SSLSocket，
    有 fileno 可用于 select）。取不到时返回 None，调用方退化为
    长读超时直读（中断粒度变差但不影响功能）。
    """
    raw = resp.raw
    conn = getattr(raw, 'connection', None) or getattr(raw, '_connection', None)
    sock = getattr(conn, 'sock', None) if conn is not None else None
    if sock is not None and hasattr(sock, 'fileno'):
        return sock
    # fallback: 取不到 connection.sock 时，尝试 raw.fileno() / raw._fp.fileno()
    # （urllib3 2.x HTTPResponse.fileno -> _fp.fileno -> socket fd），保证 select 可用。
    for _cand in (raw, getattr(raw, '_fp', None)):
        if _cand is None:
            continue
        try:
            _fd = _cand.fileno()
        except Exception:
            continue
        if _fd is not None and _fd >= 0:
            return _fd
    return None


def _iter_sse_lines(resp: requests.Response,
                    interrupt_event: Optional[threading.Event],
                    timeout: float) -> Generator[str, None, None]:
    """从流式响应迭代 SSE 的 data 行 payload（不含 'data: ' 前缀）。

    实现要点（select + 可读时 read，兼顾中断粒度与真实网络稳定性）：
      - 每 _POLL_INTERVAL 秒 select 一次 socket，检查 interrupt_event 与
        空闲超时 → 50ms 级中断响应（无 worker 线程也能及时响应 Ctrl+C）
      - socket 可读时才 resp.raw.read()（此时数据已就绪立即返回，
        不会触发 urllib3 读超时；读超时参数设为大值仅作防御）
      - 取不到底层 socket（非标准 urllib3 响应）时退化为直接 read，
        中断粒度变差但功能不受影响

    语义：
      - interrupt_event 置位 → 立即结束（调用方据此构造 interrupted extras）
      - 空闲超时（对齐 litellm read-timeout 语义）：距上次收到数据超过
        timeout 秒才判定超时；长输出/长思考（持续产出）不触发，
        总时长不做限制（即使生成 24h 也允许，只要持续产出）
      - 流结束 / 连接中断 → 结束迭代
    """
    start = time.monotonic()
    last_activity = start          # 上次收到数据的时间（空闲超时基准）
    sock = _get_stream_socket(resp)
    # sock 取不到时退化为短读超时轮询：设底层 socket 短超时，让 resp.raw.read()
    # 定期返回（ReadTimeoutError 在下文作为轮询点 continue），从而能及时检查
    # interrupt_event，避免无限直读阻塞导致中止按钮/Ctrl+C 失效。
    if sock is None:
        try:
            _fp = getattr(resp.raw, '_fp', None)
            if _fp is not None and getattr(_fp, 'fp', None) is not None:
                _fp.fp.settimeout(_POLL_INTERVAL)
        except Exception:
            pass
    buf = b""
    data_lines: list[str] = []   # 累积当前事件的 data 行（支持多行 data，W4）
    while True:
        if interrupt_event is not None and interrupt_event.is_set():
            return
        now = time.monotonic()
        idle = now - last_activity
        # 空闲超时：距上次收到数据超过 timeout 秒 → 判定卡死（对齐 litellm）。
        # 只要服务端持续产出（含 keep-alive 心跳行），last_activity 持续刷新，
        # 长输出不会因总时长被误杀（修复 received_chunks>0 仍超时的问题）。
        if idle > timeout:
            raise TimeoutError(
                f"LLM stream timeout after {timeout}s "
                f"(idle {idle:.0f}s, no data received)")
        if sock is not None:
            try:
                import select
                rdy, _, _ = select.select([sock], [], [], _POLL_INTERVAL)
            except Exception:
                rdy = []  # select 失败退化为直读
            if not rdy:
                continue  # 无数据：轮询点
        try:
            piece = resp.raw.read(4096)
        except Exception as e:
            # 读超时视为轮询点；其它异常表示流被异常截断（连接重置等），
            # 抛 LLMError（status_code=None）由 complete_stream 决定是否重试。
            if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                continue
            raise LLMError(f"LLM SSE read failed: {e}", stage="stream") from e
        if not piece:
            break  # 流正常结束：跳出后处理 buf 残留 + flush data_lines
        last_activity = time.monotonic()   # 收到任何字节即视为活跃（含心跳行）
        buf += piece
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                # 累积 data 行；SSE 规范允许 data 跨行（多行用 \n 连接）
                payload = line[5:].strip().decode("utf-8", errors="replace")
                data_lines.append(payload)
            elif not line:
                # 空行 = SSE 事件分隔符：flush 当前事件的全部 data 行
                if data_lines:
                    joined = "\n".join(data_lines)
                    data_lines = []
                    if joined:
                        yield joined
            # 其它行（event: / id: 等）忽略但保持累积（它们属于当前事件前缀）
    # 流末处理 buf 残留：最后一行可能无换行（read 边界），必须补处理
    if buf:
        line = buf.strip()
        if line.startswith(b"data:"):
            payload = line[5:].strip().decode("utf-8", errors="replace")
            data_lines.append(payload)
    # 流末 flush 残余（服务端未以空行结尾时兜底）
    if data_lines:
        joined = "\n".join(data_lines)
        if joined:
            yield joined


# ────────────────────────────────────────────────────────────────
# OpenAI 兼容范式
# ────────────────────────────────────────────────────────────────

def _ensure_assistant_reasoning_content(messages: list[dict],
                                        tools: Optional[list[dict]]) -> list[dict]:
    """DeepSeek thinking+tools：history 里每条 assistant 必须带 reasoning_content 字段。

    本地合成的 assistant（如技能注入）通常没有该字段，缺字段会在带 tools 的请求中 400。
    无思维链时用空串占位；已有则原样保留。
    """
    if not tools:
        return messages
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and "reasoning_content" not in m:
            out.append({**m, "reasoning_content": ""})
        else:
            out.append(m)
    return out


def _iter_openai_events(cfg: dict, provider: str, messages: list[dict],
                        model: Optional[str], temperature: float,
                        response_format: Optional[str],
                        reasoning_effort: Optional[str], thinking: Optional[bool],
                        tools: Optional[list[dict]], timeout: int,
                        interrupt_event: Optional[threading.Event]
                        ) -> Generator[tuple, None, None]:
    """OpenAI 兼容范式：POST /chat/completions + SSE，yield 统一事件 (kind, data)。

    kind ∈ {"text", "reasoning", "tool", "usage", "finish", "model"}
      "tool" 事件数据为 (index, meta_dict, args_delta)，对应一个 tool_call 增量片。
    """
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    base = cfg.get("base_url") or _DEFAULT_ENDPOINTS["openai"]
    url = base.rstrip("/") + "/chat/completions"
    headers.update(_opencode_extra_headers(base))

    api_messages = _ensure_assistant_reasoning_content(messages, tools)
    body: dict = {
        "model": model or cfg["default_model"],
        "messages": api_messages,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if thinking:
        body["thinking"] = {"type": "enabled"}
    if tools:
        body["tools"] = tools
    if response_format == "json":
        body["response_format"] = {"type": "json_object"}

    session = _get_session()
    for attempt in range(_MAX_HTTP_ATTEMPTS):
        try:
            resp = session.post(url, headers=headers, json=body, stream=True,
                                timeout=(_CONNECT_TIMEOUT, _stream_read_timeout(timeout)))
        except requests.exceptions.ReadTimeout as e:
            # 响应头等待超时（服务端接受连接但不返回响应头）：统一转为内置
            # TimeoutError，让 complete_stream 的空闲超时重试/兜底路径生效
            # （内置 TimeoutError 才被捕获；requests.ReadTimeout 是独立异常类）。
            raise TimeoutError(
                f"LLM response header timeout after {_stream_read_timeout(timeout)}s "
                f"({_brief_exc(e)})") from e
        except requests.exceptions.ConnectionError as e:
            # 连接类错误（DNS/握手/SSL/代理/连接重置）：网络抖动最常见，
            # 指数退避重试；3 次仍失败 → LLMError（status_code=None）。
            # 注：ConnectTimeout 是 ConnectionError 子类，走此分支保留 HTTP 层重试。
            if attempt < _MAX_HTTP_ATTEMPTS - 1:
                delay = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    f"LLM 连接失败（{_brief_exc(e)}），{delay:.1f}s 后重试第 "
                    f"{attempt + 1}/{_MAX_HTTP_ATTEMPTS - 1} 次"
                    f"（provider={provider}, model={model or cfg.get('default_model')}）")
                time.sleep(delay)
                continue
            raise LLMError(
                f"LLM 连接失败: {_brief_exc(e)}",
                provider=provider, model=model or cfg.get("default_model", ""),
                stage="connect") from e
        status = resp.status_code
        # 400 → drop_params：部分模型（如 gpt-5 系）不支持 temperature != 1，
        # 收到 400 且报错含 temperature 时去掉该参数重发（对齐 litellm drop_params）。
        if status == 400 and attempt == 0:
            _err_text = (resp.text or "").lower()
            if "temperature" in _err_text and "temperature" in body:
                logger.warning("LLM API 拒绝 temperature 参数，去掉后重试（drop_params 兼容）")
                resp.close()
                body.pop("temperature", None)
                continue
            if "reasoning_effort" in _err_text and "reasoning_effort" in body:
                logger.warning("LLM API 拒绝 reasoning_effort 参数（值域不支持），去掉后重试（drop_params 兼容）")
                resp.close()
                body.pop("reasoning_effort", None)
                continue
        # 429/5xx → 自动重试（指数退避；此时尚未开始流式，无重复计费风险）
        if status in _RETRYABLE_STATUS and attempt < _MAX_HTTP_ATTEMPTS - 1:
            delay = _RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                f"LLM API {status}（{_retryable_hint(status)}），"
                f"{delay:.1f}s 后重试第 {attempt + 1}/{_MAX_HTTP_ATTEMPTS - 1} 次"
                f"（provider={provider}, model={model or cfg.get('default_model')}）")
            resp.close()
            time.sleep(delay)
            continue
        if status != 200:
            detail = (resp.text or "")[:500]
            resp.close()
            raise LLMError(
                f"LLM API error {status}: {detail}",
                provider=provider, model=model or cfg.get("default_model", ""),
                status_code=status)
        break

    with resp:
        for payload in _iter_sse_lines(resp, interrupt_event, timeout):
            if payload == "[DONE]":
                break
            try:
                # strict=False：兼容 SSE 多行 data 拼接后字符串内的裸换行
                # （SSE 规范允许 data 跨行，拼接产物可能含控制字符）
                chunk = json.loads(payload, strict=False)
            except json.JSONDecodeError:
                continue
            # usage-only chunk（OpenAI 流式末片）
            if not chunk.get("choices"):
                if chunk.get("usage"):
                    yield ("usage", _serialize_usage(chunk["usage"]))
                continue
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}

            content_delta = delta.get("content") or ""
            if content_delta:
                yield ("text", content_delta)

            reasoning_delta = delta.get("reasoning_content") or ""
            if reasoning_delta:
                yield ("reasoning", reasoning_delta)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                meta = {}
                if tc.get("id"):
                    meta["id"] = tc["id"]
                if tc.get("type"):
                    meta["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    meta["name"] = fn["name"]
                yield ("tool", (idx, meta, fn.get("arguments") or ""))

            if choice.get("finish_reason"):
                yield ("finish", choice["finish_reason"])
            if chunk.get("model"):
                yield ("model", chunk["model"])
            if chunk.get("usage"):
                yield ("usage", _serialize_usage(chunk["usage"]))


# ────────────────────────────────────────────────────────────────
# Anthropic 原生范式
# ────────────────────────────────────────────────────────────────

_ANTHROPIC_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _openai_image_url_to_anthropic(blk: dict) -> dict:
    """OpenAI image_url 块 → Anthropic image 块（官方 SDK 格式）。

    - data: URI → base64 source（media_type 必须 ∈ Anthropic 白名单）
    - 普通 URL → url source
    超范围/缺失数据 → 返回占位文本块（避免 API 400）。
    """
    url = blk.get("image_url", {}).get("url") or ""
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/png"
        media = media or "image/png"
        if media not in _ANTHROPIC_MEDIA_TYPES:
            logger.warning(f"Anthropic 不支持 media_type={media}，已跳过该图片块")
            return {"type": "text", "text": "[图片已省略：不支持的格式]"}
        return {"type": "image",
                "source": {"type": "base64", "media_type": media, "data": data}}
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    logger.warning("image_url 块缺少 url，已跳过")
    return {"type": "text", "text": "[图片已省略：无数据]"}


def _to_anthropic_messages(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
    """OpenAI 消息格式 → Anthropic 消息格式。

    转换规则：
      - system 角色拆出为顶层 system 字段（Anthropic 无 system 消息）
      - assistant 的 tool_calls → content 内的 tool_use 块（arguments 解析为 input）
      - role=tool 的消息 → user 消息内的 tool_result 块（Anthropic 用 tool_use_id 关联）
    返回 (system_text 或 None, 转换后的 messages)。
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{len(out)}_{len(blocks)}",
                    "name": fn.get("name") or "unknown",
                    "input": args,
                })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            tool_result = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": str(content),
            }
            # Anthropic 不允许连续 role=user；连续 tool 结果必须合并到同一条 user。
            # 防御（W3）：仅当上一条 user 的 content 全部是 tool_result 块时才合并——
            # 若上一条是普通文本/多模态内容，混入 tool_result 会违反 Anthropic 校验
            # （tool_result 必须独占 user 消息，不能与 text/image 块混排）。
            last = out[-1] if out else None
            if (last and last.get("role") == "user"
                    and isinstance(last.get("content"), list)
                    and last["content"]
                    and all(b.get("type") == "tool_result" for b in last["content"])):
                last["content"].append(tool_result)
            else:
                out.append({"role": "user", "content": [tool_result]})
        else:  # user 及未知角色：支持 OpenAI 风格 image_url 块 → Anthropic image 块
            if isinstance(content, list):
                blocks = []
                for blk in content:
                    if (isinstance(blk, dict) and blk.get("type") == "image_url"
                            and isinstance(blk.get("image_url"), dict)):
                        blocks.append(_openai_image_url_to_anthropic(blk))
                    else:
                        blocks.append(blk)
                out.append({"role": "user", "content": blocks})
            else:
                out.append({"role": "user", "content": content})
    return ("\n".join(system_parts) if system_parts else None), out


def _to_anthropic_tools(tools: Optional[list[dict]]) -> list[dict]:
    """OpenAI 工具格式 → Anthropic tools 格式（input_schema）。"""
    result = []
    for t in tools or []:
        fn = t.get("function", t)
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return result


def _iter_anthropic_events(cfg: dict, provider: str, messages: list[dict],
                           model: Optional[str], temperature: float,
                           thinking: Optional[bool], tools: Optional[list[dict]],
                           timeout: int, interrupt_event: Optional[threading.Event]
                           ) -> Generator[tuple, None, None]:
    """Anthropic 原生范式：POST /messages + SSE 事件流，yield 统一事件 (kind, data)。"""
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    base = cfg.get("base_url") or _DEFAULT_ENDPOINTS["anthropic"]
    url = base.rstrip("/") + "/messages"
    headers.update(_opencode_extra_headers(base))

    system, anth_messages = _to_anthropic_messages(messages)
    body: dict = {
        "model": model or cfg["default_model"],
        "messages": anth_messages,
        "max_tokens": _ANTHROPIC_DEFAULT_MAX_TOKENS,
        "stream": True,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if system:
        body["system"] = system
    if thinking:
        # Anthropic API 要求 enabled thinking 同时提供 budget_tokens。
        body["thinking"] = {"type": "enabled", "budget_tokens": 4096}
    if tools:
        body["tools"] = _to_anthropic_tools(tools)

    session = _get_session()
    for attempt in range(_MAX_HTTP_ATTEMPTS):
        try:
            resp = session.post(url, headers=headers, json=body, stream=True,
                                timeout=(_CONNECT_TIMEOUT, _stream_read_timeout(timeout)))
        except requests.exceptions.ReadTimeout as e:
            # 响应头等待超时（同 openai 分支）：统一转内置 TimeoutError，
            # 让 complete_stream 空闲超时重试/兜底路径生效。
            raise TimeoutError(
                f"LLM response header timeout after {_stream_read_timeout(timeout)}s "
                f"({_brief_exc(e)})") from e
        except requests.exceptions.ConnectionError as e:
            # 连接类错误（DNS/握手/SSL/代理/连接重置）：指数退避重试。
            # 注：ConnectTimeout 是 ConnectionError 子类，走此分支保留 HTTP 层重试。
            if attempt < _MAX_HTTP_ATTEMPTS - 1:
                delay = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    f"LLM 连接失败（{_brief_exc(e)}），{delay:.1f}s 后重试第 "
                    f"{attempt + 1}/{_MAX_HTTP_ATTEMPTS - 1} 次"
                    f"（provider={provider}, model={model or cfg.get('default_model')}）")
                time.sleep(delay)
                continue
            raise LLMError(
                f"LLM 连接失败: {_brief_exc(e)}",
                provider=provider, model=model or cfg.get("default_model", ""),
                stage="connect") from e
        status = resp.status_code
        # 429/5xx → 自动重试（指数退避；Anthropic 无 drop_params 场景）
        if status in _RETRYABLE_STATUS and attempt < _MAX_HTTP_ATTEMPTS - 1:
            delay = _RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                f"LLM API {status}（{_retryable_hint(status)}），"
                f"{delay:.1f}s 后重试第 {attempt + 1}/{_MAX_HTTP_ATTEMPTS - 1} 次"
                f"（provider={provider}, model={model or cfg.get('default_model')}）")
            resp.close()
            time.sleep(delay)
            continue
        if status != 200:
            detail = (resp.text or "")[:500]
            resp.close()
            raise LLMError(
                f"LLM API error {status}: {detail}",
                provider=provider, model=model or cfg.get("default_model", ""),
                status_code=status)
        break

    with resp:
        for payload in _iter_sse_lines(resp, interrupt_event, timeout):
            if not payload:
                continue
            try:
                # strict=False：兼容 SSE 多行 data 拼接后字符串内的裸换行
                ev = json.loads(payload, strict=False)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "message_start":
                m = ev.get("message", {})
                usage = m.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                yield ("usage", {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                                 "total_tokens": in_tok + out_tok})
                if m.get("model"):
                    yield ("model", m["model"])
            elif t == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    yield ("tool", (ev.get("index", 0), {
                        "id": cb.get("id", ""),
                        "type": "function",
                        "name": cb.get("name", ""),
                    }, ""))
            elif t == "content_block_delta":
                d = ev.get("delta", {})
                dt = d.get("type")
                if dt == "text_delta":
                    yield ("text", d.get("text", ""))
                elif dt == "thinking_delta":
                    yield ("reasoning", d.get("thinking", ""))
                elif dt == "input_json_delta":
                    yield ("tool", (ev.get("index", 0), {}, d.get("partial_json", "")))
            elif t == "message_delta":
                yield ("finish", ev.get("delta", {}).get("stop_reason", ""))
                # Anthropic 在 message_delta 上报最终 output_tokens（累计值），
                # 与 message_start 的 input_tokens 合并为完整 usage。
                usage = ev.get("usage") or {}
                if usage:
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    yield ("usage", {"prompt_tokens": in_tok, "completion_tokens": out_tok,
                                     "total_tokens": in_tok + out_tok})


# ────────────────────────────────────────────────────────────────
# 统一聚合层 + 对外接口
# ────────────────────────────────────────────────────────────────

def _aggregate_stream(events: Generator[tuple, None, None],
                      interrupt_event: Optional[threading.Event],
                      default_model: str) -> Generator[dict, None, tuple[str, dict]]:
    """消费统一事件流，yield OpenAI 风格 chunk；结束后 return (content, extras)。

    对外 yield 的 chunk 类型（与原实现一致）：
      {"type": "text", "delta": ...}
      {"type": "reasoning", "delta": ...}
      {"type": "tool_call_chunk", "index": ...}
      {"type": "usage", "usage": {...}}
    迭代结束后 StopIteration.value = (accumulated_content, extras)。
    """
    accumulated_content = ""
    accumulated_reasoning = ""
    tool_calls_map: dict[int, dict] = {}
    final_finish_reason = ""
    final_usage: dict = {}
    final_model = ""
    try:
        for kind, data in events:
            if kind == "text":
                accumulated_content += data
                yield {"type": "text", "delta": data}
            elif kind == "reasoning":
                accumulated_reasoning += data
                yield {"type": "reasoning", "delta": data}
            elif kind == "tool":
                idx, meta, args_delta = data
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {"id": "", "type": "function",
                                           "function": {"name": "", "arguments": ""}}
                entry = tool_calls_map[idx]
                if meta.get("id"):
                    entry["id"] = meta["id"]
                if meta.get("type"):
                    entry["type"] = meta["type"]
                if meta.get("name"):
                    entry["function"]["name"] += meta["name"]
                if args_delta:
                    entry["function"]["arguments"] += args_delta
                yield {"type": "tool_call_chunk", "index": idx}
            elif kind == "usage":
                if final_usage:
                    # 多段上报（Anthropic message_start + message_delta）合并：
                    # 非 0 字段取最新值（input_tokens 只在 start 有、output_tokens
                    # 在 delta 是累计值）；total 始终按 prompt+completion 重算，
                    # 避免后段 total（仅含 output）覆盖前段 input。
                    merged = dict(final_usage)
                    if data.get("prompt_tokens"):
                        merged["prompt_tokens"] = data["prompt_tokens"]
                    if data.get("completion_tokens"):
                        merged["completion_tokens"] = data["completion_tokens"]
                    if data.get("reasoning_tokens"):
                        merged["reasoning_tokens"] = data["reasoning_tokens"]
                    merged["total_tokens"] = merged["prompt_tokens"] + merged["completion_tokens"]
                    final_usage = merged
                else:
                    final_usage = data
                yield {"type": "usage", "usage": final_usage}
            elif kind == "finish":
                final_finish_reason = data
            elif kind == "model":
                final_model = data
    finally:
        # 确保底层事件生成器（及其中 requests 响应）被关闭，防止连接泄漏
        close = getattr(events, "close", None)
        if close is not None:
            close()

    interrupted = interrupt_event is not None and interrupt_event.is_set()
    tool_calls_list = [
        {k: v for k, v in tc.items() if k != "index"}
        for tc in tool_calls_map.values()
    ] if tool_calls_map else []

    if interrupted:
        # 中断时丢弃不完整 tool calls（对齐原实现）
        extras = {
            "model": final_model or default_model,
            "finish_reason": final_finish_reason,
            "tool_calls": [],
            "interrupted": True,
        }
        if accumulated_reasoning:
            extras["reasoning_content"] = accumulated_reasoning
        extras["reasoning_len"] = len(accumulated_reasoning)
        extras["content_len"] = len(accumulated_content)
        if final_usage:
            extras["usage"] = final_usage
        return accumulated_content, extras

    # 损坏 tool_calls 检测（源头标记，由 agent 层决定重试或回传错误）
    invalid_tool_call_ids: list[str] = []
    for _tc in tool_calls_list:
        _args_str = _tc.get("function", {}).get("arguments", "")
        try:
            _parsed = json.loads(_args_str)
            _ok = isinstance(_parsed, dict)
        except (json.JSONDecodeError, TypeError):
            _ok = False
        if not _ok:
            invalid_tool_call_ids.append(_tc.get("id", "<no-id>"))
            logger.warning(
                f"[DIAG] complete_stream: 检测到损坏 tool_call arguments "
                f"id={_tc.get('id')} args={_args_str!r}"
            )

    extras: dict = {
        "model": final_model or default_model,
        "finish_reason": final_finish_reason,
        "tool_calls": tool_calls_list,
    }
    if accumulated_reasoning:
        # 与 complete() 语义对齐：reasoning_content 供 agent 落库/展示，并挂回 assistant 供 LLM 回放。
        extras["reasoning_content"] = accumulated_reasoning
    # 思考/正文字符长度（供无 reasoning_tokens 字段的模型做长度比估算）
    extras["reasoning_len"] = len(accumulated_reasoning)
    extras["content_len"] = len(accumulated_content)
    if final_usage:
        extras["usage"] = final_usage
    if invalid_tool_call_ids:
        extras["tool_calls_invalid"] = invalid_tool_call_ids
    return accumulated_content, extras


def complete_stream(
    messages: list[dict],
    model: Optional[str] = None,
    response_format: Optional[str] = None,
    temperature: float = 0.7,
    provider: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    thinking: Optional[bool] = None,
    tools: Optional[list[dict]] = None,
    timeout: int = 120,
    interrupt_event: Optional[threading.Event] = None,
) -> Generator[dict, None, tuple[str, dict]]:
    """Streaming LLM call（轻量版，无 worker 线程）。

    中断语义（与原实现一致）：
      - interrupt_event 置位 → 返回已积累内容，extras["interrupted"]=True
      - 调用方 close() 生成器 → 级联关闭底层请求连接
      - Ctrl+C / interrupt_event：select 每 _POLL_INTERVAL 秒轮询一次
        socket 可读性，50ms 级响应中断（单线程无需 worker 兜底）

    Yields dicts with keys:
      - type: "text" | "reasoning" | "tool_call_chunk" | "usage"
      - delta: str (for text/reasoning)；index: int (for tool_call_chunk)
    迭代结束后 StopIteration.value = (full_content, extras_dict)。
    """
    provider_name = provider or provider_config.get_default_provider()
    # 支持 "provider/model" 前缀语法（如 xiaomi/mimo-v2.5、opencodego/mimo-v2.5:max）：
    # 子 agent/工具调用可能传入带 provider 前缀的模型名，需拆出 provider 与纯模型名，
    # 否则前缀会被当作模型名一部分发给网关 → 401 not supported。
    if model and "/" in model:
        _maybe_p, _, _maybe_m = model.partition("/")
        if _maybe_p in provider_config.list_providers():
            provider_name = _maybe_p
            model = _maybe_m
    cfg = _resolve_provider(provider_name)

    if not cfg["api_key"]:
        raise RuntimeError(
            f"API key for provider '{provider_name}' is not set "
            f"(set config api_key or env {cfg.get('api_key_env') or provider_name.upper() + '_API_KEY'}). "
        )

    # default_model 可能带 :effort 后缀（deepseek-v4-flash:max），剥离后才是真实 API 模型名。
    # 显式传入的 model 由 agent 层 set_model 时已剥离，此处统一处理兜底（对齐 test_connectivity）。
    eff_model = model or cfg.get("default_model") or ""
    # 自愈：调用方未显式传 reasoning_effort 但 model 直接带 ":effort" 后缀时自动提取，
    # 防止新调用方直传 model:effort 导致 effort 静默丢弃（agent 层已剥离，此兜底保底）。
    if reasoning_effort is None and model:
        _pure_m, _eff_m = provider_config.split_model_effort(model)
        if _eff_m:
            reasoning_effort = _eff_m
    eff_model = provider_config.split_model_effort(eff_model)[0]

    provider_type = cfg.get("type", "openai")
    # 流级自动重试上限（超时 / 连接中断 / 空响应共用）：
    #   仅当【未收到任何内容】时重试——未产生 chunk 意味着无重复计费/重复生成风险，
    #   且每次重试都重新发起完整请求（POST 层另有 429/5xx/连接错误指数退避）。
    _MAX_STREAM_RETRIES = 2

    def _make_events():
        if provider_type == "anthropic":
            return _iter_anthropic_events(
                cfg, provider_name, messages, eff_model, temperature, thinking,
                tools, timeout, interrupt_event)
        return _iter_openai_events(
            cfg, provider_name, messages, eff_model, temperature, response_format,
            reasoning_effort, thinking, tools, timeout, interrupt_event)

    _produced = 0
    events = _make_events()
    _ctx_base = (f"provider={provider_name}, model={eff_model}, "
                 f"url={cfg.get('base_url') or '(default)'}")
    for _attempt in range(_MAX_STREAM_RETRIES + 1):
        aggr = _aggregate_stream(events, interrupt_event, cfg.get("default_model", ""))
        try:
            while True:
                chunk = next(aggr)
                _produced += 1
                yield chunk
        except StopIteration as e:
            # 空响应检测（G3）：未 yield 任何 chunk（含 usage）且未中断 →
            # 服务端返回空/垃圾响应，重试（未产生内容，无重复计费风险）。
            if (_produced == 0 and not e.value[1].get("interrupted")
                    and _attempt < _MAX_STREAM_RETRIES):
                _ctx = f"{_ctx_base}, received_chunks={_produced}"
                logger.warning(f"LLM 空响应 ({_ctx}) — 自动重试第 {_attempt + 1}/{_MAX_STREAM_RETRIES} 次")
                events = _make_events()
                continue
            return e.value  # 转成自身 StopIteration.value（生成器 return 语义）
        except TimeoutError as e:
            _ctx = f"{_ctx_base}, received_chunks={_produced}"
            if _attempt < _MAX_STREAM_RETRIES and _produced == 0:
                logger.warning(f"LLM stream timeout after {timeout}s ({_ctx}) — 未收到任何内容，自动重试第 {_attempt + 1}/{_MAX_STREAM_RETRIES} 次")
                events = _make_events()
                continue
            # 保留底层空闲超时细节（idle 秒数/总时长兜底），叠加调用上下文，
            # 便于日志区分"模型卡死"与"总时长兜底"两种超时形态。
            raise TimeoutError(f"{e} ({_ctx})") from e
        except LLMError as e:
            # 流中断（G2）：仅 SSE 流阶段错误（stage="stream"）且未产生内容时重试——
            # POST 连接失败 / HTTP 非 200 已在 HTTP 层重试 3 次（stage=connect/api），
            # 流层不再重试，避免重试放大（3 HTTP × 3 流 = 9 次）。
            # 已产生内容（_produced > 0）→ 保留部分结果交给上层（避免重复生成）。
            if (e.stage == "stream" and _produced == 0
                    and _attempt < _MAX_STREAM_RETRIES):
                _ctx = f"{_ctx_base}, received_chunks={_produced}"
                logger.warning(f"LLM 流连接中断未产生内容 ({_ctx}) — 自动重试第 {_attempt + 1}/{_MAX_STREAM_RETRIES} 次")
                events = _make_events()
                continue
            raise
        except GeneratorExit:
            aggr.close()
            raise



def complete(
    messages: list[dict],
    model: Optional[str] = None,
    response_format: Optional[str] = None,
    temperature: float = 0.7,
    provider: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    thinking: Optional[bool] = None,
    tools: Optional[list[dict]] = None,
    timeout: int = 120,
    interrupt_event: Optional[threading.Event] = None,
) -> tuple[str, dict]:
    """Non-streaming LLM call = synchronous collector over complete_stream().

    统一中断机制：非流式调用复用 complete_stream 的 interrupt_event 支持；
    中断时返回已积累的残缺内容，由调用方检查 Event 收尾（与原实现一致）。
    """
    gen = complete_stream(
        messages=messages,
        model=model,
        response_format=response_format,
        temperature=temperature,
        provider=provider,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
        tools=tools,
        timeout=timeout,
        interrupt_event=interrupt_event,
    )

    reasoning_parts: list[str] = []
    content, extras = "", {}
    try:
        while True:
            chunk = next(gen)
            if chunk["type"] == "reasoning":
                reasoning_parts.append(chunk["delta"])
    except StopIteration as e:
        content, extras = e.value

    # 补回 reasoning_content（保持 complete() 原有语义）
    if reasoning_parts:
        extras["reasoning_content"] = "".join(reasoning_parts)
    return content.strip(), extras



# ────────────────────────────────────────────────────────────────
# 联通性测试（/model test）
# ────────────────────────────────────────────────────────────────

# 联通性探测的最小回复 token 上限：近乎零成本，却能同时验证
# 网络可达 + 鉴权 + 模型可用 三件事。
_TEST_MAX_TOKENS = 1
# 部分模型（尤其 reasoning 系）要求最小输出 token 数，max_tokens=1
# 会触发 400；失败且错误信息命中 token 限制时，降级用该值重试一次。
_TEST_MAX_TOKENS_RETRY = 8
# 联通性探测默认总超时（秒）：远短于生产调用（120s），
# 避免 base_url 误配时长时间挂起。
_TEST_DEFAULT_TIMEOUT = 15
# 联通性探测的最小 prompt：纯文本单条 user 消息，无系统提示。
_TEST_PROMPT = "ping"

# 常见 HTTP 状态码 → 可读提示（用于 /model test 失败展示）。
_STATUS_HINTS = {
    401: "鉴权失败（api_key 无效或未授权）",
    403: "权限不足（api_key 无权访问该模型）",
    404: "资源不存在（模型名错误或端点路径错误）",
    429: "请求过频（限流，请稍后重试）",
}


def _safe_json(resp) -> dict:
    """从 requests 响应中安全提取 JSON dict；解析失败时返回错误摘要。"""
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"error": (resp.text or "")[:500]}
    except ValueError:
        return {"error": (resp.text or "")[:500]}


def _brief_exc(e: Exception, limit: int = 200) -> str:
    """异常 → 单行摘要（截断，避免把完整 traceback 抛给用户）。"""
    return (str(e) or e.__class__.__name__)[:limit]


def _status_hint(status: int) -> str:
    """HTTP 状态码 → 可读提示（未知状态码返回通用文案）。"""
    return _STATUS_HINTS.get(status, "服务端返回错误")


def _extract_test_echo(data: dict) -> tuple[str, dict | None]:
    """从非流式响应中提取模型回显 + usage（两种范式字段一致，统一处理）。"""
    model_echo = str(data.get("model") or "")
    usage = data.get("usage") or None
    if isinstance(usage, dict):
        usage = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return model_echo, usage


def _test_fail_result(provider: str, error: str, model: str = "",
                      status_code: int | None = None) -> dict:
    """构造联通性失败的结构化结果（与成功结果字段对齐）。"""
    return {
        "ok": False,
        "provider": provider,
        "model": model,
        "latency_ms": 0,
        "model_echo": "",
        "usage": None,
        "error": error,
        "status_code": status_code,
    }


def _test_openai(cfg: dict, model: str, timeout: int,
                 effort: Optional[str] = None) -> tuple[int, dict]:
    """OpenAI 兼容范式的联通性探测（非流式单请求）。

    兼容 drop_params：部分模型拒绝 temperature != 1（400）或要求更大
    max_tokens，命中时调整 body 后重试一次（对齐 complete_stream 的
    drop_params 设计；成本仍 < 10 token）。
    """
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    base = cfg.get("base_url") or _DEFAULT_ENDPOINTS["openai"]
    url = base.rstrip("/") + "/chat/completions"
    headers.update(_opencode_extra_headers(base))
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": _TEST_PROMPT}],
        "max_tokens": _TEST_MAX_TOKENS,
        "stream": False,
        "temperature": 0.0,
    }
    if effort:
        body["reasoning_effort"] = effort
    for _attempt in range(2):
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        status = resp.status_code
        if status == 400 and _attempt == 0:
            text = (resp.text or "").lower()
            # temperature 被拒 → 去掉重发；max_tokens 过小 → 放大重发
            if "temperature" in text and "temperature" in body:
                resp.close()
                body.pop("temperature", None)
                continue
            # reasoning_effort 值域不支持（如 qwen :max）→ 去掉重发
            if "reasoning_effort" in text and "reasoning_effort" in body:
                resp.close()
                body.pop("reasoning_effort", None)
                continue
            if body.get("max_tokens", 0) < _TEST_MAX_TOKENS_RETRY and (
                "max_tokens" in text or ("completion" in text and "token" in text)
            ):
                resp.close()
                body["max_tokens"] = _TEST_MAX_TOKENS_RETRY
                continue
        data = _safe_json(resp)
        resp.close()
        return status, data
    return 400, {"error": "request failed after adjustment retries"}


def _test_anthropic(cfg: dict, model: str, timeout: int) -> tuple[int, dict]:
    """Anthropic 原生范式的联通性探测（非流式单请求）。

    与 _test_openai 同构：max_tokens 过小被拒（400）时放大重试一次。
    """
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    base = cfg.get("base_url") or _DEFAULT_ENDPOINTS["anthropic"]
    url = base.rstrip("/") + "/messages"
    headers.update(_opencode_extra_headers(base))
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": _TEST_PROMPT}],
        "max_tokens": _TEST_MAX_TOKENS,
        "stream": False,
    }
    for _attempt in range(2):
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        status = resp.status_code
        if status == 400 and _attempt == 0 and body["max_tokens"] < _TEST_MAX_TOKENS_RETRY:
            text = (resp.text or "").lower()
            if "max_tokens" in text or ("token" in text and "minimum" in text):
                resp.close()
                body["max_tokens"] = _TEST_MAX_TOKENS_RETRY
                continue
        data = _safe_json(resp)
        resp.close()
        return status, data
    return 400, {"error": "request failed after adjustment retries"}


def test_connectivity(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = _TEST_DEFAULT_TIMEOUT,
) -> dict:
    """测试指定 provider+model 的 API 联通性（最小请求，非流式，只读）。

    设计考虑：
      - 只读探测：不切换当前 provider/model、不写状态、不落库
      - 一次最小请求同时验证三件事：网络可达 / 鉴权通过 / 模型可用
      - 双范式兼容：openai（chat/completions）与 anthropic（messages）
      - 返回结构化 dict 而非抛异常，供 web/repl 直接格式化展示

    返回字段：ok / provider / model / latency_ms / model_echo /
              usage / error / status_code
    """
    provider_name = provider or provider_config.get_default_provider()
    if not provider_name:
        return _test_fail_result(
            "", "无默认 provider：请先配置 provider.config 的 [default].provider")
    cfg = _resolve_provider(provider_name)

    # 模型名可能带 :effort 后缀（deepseek-v4-flash:max），剥离后才是真实 API 模型名。
    # 测试携带 effort（显式 model 后缀 > 配置推导），让 /model test 覆盖 reasoning_effort
    # 真实调用路径——否则 effort 值域错误的模型（如 :max 对 qwen）测试全绿、实际调用 400。
    eff_model = model or cfg.get("default_model") or ""
    _pure_m, _eff_m = provider_config.split_model_effort(eff_model)
    eff_model = _pure_m
    effort = _eff_m or provider_config.get_model_effort(provider_name, eff_model)
    if not eff_model:
        return _test_fail_result(
            provider_name, f"provider '{provider_name}' 未配置 default_model")

    if not cfg["api_key"]:
        env_hint = cfg.get("api_key_env") or f"{provider_name.upper()}_API_KEY"
        return _test_fail_result(
            provider_name,
            f"API key 未设置（配置 api_key 或环境变量 {env_hint}）",
            model=eff_model,
        )

    provider_type = cfg.get("type", "openai")
    start = time.monotonic()
    try:
        if provider_type == "anthropic":
            status, data = _test_anthropic(cfg, eff_model, timeout)
        else:
            status, data = _test_openai(cfg, eff_model, timeout, effort=effort)
    except requests.exceptions.Timeout:
        return _test_fail_result(
            provider_name,
            f"请求超时（>{timeout}s）：网络不可达或服务端无响应",
            model=eff_model,
        )
    except requests.exceptions.ConnectionError as e:
        return _test_fail_result(
            provider_name,
            f"连接失败：{_brief_exc(e)}（请检查 base_url 与网络）",
            model=eff_model,
        )
    except requests.exceptions.RequestException as e:
        return _test_fail_result(
            provider_name, f"请求异常：{_brief_exc(e)}", model=eff_model)
    except Exception as e:
        return _test_fail_result(
            provider_name, f"未知错误：{_brief_exc(e)}", model=eff_model)

    latency_ms = int((time.monotonic() - start) * 1000)
    if status == 200:
        model_echo, usage = _extract_test_echo(data)
        return {
            "ok": True,
            "provider": provider_name,
            "model": eff_model,
            "latency_ms": latency_ms,
            "model_echo": model_echo,
            "usage": usage,
            "error": None,
            "status_code": 200,
        }
    # 非 200：优先取服务端 error.message，否则用状态码提示
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        err = err.get("message") or err.get("type") or ""
    detail = str(err or "")[:300]
    return _test_fail_result(
        provider_name,
        f"HTTP {status}: {detail or _status_hint(status)}".strip(),
        model=eff_model,
        status_code=status,
    )


def format_test_report(result: dict) -> str:
    """将 test_connectivity 的结构化结果格式化为可读文本（web/repl 共用）。"""
    lines = [
        f"Provider: {result.get('provider') or '?'} | "
        f"Model: {result.get('model') or '(default)'}"
    ]
    if result.get("ok"):
        extra = ""
        echo = result.get("model_echo") or ""
        if echo:
            extra += f"model: {echo}"
        usage = result.get("usage") or {}
        if usage:
            token_txt = f"{usage.get('prompt_tokens', 0)}+{usage.get('completion_tokens', 0)} tokens"
            extra = f"{extra}, {token_txt}" if extra else token_txt
        suffix = f" ({extra})" if extra else ""
        lines.append(f"✅ 联通正常 — {result.get('latency_ms', 0)}ms{suffix}")
    else:
        lines.append(f"❌ 联通失败 — {result.get('error') or 'unknown'}")
    return "\n".join(lines)

def friendly_error_hint(error_text: str) -> str:
    """把常见 LLM 错误映射为可操作的修复提示（纯展示层，不改异常语义）。

    规则按错误关键字命中；未知错误返回通用提示，便于用户定位。
    """
    if not error_text:
        return ""
    low = str(error_text).lower()
    if "api key" in low or "not set" in low:
        return "💡 请配置 API key：编辑 .xkagent/provider.config 的 api_key，或用 /model @<provider> 选择"
    if "401" in low or "unauthorized" in low:
        return "💡 鉴权失败：请检查 api_key 是否有效（/model test 可测试）"
    if "403" in low:
        return "💡 权限不足：请确认该模型/端点对当前 key 可用"
    if "404" in low or "not found" in low:
        return "💡 资源不存在：请检查 base_url 与模型名（/model 查看可用模型）"
    if "429" in low or "rate limit" in low:
        return "💡 请求过频：请稍后重试，或降低并发"
    # ── 上下文超限（2026-09-11）：对齐自动修剪恢复机制的用户侧提示 ──
    if ("context length" in low or "longer than" in low or "context window" in low
            or "too many tokens" in low or "context_length" in low):
        return "💡 上下文超限：系统已自动修剪超大工具输出并重试；若持续失败可 /compact 压缩历史"
    if "invalidparameter" in low.replace(" ", "") or "invalid_request_error" in low:
        return "💡 请求被拒（常见原因：上下文超限或参数不兼容）：系统已自动修剪重试；可 /compact 压缩历史"
    if "timeout" in low or "timed out" in low or "connection" in low:
        return "💡 网络/超时：请检查 base_url 与网络连接，或缩短上下文"
    return "💡 可尝试 /model test 检查联通性，或查看日志定位原因"


def _iter_test_targets():
    """遍历全部 provider 的真实模型名（default_model + models 别名值），按 provider 去重。

    yield (provider_name, pure_model_name)
    设计考虑：别名 key 是快捷方式，真实 API 模型名是其 value（可能带 :effort 后缀，
    由 split_model_effort 剥离）；不同 provider 同名模型视为不同目标（端点不同）。
    """
    seen = set()
    for name in provider_config.list_providers():
        try:
            cfg = provider_config.get_provider(name)
        except ValueError:
            continue
        candidates = [str(cfg.get("default_model") or "")]
        candidates += [str(v) for v in (cfg.get("models") or {}).values()]
        for raw in candidates:
            pure = provider_config.split_model_effort(raw)[0]
            if not pure:
                continue
            key = (name, pure)
            if key in seen:
                continue
            seen.add(key)
            yield name, pure


def test_all_connectivity(timeout: int = _TEST_DEFAULT_TIMEOUT) -> tuple[list[dict], float]:
    """对全部 provider 的全部模型做联通性测速（串行，只读）。

    返回 (results, total_seconds)：
      - results: 每个元素与 test_connectivity 返回结构一致，含 provider/model
      - total_seconds: 全部测试总耗时（含失败项的超时等待）
    设计考虑：
      - 串行执行：避免并发触发服务端限流（429），且实现简单可靠
      - 只读探测：不切换模型、不写状态、不落库
    """
    targets = list(_iter_test_targets())
    results = []
    start = time.monotonic()
    for i, (prov, model) in enumerate(targets, 1):
        r = test_connectivity(provider=prov, model=model, timeout=timeout)
        r["test_index"] = i
        r["test_total"] = len(targets)
        results.append(r)
    elapsed = time.monotonic() - start
    return results, elapsed


def format_all_test_report(results: list[dict], elapsed: float) -> str:
    """格式化全模型测速报告：成功项按延迟升序 + 失败项列原因 + 汇总。

    设计考虑：成功项按延迟排序方便快速找到最快模型；
    失败项保留原始错误信息便于定位（网络/鉴权/模型名）。
    """
    ok_list = [r for r in results if r.get("ok")]
    fail_list = [r for r in results if not r.get("ok")]
    ok_sorted = sorted(ok_list, key=lambda r: r.get("latency_ms", 0))
    lines = ["--- model connectivity test (all) ---"]
    for r in ok_sorted:
        name = f"{r['provider']}.{r['model']}"
        lines.append(f"  ✅ {name:<46} {r.get('latency_ms', 0)}ms")
    for r in fail_list:
        name = f"{r['provider']}.{r['model']}"
        lines.append(f"  ❌ {name:<46} {r.get('error') or 'failed'}")
    lines.append(f"--- {len(ok_list)}/{len(results)} OK, total {elapsed:.1f}s ---")
    return "\n".join(lines)
def start_prewarm() -> None:
    """No-op：requests 导入毫秒级，无需后台预热。

    保留该函数仅为兼容 main.py 的调用（对齐原 litellm prewarm 设计）。
    """
    logger.info("轻量 llm 层：requests 即时可用，跳过 prewarm")


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="LLM completion test")
    parser.add_argument("prompt", nargs="?", default="Say hello in one sentence")
    parser.add_argument("--provider", default=provider_config.get_default_provider() or None)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"])
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--stream", action="store_true", help="Use streaming mode")
    args = parser.parse_args()

    if not args.provider:
        print("Error: 未配置默认 provider（provider.config 的 [default].provider 为空）。")
        print("请用 --provider <name> 指定，或先配置默认 provider。")
        sys.exit(1)

    cfg = _resolve_provider(args.provider)
    print(f"Provider: {args.provider} (type={cfg.get('type', 'openai')})")
    print(f"Model: {args.model or cfg['default_model']}")
    print(f"Base: {cfg.get('base_url') or '(default endpoint)'}")
    print(f"API Key: {provider_config.mask_key(cfg['api_key'])}")
    if args.reasoning_effort:
        print(f"Reasoning Effort: {args.reasoning_effort}")
    if args.thinking:
        print("Thinking: enabled")
    print(f"Stream: {args.stream}")
    print(f"Prompt: {args.prompt}")
    print("---")

    if not cfg["api_key"]:
        print(f"Error: API key not set (provider: {args.provider})")
        sys.exit(1)

    if args.stream:
        gen = complete_stream(
            messages=[{"role": "user", "content": args.prompt}],
            model=args.model,
            response_format=None,
            provider=args.provider,
            reasoning_effort=args.reasoning_effort,
            thinking=args.thinking,
        )
        try:
            while True:
                chunk = next(gen)
                if chunk["type"] == "text":
                    print(chunk["delta"], end="", flush=True)
                elif chunk["type"] == "reasoning":
                    print(f"\n[reasoning] {chunk['delta']}", end="", flush=True)
        except StopIteration as e:
            content, extras = e.value
            print(f"\n--- done ---")
            print(f"Tokens: {extras.get('usage', {})}")
    else:
        result, extras = complete(
            messages=[{"role": "user", "content": args.prompt}],
            model=args.model,
            response_format=None,
            provider=args.provider,
            reasoning_effort=args.reasoning_effort,
            thinking=args.thinking,
        )
        print(result)
