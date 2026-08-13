from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
import queue
import threading
import copy
from datetime import datetime


from codes.history import (
    session_exists,
    get_connection, add_chat, get_chat_messages, clear_chats, parse_user_prefix,
    ensure_agent_state, set_agent_state,
    get_token_state, set_token_state,
    get_mount_state, set_mount_state,
    get_search_state, set_search_state,
    _set_session_name,
)
# T4: 改用 acquire_or_recover —— 同进程 crashed 残留自锁时自动接管重试
from codes.lock import acquire_or_recover as acquire_lock
from codes.lock import release as release_lock
from codes.lock import is_locked


from codes.llm import complete, complete_stream
from codes.tools import (
    exec_pythonrt, exec_exit, exec_searchskill, exec_searchinfo, exec_agent, exec_summary,
    TOOL_PYTHONRT_SCHEMA, TOOL_EXIT_SCHEMA, TOOL_SEARCHSKILL_SCHEMA, TOOL_SEARCHINFO_SCHEMA, TOOL_AGENT_SCHEMA, TOOL_SUMMARY_SCHEMA,
    _sanitize, _tool_result_to_str,
)
from typing import Generator
from codes.skill import ToolResult, Skill, SkillLoader, getskill
from codes.search import recommend_info, searchskill, searchskill_detail
from codes._log import logger
from codes import config
from codes import provider_config

# ── 观察者模式下禁止执行的写类命令 (T3) ──
_OBSERVER_WRITE_CMDS = {"clear", "compact", "drop", "set_mode", "set_model", "set_provider", "set_skill_select", "resume", "mount", "unmount", "mount_save", "mount_refresh", "info_add", "info_deny", "info_remove", "info_clear"}

# 历史展示分隔线 (移出 f-string 表达式, 兼容 Python < 3.12)
_SEP_LINE = "\u2500" * 50

# ── /compact 压缩指令标记 ──
# /compact 不再走 _handle_command 同步路径，而是翻译为带前缀的普通消息投递，
# 由 run_forever 检测前缀后走 run_stream 普通通路（复用流式/事件/中断机制），
# 回合正常完成后由 _finalize_compact() 执行收尾副作用（写 marker + 重置上下文）。
COMPACT_MARKER = "__COMPACT__"
COMPACT_PROMPT = (
    "请对当前对话历史进行详细压缩总结。保留所有重要信息、做出的决策、"
    "编写的代码、讨论过的关键观点、文件路径、数据结构、以及任何有价值的上下文。"
    "用中文输出，保持结构清晰、信息完整，便于后续在此基础上继续对话。"
)

class _InterruptTurn(Exception):
    """内部控制流信号：用户 Ctrl+C 请求完全停止当前 turn。

    与 KeyboardInterrupt 区分开——本信号由检查点抛出，
    由 run_stream 捕获后正常收尾（保留已积累消息、标记 killed tool），
    不中断 agent 线程本身，使其回到待命状态。
    """


# ── pythonrt worker 子进程代码 ──
# pythonrt 无取消 API（进程隔离方案）：每个 pythonrt 调用放入独立子进程，
# 中断时由 run_pythonrt 主线程轮询 _interrupt_event 后 SIGKILL（tools._kill_and_reap）。
# worker 通过 stdin 接收 JSON 参数，
# 通过 stdout 回传 "__SANDBOX_RESULT__" 标记的结果（codes/sandbox.py main）。
def _detect_language(text):
    if re.search(r'[\u4e00-\u9fff]', text):
        return "zh"
    return "en"


def _build_system_prompt(lang):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt_path = os.path.join(project_root, 'system_prompt.txt')
    if os.path.isfile(prompt_path):
        with open(prompt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
    else:
        content = 'You are a code assistant agent.'

    # system_prompt.txt 已内置 pythonrt 工具描述（工具收敛后无占位符替换）
    return content

def _build_compact_prompt() -> str:
    """加载压缩专用 system prompt（system_prompt_compact.txt）。

    与 _build_system_prompt 分离的原因：压缩回合需要独立的输出约束
    （专注结构化总结、禁止技能选择声明/工具调用），不应复用通用对话
    prompt（其中含"回复首行声明技能选择"硬规则，会污染压缩输出）。

    文件缺失时回退内置默认压缩 prompt，保证压缩功能不中断。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prompt_path = os.path.join(project_root, 'system_prompt_compact.txt')
    if os.path.isfile(prompt_path):
        with open(prompt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if content:
            return content
    return (
        "You are a conversation compression assistant. "
        "Compress the following conversation history thoroughly, "
        "preserving ALL important information, decisions, code changes, "
        "file paths, data structures, and context. "
        "Output in Chinese, structured and complete."
    )




# Pricing utilities (pure data, no display dependency)
from codes.llm import (MODEL_PRICING, get_model_pricing, estimate_cost)

def _get_model_pricing(model_name):
    """兼容旧调用方，统一委托 llm.py。"""
    return get_model_pricing(model_name)

def _estimate_cost(model, prompt_tokens, completion_tokens):
    """兼容旧调用方，统一委托 llm.py。"""
    return estimate_cost(model, prompt_tokens, completion_tokens)

def _format_ratio(a, b):
    if a == 0 and b == 0:
        return "0:0"
    if b == 0:
        return f"{a}:0"
    return f"1:{b/a:.1f}" if a > 0 else f"0:{b}"

def _format_cost(usd: float) -> str:
    """将 USD 成本格式化为人类可读文本。

    设计考虑: 历史遗留 bug——get_session_stats() 调用此函数但从未定义，
    导致每次 stats 轮询都触发 NameError 崩溃 agent 线程（用户现象:
    "第一次点击 session 没有跑起来" / "No agent session active"）。
    格式策略：<0.01 保留 4 位小数，<1 保留 2 位，>=1 保留 2 位 + 逗号分隔。
    """
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1.0:
        return f"${usd:.2f}"
    return f"${usd:,.2f}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── tool_calls arguments 损坏重试上限（P0 方案 C）──
# 流式聚合产出的 arguments 可能损坏（截断/缺键名），超过此上限不再重试，
# 改由逐工具校验（[参数解析失败]/[参数缺失]）回传明确错误给 LLM 自行修复。
MAX_TOOL_ARG_RETRY = 2

def _resolve_refs(text, cwd):
    """解析用户消息中的 @路径 引用。

    支持三种形态：
      @file.py            （无空格，向后兼容）
      @"my notes.txt"     （双引号内可含空格）
      @'my notes.txt'     （单引号内可含空格）
    引号未闭合时给出明确报错，避免静默截断路径。
    """
    pattern = re.compile(r"""@(?:"([^"]+)"|'([^']+)'|(\S+))""")

    def _replace(m):
        path = m.group(1) or m.group(2) or m.group(3)
        if path is None:
            # 引号已开始但未闭合（正则只能匹配到引号本身的情况不会发生，
            # 此处为防御性兜底）
            return nl + '[引号路径未闭合]' + nl
        full = path if os.path.isabs(path) else os.path.join(cwd, path)
        nl = chr(10)
        try:
            with open(full, 'r', errors='replace') as f:
                content = f.read()
            return nl + '```' + nl + '--- ' + path + ' ---' + nl + content + nl + '--- end ' + path + ' ---' + nl + '```' + nl
        except Exception as e:
            return nl + '[error reading ' + path + ': ' + str(e) + ']' + nl

    # 引号未闭合检测：若文本含 @" 或 @' 且无对应闭合引号，给出明确提示
    def _unclosed(text):
        for q in ('"', "'"):
            idx = text.find('@' + q)
            if idx >= 0:
                rest = text[idx + 2:]
                if q not in rest:
                    return q
        return None

    q = _unclosed(text)
    if q:
        text = text.replace('@' + q, f'[引号路径未闭合: 缺少 {q}]', 1)
    return pattern.sub(_replace, text)

PERMISSION_FILE = os.path.join(str(config.get_data_dir()), "permission.txt")

# ── /mount 动态挂载 ──
# 来源标记：default(workdir//tmp) / permission(permission.txt) / dynamic(/mount 命令)
MOUNT_SOURCE_DYNAMIC = "dynamic"
MOUNT_SOURCE_PERMISSION = "permission"
MOUNT_SOURCE_DEFAULT = "default"

# 危险路径前缀：默认拒绝挂载，除非 --force（决策 D）
DANGEROUS_MOUNT_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/proc",
    "/sys", "/dev", "/boot", "/var", "/home/", "/opt", "/root",
)


def _normalize_mount_path(path: str, base: str | None = None) -> str:
    """规范化挂载路径：expanduser + abspath。

    设计意图：/mount 命令路径可能含 ~ 或相对路径。相对路径以 base
    （默认 workdir）为解析基准，绝对路径不受 base 影响 —— 保证
    --workdir 显式指定时，/mount data 仍解析到 workdir/data 而非启动目录。
    """
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return os.path.abspath(p)
    base = base or os.getcwd()
    return os.path.abspath(os.path.join(base, p))


def _is_dangerous_mount(path: str) -> bool:
    """危险路径校验：系统关键目录或用户主目录默认拒绝挂载。

    设计意图：rw 挂载外部路径等于给沙箱开洞，/ 与系统目录一旦挂载
    会破坏沙箱隔离。返回 True 表示危险（需 --force 绕过）。
    """
    if path == "/":
        return True
    for prefix in DANGEROUS_MOUNT_PREFIXES:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _parse_permission_file(filepath=None):
    """Parse permission.txt: each line = <path> <ro|ro/rw>（v2 语法，按 mode 区分）。

    语法语义（writable=True 表示 build 可写，plan 恒只读）：
      ro    → plan / build 均只读
      ro/rw → plan 只读 / build 可写
    旧词兼容（自动迁移 + 提示）：read → ro，write → ro/rw。

    Returns:
        list of (abs_path: str, writable: bool)
    """
    if filepath is None:
        filepath = PERMISSION_FILE
    if not os.path.isfile(filepath):
        return []

    entries = []
    seen = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                print(f"  ⚠️  permission.txt:{lineno}: invalid format, skipping")
                continue
            path, perm = parts[0].strip(), parts[1].strip().lower()
            # v2 语法：ro / ro/rw（按 mode 区分）；旧词 read/write 等价兼容
            if perm in ("ro", "read"):
                writable = False          # plan/build 均只读
                legacy = "read → ro" if perm == "read" else ""
            elif perm in ("ro/rw", "write"):
                writable = True           # plan 只读 / build 可写
                legacy = "write → ro/rw" if perm == "write" else ""
            else:
                print(f"  ⚠️  permission.txt:{lineno}: unknown permission '{perm}', expected ro / ro/rw (legacy: read/write), skipping")
                continue
            abs_path = os.path.abspath(os.path.expanduser(path))
            if not os.path.exists(abs_path):
                print(f"  ⚠️  permission.txt:{lineno}: {abs_path} does not exist, skipping")
                continue
            if legacy:
                print(f"  🔄  permission.txt:{lineno}: 旧词迁移 {legacy}")
            seen[abs_path] = (abs_path, writable)

    entries = list(seen.values())
    if entries:
        print(f"  📁  Loaded {len(entries)} permission entries from permission.txt")
    return entries


def _load_dyn_mounts(session: str) -> list[dict]:
    """从 session db mount_state 表恢复动态挂载（重启自动恢复，无需 /mount save）。

    返回项与 _cmd_mount 写入格式一致: {"path", "writable", "source", "type"}。
    路径已不存在的条目跳过并告警（对齐 _parse_permission_file 对失效条目的处理）。
    """
    mounts = []
    try:
        for m in get_mount_state(session):
            p = m["path"]
            if not os.path.exists(p):
                logger.warning(f"动态挂载恢复跳过（路径不存在）: {p} (session={session})")
                continue
            mounts.append({
                "path": p,
                "writable": m["writable"],
                "source": MOUNT_SOURCE_DYNAMIC,
                "type": m.get("mount_type", "dir"),
            })
    except Exception as e:
        logger.warning(f"动态挂载恢复失败（回退空列表）: session={session} err={e}")
    return mounts



# ── Mode cycle: used by Tab /mode toggle across 3 modes ──
MODE_CYCLE = {
    'plan': 'build',
    'build': 'build-unsafe',
    'build-unsafe': 'plan',
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent class
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━



def _tool_result_event(name: str, result: "ToolResult", elapsed: float) -> dict:
    """构造 tool_result 流式事件（对齐 web.py / repl.py 消费端字段）。

    设计意图：工具执行的实时结果显示依赖该事件；所有分支（成功/参数解析失败/
    类型错误/参数缺失/未知工具/被 kill）统一走此构造，保证字段一致
    （name/exit_code/stdout/stderr/error/elapsed），避免各分支手写遗漏。
    """
    return {
        "type": "tool_result",
        "name": name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": result.error or "",
        "elapsed": elapsed,
    }



def _make_image_url_block(path: str):
    """本地图片 → OpenAI 风格 image_url 块（base64 data URI，不依赖外链）。

    返回 None 表示无法读取（调用方跳过该图，不阻断对话）。
    """
    import base64
    import mimetypes
    try:
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except (OSError, IOError):
        logger.warning(f"图片读取失败，已跳过: {path}")
        return None
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


class Agent:
    def __init__(
        self,
        session: str,
        provider: str | None = None,
        mode: str = "cli",
        input_queue: "queue.Queue | None" = None,
        output_queue: "queue.Queue | None" = None,
    ):
        self.session = session
        # 状态恢复：session db(agent_state) > 显式 provider 参数 > [default] > 引导
        saved_provider, saved_model = ensure_agent_state(session)
        self.provider = provider or saved_provider
        self.model = saved_model or None
        if self.model:
            # 清洗旧 session 残留的带后缀模型名（gpt-5.4:max → gpt-5.4），
            # 保证 self.model 恒为纯模型名，effort 走 _cmd_effort/_resolve_effort 通道
            self.model = provider_config.split_model_effort(self.model)[0]
        # 临时 reasoning_effort 覆盖（/model 模型名:effort 语法设置）。
        # 不持久化到 db：重启后回落到 provider.config 中模型的配置值。
        self._cmd_effort = None
        if not self.provider:
            logger.warning(
                f"session '{session}' 未配置 provider：请配置 provider.config [default] "
                f"或使用 /model @<provider> 选择。"
            )
        self.cwd = str(config.get_workdir())
        # 统一从 config 获取 workdir，确保：
        #   1. 所有 Agent 实例共享同一个 workdir（由 --workdir 或 os.getcwd() 决定）
        #   2. session 切换时不依赖外部传递，避免路径不一致
        #   3. config 是全局单例，只需在入口处 set_workdir 一次
        logger.info(f"workdir 初始化: {self.cwd}")
        self._mode = mode
        self._input_queue = input_queue
        self._output_queue = output_queue
        self.messages = []
        self._last_sync_max_id = 0
        self._last_sync_check = 0.0
        self.active_skills = []
        self._pending_skill_req = None
        # ── Token 统计恢复：重启后从 session db 恢复累计值 ──
        # 使 get_session_stats / web 统计在重启后不丢失（原实现每次启动清零）。
        _tok = get_token_state(session)
        self.total_prompt_tokens = _tok["prompt_tokens"]
        self.skill_select_enabled = True  # 默认启用 skill 自动选择（向后兼容）
        self.total_completion_tokens = _tok["completion_tokens"]
        self._turn_usage_history = []
        self._turn_count = _tok["turn_count"]
        self._tool_retry_count = 0
        self._last_model = _tok["model"] or ""

        # ── Exit tool state ──
        self._exit_requested = False
        self._exit_reason = ""
        self._exit_note = ""   # summary/exit 调用时设置，供 turn_end_by_tool 事件展示

        # ── Mode: plan (read-only) | build (read-write) | build-unsafe (no sandbox) ──
        self.mode = 'plan'

        # ── LLM safety check cache ──
        self._llm_check_cache: dict[str, tuple[bool, str]] = {}

        # ── Pause state (ESC key) ──
        self._paused = False
        self._pause_check_counter = 0

        # ── Interrupt state (Ctrl+C) ──
        # 用户按 Ctrl+C 请求完全停止当前 turn（不抛异常，正常收尾后回到待命）。
        # 线程安全: 主线程经 manager.request_interrupt() 设置，agent 线程在检查点消费。
        self._interrupt_event = threading.Event()
        self._in_tool_exec = False  # 当前是否正在执行 tool（供主线程直调 cancel）

        # ---- 实时状态（供 repl/web 查询各 agent 当前处理阶段）----
        # phase: "starting"(初始化) "idle"(空闲/tool执行) "llm"(处理LLM回复)
        # in_tool: 是否有 tool 正在执行（tool 并入 idle，用布尔区分）
        # 线程安全: agent 线程写、主线程读，GIL 保证标量赋值原子性（同 _in_tool_exec）
        self.phase: str = "starting"
        self.in_tool: bool = False

        # ── pythonrt 统一运行时初始化 ──
        logger.info(f"Agent 初始化: session={self.session}, mode={self.mode}, provider={self.provider}")

        # ── Parse extra mount paths from permission.txt（pythonrt 软边界根）──
        perm_entries = _parse_permission_file()
        self._perm_paths = [p for p, w in perm_entries]
        self._perm_volumes = [(p, w) for p, w in perm_entries]

        # ── 动态挂载（/mount 命令）单一事实源 ──
        # 每项: {"path": str, "writable": bool, "source": "dynamic", "type": "dir"|"file"}
        # pythonrt 按 mode 合并 rw/ro roots：plan 只读 / build 按声明 / build-unsafe 忽略（软边界）
        # v3: /mount 动态挂载持久化到 session db（mount_state 表），重启自动恢复；
        #     恢复时校验路径存在性（失效条目跳过 + 告警，对齐 permission.txt 解析语义）
        self._dyn_mounts = _load_dyn_mounts(session)
        # ── Simple DB connection ──
        _set_session_name(session)
        self.db = get_connection(session)

        # ── Session lock ──
        self._lock_held = False
        self._observing = False
        # 回合级活跃标志（2026-08-10 修复）：区别于 phase（技能选择/tool 间隙
        # 会临时复位 idle），_turn_active 在 run_stream 开头置 True、run_forever
        # finally 才置 False——是"一个回合是否仍在进行"的权威信号，供 web 前端
        # busy 判定与"待查看"标记使用，避免 LLM 还在执行却谎报回合完成。
        self._turn_active = False
        self._lock_holder_info = None
        self._lock_fd = None
        self._try_acquire_lock(session)


        # ── per-agent 工具 schema 实例化（修复：模块级单例被多 agent 覆盖）──
        # 原实现直接给模块级 TOOL_*_SCHEMA 单例赋值 execute，后初始化的 agent
        # 会覆盖前一个 agent 的绑定 → pythonrt 的 mode 判断读到被劫持的 agent.mode
        # （现象：切 build/build-unsafe 后 pythonrt 仍按 plan 只读执行）。
        # 深拷贝后每个 agent 持有独立副本，execute 闭包恒绑定本实例。
        self._tool_defs: list = []
        for _schema, _fn in (
            (TOOL_PYTHONRT_SCHEMA, exec_pythonrt),
            (TOOL_EXIT_SCHEMA, exec_exit),
            (TOOL_SEARCHSKILL_SCHEMA, exec_searchskill),
            (TOOL_SEARCHINFO_SCHEMA, exec_searchinfo),
            (TOOL_AGENT_SCHEMA, exec_agent),
            (TOOL_SUMMARY_SCHEMA, exec_summary),
        ):
            _t = copy.deepcopy(_schema)
            # 外层 lambda 用默认参数固定 _fn（防循环变量捕获），内层接收工具参数
            _t.execute = (lambda _fn=_fn: (lambda **kw: _fn(self, **kw)))()
            self._tool_defs.append(_t)

    def _all_tools(self):
        # pythonrt 为唯一核心执行工具；exit/searchskill/agent 为辅助工具
        # 返回 per-agent 独立副本（深拷贝自模块级单例，execute 绑定本实例），
        # 多 session 并发时互不覆盖，pythonrt 的 mode 恒取当前回合所属 agent。
        return self._tool_defs

    @staticmethod
    def _log_error(source, session, detail):
        """记录错误到【当前进程对应的日志文件】（不再写独立的 errors.log）。

        设计考虑: 用户要求所有 errors 都写入本次启动对应的 log 文件
        （.xkagent/logs/<启动时间>.log），便于按次运行统一查看；
        使用 codes._log.LOG_FILE 保证与运行期 logging 输出同一文件。
        """
        from datetime import datetime
        from codes._log import LOG_FILE
        log_path = str(LOG_FILE)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [session={session}] [{source}] {detail}\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
            print(f"  ℹ️ 错误已记录到: {log_path}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  ⚠️ 写入错误日志失败: {e} ({log_path})", file=sys.stderr, flush=True)

    def _find_tool(self, name):
        for t in self._all_tools():
            if t.name == name:
                return t
        return None


    def _resolve_effort(self) -> str | None:
        """当前生效的 reasoning_effort：临时覆盖 > 配置推导。

        临时覆盖（self._cmd_effort）由 /model 模型名:effort 语法设置，仅本会话有效；
        未设置时查 provider.config 中该 provider 下当前模型的配置后缀。
        """
        if self._cmd_effort:
            return self._cmd_effort
        if self.provider and self.model:
            return provider_config.get_model_effort(self.provider, self.model)
        return None

    def resume(self):
        self.messages = get_chat_messages(self.db)
        self._patch_orphaned_tool_calls()
        row = self.db.execute(
            "SELECT MAX(id) FROM messages WHERE role NOT IN ('git', 'compact', 'drop', 'command')"
        ).fetchone()
        self._last_sync_max_id = row[0] if row else 0


    def _persist_token_state(self):
        """将内存中 token 累计值显式持久化到当前 session 的 db。

        设计：所有累加点（run_stream / _select_skill / _compact_history /
        切换 session 前）共用此方法，避免重复 UPSERT 代码；
        持久化失败时降级为日志，不阻断对话主流程。
        """
        try:
            # v2: 同步持久化"最近一轮单轮值"（last_*），供 web 面板进入 session 时显示上一轮 token。
            # 来源 _turn_usage_history[-1]（每轮 append：主对话/压缩轮），技能选择不 append 不污染。
            _last = self._turn_usage_history[-1] if self._turn_usage_history else None
            set_token_state(
                self.session,
                prompt_tokens=self.total_prompt_tokens,
                completion_tokens=self.total_completion_tokens,
                turn_count=self._turn_count,
                model=self._last_model or "",
                last_prompt_tokens=_last["prompt"] if _last else None,
                last_completion_tokens=_last["completion"] if _last else None,
            )
        except Exception as e:
            logger.warning(f"token_state 持久化失败: {e}")


    def switch_session(self, name: str) -> bool:
        logger.info(f"切换 session: {name}")
        """Switch to an existing session: reconnect DB and reload messages."""
        if not session_exists(name):
            return False

        # 0. 保存旧 session 的 token 累计值（显式持久化到旧 db）
        self._persist_token_state()

        # 1. Release old session lock
        self._release_lock()

        # 2. Switch DB connection
        old_db = self.db
        self.session = name
        _set_session_name(name)
        self.db = get_connection(name)
        self.messages = get_chat_messages(self.db)
        self._patch_orphaned_tool_calls()
        try:
            old_db.close()
        except Exception:
            pass

        # 2.5 恢复新 session 的 token 累计值（先清零再恢复，消除跨 session 混累加）
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._turn_count = 0
        self._turn_usage_history.clear()
        _tok = get_token_state(name)
        self.total_prompt_tokens = _tok["prompt_tokens"]
        self.total_completion_tokens = _tok["completion_tokens"]
        self._turn_count = _tok["turn_count"]
        self._last_model = _tok["model"] or ""

        # 3. Acquire new session lock
        self._try_acquire_lock(name)

        return True



    def _try_acquire_lock(self, session: str):
        """Try to acquire the session lock; set observer state on failure."""
        import os as _os
        import threading as _threading
        pid = _os.getpid()
        tid = _threading.current_thread().ident or 0
        holder_name = f"pid:{pid}:tid:{tid}"

        ok, info = acquire_lock(session, pid, holder_name, thread_id=tid)
        if ok:
            self._lock_held = True
            self._observing = False
            self._lock_fd = info
            self._lock_holder_info = None  # WARN-B 修复: 成功分支重置脏数据
        else:
            # P0 修复: 失败分支先清理可能残留的旧锁上下文（_lock_fd 非 None
            # 表示此前 acquire 成功但状态未正确释放 → fd/锁泄漏，观察者永久误判）
            if self._lock_fd is not None:
                try:
                    release_lock(self._lock_fd)
                except Exception:
                    pass
                self._lock_fd = None
            self._lock_held = False
            self._observing = True
            # T4 修复: acquire 失败时读取锁文件元数据，让观察者能显示持有者身份
            _, holder = is_locked(session)
            self._lock_holder_info = holder if isinstance(holder, dict) else None

    def _try_upgrade_lock(self):
        """观察者模式周期尝试重新获取锁 (WARN-D 修复)。

        持锁进程退出后 fcntl 锁自动释放，观察者通过空闲轮询重新 acquire，
        成功后自动升级为持有者并通知 UI 刷新状态。
        """
        if not self._observing:
            return
        import os as _os
        import threading as _threading
        pid = _os.getpid()
        tid = _threading.current_thread().ident or 0
        holder_name = f"pid:{pid}:tid:{tid}"
        ok, info = acquire_lock(self.session, pid, holder_name, thread_id=tid)
        if not ok:
            return
        self._lock_held = True
        self._observing = False
        self._lock_fd = info
        self._lock_holder_info = None
        logger.info(f"观察者自动接管锁: session={self.session}")
        if self._output_queue:
            self._output_queue.put({
                "type": "_lock_status",
                "is_locked": True, "is_observing": False, "holder_info": None,
            })

    def _release_lock(self):
        """Release the current session lock if held.

        P0 修复: 基于 _lock_fd 而非 _lock_held —— 避免状态标志与真实 fd
        脱节时（如失败分支未清 fd）锁泄漏。
        """
        if self._lock_fd is not None:
            try:
                release_lock(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None
            self._lock_held = False
            self._observing = False

    def is_locked(self) -> bool:
        """Return True if this agent holds the session lock."""
        return self._lock_held

    def is_observing(self) -> bool:
        """Return True if this agent is in observer mode (lock held by another)."""
        return self._observing

    def get_lock_status_str(self) -> str:
        """Return a short status string for the prompt."""
        if self._lock_held:
            return ""
        if isinstance(self._lock_holder_info, dict):
            name = self._lock_holder_info.get("holder", "?")
            return f" \u23f3{name}"
        return " \u23f3\u88ab\u5360\u7528"

    def close(self):
        logger.info(f"Agent 关闭: session={self.session}")
        """Release lock and close DB connection. Call on exit."""
        self._release_lock()
        try:
            self.db.close()
        except Exception:
            pass


    def _sync_from_db(self):
        """Check DB for new messages from other processes and reload if needed.

        Called periodically from run_forever() when idle.
        Detects writes by other processes and refreshes self.messages.
        """
        now = time.time()
        if now - self._last_sync_check < 3.0:
            return
        self._last_sync_check = now
        try:
            row = self.db.execute(
                "SELECT MAX(id) FROM messages WHERE role NOT IN ('git', 'compact', 'drop', 'command')"
            ).fetchone()
            max_id = row[0] if row else 0
            if max_id > self._last_sync_max_id:
                old_max_id = self._last_sync_max_id
                self.messages = get_chat_messages(self.db)
                self._patch_orphaned_tool_calls()
                self._last_sync_max_id = max_id
                new_rows = self.db.execute(
                    "SELECT role, content FROM messages WHERE id > ? AND role NOT IN ('git', 'compact', 'drop', 'command') ORDER BY id",
                    (old_max_id,)
                ).fetchall()
                if new_rows and self._output_queue:
                    self._output_queue.put({
                        'type': '_sync_update',
                        'messages': [{**dict(r), "content": r["content"] or ""} for r in new_rows],  # F2: 防御 content=None
                    })
        except Exception:
            pass

    def run_forever(self):
        """Run in a loop: read from input_queue, process via run_stream, push to output_queue.

        Designed for web/thread mode (mode="web"). Reads one item at a time
        from self._input_queue:
          - str         → treated as user instruction, processed via run_stream()
          - dict with _cmd key → treated as control command
          - None        → sentinel, stops the loop

        Each yielded event from run_stream is pushed to self._output_queue.
        """
        if self._mode != "web" or self._input_queue is None or self._output_queue is None:
            raise RuntimeError(
                "run_forever() requires mode='web' with input_queue and output_queue"
            )
        # Signal ready so the manager knows initialization is complete
        self._output_queue.put({"type": "_ready"})
        self.phase = "idle"   # agent 就绪，进入空闲待命
        while True:
            try:
                item = self._input_queue.get(timeout=3.0)
            except queue.Empty:
                self._sync_from_db()
                self._try_upgrade_lock()  # WARN-D 修复: 观察者自动升级
                continue
            if item is None:  # sentinel: stop
                break
            if isinstance(item, dict) and "_cmd" in item:
                self._handle_command(item)
            else:
                compact_requested = isinstance(item, str) and item.startswith(COMPACT_MARKER)
                if compact_requested:
                    item = item[len(COMPACT_MARKER):].lstrip()
                try:
                    turn_ok = True
                    # 压缩回合走专用流程 run_stream_compact（内部不触发技能选择/
                    # 工具调用，无需再临时切换 skill_select_enabled——保护已内聚）
                    stream = (self.run_stream_compact() if compact_requested
                              else self.run_stream(item))
                    for event in stream:
                        self._output_queue.put(event)
                        # 回合异常（error/interrupted）→ 跳过收尾，历史保持原样
                        if isinstance(event, dict) and event.get("type") in ("error", "interrupted"):
                            turn_ok = False
                    try:
                        row = self.db.execute("SELECT MAX(id) FROM messages").fetchone()
                        if row and row[0]:
                            self._last_sync_max_id = row[0]
                    except Exception:
                        pass
                    if compact_requested:
                        if turn_ok:
                            self._finalize_compact()
                        else:
                            logger.warning("压缩回合异常，跳过收尾副作用（历史保持不变）")
                finally:
                    # ── 回合结束：统一复位实时状态（覆盖所有退出路径）──
                    self.phase = "idle"
                    self.in_tool = False
                    self._turn_active = False   # 回合级标志：仅回合真正结束才复位
                    self._output_queue.put({"type": "_turn_end"})

    def _cmd_text(self, cmd_name: str, args: dict) -> str:
        """还原 mount 系列命令为可读文本（D3 决策），用于 history/日志展示。

        /mount <path> [ro|rw] [--force]  /unmount <path>  /mount list|save|refresh
        """
        if cmd_name == "mount":
            path = args.get("path", "") or ""
            parts = ["/mount", path]
            if args.get("writable") is False:
                parts.append("ro")
            elif args.get("writable") is True:
                parts.append("rw")
            if args.get("force"):
                parts.append("--force")
            return " ".join(parts)
        if cmd_name == "unmount":
            return f"/unmount {args.get('path', '')}".rstrip()
        sub = {"mount_list": "list", "mount_save": "save", "mount_refresh": "refresh"}.get(cmd_name)
        if sub:
            return f"/mount {sub}"
        return f"/{cmd_name}"

    def _record_command(self, cmd_name: str, args: dict, ok: bool, result: str) -> None:
        """统一记录 mount 系列命令执行痕迹（D1-b：独立 role='command'，不进 LLM 上下文）。

        写 history（add_chat role='command'）+ 打日志 + 刷新 _last_sync_max_id
        （避免被 _sync_from_db 当作其他进程新消息实时推送）。
        """
        text = self._cmd_text(cmd_name, args or {})
        try:
            add_chat(self.db, "command", text,
                     {"cmd": cmd_name, "ok": ok, "result": result})
        except Exception as e:
            logger.warning(f"记录 command 历史失败: {text!r}: {e}")
        logger.info(f"[cmd] {text} -> {'OK' if ok else 'FAIL'}: {(result or '')[:120]}")
        try:
            row = self.db.execute("SELECT MAX(id) FROM messages").fetchone()
            if row and row[0]:
                self._last_sync_max_id = max(self._last_sync_max_id or 0, row[0])
        except Exception:
            pass
    def _handle_command(self, cmd: dict):
        """Handle a control command sent via the input queue."""
        cmd_name = cmd["_cmd"]
        args = cmd.get("_args", {})
        # ── 观察者模式 gate (T3): 写类命令拒绝执行，读类命令放行 ──
        if self._observing and cmd_name in _OBSERVER_WRITE_CMDS:
            logger.warning(f"观察者模式拒绝写命令: session={self.session} cmd={cmd_name}")
            self._output_queue.put({
                "type": "_cmd_result", "cmd": cmd_name,
                "ok": False, "error": "观察者只读模式：session 已被其他进程占用",
            })
            return

        if cmd_name == "clear":
            self.clear()
            self._output_queue.put({"type": "_cmd_result", "cmd": "clear", "ok": True})

        elif cmd_name == "drop":
            self.drop()
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "drop", "ok": True,
            })

        elif cmd_name == "set_mode":
            mode = args.get("mode")
            if mode:
                self.set_mode(mode)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "set_mode",
                "data": self.mode,
            })

        elif cmd_name == "mount":
            try:
                result = self._cmd_mount(
                    path=args.get("path", ""),
                    writable=bool(args.get("writable", True)),
                    force=bool(args.get("force", False)),
                )
                self._record_command("mount", args, not result.startswith("❌"), result)
                self._output_queue.put({
                    "type": "_cmd_result", "cmd": "mount",
                    "ok": not result.startswith("❌"),
                    "data": result,
                })
            except Exception as e:
                logger.error(f"mount 命令异常: {e}", exc_info=True)
                self._record_command("mount", args, False, f"❌ mount 异常: {e}")
                self._output_queue.put({
                    "type": "_cmd_result", "cmd": "mount",
                    "ok": False, "error": str(e), "data": f"❌ mount 异常: {e}",
                })

        elif cmd_name == "unmount":
            try:
                result = self._cmd_unmount(args.get("path", ""))
                self._record_command("unmount", args, not result.startswith("❌"), result)
                self._output_queue.put({
                    "type": "_cmd_result", "cmd": "unmount",
                    "ok": not result.startswith("❌"),
                    "data": result,
                })
            except Exception as e:
                logger.error(f"unmount 命令异常: {e}", exc_info=True)
                self._record_command("unmount", args, False, f"❌ unmount 异常: {e}")
                self._output_queue.put({
                    "type": "_cmd_result", "cmd": "unmount",
                    "ok": False, "error": str(e), "data": f"❌ unmount 异常: {e}",
                })

        elif cmd_name == "mount_list":
            result = self._cmd_mount_list()
            self._record_command("mount_list", args, True, result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "mount_list",
                "ok": True, "data": result,
            })

        elif cmd_name == "mount_save":
            result = self._cmd_mount_save()
            self._record_command("mount_save", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "mount_save",
                "ok": not result.startswith("❌"),
                "data": result,
            })

        elif cmd_name == "mount_refresh":
            result = self._cmd_mount_refresh()
            self._record_command("mount_refresh", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "mount_refresh",
                "ok": not result.startswith("❌"),
                "data": result,
            })

        elif cmd_name == "info_add":
            result = self._cmd_info_add(
                args.get("path", ""), args.get("scope", "extra"),
                bool(args.get("global", False)))
            self._record_command("info_add", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "info_add",
                "ok": not result.startswith("❌"), "data": result,
            })

        elif cmd_name == "info_deny":
            result = self._cmd_info_deny(args.get("path", ""), bool(args.get("global", False)))
            self._record_command("info_deny", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "info_deny",
                "ok": not result.startswith("❌"), "data": result,
            })

        elif cmd_name == "info_remove":
            result = self._cmd_info_remove(args.get("path", ""), bool(args.get("global", False)))
            self._record_command("info_remove", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "info_remove",
                "ok": not result.startswith("❌"), "data": result,
            })

        elif cmd_name == "info_clear":
            result = self._cmd_info_clear(bool(args.get("global", False)))
            self._record_command("info_clear", args, not result.startswith("❌"), result)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "info_clear",
                "ok": not result.startswith("❌"), "data": result,
            })

        elif cmd_name == "set_model":
            model = args.get("model")
            if model:
                # 存纯模型名：拆掉 :effort 后缀（effort 走 _cmd_effort 通道）
                self.model = provider_config.split_model_effort(str(model))[0]
                set_agent_state(self.session, model=self.model)  # 持久化到 session db
            # 可选 reasoning_effort：仅作会话级临时覆盖，不持久化（重启回落配置）。
            # 每次 set_model 都重置 _cmd_effort —— 未显式指定则回落配置值（None），
            # 防止"先 /model x:high 再 /model y" 时旧 effort 残留串台。
            self._cmd_effort = str(args.get("reasoning_effort") or "") or None
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "set_model",
                "data": self.model,
            })

        elif cmd_name == "set_provider":
            pname = args.get("provider")
            if pname:
                # 切换 provider：先校验存在性；旧 provider 的 model 可能不适用，重置
                try:
                    provider_config.get_provider(pname)
                except ValueError as e:
                    self._output_queue.put({
                        "type": "_cmd_result", "cmd": "set_provider",
                        "ok": False, "data": str(e),
                    })
                else:
                    self.provider = pname
                    self.model = None
                    self._cmd_effort = None  # effort 跟随 model 重置，防串台
                    set_agent_state(self.session, provider=pname, model="")  # 持久化
                    new_default = provider_config.get_provider(pname).get("default_model", "")
                    hint = f" (旧模型已重置，默认将使用 {new_default})" if new_default else ""
                    self._output_queue.put({
                        "type": "_cmd_result", "cmd": "set_provider",
                        "ok": True, "data": f"Provider switched to {pname}{hint}",
                    })
            else:
                self._output_queue.put({
                    "type": "_cmd_result", "cmd": "set_provider",
                    "ok": False, "data": "usage: set_provider {name}",
                })
        elif cmd_name == "set_skill_select":
            # FIX: skill_select 开关同步到 agent 线程（原实现只改 manager 缓存，
            # agent 实际仍按 self.skill_select_enabled 执行，导致 UI 与行为不一致）
            enable = args.get("enabled")
            if isinstance(enable, bool):
                self.skill_select_enabled = enable
            else:
                self.skill_select_enabled = not self.skill_select_enabled
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "set_skill_select",
                "data": self.skill_select_enabled,
            })
        elif cmd_name == "resume":
            self.resume()
            self._output_queue.put({"type": "_cmd_result", "cmd": "resume", "ok": True})

        elif cmd_name == "load_skill":
            name = args.get("name", "")
            ok = self.load_skill(name)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "load_skill",
                "ok": ok, "data": name,
            })

        elif cmd_name == "get_info":
            import os as _os
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "get_info",
                "data": {
                    "session": self.session,
                    "mode": self.mode,
                    "model": self.model or "",
                    "provider": self.provider,
                    "reasoning_effort": self._resolve_effort(),
                    "msg_count": len(self.messages),
                    "is_locked": self._lock_held,
                    "is_observing": self._observing,
                    "holder_info": self._lock_holder_info,  # T5 新增
                    "pid": _os.getpid(),
                    "skill_select_enabled": self.skill_select_enabled,  # FIX: 供 manager 缓存同步
                },
            })

        elif cmd_name == "get_latest_rounds":
            n = args.get("n", 3)
            text = self.get_latest_rounds(n)
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "get_latest_rounds",
                "data": text,
            })

        elif cmd_name == "get_session_stats":
            """Return formatted session statistics (token usage, message count, etc.)."""
            try:
                text = self.get_session_stats()
            except Exception as e:
                text = f"⚠️ Stats unavailable: {e}"
                logger.warning(f"get_session_stats 失败: {e}")
            self._output_queue.put({
                "type": "_cmd_result", "cmd": "get_session_stats",
                "data": text,
            })

        else:
            self._output_queue.put({
                "type": "_cmd_result", "cmd": cmd_name,
                "ok": False, "error": f"Unknown command: {cmd_name}",
            })


    # ── /mount 动态挂载命令实现 ──

    # 确认标记：危险路径挂载需用户确认（生产端：_cmd_mount 返回该前缀；
    # repl 用 _ask 确认后自动带 --force 重发，web 提示用户显式 --force）
    CONFIRM_REQUIRED_PREFIX = "CONFIRM_REQUIRED:"

    def _cmd_mount(self, path: str, writable: bool, force: bool = False) -> str:
        """/mount <path> [ro|rw] [--force]：动态挂载宿主机路径到 pythonrt（软边界）。

        统一维护 _dyn_mounts 表（单一事实源）：目录/文件均记录路径+权限；
        pythonrt 受限模式（plan/build）按 rw/ro roots 应用；build-unsafe 忽略（软边界）。
        plan 模式 rw 报错；危险路径拒绝；tool 执行中拒绝。
        """
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝挂载操作，请稍后重试"
        norm = _normalize_mount_path(path, base=self.cwd)
        if not os.path.exists(norm):
            return f"❌ 路径不存在: {norm}"
        if norm == self.cwd:
            return f"❌ workdir 已自动挂载，无需重复挂载: {norm}"
        if _is_dangerous_mount(norm) and not force:
            # CONFIRM_REQUIRED 生产端：返回标记 → repl _ask 确认后自动带 --force 重发，
            # web 提示用户显式加 --force 完成确认（对齐 repl 语义）。
            return (f"{self.CONFIRM_REQUIRED_PREFIX}危险路径 {norm} 默认拒绝"
                    f"（系统目录/主目录），确需挂载请确认")
        if self.mode == "plan" and writable:
            return "❌ plan 模式只读，禁止 rw 挂载（可挂载 ro）"

        is_file = os.path.isfile(norm)
        # 幂等：移除同路径旧动态挂载
        for m in list(self._dyn_mounts):
            if m["path"] == norm:
                self._dyn_mounts.remove(m)
        self._dyn_mounts.append({
            "path": norm, "writable": writable, "source": MOUNT_SOURCE_DYNAMIC,
            "type": "file" if is_file else "dir",
        })
        # v3: /mount 即时持久化到 session db（重启自动恢复）；/mount save 仍写全局 permission.txt
        try:
            set_mount_state(self.session, self._dyn_mounts)
        except Exception as e:
            logger.warning(f"动态挂载写库失败: {norm} err={e}")
        return f"✅ 已挂载 {norm} ({'rw' if writable else 'ro'}) → pythonrt"
    def _cmd_unmount(self, path: str) -> str:
        """/unmount <path>：卸载动态挂载（移除 _dyn_mounts 记录）。"""
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝卸载操作，请稍后重试"
        norm = _normalize_mount_path(path, base=self.cwd)
        removed = [m for m in self._dyn_mounts if m["path"] == norm]
        if not removed:
            return f"ℹ️ 未找到动态挂载: {norm}（/mount list 查看）"
        self._dyn_mounts = [m for m in self._dyn_mounts if m["path"] != norm]
        # v3: /unmount 即时持久化到 session db
        try:
            set_mount_state(self.session, self._dyn_mounts)
        except Exception as e:
            logger.warning(f"动态挂载卸载写库失败: {norm} err={e}")
        return f"✅ 已卸载: {norm}"
    def _cmd_mount_list(self) -> str:
        """/mount list：列出当前所有挂载（default/permission/dynamic 三来源）。"""
        lines = ["  路径 | 权限 | 来源 | 生效范围"]
        lines.append("  " + "-" * 56)
        wd_writable = self.mode in ("build", "build-unsafe")
        lines.append(f"  {self.cwd} | {'rw' if wd_writable else 'ro'} | default | pythonrt")
        lines.append("  /tmp | rw | default | pythonrt")
        # v3 去重：同一 path 同时存在 permission（/mount save 全局）与 dynamic（session db）时
        # 显示一次，权限以 dynamic（当前 session 即时声明）为准；roots 合并由 tools.py 兜底
        merged: dict[str, tuple[str, str]] = {}
        for path, w in self._perm_volumes:
            merged[path] = (f"{'ro/rw' if w else 'ro'}", "permission")
        for m in self._dyn_mounts:
            eff = m["writable"] and self.mode in ("build", "build-unsafe")
            merged[m["path"]] = (f"{'rw' if eff else 'ro'}", "dynamic")
        for path, (perm, src) in merged.items():
            lines.append(f"  {path} | {perm} | {src} | pythonrt")
        if not self._dyn_mounts:
            lines.append("  (无动态挂载)")
        lines.append("")
        lines.append(f"  动态挂载 {len(self._dyn_mounts)} 项 | 当前模式: {self.mode}")
        return "\n".join(lines)

    # ── /info 搜索范围配置命令实现（per-session + 全局）──

    def _cmd_info_add(self, path: str, scope: str = "extra", is_global: bool = False) -> str:
        """/info add [--global] <path> [scope]：添加路径/目录进搜索范围。

        per-session 存 session db（search_state 表）；--global 写
        .xkagent/search_ranges.txt（全局生效，重启不丢）。
        目录或文件均可；scope 默认 extra（内置 5 范围之外的附加范围）。
        """
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝修改搜索范围，请稍后重试"
        norm = _normalize_mount_path(path, base=self.cwd)
        if not os.path.exists(norm):
            return f"❌ 路径不存在: {norm}"
        scope = (scope or "extra").strip() or "extra"
        if is_global:
            return self._global_search_config("add", norm, scope)
        items = get_search_state(self.session)
        items = [it for it in items if it["path"] != norm]
        items.append({"path": norm, "action": "add", "scope": scope})
        try:
            set_search_state(self.session, items)
        except Exception as e:
            return f"❌ 写库失败: {e}"
        return f"✅ 已添加搜索范围: {norm} (scope={scope}, session={self.session})"

    def _cmd_info_deny(self, path: str, is_global: bool = False) -> str:
        """/info deny [--global] <path>：禁止路径参与搜索（前缀匹配，文件/子树均排除）。"""
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝修改搜索范围，请稍后重试"
        norm = _normalize_mount_path(path, base=self.cwd)
        if not os.path.exists(norm):
            return f"❌ 路径不存在: {norm}"
        if is_global:
            return self._global_search_config("deny", norm)
        items = get_search_state(self.session)
        items = [it for it in items if it["path"] != norm]
        items.append({"path": norm, "action": "deny", "scope": ""})
        try:
            set_search_state(self.session, items)
        except Exception as e:
            return f"❌ 写库失败: {e}"
        return f"✅ 已禁止搜索: {norm} (session={self.session})"

    def _cmd_info_remove(self, path: str, is_global: bool = False) -> str:
        """/info remove [--global] <path>：移除该路径的搜索范围配置项。"""
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝修改搜索范围，请稍后重试"
        norm = _normalize_mount_path(path, base=self.cwd)
        if is_global:
            return self._global_search_config("remove", norm)
        items = get_search_state(self.session)
        before = len(items)
        items = [it for it in items if it["path"] != norm]
        if len(items) == before:
            return f"ℹ️ 未找到配置项: {norm}（/info 查看）"
        try:
            set_search_state(self.session, items)
        except Exception as e:
            return f"❌ 写库失败: {e}"
        return f"✅ 已移除: {norm}"

    def _cmd_info_clear(self, is_global: bool = False) -> str:
        """/info clear [--global]：清空搜索范围配置（session 或全局）。"""
        if self._in_tool_exec:
            return "❌ 工具执行中，拒绝修改搜索范围，请稍后重试"
        if is_global:
            return self._global_search_config("clear", "")
        items = get_search_state(self.session)
        if not items:
            return "ℹ️ 当前 session 无搜索范围配置"
        try:
            set_search_state(self.session, [])
        except Exception as e:
            return f"❌ 写库失败: {e}"
        return f"✅ 已清空 session 搜索范围配置（{len(items)} 项）"

    def _global_search_config(self, action: str, path: str, scope: str = "") -> str:
        """更新全局搜索范围配置 .xkagent/search_ranges.txt（全量重写，对齐 permission.txt 风格）。

        action: add | deny | remove | clear
        """
        from codes.search import SEARCH_CONFIG_FILE, load_search_config
        fp = os.path.join(str(config.get_data_dir()), SEARCH_CONFIG_FILE)
        cfg = load_search_config()
        adds = [it for it in cfg["adds"] if it["path"] != path]
        denies = [d for d in cfg["denies"] if d != path]
        if action == "add":
            adds.append({"path": path, "scope": scope or "extra"})
        elif action == "deny":
            denies.append(path)
        # remove/clear: 仅重写剩余项
        lines = [
            "# search_ranges.txt — 搜索范围配置（/info --global 管理）",
            "# 语法: <path> <add|deny> [scope]   scope 默认 extra；deny 前缀匹配排除",
        ]
        for it in adds:
            lines.append(f"{it['path']} add {it['scope'] or 'extra'}")
        for d in denies:
            lines.append(f"{d} deny")
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            return f"❌ 写全局配置失败: {e}"
        verb = {"add": "已添加", "deny": "已禁止", "remove": "已移除", "clear": "已清空"}.get(action, "已更新")
        return f"✅ {verb}（全局）: {path or '全部配置'} → {fp}"

    def _cmd_mount_save(self) -> str:
        """/mount save：将当前动态挂载写回 permission.txt（持久化，决策 A）。

        整体重写策略：保留原有注释与静态条目，剔除旧"动态挂载"块，
        再追加当前动态条目 —— 避免重复 save 产生重复块污染文件。
        目录与文件条目统一写入 <path> <read|write>；文件条目含二进制
        时 save 前不校验类型（重启加载时再按 _is_text_file 分类）。
        """
        if not self._dyn_mounts:
            return "ℹ️ 当前无动态挂载可保存"
        try:
            lines = []
            if os.path.isfile(PERMISSION_FILE):
                with open(PERMISSION_FILE, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            # 剔除旧动态块
            kept = []
            in_dyn = False
            for ln in lines:
                stripped = ln.strip()
                if stripped.startswith("# 动态挂载 (saved by /mount save)"):
                    in_dyn = True
                    continue
                if in_dyn:
                    if stripped and not stripped.startswith("#"):
                        continue
                    in_dyn = False
                kept.append(ln)
            while kept and not kept[-1].strip():
                kept.pop()
            with open(PERMISSION_FILE, "w", encoding="utf-8") as f:
                for ln in kept:
                    f.write(ln + "\n")
                f.write("\n# 动态挂载 (saved by /mount save)\n")
                for m in self._dyn_mounts:
                    f.write(f"{m['path']} {'ro/rw' if m['writable'] else 'ro'}\n")
            for m in self._dyn_mounts:
                self._perm_volumes = [v for v in self._perm_volumes if v[0] != m["path"]]
                self._perm_volumes.append((m["path"], m["writable"]))
            self._perm_paths = [p for p, w in self._perm_volumes]
            return f"✅ 已保存 {len(self._dyn_mounts)} 项挂载到 permission.txt"
        except Exception as e:
            return f"❌ 保存失败: {e}"
    def _cmd_mount_refresh(self) -> str:
        """/mount refresh：重新解析 permission.txt（无需重启）。"""
        self._perm_volumes = _parse_permission_file()
        self._perm_paths = [p for p, w in self._perm_volumes]
        return f"✅ 已重新加载 permission.txt，挂载 {len(self._perm_volumes)} 项"
    def set_mode(self, mode):
        logger.info(f"模式切换: {mode}")
        """Switch between plan / build / build-unsafe mode."""
        if mode not in ('plan', 'build', 'build-unsafe'):
            return False
        self.mode = mode
        # Clear LLM check cache when entering/leaving unsafe mode
        self._llm_check_cache.clear()
        return True
    @staticmethod
    def _recent_rounds(messages: list, n: int = 2) -> list:
        """从 messages 末尾倒序取最近 n 轮（按 user 消息切分，含当前轮）。

        直接倒序遍历找 user 消息：找到第 n 个 user 即为其起始位置；
        不足 n 个 user → 返回从首个 user 起（即全部有 user 的部分）。
        无 user 消息 → 返回全量。
        """
        if not messages:
            return []
        count = 0
        start = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                count += 1
                if count == n:
                    start = i
                    break
        return messages[start:]

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 LLM 回复中提取 JSON 对象（支持嵌套花括号）。

        raw_decode 从首个 '{' 尝试完整解析；失败回退简单正则。
        """
        if not text:
            return None
        decoder = json.JSONDecoder()
        i = text.find("{")
        while i >= 0:
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except (json.JSONDecodeError, ValueError):
                pass
            i = text.find("{", i + 1)
        m = re.search(r"\{[^}]*\}", text)
        if m:
            try:
                obj = json.loads(m.group())
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        return None

    def _select_skill(self, instruction: str) -> tuple[str, str, list] | None:
        """Phase 1: skill selection loop with temp_msgs (Plan B).

        Returns (skill_name, reason, collected_info) 3-tuple.
        失败/跳过时返回 (None, reason_with_error, []) — 错误原因随 reason 返回，供上层可见提示。
        """
        skill_list = SkillLoader.list_skills()
        if not skill_list:
            return (None, "", [])

        phase1_system = (
            "分析用户需求，从「建议技能」中选择最合适的一个，以 JSON 格式回复。\n"
            "回复包含两个字段（每次都必须同时给出）:\n"
            '  {"skill": "技能名" 或 null, "searchinfo": {...} 或 null, "reason": "理由"}\n'
            "字段说明:\n"
            '  - skill: 选中的技能名（建议技能列表内）；null = 无需技能\n'
            '  - searchinfo: 需要进一步收集信息的目录搜索参数 {"dirs_paths": [...], "query_or_keyword": "..."}；\n'
            "    null = 不再进一步收集信息（信息已足够，直接决策）\n"
            "流程: 信息不足时可先通过 searchinfo 收集指定目录信息，再基于新信息重选技能；\n"
            "      收集足够后返回 searchinfo: null + 最终 skill 选择。\n"
            "⚠️ 仅输出 JSON，不附加任何其他文字。\n"
            "reason 字段: 你选择该技能的自述理由（简洁一句），将注入对话历史作为自述承诺。\n"
        )

        current_instruction = instruction
        temp_msgs: list[dict] = []
        collected_info: list = []    # searchinfo 收集结果（需求4：自承时带上）
        searched_keys: set = set()   # 去重记忆：同 (dirs, query) 不重复执行

        for attempt in range(5):
            # ── Ctrl+C 中断检查：每次 attempt 前（与 run_stream 循环开头对称）──
            try:
                self._check_interrupt()
            except _InterruptTurn:
                raise  # 向上传递，由 run_stream 统一收尾（不写技能记录）

            if attempt == 0:
                # 首轮加强规则：禁止 null（要么选中技能，要么 searchskill 搜索新技能）
                phase1_system = (
                    "分析用户需求，从「建议技能」中选择最合适的一个，以 JSON 格式回复。\n"
                    "回复包含两个字段（每次都必须同时给出）:\n"
                    '  {"skill": "技能名" 或 null, "searchinfo": {...} 或 null, "reason": "理由"}\n'
                    "字段说明:\n"
                    '  - skill: 第一轮必须从建议技能中选择一个，或返回 "searchskill" 搜索新技能；禁止返回 null\n'
                    '  - searchinfo: 需要进一步收集信息的目录搜索参数 {"dirs_paths": [...], "query_or_keyword": "..."}；\n'
                    "    null = 不再进一步收集信息（信息已足够，直接决策）\n"
                    "流程: 信息不足时可先通过 searchinfo 收集指定目录信息，再基于新信息重选技能；\n"
                    "      收集足够后返回 searchinfo: null + 最终 skill 选择。\n"
                    "⚠️ 仅输出 JSON，不附加任何其他文字。\n"
                    "reason 字段: 你选择该技能的自述理由（简洁一句），将注入对话历史作为自述承诺。\n"
                )

            phase1_messages = (
                [{"role": "system", "content": phase1_system}]
                + self.messages
                + temp_msgs
            )

            try:
                self.phase = "llm"   # 技能选择也是一次 LLM 调用
                c, extras = complete(
                    messages=phase1_messages,
                    model=self.model,
                    provider=self.provider,
                    reasoning_effort=self._resolve_effort(),
                    tools=None,
                    interrupt_event=self._interrupt_event,
                )
            except Exception as e:
                self._log_error("skill_select", self.session, f"attempt={attempt} error={e}")
                # 工具消息格式错误（DeepSeek: "tool must be response to tool_calls"）
                # → 修补孤立 tool call 后重试，避免静默失败
                err_lower = str(e).lower()
                if "tool" in err_lower and "response" in err_lower:
                    logger.warning(f"_select_skill 遇 tool 格式错误 (attempt={attempt})，修补后重试")
                    self._patch_orphaned_tool_calls()
                    continue
                logger.warning(f"_select_skill 遇非 tool 错误 (attempt={attempt})，放弃技能选择: {e}")
                return (None, f"⚠️ 技能选择失败: {e}", [])

            self.phase = "idle"   # 技能选择 LLM 调用结束，复位（异常路径由 _turn_end 兜底）
            # ── Ctrl+C 中断检查：complete 返回后（llm break 可能已发生）──
            if self._interrupt_event.is_set():
                self._interrupt_event.clear()
                raise _InterruptTurn()

            usage = extras.get("usage", {})
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

            # ── 技能选择也是 LLM 调用，同步持久化 token 累计值 ──
            self._persist_token_state()

            json_obj = self._extract_json(c)
            if json_obj is not None:
                skill = json_obj.get("skill")
                si = json_obj.get("searchinfo")

                # ── searchinfo 分支：先收集指定目录信息再继续选技能 ──
                if isinstance(si, dict):
                    dirs = si.get("dirs_paths") or []
                    q = si.get("query_or_keyword")
                    if dirs and q:
                        key = (tuple(sorted(str(x) for x in dirs)), str(q))
                        if key in searched_keys:
                            temp_msgs.append({
                                "role": "user",
                                "content": "该目录/查询已收集过信息，请基于现有信息直接决策（返回 searchinfo: null + skill 选择）。",
                            })
                            continue
                        searched_keys.add(key)
                        try:
                            from codes.search import searchinfo
                            items = searchinfo(list(dirs), str(q), top_k=5, session=self.session)
                        except Exception as e:
                            logger.warning(f"searchinfo 执行失败: {e}")
                            items = []
                        if items:
                            collected_info.extend(items)
                            detail = "\n".join(
                                f"  [{it['method']}] {it['path']} | {it['snippet']}"
                                for it in items[:5]
                            )
                            temp_msgs.append({
                                "role": "user",
                                "content": (
                                    f"searchinfo 在指定目录收集到 {len(items)} 条相关片段:\n{detail}\n"
                                    "请基于这些信息从「建议技能」中选择（searchinfo: null），或继续收集。"
                                ),
                            })
                        else:
                            temp_msgs.append({
                                "role": "user",
                                "content": "searchinfo 未找到匹配内容。请基于现有信息直接决策（searchinfo: null + skill 选择）。",
                            })
                        continue

                # case 1: 选到具体技能
                if skill and skill in skill_list:
                    logger.info(f"技能选择: {skill}")
                    return (skill, json_obj.get("reason") or "", collected_info)

                # case 2: 无需技能（首轮禁止 null：要么选中技能，要么 searchskill 搜索）
                if skill is None:
                    if attempt == 0:
                        temp_msgs.append({"role": "user", "content":
                            "第一轮不能返回 null：必须从建议技能中选择一个，或返回 searchskill 搜索新技能。"})
                        continue
                    return (None, None, collected_info)

                # case 3: 需要搜索技能库
                if skill == "searchskill":
                    new_names = searchskill(current_instruction, top_k=5)
                    if not new_names:
                        return (None, None, collected_info)
                    new_details = searchskill_detail(current_instruction, top_k=5)
                    new_suggested = "\n".join(
                        f"  [{d['method']}] {d['path']} | {d['snippet']}" for d in new_details
                    ) if new_details else ""
                    temp_msgs.append({"role": "assistant", "content": c})
                    temp_msgs.append({
                        "role": "user",
                        "content": (
                            "搜索到以下新技能，请从它们中选择：\n"
                            f"建议技能: {new_suggested}\n"
                            f"原始需求: {current_instruction}"
                        )
                    })
                    continue

            # JSON 解析失败
            temp_msgs.append({"role": "assistant", "content": c})
            temp_msgs.append({
                "role": "user",
                "content": "响应格式错误，请严格按照 JSON 格式回复。只输出 JSON，包含 skill 与 searchinfo 字段。"
            })

        return (None, None, collected_info)


    def _skill_full_marker(self, name: str, version: str = "") -> str:
        """构造技能全文注入标记。

        标记带版本号：skill.md 更新后版本不匹配 → _skill_full_injected
        判定为"未注入" → 触发全文重注入，避免锚点指向旧版内容。
        """
        ver_part = f" v{version}" if version else ""
        return f"[技能 {name}{ver_part} 定义 - 完整版]"


    def _skill_full_injected(self, name: str) -> bool:
        """扫描 self.messages，判定该技能全文是否已注入且版本一致。

        判定来源是当前上下文（resume/compact/版本漂移后自然失效），
        不引入额外持久化状态，与「从 self.messages 实时判定」的设计一致。
        """
        meta = SkillLoader.load_meta(name) or {}
        version = meta.get("version", "")
        marker = self._skill_full_marker(name, version)
        return any(
            m.get("role") == "assistant" and marker in (m.get("content") or "")
            for m in self.messages
        )


    @staticmethod
    def _skill_anchor(skill_md: str, max_chars: int = 300) -> str:
        """提取技能全文的锚点（前缀...中段...后缀）。

        取首个、中段、末个 markdown 标题行作为语义锚点，供 LLM 在上下文中
        按标题回溯定位。仅取首尾标题在多技能间高度重复（如 check/plan 均为
        "## 概要 ... ## 文件引用"），区分度不足；中段选取信息量最大的中间标题
        （字符数最长者）并附带其下首行实质正文片段（跳过代码围栏），显著提升
        定位精确度。标题不足时逐级退化为 首尾/单个/截断，保证必有锚点。
        """
        lines = skill_md.splitlines()
        heading_idx = [i for i, ln in enumerate(lines)
                       if ln.strip().startswith("## ")]
        if len(heading_idx) >= 3:
            head = lines[heading_idx[0]].strip()
            tail = lines[heading_idx[-1]].strip()
            # 中段候选：中间所有标题，取字符数最长者（信息量最大、最具区分度）
            mid, mid_i = max(
                ((lines[i].strip(), i) for i in heading_idx[1:-1]),
                key=lambda t: len(t[0]),
            )
            # 中段标题下首行实质正文（跳过空行/标题/代码围栏），截断防超长
            snippet = ""
            for ln in lines[mid_i + 1:mid_i + 8]:
                s = ln.strip()
                if not s or s.startswith("#") or s.startswith("```") or s.startswith("~~~"):
                    continue
                snippet = s[:60]
                break
            anchor = f"{head} ... {mid}"
            if snippet:
                anchor += f" ({snippet})"
            anchor += f" ... {tail}"
            return anchor if len(anchor) <= max_chars else anchor[:max_chars]
        if len(heading_idx) == 2:
            return f"{lines[heading_idx[0]].strip()} ... {lines[heading_idx[-1]].strip()}"
        if heading_idx:
            return lines[heading_idx[0]].strip()
        return skill_md[:max_chars]


    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """剥离 skill.md 的 YAML frontmatter（---...---），只返回正文。

        全文注入只需向 LLM 提供技能正文，frontmatter（版本/作者等内部
        元数据）对执行无意义且浪费 token，故注入前剥离。
        非 frontmatter 内容原样返回（防御性）。
        """
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].lstrip("\n")
        return content


    def _build_skill_commitment(self, name: str, reason: str, skill_md: str,
                                full_injected: bool) -> str:
        """将 LLM 的技能选择回复改写为自述承诺 assistant 消息。

        形态 A（full_injected=False）: 技能全文首次注入，LLM 由此认识技能。
          文案: "选择 xxx skill, 因为 reason。该 skill 的内容我记得是: <全文>"
        形态 B（full_injected=True）: 全文已在上文，仅保留 首/中/末 标题锚点
          供回溯，大幅节省 token。
          文案: "选择 xxx skill, 因为 reason。该 skill 的流程全文之前有用过,
                 为 '前缀 ... 中段 ... 后缀'，中间省略，需要细节请回溯上文。"
        自述承诺利用一致性偏置：LLM 更倾向于遵守自己"说出口"的承诺，
        而非纯外部注入的指令。
        """
        meta = SkillLoader.load_meta(name) or {}
        version = meta.get("version", "")
        body = self._strip_frontmatter(skill_md)
        reason_part = f", 因为 {reason}" if reason else ""
        if full_injected:
            anchor = self._skill_anchor(body)
            return (
                f"选择 {name} skill{reason_part}。该 skill 的流程全文之前有用过，"
                f"为 \"{anchor}\"，中间省略，需要细节请回溯上文。"
            )
        marker = self._skill_full_marker(name, version)
        return (
            f"选择 {name} skill{reason_part}。该 skill 的内容我记得是:\n\n"
            f"{marker}\n{body}"
        )

    def _patch_orphaned_tool_calls(self):
        """修补消息历史中孤立的 tool 调用消息。

        问题背景
        --------
        DeepSeek API 要求每条 role='tool' 的消息前必须有一条
        role='assistant' + tool_calls 的消息来「声明」该调用。
        当用户通过 Ctrl+C (KeyboardInterrupt) 中断一个正在执行的
        tool 时，assistant 消息已写入 DB（含 tool_calls），
        但 tool 结果未写入。重新加载后出现「孤立 tool」→ API 报错。

        本函数扫描 self.messages：
          Step 1: 清理上一次运行留下的孤立 "tool is killed by user" 条目
          Step 2: 找出缺少 tool 响应的 assistant.tool_calls，补齐
                  "tool is killed by user"（避免多轮/多 tool 场景遗漏）

        整体策略：先清理旧标记、再补齐新标记——保持幂等性。
        """
        if not self.messages:
            return

        try:
            # ── Step 1: 清理历史遗留的 "tool is killed by user" ──
            # 这些是之前被插入但可能位置不对的旧条目（旧版 bug 遗留），
            # 先全部删除，Step 2 会重新插入到正确位置。
            stale_ids = []
            i = 0
            while i < len(self.messages):
                msg = self.messages[i]
                if (msg.get("role") == "tool"
                        and msg.get("content") == "tool is killed by user"):
                    stale_ids.append(msg.get("tool_call_id"))
                    self.messages.pop(i)  # 从 self.messages 中删除
                else:
                    i += 1

            # 同步从 DB 删除
            if stale_ids:
                for tid in stale_ids:
                    if tid:
                        # 兼容旧记录 extras 为空或非标准 JSON：先按 id 精确删除，
                        # 不再依赖 json_extract(NULL) 静默匹配失败。
                        self.db.execute(
                            "DELETE FROM messages WHERE role='tool' "
                            "AND content='tool is killed by user' "
                            "AND (json_extract(extras, '$.tool_call_id') = ? "
                            "OR extras LIKE ?)",
                            (tid, f'%' + tid + '%'),
                        )
                    else:
                        self.db.execute(
                            "DELETE FROM messages WHERE role='tool' "
                            "AND content='tool is killed by user' "
                            "AND (extras IS NULL OR extras = '')"
                        )
                self.db.commit()
                logger.info(f"补丁 Step 1: 清理了 {len(stale_ids)} 个孤立 tool 标记")

            # ── Step 2: 反向扫描，补齐缺失的 tool 响应 ──
            # 反向扫描确保嵌套/连续的 tool_calls 都能被正确处理：
            # 插入新条目不会影响尚未扫描到的更早 assistant 消息的索引。
            patched_count = 0
            for i in range(len(self.messages) - 1, -1, -1):
                msg = self.messages[i]
                if msg.get("role") == "assistant" and "tool_calls" in msg:
                    # 使用 .get() 避免 KeyError——极端情况下工具调用可能缺少 id
                    tc_ids = {tc.get('id') for tc in msg['tool_calls'] if tc.get('id')}
                    if not tc_ids:
                        continue

                    # 找出此 assistant 消息之后已有的 tool 响应
                    responded = set()
                    for j in range(i + 1, len(self.messages)):
                        if self.messages[j].get("role") == "tool":
                            responded.add(self.messages[j].get("tool_call_id"))

                    # 计算缺失的 tool_call_id
                    missing = tc_ids - responded
                    if missing:
                        # 找到插入位置：紧跟在所有连续 tool 响应之后
                        insert_pos = i + 1
                        for k in range(i + 1, len(self.messages)):
                            if self.messages[k].get("role") != "tool":
                                break
                            insert_pos = k + 1
                        for tid in sorted(missing):
                            tool_msg = {
                                "role": "tool",
                                "tool_call_id": tid,
                                "content": "tool is killed by user",
                            }
                            self.messages.insert(insert_pos, tool_msg)
                            add_chat(self.db, "tool", "tool is killed by user", {"tool_call_id": tid})
                            insert_pos += 1
                            patched_count += 1

            if patched_count > 0:
                logger.info(f"补丁 Step 2: 补齐了 {patched_count} 个孤立 tool 标记")

            # ── Step 3: 反向扫描，修复孤儿 tool 消息（缺少前置 assistant.tool_calls）──
            # 某些代码路径（如 run_stream 早期的 bug）可能直接追加了 role=tool 消息
            # 但忘记先追加对应的 assistant（含 tool_calls）消息。
            # 这会导致 DeepSeek API 拒绝："Messages with role 'tool' must be a response
            # to a preceding message with 'tool_calls'"
            i = 0
            while i < len(self.messages):
                if self.messages[i].get("role") != "tool":
                    i += 1
                    continue

                tid = self.messages[i].get("tool_call_id")
                if not tid:
                    i += 1
                    continue

                # 向前查找是否有匹配的 assistant.tool_calls
                found_parent = False
                for j in range(i - 1, -1, -1):
                    prev = self.messages[j]
                    if prev.get("role") == "assistant" and "tool_calls" in prev:
                        for tc in prev["tool_calls"]:
                            if tc.get("id") == tid:
                                found_parent = True
                                break
                    if found_parent:
                        break

                if not found_parent:
                    # 收集这一批连续的孤儿 tool 消息
                    orphan_start = i
                    orphan_end = i
                    orphan_ids = []
                    while orphan_end < len(self.messages) and self.messages[orphan_end].get("role") == "tool":
                        orphan_ids.append(self.messages[orphan_end].get("tool_call_id", ""))
                        orphan_end += 1

                    # 构造合成 assistant 消息，认领这些孤儿
                    synthetic_tool_calls = []
                    for oid in orphan_ids:
                        synthetic_tool_calls.append({
                            "id": oid,
                            "type": "function",
                            "function": {"name": "unknown", "arguments": "{}"},
                        })

                    assistant_msg = {
                        "role": "assistant",
                        "content": "[auto-repaired: recreated missing tool_calls for orphaned tool messages]",
                        "tool_calls": synthetic_tool_calls,
                    }
                    self.messages.insert(orphan_start, assistant_msg)
                    add_chat(self.db, "assistant",
                             "[auto-repaired: recreated missing tool_calls for orphaned tool messages]",
                             {"tool_calls": synthetic_tool_calls})
                    logger.info(f"补丁 Step 3: 修复了 {len(orphan_ids)} 个孤儿 tool 消息 (插入虚假 assistant.tool_calls)")
                    # 跳过已处理的块
                    i = orphan_end + 1
                else:
                    i += 1

        except Exception as e:
            # ── 异常安全 ──
            # _patch_orphaned_tool_calls 本身绝不能崩溃，否则 self.messages
            # 会处于「部分删除但未完全修复」的不一致状态，下次 LLM 调用一定报错。
            # 这里即使异常，至少不影响后续流程继续运行。
            logger.error(f"_patch_orphaned_tool_calls 异常: {e}")
            self._log_error("patch_orphaned", self.session, f"error={e}")

    def clear(self):
        self.messages.clear()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._turn_usage_history.clear()
        self._turn_count = 0
        self._exit_requested = False
        self._exit_reason = ""
        self._exit_note = ""
        self._tool_retry_count = 0
        self._llm_check_cache.clear()
        clear_chats(self.db)
        # ── /clear 清空消息时同步清零 token 累计（db 与内存一致）──
        try:
            set_token_state(
                self.session,
                prompt_tokens=0,
                completion_tokens=0,
                turn_count=0,
                model="",
                last_prompt_tokens=0,
                last_completion_tokens=0,
            )
        except Exception as e:
            logger.warning(f"token_state 清零失败: {e}")

    def drop(self):
        """Drop conversation history: insert a drop marker and clear in-memory messages.

        Similar to compact() but skips LLM compression — simply marks the
        current history as dropped and resets the in-memory state.
        """
        # ── Get current max message id before inserting marker ──
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) as max_id FROM messages").fetchone()
        cutoff_max_id = row["max_id"]

        # ── Insert drop marker (keeps DB history intact) ──
        add_chat(self.db, "drop", "", {"cutoff_max_id": cutoff_max_id})

        # ── Clear in-memory messages, insert synthetic marker ──
        self.messages.clear()
        dropped_msg = "[对话历史已丢弃。之前的消息已标记为丢弃，后续消息从此处开始。]\n"
        self.messages.append({"role": "user", "content": dropped_msg})
        return True

    def load_skill(self, name):
        logger.info(f"加载技能: {name}")
        skill = SkillLoader.load(name)
        if not skill:
            return False
        self.active_skills = [s for s in self.active_skills if s.name != name]
        self.active_skills.append(skill)
        return True

    def _finalize_compact(self) -> bool:
        """压缩收尾副作用：写 compact marker + 重置内存上下文。

        由 run_forever 在压缩回合（run_stream 正常完成）后调用。
        run_stream 已把压缩总结作为 assistant 消息写入 DB，这里只需：
          1. 取最后一条 assistant 消息为 summary
          2. 写 compact marker（cutoff_max_id = 当前 MAX(id)，截断旧历史）
          3. 重置 self.messages = [总结]（后续对话从总结继续）

        返回 True=成功执行收尾；False=无可压缩内容（guard 通过，历史保持不变）。
        """
        # ── 取最后一条 assistant 消息作为 summary ──
        summary = ""
        for m in reversed(self.messages):
            if m.get("role") == "assistant":
                summary = (m.get("content") or "").strip()
                break
        if not summary:
            logger.warning("压缩回合未产生总结内容，跳过收尾副作用")
            return False

        # ── Get current max message id before inserting marker ──
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) as max_id FROM messages").fetchone()
        cutoff_max_id = row["max_id"]

        # ── Insert compact marker (keeps DB history intact) ──
        add_chat(self.db, "compact", summary, {"cutoff_max_id": cutoff_max_id})

        # ── Clear in-memory messages, insert synthetic summary ──
        self.messages.clear()
        compressed_msg = (
            "[对话历史已压缩。以下是完整上下文，请基于此继续当前任务：]\n\n"
            + summary + chr(10)
        )
        self.messages.append({"role": "user", "content": compressed_msg})

        # ── summary 持久化：压缩总结落盘 docs（失败仅降级，不阻断收尾）──
        try:
            from codes.search import write_doc
            _rel = write_doc(self.session, summary, source="compact",
                             title="对话压缩总结", model=self._last_model or "")
            logger.info(f"压缩总结已落盘: {_rel}")
        except Exception as e:
            logger.warning(f"压缩总结落盘失败（降级，不影响压缩）: {e}")

        logger.info(f"压缩收尾完成: summary={len(summary)}字符, cutoff_max_id={cutoff_max_id}")
        return True


    def get_session_stats(self):
        msgs = len(self.messages)
        total = self.total_prompt_tokens + self.total_completion_tokens
        ratio = _format_ratio(self.total_prompt_tokens, self.total_completion_tokens)
        cost = _estimate_cost(self._last_model, self.total_prompt_tokens, self.total_completion_tokens)
        lines = ['']
        lines.append(f"  {chr(8212) * 40}")
        lines.append(f"  Session: {self.session}")
        lines.append(f"  {'─' * 40}")
        lines.append(f"  Model:   {self.model or '(default)'}")
        lines.append(f"  Mode:    {self.mode}")
        lines.append(f"  Status:  active")
        lines.append(f"  Messages: {msgs}")
        lines.append(f"  Turns:    {self._turn_count}")
        lines.append("")
        lines.append(f"  Token Usage")
        lines.append(f"     Prompt:     {self.total_prompt_tokens:>10,}")
        lines.append(f"     Completion: {self.total_completion_tokens:>10,}")
        lines.append(f"     Total:      {total:>10,}")
        lines.append(f"     Ratio:      {ratio}")
        lines.append(f"     Cost:       {_format_cost(cost)}")
        return '\n'.join(lines)

    def get_latest_rounds(self, n: int = 3) -> str:
        """Return formatted text of the latest N conversation rounds.

        A round = user message + assistant/tool messages that follow.
        展示层剥离用户消息的系统前缀（时间/系统模式/建议技能/正文），
        按角色分层渲染，避免元数据与正文、LLM 回复混排。
        """
        msgs = self.messages
        rounds = []
        current_round = []
        for msg in reversed(msgs):
            current_round.insert(0, msg)
            if msg["role"] == "user":
                rounds.append(current_round)
                current_round = []
                if len(rounds) == n:
                    break
        rounds.reverse()
        if not rounds:
            return "  \u2139\ufe0f No conversation history."

        lines = []
        lines.append(f"  {_SEP_LINE}")
        lines.append(f"  \U0001f4ac Latest {len(rounds)} round(s):")
        lines.append(f"  {_SEP_LINE}")

        def _indent(text: str, prefix: str = "     ") -> str:
            """多行内容统一缩进对齐，避免与角色标签混排。"""
            if "\n" not in text:
                return text
            return ("\n" + prefix).join(text.split("\n"))

        for i, rnd in enumerate(rounds):
            for msg in rnd:
                role = msg["role"]
                content = msg.get("content", "") or ""
                if role == "user":
                    parsed = parse_user_prefix(content)
                    display = _indent(parsed["body"] if parsed else content)
                    lines.append(f"  \U0001f9d1 [{i+1}] \u4f60: {display}")
                elif role == "assistant":
                    if "tool_calls" in msg:
                        tc_names = [tc.get("function", {}).get("name", "?")
                                    for tc in msg["tool_calls"]]
                        lines.append(f"  \U0001f916 LLM: {_indent(content)}  [{', '.join(tc_names)}]")
                    else:
                        lines.append(f"  \U0001f916 LLM: {_indent(content)}")
                elif role == "tool":
                    lines.append(f"    \U0001f527 Tool: {_indent(content)}")
            if i < len(rounds) - 1:
                lines.append(f"  {_SEP_LINE}")
        lines.append(f"  {_SEP_LINE}")
        return '\n'.join(lines)

    # ── Pause / Resume (ESC key) ──

    def _check_esc(self):
        """Non-blocking check for a single ESC / Ctrl+C keypress on stdin.

        ESC   → toggle pause state (暂停/恢复)
        Ctrl+C (0x03) → set interrupt event (完全停止当前 turn)

        Returns True if any key was consumed.
        Only checks every 4 calls to avoid excessive syscalls during streaming.
        """
        self._pause_check_counter += 1
        if self._pause_check_counter % 4 != 0:
            return False

        if not sys.stdin.isatty():
            return False

        fd = sys.stdin.fileno()
        try:
            if select.select([fd], [], [], 0) == ([fd], [], []):
                ch = os.read(fd, 1)
                if ch == b'\x1b':
                    self._paused = not self._paused
                    return True
                if ch == b'\x03':  # Ctrl+C → 请求中断当前 turn
                    self._interrupt_event.set()
                    return True
        except (BlockingIOError, OSError, ValueError, AttributeError):
            pass
        return False

    def _wait_for_resume(self):
        """Block until the user presses ESC, Enter, or Space to resume."""
        print()
        print('  [Paused] Press ESC or Enter to resume...', end='', flush=True)
        fd = sys.stdin.fileno()
        while self._paused:
            try:
                if select.select([fd], [], [], 0.1) == ([fd], [], []):
                    ch = os.read(fd, 1)
                    if ch in (b'\x1b', b'\r', b'\n', b' '):
                        self._paused = False
                        print('\r' + ' ' * 60 + '\r', end='', flush=True)
                        return
            except (BlockingIOError, OSError, ValueError):
                break
        print('\r' + ' ' * 60 + '\r', end='', flush=True)

    def _check_pause_point(self):
        """Check ESC and handle pause if needed. Call at streaming/tool boundaries."""
        changed = self._check_esc()
        if changed and self._paused:
            self._wait_for_resume()

    # ── Interrupt (Ctrl+C) — 完全停止当前 turn ──

    def request_interrupt(self):
        """请求中断当前 turn（线程安全，主线程经 manager 调用）。

        只设置事件标志，不抛异常；agent 线程在下一个检查点消费并正常收尾。
        """
        self._interrupt_event.set()

    def interrupt_tool(self):
        """中断当前正在执行的 tool（主线程直接调用）。

        pythonrt 工具: run_pythonrt 主线程每 50ms 轮询 _interrupt_event，
        检测到后 SIGKILL 终止 worker 子进程（tools._kill_and_reap）。
        """
        if self._in_tool_exec:
            self._interrupt_event.set()
    def _check_interrupt(self):
        """中断检查点：检测到中断事件则抛 _InterruptTurn 停止当前 turn。

        在 LLM 流式 chunk 间、tool 执行前、每轮循环开头调用。
        """
        self._check_esc()  # 顺带检查 stdin 上的 Ctrl+C
        if self._interrupt_event.is_set():
            self._interrupt_event.clear()
            raise _InterruptTurn()

    def run_stream(self, instruction):
        logger.info(f"run_stream() 开始: instruction={instruction!r:.80}")
        # ── 观察者模式 gate (T2): session 被其他进程占用时拒绝 LLM 交互与 DB 写入 ──
        if self._observing:
            logger.warning(f"观察者模式拒绝输入: session={self.session} 被其他进程占用")
            yield {"type": "blocked", "reason": "session 已被其他进程占用（观察者只读模式）"}
            return
        self.resume()
        self._exit_requested = False
        self._exit_reason = ""
        self._tool_retry_count = 0
        self._turn_active = True   # 回合开始（含技能选择/工具间隙全程保持）
        instruction = _resolve_refs(instruction, self.cwd)
        instruction = _sanitize(instruction)

        # ── Build user message metadata (timestamp + mode + suggested skills) ──
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        skill_details = searchskill_detail(instruction, top_k=5)
        skill_names = [d["name"] for d in skill_details]
        suggested_skills = "\n".join(
            f"  [{d['method']}] {d['path']} | {d['snippet']}" for d in skill_details
        ) if skill_details else ""
        mode_label = self.mode
        req_line = ""
        if self._pending_skill_req:
            req_line = f"要求: 用户要求调用{self._pending_skill_req}\n"
            self._pending_skill_req = None

        # ── 推荐信息：跨范围（skills/docs/historys/logs，codes 默认关闭）检索相关片段+路径 ──
        info_lines = ""
        info_items = []
        try:
            info_items = recommend_info(instruction, top_k=5, exclude_session=self.session, session=self.session)
        except Exception as e:
            logger.warning(f"recommend_info 静默降级: {e}")
        if info_items:
            info_lines = "推荐信息:\n" + "".join(
                f"  [{it['scope']}] {it['path']} | {it['snippet']}\n" for it in info_items
            )

                # ── 路径访问权限摘要（按当前 mode 生成，供 pythonrt 调用前核对）──
        _perm_parts = []
        for _p, _w in getattr(self, "_perm_volumes", []):
            _perm_parts.append("%s(%s)" % (_p, "ro/rw" if _w else "ro"))
        for _m in getattr(self, "_dyn_mounts", []):
            _perm_parts.append("%s(%s)" % (_m.get("path"), "ro/rw" if _m.get("writable") else "ro"))
        _root_mode = "可写" if self.mode in ("build", "build-unsafe") else "只读"
        _perm_summary = ", ".join(_perm_parts) if _perm_parts else "无"
        perm_line = "路径访问权限: 项目根(%s)=%s; /tmp=可写(不持久化); %s/=只读; 挂载: %s\n" % (
            self.cwd, _root_mode, config.DATA_DIR_NAME, _perm_summary)
        sys_prefix = (
            f"时间: {now_str}\n"
            f"系统模式: {mode_label}\n"
            f"{perm_line}"
            f"建议技能: {suggested_skills}\n"
            f"{info_lines}"
            f"{req_line}"
            f"正文:\n"
        )
        instruction = sys_prefix + instruction


        self._patch_orphaned_tool_calls()
        self.messages.append({'role': 'user', 'content': instruction})
        add_chat(self.db, 'user', instruction)

        lang = _detect_language(instruction)
        system_prompt = _build_system_prompt(lang)

        tools = self._all_tools()
        tool_schemas = [t.to_openai_schema() for t in tools]

        selected_skill = None
        if self.skill_select_enabled:
            # ── 方案B: 实时路径访问权限提示（供 web/repl 实时渲染）──
            yield {"type": "permission", "data": perm_line.strip()}
            # ── Show suggested skills to user ──
            yield {"type": "suggested_skills", "skills": skill_names}
            if info_items:
                yield {"type": "recommended_info", "items": info_items}
            if req_line:
                skill_name_only = req_line.replace("要求: 用户要求调用", "").replace("\n", "")
                yield {"type": "skill_req", "name": skill_name_only}
            try:
                selected = self._select_skill(instruction)
            except _InterruptTurn:
                # skill_select 期间 Ctrl+C：不写任何技能记录，直接中断收尾
                self._log_error("interrupt", self.session, "user interrupted turn")
                yield {"type": "interrupted"}
                return
            selected_skill, skill_reason, collected_info = (
                selected if selected and len(selected) == 3 else (None, "", [])
            )
            # ── Phase 1: 技能选择 & 历史写入（自述承诺注入）──
            if selected_skill:
                skill_dir = SkillLoader._find_skill_dir(selected_skill)
                if skill_dir is not None:
                    skill_path = skill_dir / "skill.md"
                    skill_md = skill_path.read_text(encoding="utf-8")
                    # 判定全文是否已注入（含版本比对）→ 全文版 / 锚点版
                    full_injected = self._skill_full_injected(selected_skill)
                    inject_msg = self._build_skill_commitment(
                        selected_skill, skill_reason, skill_md, full_injected
                    )
                    if collected_info:
                        _info_summary = "\n".join(
                            f"  [{it['method']}] {it['path']} | {it['snippet']}"
                            for it in collected_info[:5]
                        )
                        inject_msg += f"\n\n📎 技能选择前收集的信息:\n{_info_summary}"
                    meta = SkillLoader.load_meta(selected_skill) or {}
                    version = meta.get("version", "")
                    # 自述承诺作为 assistant 消息落库，extras 标注注入元数据
                    extras = {
                        "injected": True,
                        "inject_type": "recall" if full_injected else "full",
                        "skill_name": selected_skill,
                        "skill_version": version,
                        "reason": skill_reason,
                        "searchinfo_items": collected_info[:5],
                    }
                    self.messages.append({"role": "assistant", "content": inject_msg})
                    add_chat(self.db, "assistant", inject_msg, extras)
                    yield {"type": "skill_selected", "name": selected_skill,
                           "reason": skill_reason}
                else:
                    # skill.md 缺失：不再静默 pass，显式记录 + 通知 UI
                    logger.warning(
                        f"技能 {selected_skill} 已选中但 skill.md 缺失（双目录均无）")
                    missing_msg = f"🎯 技能选择: {selected_skill}（skill.md 缺失）"
                    self.messages.append({"role": "assistant", "content": missing_msg})
                    add_chat(self.db, "assistant", missing_msg,
                             {"injected": False, "skill_name": selected_skill,
                              "missing": True})
                    yield {"type": "skill_selected", "name": selected_skill,
                           "reason": skill_reason, "missing": True}
            else:
                self.messages.append({
                    "role": "assistant",
                    "content": "🎯 技能选择: 无"
                })
                add_chat(self.db, "assistant", "🎯 技能选择: 无", {"injected": False})
                yield {"type": "no_skill", "reason": skill_reason}

        while True:

            # 每次 LLM 流式调用前修补孤立 tool 调用
            # 用户可能在任意 tool 执行中按 Ctrl+C（KeyboardInterrupt），
            # 导致 assistant 消息已写 DB（含 tool_calls）但 tool 结果未写入。
            # 这种「孤儿」消息会让 DeepSeek API 拒绝请求。
            self._patch_orphaned_tool_calls()
            # ── exit tool 检查：真正退出循环（T8）──
            if self._exit_requested:
                yield {"type": "turn_end_by_tool", "name": "exit",
                       "key": getattr(self, "_exit_note", "")}
                self._exit_requested = False
                self._exit_reason = ""
                self._exit_note = ""
                return
            # ── Ctrl+C 中断检查：每轮循环开头 ──
            try:
                self._check_interrupt()
            except _InterruptTurn:
                self._log_error("interrupt", self.session, "user interrupted turn")
                yield {"type": "interrupted"}
                return
            full_messages = [{'role': 'system', 'content': system_prompt}] + self.messages

            # ── 多模态图像注入（/image add 附加的图片，随最新 user 消息发送）──
            # 默认一次性：注入后自动清空附件（/image clear 等价效果），
            # 避免多轮对话持续携带图像导致计费膨胀。
            # 仅 run_stream 主循环注入（run_stream_compact 压缩回合不注入）；
            # 注入只发生在 full_messages 临时副本，不回写 self.messages/DB。
            try:
                from codes.history import get_images, clear_images
                imgs = get_images(self.session)
                if imgs:
                    injected = False
                    for idx in range(len(full_messages) - 1, -1, -1):
                        if full_messages[idx].get("role") == "user":
                            msg = full_messages[idx]
                            text = msg.get("content")
                            if isinstance(text, str) and text.strip():
                                blocks = [{"type": "text", "text": text}]
                                for ip in imgs:
                                    blk = _make_image_url_block(ip)
                                    if blk is not None:
                                        blocks.append(blk)
                                        injected = True
                                full_messages[idx] = {**msg, "content": blocks}
                            break
                    # 一次性语义：图已编码进本轮消息（full_messages 副本），
                    # 注入后立即清空附件，下次对话不再携带。
                    if injected:
                        try:
                            clear_images(self.session)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"图像注入失败: {e}")

            yield {"type": "_lock_status", "is_locked": self._lock_held, "is_observing": self._observing, "holder_info": self._lock_holder_info}
            self.phase = "llm"   # 进入 LLM 回复流（含流式 chunk）
            yield {"type": "thinking"}

            try:
                stream_gen = complete_stream(
                    messages=full_messages,
                    model=self.model,
                    provider=self.provider,
                    reasoning_effort=self._resolve_effort(),
                    tools=tool_schemas,
                    interrupt_event=self._interrupt_event,
                )

                accumulated_content = ''
                accumulated_tool_calls = []
                first_token = True

                try:
                    while True:
                        chunk = next(stream_gen)
                        if chunk['type'] == 'text':
                            if first_token:
                                yield {"type": "clear_thinking"}
                                first_token = False
                            accumulated_content += chunk['delta']
                            yield {"type": "text", "data": chunk["delta"]}
                            # ── ESC pause check during streaming ──
                            self._check_pause_point()
                            # ── Ctrl+C interrupt check during streaming ──
                            self._check_interrupt()
                        elif chunk['type'] == 'reasoning':
                            # 思考链内容（DeepSeek reasoning_content）：
                            # 逐 delta 转发，前端折叠展示（thinking 框）。
                            # 注意：不触发 clear_thinking —— 思考结束后首个 text 才触发。
                            yield {"type": "thinking_content", "data": chunk["delta"]}
                except _InterruptTurn:
                    # 用户中断流式返回：关闭底层流，丢弃未完成内容，正常返回
                    try:
                        stream_gen.close()
                    except Exception:
                        pass
                    self._log_error("interrupt", self.session, "user interrupted turn")
                    yield {"type": "interrupted"}
                    return
                except StopIteration as e:
                    full_content, extras = e.value
                    # thinking 内容（llm 层已聚合到 extras.reasoning_content）：
                    # 仅用于落库 thinking 角色供 web 展示，绝不写入 self.messages，
                    # 保证 get_chat_messages 给 LLM 的历史不含 thinking。
                    accumulated_reasoning = (extras.get("reasoning_content") or "").strip()
                    # ── B1 核心修复：llm.py break 后 Event 残留 → 检测并中断收尾 ──
                    if self._interrupt_event.is_set() or extras.get("interrupted"):
                        self._interrupt_event.clear()
                        self._log_error("interrupt", self.session, "user interrupted turn")
                        yield {"type": "interrupted"}
                        return
                    if not accumulated_content and full_content:
                        accumulated_content = full_content
                    accumulated_tool_calls = extras.get('tool_calls', [])
                    logger.info(f"流式响应完成: tools={len(accumulated_tool_calls)}")

                    # ── 修复:tool_calls arguments 损坏重试（P0 方案 C）──
                    # complete_stream 已把损坏调用 id 记入 extras["tool_calls_invalid"]。
                    # 本 turn 尚未写入消息历史（assistant_msg 在下方才构造）→ 直接
                    # continue 重试 LLM 调用（最多 MAX_TOOL_ARG_RETRY 次），避免把
                    # 损坏 arguments 写入 assistant 消息污染后续上下文。
                    _invalid_ids = extras.get("tool_calls_invalid", [])
                    if _invalid_ids and not accumulated_content:
                        if self._tool_retry_count < MAX_TOOL_ARG_RETRY:
                            self._tool_retry_count += 1
                            logger.warning(
                                f"[修复] tool_calls arguments 损坏 {_invalid_ids}，"
                                f"重试 LLM 调用 ({self._tool_retry_count}/{MAX_TOOL_ARG_RETRY})"
                            )
                            continue  # 回到主循环开头重新生成
                        logger.warning(
                            f"[修复] tool_calls arguments 损坏 {_invalid_ids}，"
                            f"已达重试上限，交由逐工具校验兜底"
                        )


            except Exception as e:
                self._log_error("llm_stream", self.session, f"error={e}")
                yield {"type": "error", "data": f"LLM call failed: {e}"}
                return
            usage = extras.get('usage', {})
            pt = usage.get('prompt_tokens', 0)
            ct = usage.get('completion_tokens', 0)
            self.total_prompt_tokens += pt
            self.total_completion_tokens += ct
            self._last_model = extras.get('model', self.model or '')

            self._turn_count += 1
            turn_tc_count = len(accumulated_tool_calls)
            self._turn_usage_history.append({
                'prompt': pt,
                'completion': ct,
                'turn': self._turn_count,
                'model': self._last_model,
                'tool_calls': turn_tc_count,
            })

            # ── 显式持久化 token 累计值到 session db ──
            # 使切换 session / 重启时能恢复（与 _select_skill / compact 共用）
            self._persist_token_state()

            tool_calls = accumulated_tool_calls

            if not tool_calls:
                final_answer = _sanitize(accumulated_content)
                # thinking 先落库（顺序：thinking → assistant），与实时流一致；
                # 仅写 db 供 web 展示，不写 self.messages（LLM 上下文不含 thinking）。
                if accumulated_reasoning:
                    add_chat(self.db, "thinking", accumulated_reasoning)
                self.messages.append({'role': 'assistant', 'content': final_answer})
                add_chat(self.db, 'assistant', final_answer)
                total = pt + ct
                ratio = _format_ratio(pt, ct)
                cost = _estimate_cost(self._last_model, pt, ct)
                up = chr(8593)
                down = chr(8595)
                yield {"type": "stats", "prompt_tokens": pt, "completion_tokens": ct, "model": self._last_model}
                return
            # thinking 先落库（顺序：thinking → assistant(tool_call) → tool）
            if accumulated_reasoning:
                add_chat(self.db, "thinking", accumulated_reasoning)
            assistant_msg = {'role': 'assistant', 'content': accumulated_content or ''}
            if tool_calls:
                assistant_msg['tool_calls'] = tool_calls
            self.messages.append(assistant_msg)
            add_chat(self.db, 'assistant', accumulated_content or '', {'tool_calls': tool_calls})
            killed = False
            stop_turn = False
            stop_turn_tool = ""

            for i, tc in enumerate(tool_calls):
                if killed:
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "tool is killed by user",
                    })
                    add_chat(self.db, "tool", "tool is killed by user", {"tool_call_id": tc["id"]})
                    continue
                # ── ESC pause check before each tool call ──
                self._check_pause_point()
                # ── Ctrl+C interrupt check before each tool call ──
                try:
                    self._check_interrupt()
                except _InterruptTurn:
                    # 中断：当前及剩余 tool_calls 标记 killed，正常返回
                    for tc_rest in tool_calls[i:]:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc_rest["id"],
                            "content": "tool is killed by user",
                        })
                        add_chat(self.db, "tool", "tool is killed by user", {"tool_call_id": tc_rest["id"]})
                    self._log_error("interrupt", self.session, "user interrupted turn")
                    yield {"type": "interrupted"}
                    return

                tool_name = tc['function']['name']
                tool_args_str = tc["function"]["arguments"]
                try:
                    tool_args = json.loads(tool_args_str)
                except json.JSONDecodeError:
                    # 修复:不再静默降级为 {}，而是把真实根因回传 LLM
                    # 之前 LLM 只会看到 "missing 1 required positional argument"
                    # 而不知道是自己的 arguments 输出非法，导致反复错误调用。
                    result = ToolResult(
                        error=(f"[参数解析失败] 工具 {tool_name} 的 arguments 不是合法 JSON："
                               f"{tool_args_str!r}\n"
                               f"请重新生成，arguments 必须是合法的 JSON 对象，"
                               f"如 {{\"key\": \"value\"}}")
                    )
                    result_str = _tool_result_to_str(result)
                    self.messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': result_str})
                    add_chat(self.db, 'tool', result_str, {'tool_call_id': tc['id']})
                    yield _tool_result_event(tool_name, result, 0.0)
                    continue  # 跳过本次执行，直接处理下一个 tool_call

                # 修复:类型校验，防止 LLM 输出字符串/列表等非对象参数
                # （如 arguments='" &&"' 解析成 str，**str 会抛 TypeError）
                if not isinstance(tool_args, dict):
                    result = ToolResult(
                        error=(f"[参数类型错误] 工具 {tool_name} 的 arguments 解析结果为 "
                               f"{type(tool_args).__name__}，必须是 JSON 对象（dict）。"
                               f"收到: {tool_args!r}")
                    )
                    result_str = _tool_result_to_str(result)
                    self.messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': result_str})
                    add_chat(self.db, 'tool', result_str, {'tool_call_id': tc['id']})
                    yield _tool_result_event(tool_name, result, 0.0)
                    continue

                # ── 修复:必需键校验（P0 方案 B）──
                # 类型校验只保证是 dict，但 arguments 可能是 {} 或缺关键字段
                # （如 {"cmd": "..."} 少了 command），** 解包仍会报
                # "missing positional argument"。按工具 schema 的 required
                # 字段精确校验，给 LLM 明确的缺失清单。
                _tool_def = self._find_tool(tool_name)
                _required = (_tool_def.parameters.get("required") or []) if _tool_def else []
                _missing = [k for k in _required if k not in tool_args]
                if _missing:
                    result = ToolResult(
                        error=(f"[参数缺失] 工具 {tool_name} 缺少必需参数: {_missing}。"
                               f"收到 arguments: {tool_args_str!r}\n"
                               f"请重新生成，必须包含全部必需键: {_required}")
                    )
                    result_str = _tool_result_to_str(result)
                    self.messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': result_str})
                    add_chat(self.db, 'tool', result_str, {'tool_call_id': tc['id']})
                    self._log_error("tool_args_missing", self.session,
                                    f"tool={tool_name} missing={_missing} args={tool_args_str!r}")
                    yield _tool_result_event(tool_name, result, 0.0)
                    continue

                yield {"type": "tool_call", "name": tool_name, "args": tool_args, "index": i, "total": len(tool_calls), "mode": self.mode}
                tool = self._find_tool(tool_name)
                t_elapsed = 0.0   # unknown tool 分支无执行耗时
                if not tool:
                    result = ToolResult(error=f'Unknown tool: {tool_name}')
                    self._log_error("tool_unknown", self.session, f"tool={tool_name}")
                else:
                    t_start = time.time()
                    self.phase = "idle"   # tool 执行期间不视为 LLM 处理（tool 并入 idle）
                    self.in_tool = True    # 标记有 tool 正在执行
                    self._in_tool_exec = True
                    try:
                        # ── 方案2: 工具执行线程化 + 进度事件实时 yield ──
                        # execute 移入后台线程（exec_pythonrt/exec_agent 内部读线程把
                        # worker stdout 逐行 put 进 _tool_progress_q）；主循环轮询队列
                        # yield tool_progress，使 web.py 前端实时看到工具执行进度。
                        # 中断语义（与旧 KeyboardInterrupt 分支等价）：Ctrl+C →
                        # _check_esc 置 _interrupt_event → tools.py 轮询 kill worker →
                        # execute 返回 exit_code=130 的 ToolResult → 本循环正常收尾。
                        import queue as _queue
                        self._tool_progress_q = _queue.Queue()
                        _holder: dict = {}

                        def _exec_tool():
                            try:
                                logger.debug(f"[流式] 工具执行: {tool_name}")
                                _holder["result"] = tool.execute(**tool_args)
                            except KeyboardInterrupt:
                                _holder["result"] = ToolResult(error="tool is killed by user")
                            except Exception as e:
                                self._log_error("tool_exec", self.session,
                                                f"tool={tool_name} args={tool_args!r} error={e}")
                                _holder["result"] = ToolResult(error=str(e))

                        _exec_thread = threading.Thread(target=_exec_tool, daemon=True)
                        _exec_thread.start()
                        _prog_idx = 0
                        while _exec_thread.is_alive():
                            self._check_esc()   # Ctrl+C → 置中断事件（tools.py kill worker）
                            try:
                                while True:
                                    _s, _l = self._tool_progress_q.get_nowait()
                                    _prog_idx += 1
                                    yield {"type": "tool_progress", "name": tool_name,
                                           "line": _l, "idx": _prog_idx, "stream": _s}
                            except _queue.Empty:
                                pass
                            time.sleep(0.05)
                        # 收尾 drain 残留进度行（线程结束后队列可能还有积压）
                        try:
                            while True:
                                _s, _l = self._tool_progress_q.get_nowait()
                                _prog_idx += 1
                                yield {"type": "tool_progress", "name": tool_name,
                                       "line": _l, "idx": _prog_idx, "stream": _s}
                        except _queue.Empty:
                            pass
                        result = _holder.get("result", ToolResult(error="tool execute 无返回"))
                    finally:
                        self._tool_progress_q = None   # 清理：tools.py getattr 后不再回调
                        self._in_tool_exec = False
                        self.in_tool = False   # tool 执行结束
                    t_elapsed = time.time() - t_start

                result_str = _tool_result_to_str(result)
                yield _tool_result_event(tool_name, result, t_elapsed)

                self.messages.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': result_str,
                })
                add_chat(self.db, 'tool', result_str, {'tool_call_id': tc['id']})
                # ── summary 工具成功 → 终止本轮（stop_turn 通用收尾标记）──
                if getattr(result, "stop_turn", False):
                    stop_turn = True
                    stop_turn_tool = tool_name
                    # 剩余 tool_calls 未执行 → 补 tool 响应（协议成对，与 interrupt 分支同构；
                    # 避免 assistant 声明 N 个 tool_calls 却只有部分 tool 响应 → API 400）
                    for tc_rest in tool_calls[i + 1:]:
                        _skip_note = "tool is skipped (summary ended turn)"
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc_rest["id"],
                            "content": _skip_note,
                        })
                        add_chat(self.db, "tool", _skip_note, {"tool_call_id": tc_rest["id"]})
                    break

            
            if stop_turn:
                yield {"type": "turn_end_by_tool", "name": stop_turn_tool,
                       "key": getattr(self, "_exit_note", "")}
                return

    def run_stream_compact(self):
        """压缩专用流程：独立于 run_stream 的压缩回合执行。

        为什么独立（而非给 run_stream 加参数）：
          1. 压缩不需要技能选择/工具调用/建议技能元信息——注入只会污染总结
          2. 需要专用 system prompt（system_prompt_compact.txt）约束结构化输出
          3. 历史以单条打包消息传入，压缩指令本身不写入 self.messages
        事件协议与 run_stream 一致（text/stats/error/interrupted），
        正常完成后由 run_forever 调用 _finalize_compact() 写 marker 收尾。
        """
        logger.info(f"run_stream_compact() 开始: messages={len(self.messages)}")
        # ── 观察者模式 gate: 与 run_stream 一致 ──
        if self._observing:
            logger.warning(f"观察者模式拒绝输入: session={self.session} 被其他进程占用")
            yield {"type": "blocked", "reason": "session 已被其他进程占用（观察者只读模式）"}
            return
        # ── 压缩回合中断检查（进入流式前）──
        try:
            self._check_interrupt()
        except _InterruptTurn:
            self._log_error("interrupt", self.session, "user interrupted turn")
            yield {"type": "interrupted"}
            return

        # ── 构建压缩专用 system prompt + 历史单条打包 ──
        compact_prompt = _build_compact_prompt()
        history_json = json.dumps(
            [{"role": m["role"], "content": m.get("content", "")}
             for m in self.messages],
            ensure_ascii=False, indent=2,
        )
        full_messages = [
            {"role": "system", "content": compact_prompt},
            {"role": "user", "content": (
                COMPACT_PROMPT + "\n\n--- Conversation History ---\n" + history_json
            )},
        ]

        # ── 流式压缩（无 tools，不触发技能选择）──
        self.phase = "llm"
        yield {"type": "thinking"}
        accumulated_content = ""
        first_token = True
        try:
            stream_gen = complete_stream(
                messages=full_messages,
                model=self.model,
                provider=self.provider,
                reasoning_effort=self._resolve_effort(),
                tools=None,
                interrupt_event=self._interrupt_event,
            )
            while True:
                chunk = next(stream_gen)
                if chunk["type"] == "text":
                    if first_token:
                        yield {"type": "clear_thinking"}
                        first_token = False
                    accumulated_content += chunk["delta"]
                    yield {"type": "text", "data": chunk["delta"]}
                    self._check_pause_point()
                    self._check_interrupt()
                elif chunk["type"] == "reasoning":
                    yield {"type": "thinking_content", "data": chunk["delta"]}
        except _InterruptTurn:
            try:
                stream_gen.close()
            except Exception:
                pass
            self._log_error("interrupt", self.session, "user interrupted turn")
            yield {"type": "interrupted"}
            return
        except StopIteration as e:
            full_content, extras = e.value
            accumulated_reasoning = (extras.get("reasoning_content") or "").strip()
            # ── 中断标记检测（与 run_stream B1 修复一致）──
            if self._interrupt_event.is_set() or extras.get("interrupted"):
                self._interrupt_event.clear()
                self._log_error("interrupt", self.session, "user interrupted turn")
                yield {"type": "interrupted"}
                return
            if not accumulated_content and full_content:
                accumulated_content = full_content
        except Exception as e:
            self._log_error("llm_stream", self.session, f"error={e}")
            yield {"type": "error", "data": f"LLM call failed: {e}"}
            return

        summary = _sanitize(accumulated_content).strip()
        if not summary:
            logger.warning("压缩回合未产生总结内容")
            yield {"type": "error", "data": "压缩失败：LLM 未生成总结内容"}
            return

        # ── 与 run_stream 一致：总结作为 assistant 消息写入历史 + DB ──
        # 供 run_forever 在 turn_ok 时调用 _finalize_compact() 取用收尾
        if accumulated_reasoning:
            add_chat(self.db, "thinking", accumulated_reasoning)
        self.messages.append({"role": "assistant", "content": summary})
        add_chat(self.db, "assistant", summary)

        # ── usage 统计（对齐 run_stream）──
        usage = extras.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self._last_model = extras.get("model", self.model or "")
        self._turn_count += 1
        self._turn_usage_history.append({
            "prompt": pt, "completion": ct,
            "turn": self._turn_count, "model": self._last_model,
            "tool_calls": 0,
        })

        # ── 压缩轮也是 LLM 调用，同步持久化 token 累计值 ──
        self._persist_token_state()
        yield {"type": "stats", "prompt_tokens": pt, "completion_tokens": ct, "model": self._last_model}
        return
