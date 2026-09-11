"""codes/agent_runner.py — 可复用的子 agent 执行器（独立化增强版）。

设计意图：
  - 将 tools.exec_agent 中的核心多步循环抽离为独立模块，主进程工具可直接复用；
  - 增加"自动后端选择"：优先 litellm（主进程），受限沙箱内自动降级为纯 stdlib
    （urllib + provider_config）直连 OpenAI 兼容端点 —— 网络已放行、零第三方依赖；
  - 保持现有行为：超时 / 中断 / max_steps / JSON 最终回复校验 / tool_calls 循环；
  - 依赖注入（run_agent）与零依赖便捷入口（run_agent_simple）双形态，便于沙箱内复用。
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

# ── 后端选择（进程内缓存）──
_BACKEND: Optional[str] = None  # None=未探测 | 'litellm' | 'stdlib'


def _select_backend() -> str:
    """探测可用 LLM 后端：litellm 可 import 则用它，否则降级 stdlib。

    设计意图：受限沙箱内 litellm 被 ImportGate 拒绝（第三方），但网络已放行，
    因此 stdlib 直连可作为同一 agent 循环的降级后端，保证"沙箱内也能跑子 agent"。
    """
    global _BACKEND
    if _BACKEND is None:
        try:
            import litellm  # noqa: F401
            _BACKEND = "litellm"
        except Exception:
            _BACKEND = "stdlib"
    return _BACKEND


def is_valid_json(s: str) -> bool:
    """判断字符串是否为合法 JSON。

    设计意图：子 agent 最终输出要求为单个 JSON 对象；这里抽成纯函数，便于
    主进程工具与未来独立调用方复用同一校验逻辑。
    """
    if not s:
        return False
    try:
        return isinstance(json.loads(s), dict)
    except Exception:
        return False


# ── stdlib LLM 后端 ────────────────────────────────────────────

def complete_stdlib(
    messages: list[dict],
    model: Optional[str] = None,
    provider: Optional[str] = None,
    response_format: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    tools: Optional[list[dict]] = None,
    timeout: int = 120,
    interrupt_event=None,
    **kwargs,
) -> tuple[str, dict]:
    """纯 stdlib LLM 调用：urllib + provider_config 直连 OpenAI 兼容端点。

    受限沙箱内可用（network 放行、零第三方依赖）。同步阻塞，无法中途打断单次
    请求；但每次调用前检查 interrupt_event（快速失败），总超时由 run_agent 的
    deadline 控制（每次传剩余时间）。
    """
    import urllib.request as _ureq
    from codes import provider_config

    if interrupt_event is not None and interrupt_event.is_set():
        raise RuntimeError("interrupted")

    prov = provider or provider_config.get_default_provider()
    if not prov:
        raise ValueError("未配置默认 provider（provider.config [default].provider）")
    cfg = provider_config.get_provider(prov)
    api_key = str(cfg.get("api_key") or "")
    if not api_key:
        raise RuntimeError(f"API key for provider '{prov}' is not set")
    base_url = str(cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    if not model:
        model = provider_config.get_default_model() or cfg.get("default_model") or "gpt-4o"

    provider_type = str(cfg.get("type") or "openai").lower()
    if provider_type == "anthropic":
        # Anthropic 将 system 单独传递，tool_calls/tool 结果转换为 content blocks。
        system_parts = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
        anthropic_messages = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue
            if role == "assistant":
                blocks = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": str(msg["content"])})
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or "toolu_unknown",
                                   "name": fn.get("name") or "unknown", "input": args})
                anthropic_messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                result = {"type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""),
                          "content": str(msg.get("content") or "")}
                if anthropic_messages and anthropic_messages[-1].get("role") == "user":
                    anthropic_messages[-1]["content"].append(result)
                else:
                    anthropic_messages.append({"role": "user", "content": [result]})
            else:
                content = msg.get("content")
                if anthropic_messages and anthropic_messages[-1].get("role") == "user":
                    previous = anthropic_messages[-1]["content"]
                    if isinstance(previous, str):
                        anthropic_messages[-1]["content"] = previous + "\n" + str(content or "")
                    else:
                        anthropic_messages.append({"role": "user", "content": str(content or "")})
                else:
                    anthropic_messages.append({"role": "user", "content": str(content or "")})
        payload = {"model": model, "messages": anthropic_messages, "max_tokens": 4096}
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if tools:
            payload["tools"] = [{"name": t.get("function", {}).get("name", ""),
                                  "description": t.get("function", {}).get("description", ""),
                                  "input_schema": t.get("function", {}).get("parameters") or {"type": "object"}}
                         for t in tools]
        endpoint = base_url + "/messages"
        headers = {"Content-Type": "application/json", "x-api-key": api_key,
                   "anthropic-version": "2023-06-01"}
    else:
        payload = {"model": model, "messages": messages, "temperature": 0.7}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = {"type": "json_object"}
        endpoint = base_url + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    req = _ureq.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    with _ureq.urlopen(req, timeout=max(1, int(timeout))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if provider_type == "anthropic":
        blocks = data.get("content") or []
        content = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
        raw_tc = [{"id": b.get("id", ""), "type": "function",
                   "function": {"name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                  for b in blocks if b.get("type") == "tool_use"]
    else:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        raw_tc = msg.get("tool_calls") or []
    tool_calls = [
        {
            "id": t.get("id", ""),
            "type": t.get("type", "function"),
            "function": {
                "name": t["function"]["name"],
                "arguments": t["function"].get("arguments") or "{}",
            },
        }
        for t in raw_tc
    ]
    usage = data.get("usage") or {}
    return content, {
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        },
        "tool_calls": tool_calls,
    }


def auto_complete(**kwargs) -> tuple[str, dict]:
    """自动选择后端的 LLM 调用入口（run_agent_simple 使用）。"""
    if _select_backend() == "litellm":
        from codes.llm import complete
        return complete(**kwargs)
    return complete_stdlib(**kwargs)


# ── 核心多步循环（依赖注入版）──────────────────────────────────

def run_agent(
    *,
    prompt: str,
    system_prompt: Optional[str],
    tools: list[str] | None,
    max_steps: int,
    timeout: int | float,
    model: Optional[str],
    provider: Optional[str],
    allow_agent_tool: bool,
    allow_exit: bool,
    interrupt_event,
    llm_complete: Callable[..., tuple[str, dict]],
    reasoning_effort: Optional[str],
    list_all_tools: Callable[[], list],
    find_tool: Callable[[str], object | None],
    tool_result_to_str: Callable[[object], str],
    usage_callback: Optional[Callable[[int, int], None]] = None,
    log_error: Optional[Callable[[str], None]] = None,
) -> dict:
    """执行子 agent 循环，返回统一 JSON 结果 dict。

    参数通过依赖注入保持与具体 Agent/Tool 实现解耦：
      - llm_complete: 单次 LLM 调用函数（litellm 或 stdlib 均可注入）
      - list_all_tools/find_tool: 工具发现与执行入口
      - tool_result_to_str: ToolResult -> 文本，供消息回填
      - usage_callback: token 用量统计回传到主 agent
    """
    # 多模态支持：prompt 可为 str，或非空 blocks 列表（[{"type": "text"/"image_url", ...}]）
    if not ((isinstance(prompt, str) and prompt.strip())
            or (isinstance(prompt, list) and prompt)):
        return {"status": "error", "content": None, "steps": 0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "error": "prompt 必须是非空字符串或非空多模态块列表"}
    if not isinstance(max_steps, int) or max_steps < 1:
        max_steps = 10
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 120

    sys_prompt = system_prompt or (
        "You are a sub-agent executing a delegated task. "
        "Use the provided tools to accomplish the task step by step if needed. "
        "When finished, reply with a SINGLE valid JSON object ONLY "
        "(no markdown fences, no extra text), e.g. "
        '{"result": ...} or {"summary": ..., "details": ...}. '
        "The final reply MUST be valid JSON."
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]

    schemas: list[dict] = []
    all_tools = {t.name: t for t in list_all_tools()}
    for name in tools or []:
        t = all_tools.get(name)
        if t is None:
            return {"status": "error", "content": None, "steps": 0,
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                    "error": f"未知工具名: {name}（可用: {list(all_tools)}）"}
        if name == "agent" and not allow_agent_tool:
            continue
        if name == "exit" and not allow_exit:
            continue
        schemas.append(t.to_openai_schema())

    deadline = time.time() + float(timeout)
    usage_agg = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    steps = 0

    def finalize(status: str, content, error: str | None = None) -> dict:
        return {
            "status": status,
            "content": content,
            "steps": steps,
            "usage": usage_agg,
            "error": error,
        }

    for step in range(1, max_steps + 1):
        if interrupt_event is not None and interrupt_event.is_set():
            return finalize("interrupted", None, "子 agent 被用户中断")
        if time.time() > deadline:
            return finalize("timeout", None, f"子 agent 超过总超时 {timeout}s")

        try:
            content, extras = llm_complete(
                timeout=max(1, min(120, int(deadline - time.time()))),
                messages=messages,
                model=model,
                provider=provider,
                reasoning_effort=reasoning_effort,
                tools=schemas or None,
                interrupt_event=interrupt_event,
            )
        except Exception as e:
            if log_error:
                log_error(f"sub-llm error={e}")
            return finalize("error", None, f"子 LLM 调用失败: {e}")

        usage = extras.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        # 思考 token 拆分（与主 agent 一致：有字段用字段，无字段用长度比估算）
        from codes.llm import split_reasoning_tokens
        rt, _content_ct = split_reasoning_tokens(
            usage,
            reasoning_len=extras.get("reasoning_len", 0),
            content_len=extras.get("content_len", 0),
        )
        usage_agg["prompt_tokens"] += pt
        usage_agg["completion_tokens"] += ct
        usage_agg["reasoning_tokens"] += rt
        if usage_callback is not None:
            usage_callback(pt, ct)
        steps = step

        if interrupt_event is not None and interrupt_event.is_set():
            return finalize("interrupted", content or None, "子 agent 被用户中断")

        # ── 子 agent 中间状态打印（stdout → progress_q → tool_progress 前端实时渲染）──
        print(f"[agent:llm] step {step}: {content or ''}", flush=True)
        tool_calls = extras.get("tool_calls", [])
        if tool_calls:
            print(f"[agent:tool_calls] step {step}: {len(tool_calls)} 个工具调用", flush=True)
        if not tool_calls:
            reply = (content or "").strip()
            if not is_valid_json(reply):
                try:
                    c2, _ = llm_complete(
                        timeout=max(1, min(120, int(deadline - time.time()))),
                        messages=messages,
                        model=model,
                        provider=provider,
                        reasoning_effort=reasoning_effort,
                        response_format="json",
                        interrupt_event=interrupt_event,
                    )
                    reply = (c2 or "").strip()
                except Exception as e:
                    return finalize("error", reply or None, f"JSON 终态修复失败: {e}")
            if not is_valid_json(reply):
                return finalize("error", reply or None, "子 agent 最终输出不是 JSON 对象")
            return finalize("ok", reply)

        messages.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            args_raw = fn.get("arguments", "{}")
            try:
                tool_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except Exception:
                tool_args = {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            print(f"[agent:tool_call] step {step}: {tool_name} args={args_raw}", flush=True)
            if interrupt_event is not None and interrupt_event.is_set():
                return finalize("interrupted", None, "子 agent 被用户中断")
            tool = find_tool(tool_name)
            if tool is None:
                result = type('AnonymousResult', (), {'stdout': '', 'stderr': '', 'error': f'Unknown tool: {tool_name}'})()
            else:
                try:
                    result = tool.execute(**tool_args)
                except Exception as e:
                    if log_error:
                        log_error(f"tool={tool_name} args={tool_args!r} error={e}")
                    result = type('AnonymousResult', (), {'stdout': '', 'stderr': '', 'error': str(e)})()
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result_to_str(result),
            })
            print(f"[agent:tool_result] step {step}: {tool_result_to_str(result)}", flush=True)

    return finalize("max_steps", None, f"子 agent 达到最大步数 {max_steps}")


# ── 便捷入口（自动后端 + 最小依赖）──────────────────────────────

def run_agent_simple(
    prompt: str,
    system_prompt: Optional[str] = None,
    tools: Optional[list[str]] = None,
    max_steps: int = 10,
    timeout: int | float = 120,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    allow_agent_tool: bool = False,
    allow_exit: bool = False,
    interrupt_event=None,
    tool_executor: Optional[Callable[[str, dict], str]] = None,
    usage_callback: Optional[Callable[[int, int], None]] = None,
    log_error: Optional[Callable[[str], None]] = None,
) -> dict:
    """零依赖便捷入口：自动选择 LLM 后端，适合沙箱内/独立脚本直接调用。

    - tools: 工具名列表（如 ['pythonrt']）；实际执行需提供 tool_executor(name, args) -> str。
      未提供执行器时，工具调用将返回"无执行器"错误文本（仍走完循环，不阻塞）。
    - 主进程若已有完整工具注册表，请改用 run_agent（显式依赖注入）。
    """
    backend = _select_backend()

    def llm_complete(**kwargs) -> tuple[str, dict]:
        if backend == "litellm":
            from codes.llm import complete
            return complete(**kwargs)
        return complete_stdlib(**kwargs)

    class _SimpleTool:
        def __init__(self, name: str):
            self.name = name
        def to_openai_schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        def execute(self, **kwargs):
            if tool_executor is None:
                return type('R', (), {'stdout': '', 'stderr': '',
                                      'error': f'tool {self.name} 无执行器'})()
            try:
                out = tool_executor(self.name, kwargs)
                return type('R', (), {'stdout': str(out), 'stderr': '', 'error': None})()
            except Exception as e:
                return type('R', (), {'stdout': '', 'stderr': '', 'error': str(e)})()

    simple_tools = [_SimpleTool(n) for n in (tools or [])]

    def list_all():
        return simple_tools

    def find(n):
        return next((t for t in simple_tools if t.name == n), None)

    def render(r):
        parts = []
        if getattr(r, 'stdout', ''):
            parts.append(r.stdout)
        if getattr(r, 'stderr', ''):
            parts.append('[stderr]\n' + r.stderr)
        if getattr(r, 'error', None):
            parts.append('[error] ' + r.error)
        return '\n'.join(parts)

    return run_agent(
        prompt=prompt,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=max_steps,
        timeout=timeout,
        model=model,
        provider=provider,
        allow_agent_tool=allow_agent_tool,
        allow_exit=allow_exit,
        interrupt_event=interrupt_event,
        llm_complete=llm_complete,
        reasoning_effort=None,
        list_all_tools=list_all,
        find_tool=find,
        tool_result_to_str=render,
        usage_callback=usage_callback,
        log_error=log_error,
    )
