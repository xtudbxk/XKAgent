"""codes/commands.py — 统一命令注册表与调度（REPL / Web 共用）。

背景
----
repl.py 的 slash 命令分支（原 ~490 行）与 web.py 的 _execute_command
（原 ~355 行）是两份平行实现，命令逻辑漂移风险高。本模块把命令收敛为
「注册表 + dispatch」，两端只保留 UI 特有能力的注入点（CommandContext）。

命令分类
--------
- 纯共享命令：仅依赖 AgentManager / codes.* 共享模块，返回展示文本。
- 注入依赖命令：通过 CommandContext 注入 UI 能力：
    * session 切换/多选/确认（repl 有 stdin 交互，web 无）
    * mount CONFIRM_REQUIRED 确认（repl 自动重发 --force，web 提示手动）
    * restart 历史保存（repl 的 readline history）
    * exit 退出钩子（repl break 主循环 / web 关闭服务器）
- 特殊命令（不进本注册表）：
    * /compact —— 两端各自在消息通路拦截（改写为 COMPACT_MARKER + COMPACT_PROMPT）
    * !xxx —— 两端各自实现 bash 执行（落库语义不同）

用法
----
from codes.commands import dispatch, CommandContext
text = dispatch(mgr, "/model test", ctx=CommandContext(...))
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from codes.agent import MODE_CYCLE
from codes._log import logger
from codes import config
from codes.history import (
    add_session,
    delete_session,
    fork_session,
    get_command_history,
    get_conn,
    get_search_state,
    list_sessions,
    rename_session,
    session_exists,
    sync_session,
)

# ────────────────────────────────────────────────────────────────
#  注册表
# ────────────────────────────────────────────────────────────────

COMMANDS: dict[str, dict] = {}


def cmd(*names: str, help_text: str = "", ui: tuple = ("repl", "web")):
    """命令注册装饰器。

    参数:
        names: 命令名（第一个为主名，其余为别名，如 skills/showskills）
        help_text: 帮助文本（/help 生成时使用）
        ui: 该命令可用的 UI 端（默认两端都可用）
    """
    def decorator(func):
        entry = {"handler": func, "help": help_text, "ui": ui, "names": names}
        for n in names:
            COMMANDS[n] = entry
        return func
    return decorator


# ────────────────────────────────────────────────────────────────
#  上下文（UI 能力注入点）
# ────────────────────────────────────────────────────────────────

@dataclass
class CommandContext:
    """UI 特有能力的注入点；缺省 None = 该 UI 无此能力。

    设计考虑: 不引入 Protocol/接口类，用可空回调保持最小侵入——
    repl 注入 stdin 交互能力，web 注入全局会话同步能力。
    """
    confirm_handler: Callable[[str], bool] | None = None
    """(prompt) -> bool 交互确认。repl=_ask 版；web=None（不确认直接执行）。"""
    save_history: Callable[[], None] | None = None
    """restart 前保存 UI 历史。repl=reader.save_history；web=None。"""
    exit_hook: Callable[[], str] | None = None
    """exit 命令触发后的 UI 清理，返回展示文本。web=close_web_server。"""
    exit_requested: bool = False
    """exit 命令触发标记。repl 检查后 break 主循环。"""
    get_session: Callable[[], str | None] | None = None
    """当前 session 名。repl=manager.focus；web=_current_session。"""
    switch_session_hook: Callable[[str], None] | None = None
    """session 切换后的 UI 同步。web=更新 _current_session；repl=刷新 prompt。"""
    pick_session: Callable[[list, str, str], str | None] | None = None
    """(matches, current, name) -> 选中项 | None。repl=stdin 多选；web=None。"""
    format_session_line: Callable[[object, str], str] | None = None
    """(session_info, mark) -> 行文本。repl=带 phase/lock tag；web=None。"""
    format_other_session: Callable[[str], str] | None = None
    """(session_name) -> 未运行会话行文本。repl=带 lock tag；web=None。"""
    help_extra: str = ""
    """UI 特有的帮助补充（repl 的 shortcuts/multiline 说明）。"""


# ────────────────────────────────────────────────────────────────
#  核心工具
# ────────────────────────────────────────────────────────────────

def run_cmd(mgr, cmd_name: str, args: dict | None = None, timeout: float = 5.0) -> dict | None:
    """发送控制命令并等待 _cmd_result（统一 repl._mount_wait / web._run_cmd_wait）。

    设计考虑: AgentManager.send_command 只保证入队成功（bool），执行结果
    必须靠轮询 _cmd_result 获取；ok=False 表示观察者只读模式拒绝执行（T3）。
    """
    result = mgr.send_command_wait(cmd_name, args, timeout=timeout)
    return result or {"ok": False, "error": f"命令超时或 agent 未运行: {cmd_name}"}


def _set_mode(mgr, new_mode: str) -> str:
    """切换模式：等待 agent 真实结果后再更新缓存。"""
    result = mgr.send_command_wait("set_mode", {"mode": new_mode}, timeout=2.0)
    if result and result.get("ok", True) is not False:
        mgr._focus_mode = result.get("data") or new_mode
        return ""
    info = mgr.get_focus_info(timeout=2.0)
    if info:
        mgr._focus_mode = info.get("mode", mgr._focus_mode)
    return f"⚠️ 模式切换失败：当前仍为 [{getattr(mgr, '_focus_mode', 'plan')}] mode"


def _cur_session(mgr, ctx: CommandContext | None) -> str | None:
    """解析当前 session 名（cmds 等命令需要）。"""
    if ctx and ctx.get_session:
        return ctx.get_session()
    return mgr.focus


def _switch_session(mgr, name: str, ctx: CommandContext | None) -> None:
    """聚焦 + resume + 同步缓存（对齐侧边栏 switch / focus_session）。"""
    mgr.focus_session(name)
    if ctx and ctx.switch_session_hook:
        ctx.switch_session_hook(name)


def _foreign_lock_blocks(session: str) -> str | None:
    """若 session 被他进程持锁则返回错误文案（变异操作 gate）。"""
    if not session:
        return None
    from codes.lock import is_locked, is_same_process
    locked, meta = is_locked(session)
    if locked and isinstance(meta, dict) and not is_same_process(meta):
        holder = meta.get("holder") or meta.get("holder_name") or "unknown"
        return f"⚠️ session '{session}' 被其他进程占用 ({holder})，只读模式不可修改"
    return None


# ────────────────────────────────────────────────────────────────
#  命令实现
# ────────────────────────────────────────────────────────────────

@cmd("help", help_text="/help — 帮助")
def cmd_help(mgr, arg: str, ctx: CommandContext | None) -> str:
    """从 COMMANDS 注册表生成帮助，避免与实现漂移。"""
    seen: set[int] = set()
    lines: list[str] = []
    for entry in COMMANDS.values():
        key = id(entry)
        if key in seen:
            continue
        seen.add(key)
        text = (entry.get("help") or "").strip()
        if text:
            lines.append(text)
    lines.append("/compact — 压缩会话历史（走消息通路，不经命令表）")
    lines.append("!<command> — 执行 bash")
    if ctx and ctx.help_extra:
        lines.append("")
        lines.append(ctx.help_extra)
    return "\n".join(lines)


@cmd("logfile", help_text="/logfile — 查看本次运行的日志文件尾部")
def cmd_logfile(mgr, arg: str, ctx: CommandContext | None) -> str:
    """查看本次进程对应的日志文件（尾部 60 行）。"""
    from codes._log import get_log_file, tail_log_file
    session = _cur_session(mgr, ctx)
    log_path = get_log_file(session)
    tail = tail_log_file(60, session)
    return f"📄 日志文件: {log_path}\n\n{tail}"


@cmd("cmds", help_text="/cmds [n] — 查看最近 n 条命令历史")
def cmd_cmds(mgr, arg: str, ctx: CommandContext | None) -> str:
    """查看最近 n 条命令历史（默认 50）。"""
    try:
        n = int(arg) if arg and arg.isdigit() else 50
        session = _cur_session(mgr, ctx)
        if not session:
            return "ℹ️ No active session."
        lines = []
        for rec in get_command_history(get_conn(session), limit=n):
            tag = "!" if rec["kind"] == "bang" else "/"
            cmd_text = rec["cmd"]
            if not cmd_text.startswith(tag):
                cmd_text = tag + cmd_text
            ec = f" (exit={rec['exit_code']})" if rec.get("exit_code") is not None else ""
            lines.append(f"#{rec['id']} [{rec['created_at']}] {cmd_text}{ec}")
            res = rec.get("result")
            if res:
                preview = str(res).replace("\n", " ")[:300]
                lines.append(f"    → {preview}{'…' if len(str(res)) > 300 else ''}")
        return "\n".join(lines) or "(no command history yet)"
    except Exception as e:
        return f"❌ /cmds error: {e}"


@cmd("clear", help_text="/clear — 清空会话")
def cmd_clear(mgr, arg: str, ctx: CommandContext | None) -> str:
    """清空会话（T3: 观察者模式下 agent 拒绝执行时如实报告）。"""
    result = run_cmd(mgr, "clear", timeout=5.0)
    if result and result.get("ok") is False:
        return f"⚠️ {result.get('error', '观察者只读模式：session 已被其他进程占用')}"
    return "✅ Session cleared.（断点标记：历史消息已保留在存储中，LLM 上下文已重置）"


@cmd("drop", help_text="/drop — 丢弃历史（清内存，保留DB记录）")
def cmd_drop(mgr, arg: str, ctx: CommandContext | None) -> str:
    """丢弃历史（T3: 观察者模式下 agent 拒绝执行时如实报告）。"""
    result = run_cmd(mgr, "drop", timeout=5.0)
    if result and result.get("ok") is False:
        return f"⚠️ {result.get('error', '观察者只读模式：session 已被其他进程占用')}"
    return "✅ History dropped."


@cmd("autocompactlimit",
     help_text="/autocompactlimit [-1|N] — 查看/设置自动压缩阈值（-1=禁用；默认 600000（2026-09-11 起默认开启）；N=上下文超过 N tokens 时先自动修剪/压缩）")
def cmd_autocompactlimit(mgr, arg: str, ctx: CommandContext | None) -> str:
    """查看/设置 autocompactlimit（统一 repl/web；经 agent 执行并持久化 agent_state）。"""
    arg = (arg or "").strip()
    if not arg:
        # 无参：显示当前值（经 get_info 读取 agent 真实状态）
        result = run_cmd(mgr, "get_info", timeout=2.0)
        info = (result or {}).get("data") if isinstance(result, dict) else None
        cur = info.get("autocompactlimit") if isinstance(info, dict) else None
        if cur is None:
            return "⚠️ 无法读取当前值（agent 未运行或版本过旧）"
        if cur == -1:
            return "autocompactlimit = -1（禁用自动压缩）"
        return (f"autocompactlimit = {cur}\n"
                f"  处理用户消息前，上下文估算 > {cur} tokens 时先自动压缩再处理")
    try:
        val = int(arg, 10)
    except ValueError:
        return f"⚠️ 无效值 {arg!r}：仅接受 -1（禁用）或正整数"
    if val != -1 and val <= 0:
        return f"⚠️ 无效值 {val}：仅接受 -1（禁用）或正整数"
    warn = "（⚠️ 阈值偏小：压缩总结本身约 2-3k tokens，可能频繁压缩）" if val < 4000 else ""
    result = run_cmd(mgr, "set_autocompactlimit", {"limit": val}, timeout=5.0)
    if isinstance(result, dict) and result.get("ok") is False:
        return f"⚠️ {result.get('error', '设置失败')}"
    return ("✅ autocompactlimit = %d %s" % (val, warn)).rstrip()


@cmd("session", help_text="/session — 会话信息与子命令（add/fork/remove/stop/rename/sync/title/切换）")
def cmd_session(mgr, arg: str, ctx: CommandContext | None) -> str:
    """会话管理子命令（统一 repl/web；交互确认与多选通过 ctx 注入）。"""
    if not arg:
        # 裸 /session：会话信息 + token 统计
        # B4 修复（2026-08-22）：直接读 session db（对齐 web _fetch_token_stats），
        # 避免 agent 忙碌时命令排队 3s 超时导致 "stats unavailable"。
        name = _cur_session(mgr, ctx) or (mgr.focus or "")
        if not name:
            return "No active session."
        try:
            from codes.history import get_token_state, get_conn
            _ts = get_token_state(name)
            _p = _ts.get("prompt_tokens", 0) or 0
            _c = _ts.get("completion_tokens", 0) or 0
            _r = _ts.get("reasoning_tokens", 0) or 0
            _t = _ts.get("turn_count", 0) or 0
            _m = _ts.get("model") or ""
            try:
                _conn = get_conn(name)
                _msgc = _conn.count_visible()
                _conn.close()
            except Exception:
                _msgc = 0
            from codes.session_registry import title_of as _title_of
            _title = _title_of(name)
            _lines = [
                f"  Session: {name}",
                f"  Title:   {_title or '(未设置 → 显示 name)'}",
                f"  Model:   {_m or '(default)'}",
                f"  Messages: {_msgc}",
                f"  Turns:    {_t}",
                "",
                f"  Token Usage",
                f"     Prompt:     {_p:>10,}",
                f"     Completion: {_c:>10,}",
                f"     Reasoning:  {_r:>10,}",
                f"     Total:      {_p + _c:>10,}",
            ]
            return "\n".join(_lines)
        except Exception as e:
            return f"Session: {name} (stats unavailable: {e})"

    try:
        sub_parts = shlex.split(arg)
    except ValueError as e:
        return f"⚠️ Invalid arguments: {e}"
    sub_cmd = sub_parts[0]
    sub_args = sub_parts[1:]
    current_focus = mgr.focus or ""

    if sub_cmd == "add":
        no_switch = "--no-switch" in sub_args
        workdir = None
        title = None          # 可选展示标题（支持中文），见 --title
        positional = []
        i = 0
        while i < len(sub_args):
            token = sub_args[i]
            if token == "--no-switch":
                i += 1
                continue
            if token == "--workdir":
                if i + 1 >= len(sub_args):
                    return "⚠️ --workdir requires a path"
                workdir = sub_args[i + 1]
                i += 2
                continue
            if token.startswith("--workdir="):
                workdir = token.split("=", 1)[1]
                i += 1
                continue
            if token == "--title":
                if i + 1 >= len(sub_args):
                    return "⚠️ --title requires a value"
                title = sub_args[i + 1]
                i += 2
                continue
            if token.startswith("--title="):
                title = token.split("=", 1)[1]
                i += 1
                continue
            if token.startswith("--"):
                return f"⚠️ Unknown option: {token}"
            positional.append(token)
            i += 1
        if len(positional) != 1:
            return "⚠️ Usage: /session add <name> [--workdir <path>] [--no-switch] [--title <text>]"
        name = positional[0]
        ok, msg = add_session(name, workdir=workdir, title=title)
        if not ok:
            return f"❌ {msg}"
        if no_switch:
            return f"✅ {msg}"
        # 后台启动 agent（wait_ready=False）：命令立即返回，避免 bashkit 初始化阻塞
        _switch_session(mgr, name, ctx)
        return f"✅ {msg}  Switched to: {name}"

    elif sub_cmd == "fork":
        if not sub_args:
            return "⚠️ Usage: /session fork <name>"
        src = current_focus
        if not src:
            return "⚠️ No active session to fork from. Switch to a session first."
        dst = sub_args[0]
        block = _foreign_lock_blocks(src)
        if block:
            return block
        ok, msg = fork_session(src, dst)
        if ok:
            # fork 继承状态板（含软删除存档）；失败静默降级不影响 fork 本身
            try:
                from codes.session_info import copy_state
                copy_state(src, dst)
            except Exception:
                logger.warning(f"fork 状态板复制失败（静默降级）: {src} -> {dst}", exc_info=True)
        return f"{'✅' if ok else '❌'} {msg}"

    elif sub_cmd == "remove":
        if not sub_args:
            return "⚠️ Usage: /session remove <name> [--force]"
        force = "--force" in sub_args
        name = next(a for a in sub_args if not a.startswith("--"))
        if not session_exists(name):
            return f"❌ Session '{name}' does not exist."
        if name == current_focus:
            return "⚠️ Cannot remove the currently active session."
        block = _foreign_lock_blocks(name)
        if block:
            return block
        if not force and ctx and ctx.confirm_handler and not ctx.confirm_handler(
                f"Are you sure you want to remove '{name}'? (y/N): "):
            return "ℹ️ Cancelled."
        if not force and ctx and ctx.confirm_handler is None:
            return f"⚠️ 请确认删除: /session remove {name} --force"
        mgr.stop_agent(name)
        time.sleep(0.5)
        ok, msg = delete_session(name)
        return f"{'✅' if ok else '❌'} {msg}"

    elif sub_cmd == "stop":
        if not sub_args:
            return "⚠️ Usage: /session stop <name>"
        name = sub_args[0]
        if not session_exists(name):
            return f"❌ Session '{name}' does not exist."
        mgr.stop_agent(name)
        return f"✅ Stopped agent for session: {name}"

    elif sub_cmd == "title":
        # /session title [--session <name>] <text...>   清除用 --clear
        # title 仅展示（支持中文/空格/emoji），不改变 session name 与磁盘路径
        target = current_focus
        clear = False
        text_parts = []
        i = 0
        while i < len(sub_args):
            tok = sub_args[i]
            if tok in ("--session", "-s"):
                if i + 1 >= len(sub_args):
                    return "⚠️ --session requires a name"
                target = sub_args[i + 1]
                i += 2
                continue
            if tok.startswith("--session="):
                target = tok.split("=", 1)[1]
                i += 1
                continue
            if tok == "--clear":
                clear = True
                i += 1
                continue
            if tok.startswith("--"):
                return f"⚠️ Unknown option: {tok}"
            text_parts.append(tok)
            i += 1
        if not target:
            return "⚠️ No active session. Usage: /session title [--session <name>] <text>"
        from codes.session_registry import get as _reg_get
        from codes.session_registry import set_title as _reg_set_title
        if _reg_get(target) is None:
            return f"❌ Session '{target}' not found"
        text = "" if clear else " ".join(text_parts).strip()
        if not clear and not text:
            return ("⚠️ Usage: /session title [--session <name>] <text>；"
                    "清除标题用 /session title --clear [--session <name>]")
        try:
            _ctx_title = _reg_set_title(target, text)
        except (ValueError, KeyError) as e:
            return f"❌ {e}"
        if _ctx_title.title:
            return f"✅ Title set for '{target}': {_ctx_title.title}"
        return f"✅ Title cleared for '{target}'（展示回退 name: {target}）"

    elif sub_cmd == "rename":
        if not sub_args:
            return "⚠️ Usage: /session rename <name> [--force]"
        force = "--force" in sub_args
        new = next(a for a in sub_args if not a.startswith("--"))
        old = current_focus
        if not force and ctx and ctx.confirm_handler and not ctx.confirm_handler(
                f"Rename current session -> '{new}'? (y/N): "):
            return "ℹ️ Cancelled."
        if not force and ctx and ctx.confirm_handler is None:
            return f"⚠️ 请确认重命名: /session rename {new} --force"
        ok, msg = rename_session(old, new)
        if ok:
            if old == current_focus:
                mgr.stop_agent(old)
                _switch_session(mgr, new, ctx)
                return f"✅ {msg}  (active session)"
            return f"✅ {msg}"
        return f"❌ {msg}"

    elif sub_cmd == "sync":
        name = sub_args[0] if sub_args else current_focus
        ok, msg = sync_session(name)
        return f"{'✅' if ok else '❌'} {msg}"

    else:
        # 切换到会话（精确 → 模糊单匹配 → 多选 → 无结果）
        name = sub_cmd
        if session_exists(name):
            if name == current_focus:
                return f"ℹ️ Already in session: {name}"
            _switch_session(mgr, name, ctx)
            return f"✅ Switched to session: {name}"
        matches = [s for s in list_sessions() if name.lower() in s.lower()]
        if len(matches) == 1:
            matched = matches[0]
            if matched == current_focus:
                return f"ℹ️ Already in session: {matched}"
            _switch_session(mgr, matched, ctx)
            return f"✅ Switched to session: {matched}"
        elif len(matches) > 1:
            if ctx and ctx.pick_session:
                selected = ctx.pick_session(matches, current_focus, name)
                if selected is None:
                    return "ℹ️ Cancelled."
                _switch_session(mgr, selected, ctx)
                return f"✅ Switched to session: {selected}"
            return f"Multiple matches: {', '.join(matches)}"
        return f"❌ Session '{name}' not found"


@cmd("sessions", help_text="/sessions — 列出所有会话")
def cmd_sessions(mgr, arg: str, ctx: CommandContext | None) -> str:
    """列出所有会话（managed + 未运行）。"""
    fmt_line = (ctx.format_session_line if ctx and ctx.format_session_line else None)
    fmt_other = (ctx.format_other_session if ctx and ctx.format_other_session else None)
    info_list = mgr.list_sessions()
    if info_list:
        current = mgr.focus
        lines = ["Sessions (managed by this repl):"]
        for si in info_list:
            mark = "▶" if si.session == current else " "
            lines.append(fmt_line(si, mark) if fmt_line else f"  {mark} {si.session}  [{si.status}]")
        managed = {si.session for si in info_list}
        unmanaged = [s for s in list_sessions() if s not in managed]
        if unmanaged:
            lines.append("\nOther sessions (not running):")
            for s in unmanaged:
                lines.append(fmt_other(s) if fmt_other else f"    ○ {s}")
        return "\n".join(lines)
    all_sessions = list_sessions()
    if all_sessions:
        if fmt_other:
            return "Sessions (not running):\n" + "\n".join(fmt_other(s) for s in all_sessions)
        return "Sessions (not running):\n" + "\n".join(f"    ○ {s}" for s in all_sessions)
    return "No sessions."


@cmd("model", help_text="/model [name] — 显示/切换模型 | /model test — 全量测速")
def cmd_model(mgr, arg: str, ctx: CommandContext | None) -> str:
    """模型显示/切换/测速（统一 repl/web 逐行一致的逻辑）。"""
    # /model test [target] — 测试联通性；无 target 时全量测速（只读探测，不切换模型）
    if arg and (arg == "test" or arg.startswith("test ")):
        from codes import llm as _llm
        from codes import provider_config as _pc
        target = arg[5:].strip() if arg.startswith("test ") else ""
        if target:
            info = mgr.get_focus_info(timeout=2.0)
            cur_prov = info.get("provider") if info else None
            try:
                model_part, _ = _pc.split_model_effort(target)
                prov, resolved = _pc.resolve_model_target(model_part, cur_prov)
            except ValueError as e:
                return f"⚠️ {e}"
            result = _llm.test_connectivity(provider=prov, model=resolved, timeout=15)
            return _llm.format_test_report(result)
        results, elapsed = _llm.test_all_connectivity(timeout=15)
        return _llm.format_all_test_report(results, elapsed)

    from codes import provider_config
    if not arg:
        # 无参：显示当前 provider + 实际生效模型 + model 列表
        info = mgr.get_focus_info(timeout=2.0)
        if info:
            prov = info.get("provider", "?") or "(unset)"
            cur_model = info.get("model") or ""
            try:
                eff = cur_model or provider_config.get_provider(prov).get("default_model", "(未配置)")
            except Exception:
                eff = cur_model or "(未知)"
            effort = info.get("reasoning_effort") or ""
            base = f"Provider: {prov}\nModel: {eff}\n"
            if effort:
                base += f"Reasoning Effort: {effort}\n"
        else:
            base = "Model: (tracked in agent thread)\n"
        return base + "--- available models ---\n" + provider_config.format_providers()

    if arg.startswith("@"):
        # 切换 provider（@name 语法）
        pname = arg[1:]
        result = mgr.send_command_wait("set_provider", {"provider": pname}, timeout=2.0)
        if not result or result.get("ok") is False:
            err = (result or {}).get("error", "命令超时或 agent 未运行")
            return f"⚠️ Provider 切换失败: {err}"
        return f"✅ Provider switched to: {pname}"

    # 模型解析：返回 (provider, model)，联动 set_provider + set_model
    # 支持 模型名:effort 语法（effort 为会话级临时覆盖，不持久化）
    info = mgr.get_focus_info(timeout=2.0)
    cur_prov = info.get("provider") if info else None
    try:
        model_part, effort_arg = provider_config.split_model_effort(arg)
        prov, resolved = provider_config.resolve_model_target(model_part, cur_prov)
    except ValueError as e:
        return f"⚠️ {e}"
    if prov and prov != cur_prov:
        result = mgr.send_command_wait("set_provider", {"provider": prov}, timeout=2.0)
        if not result or result.get("ok") is False:
            err = (result or {}).get("error", "命令超时或 agent 未运行")
            return f"⚠️ Provider 切换失败: {err}"
    send_kwargs = {"model": resolved}
    if effort_arg:
        send_kwargs["reasoning_effort"] = effort_arg
    result = mgr.send_command_wait("set_model", send_kwargs, timeout=2.0)
    if not result or result.get("ok") is False:
        err = (result or {}).get("error", "命令超时或 agent 未运行")
        return f"⚠️ Model 切换失败: {err}"
    msg = f"✅ Model set to: {resolved}"
    if effort_arg:
        msg += f" (reasoning_effort={effort_arg} 临时覆盖)"
    return msg


@cmd("skills", "showskills", help_text="/skills /showskills — 列出技能")
def cmd_skills(mgr, arg: str, ctx: CommandContext | None) -> str:
    """列出可用技能。"""
    from codes.skill import SkillLoader
    skills = SkillLoader.list_skills()
    if skills:
        return "Available skills:\n" + "\n".join(f"  ○ {n}" for n in skills)
    return "No skills available."


@cmd("updateembedding", "updateskillembedding", help_text="/updateembedding — 重建向量索引（skills/docs/historys/logs，codes 默认关闭）")
def cmd_updateembedding(mgr, arg: str, ctx: CommandContext | None) -> str:
    """重建当前 session workdir 的全部或指定范围向量索引。"""
    from codes.search import update_embeddings
    scope = (arg or "").strip() or None
    res = update_embeddings(verbose=True, session=_cur_session(mgr, ctx), scopes=scope)
    total = sum(res.values())
    msg = f"💡 Updated embeddings: {total} keys across {len(res)} scopes."
    if total > 0:
        msg += '\n💡 Use: searchskill("query", top_k=5)'
    return msg


@cmd("info", help_text="/info [add|deny|remove|clear|list] [--global] <path> [scope] — 管理搜索范围（per-session 默认，--global 全局）")
def cmd_info(mgr, arg: str, ctx: CommandContext | None) -> str:
    """搜索范围管理：add 添加路径/目录进搜索范围，deny 禁止（前缀匹配），
    remove/clear 移除配置。per-session 存 session db；--global 写
    .xkagent/search_ranges.txt（全局生效，重启不丢）。无参数 → 查看生效配置。
    """
    arg = (arg or "").strip()
    toks = shlex.split(arg) if arg else []
    sub = toks[0].lower() if toks else "list"
    rest = toks[1:]
    is_global = "--global" in rest
    rest = [t for t in rest if t != "--global"]

    if sub == "list":
        return _info_list(mgr, ctx)
    if sub == "clear":
        return _info_wait(mgr, f"info_{sub}", {"global": is_global}, ctx=ctx)
    if sub in ("add", "deny", "remove"):
        if not rest:
            return f"⚠️ Usage: /info {sub} [--global] <path>"
        path = rest[0]
        if sub == "add":
            scope = rest[1] if len(rest) > 1 else "extra"
            return _info_wait(mgr, "info_add", {"path": path, "scope": scope, "global": is_global}, ctx=ctx)
        return _info_wait(mgr, f"info_{sub}", {"path": path, "global": is_global}, ctx=ctx)
    return f"⚠️ 未知子命令: {sub}（支持 add/deny/remove/clear/list）"


def _info_list(mgr, ctx: CommandContext | None) -> str:
    """展示全局 + 当前 session 合并生效的搜索范围配置。"""
    from codes.search import _effective_search_config, load_search_config
    session = ctx.get_session() if (ctx and ctx.get_session) else None
    cfg = load_search_config()
    lines = ["📁 搜索范围配置"]
    lines.append(f"  [全局] {config.DATA_DIR_NAME}/search_ranges.txt")
    if cfg["adds"]:
        lines.append(f"    add ×{len(cfg['adds'])}:")
        for it in cfg["adds"]:
            lines.append(f"      + {it['path']} (scope={it['scope']})")
    else:
        lines.append("    add: (无)")
    if cfg["denies"]:
        lines.append(f"    deny ×{len(cfg['denies'])}:")
        for d in cfg["denies"]:
            lines.append(f"      - {d}")
    else:
        lines.append("    deny: (无)")
    if session:
        sess_items = get_search_state(session)
        lines.append(f"  [session] {session}")
        if sess_items:
            for it in sess_items:
                mark = "+" if it["action"] == "add" else "-"
                extra = f" (scope={it['scope']})" if it.get("scope") else ""
                lines.append(f"      {mark} {it['path']}{extra}")
        else:
            lines.append("    (无配置)")
        eff = _effective_search_config(session)
        lines.append(f"  [生效合并] adds={len(eff['adds'])} denies={len(eff['denies'])}")
    lines.append("  💡 用法: /info add|deny|remove|clear [--global] <path> [scope]")
    return "\n".join(lines)


def _info_wait(mgr, cmd_name: str, args: dict, *, ctx: CommandContext | None, timeout: float = 5.0) -> str:
    """发送 info 系列命令并等待 _cmd_result（复用 _mount_wait 轮询）。"""
    return _mount_wait(mgr, cmd_name, args, ctx=ctx, timeout=timeout)


@cmd("image", help_text="/image add|clear|list <path> — 附加图片给多模态 LLM")
def cmd_image(mgr, arg: str, ctx: CommandContext | None) -> str:
    """图像附件管理：add 追加图片（可多张累积），clear 清空，list 查看。

    per-session 存 session db（image_state 表），发送时由 agent 注入最新 user 消息。
    路径存绝对路径，读取时再编码 base64（省 DB 空间）。
    """
    from codes.history import get_images, add_image, clear_images
    session = ctx.get_session() if (ctx and ctx.get_session) else None
    if not session:
        return "⚠️ 无法确定当前 session。"
    arg = (arg or "").strip()
    toks = arg.split(None, 1)
    sub = toks[0].lower() if toks else "list"
    rest = toks[1].strip() if len(toks) > 1 else ""

    if sub == "list":
        imgs = get_images(session)
        if not imgs:
            return "🖼️ 图片附件: (无)"
        lines = [f"🖼️ 图片附件 ×{len(imgs)}:"]
        for i, p in enumerate(imgs, 1):
            lines.append(f"  {i}. {p}")
        return "\n".join(lines)
    if sub == "clear":
        n = clear_images(session)
        return f"✅ 已清空 {n} 张图片附件。"
    if sub == "add":
        if not rest:
            return "⚠️ Usage: /image add <path>"
        path = os.path.expanduser(rest)
        if not os.path.isabs(path):
            # 相对路径基于 session workdir 解析（对齐 web._resolve_workdir 语义），
            # 避免进程 cwd 与 session workdir 不一致时附件落错位置
            try:
                from codes.session_registry import get as _get_ctx
                _ctx = _get_ctx(session)
                _base = str(_ctx.workdir) if _ctx is not None else os.getcwd()
            except Exception:
                _base = os.getcwd()
            path = os.path.join(_base, path)
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return f"❌ 文件不存在或不可读: {rest}"
        size = os.path.getsize(path)
        if size > 10 * 1024 * 1024:
            return f"❌ 图片过大（{size // 1024}KB > 10MB），请压缩后重试。"
        ok = add_image(session, path)
        n = len(get_images(session))
        if ok:
            return f"✅ 已附加图片: {path}（共 {n} 张）"
        return f"ℹ️ 图片已存在（当前共 {n} 张）: {path}"
    return "⚠️ Usage: /image add <path> | /image clear | /image list"


@cmd("validate", help_text="/validate [names] — 校验技能")
def cmd_validate(mgr, arg: str, ctx: CommandContext | None) -> str:
    """校验技能（全部或指定名称）。"""
    from codes.skill import SkillLoader
    names = arg.split() if arg else SkillLoader.list_skills()
    if not names:
        return "No skills to validate."
    lines = []
    for n in names:
        errs = SkillLoader.validate(n)
        if errs:
            lines.append(f"❌ {n}")
            for e in errs:
                lines.append(f"   - {e}")
        else:
            lines.append(f"✅ {n}")
    return "\n".join(lines)


@cmd("skill", help_text="/skill <name> — 加载技能")
def cmd_skill(mgr, arg: str, ctx: CommandContext | None) -> str:
    """加载指定技能（send + 轮询 _cmd_result）。"""
    skill_name = arg.split(None, 1)[0] if arg else ""
    if not skill_name:
        return "⚠️ Usage: /skill <name>"
    result = run_cmd(mgr, "load_skill", {"name": skill_name}, timeout=3.0)
    if result and result.get("ok"):
        return f"Skill loaded: {skill_name}"
    return f"Skill not found: {skill_name}"


@cmd("plan", "build", "build-unsafe", help_text="/plan /build /build-unsafe — 切换模式")
def cmd_set_mode(mgr, arg: str, ctx: CommandContext | None) -> str:
    """直接切换指定模式（命令名即模式名）。"""
    return _set_mode(mgr, _current_cmd_name())


@cmd("mode", help_text="/mode — 循环切换 mode")
def cmd_mode(mgr, arg: str, ctx: CommandContext | None) -> str:
    """循环切换模式 plan → build → build-unsafe → plan。"""
    current_mode = getattr(mgr, "_focus_mode", "plan")
    new_mode = MODE_CYCLE.get(current_mode, "plan")
    return _set_mode(mgr, new_mode)


@cmd("mount", help_text="/mount <path> [ro|rw|ro/rw] [--force] — 挂载路径到 pythonrt（软边界）")
def cmd_mount(mgr, arg: str, ctx: CommandContext | None) -> str:
    """挂载路径（统一 repl._mount_command / web._web_mount_command）。"""
    return _mount_impl(mgr, arg, unmount=False, ctx=ctx)


@cmd("unmount", help_text="/unmount <path> — 卸载动态挂载")
def cmd_unmount(mgr, arg: str, ctx: CommandContext | None) -> str:
    """卸载动态挂载。"""
    return _mount_impl(mgr, arg, unmount=True, ctx=ctx)


@cmd("restart", help_text="/restart [overrides] — 重启进程（同参数或覆盖参数）")
def cmd_restart(mgr, arg: str, ctx: CommandContext | None) -> str:
    """进程重启：停止 agent → 保存历史 → 关闭 web socket → os.execv。

    --workdir 相对路径处理（2026-08-06 修复）:
      进程从不 chdir，execv 后新进程 cwd 仍是用户启动目录；相对 workdir
      必须以「当前 workdir」为基准解析为绝对路径，否则第二次相对 restart
      会解析到不存在的目录（如 workdir=xkagent 时传 .. 再传
      xkagent，第二次拼成 xkagent/xkagent）。
      目录不存在时直接报错返回，避免新进程在日志初始化前静默崩溃。
    """
    overrides = parse_restart_overrides(arg)

    # ── --workdir：基于当前 workdir 解析相对路径 + 存在性预检 ──
    if overrides.get('--workdir'):
        wd = overrides['--workdir']
        wd = os.path.expanduser(wd)
        if not os.path.isabs(wd):
            try:
                from codes import config as _cfg
                base = _cfg.get_workdir()
            except Exception:
                base = os.getcwd()
            wd = os.path.normpath(os.path.join(base, wd))
        # 无条件写回：expanduser 可能已把 ~ 解析为绝对路径，必须同步到 overrides
        overrides['--workdir'] = wd
        if not os.path.isdir(wd):
            return f"❌ workdir 不存在: {wd}（/restart 已取消）"

    new_argv = apply_argv_overrides(sys.argv, overrides)

    # ── 入口脚本绝对化：防 execv 后相对路径失效 ──
    if new_argv and not os.path.isabs(new_argv[0]):
        new_argv[0] = os.path.abspath(new_argv[0])

    save_history = ctx.save_history if ctx else None
    restart_process(mgr, save_history=save_history, argv=new_argv)
    return ""  # 进程替换后不会执行到；失败时 restart_process 已打印错误

@cmd("exit", help_text="/exit — 退出")
def cmd_exit(mgr, arg: str, ctx: CommandContext | None) -> str:
    """退出：置 exit_requested 标记 + 触发 UI 退出钩子。"""
    if ctx:
        ctx.exit_requested = True
        if ctx.exit_hook:
            return ctx.exit_hook() or ""
    return ""


# ────────────────────────────────────────────────────────────────
#  mount 实现（CONFIRM_REQUIRED 交互确认）
# ────────────────────────────────────────────────────────────────

def _mount_impl(mgr, arg: str, *, unmount: bool, ctx: CommandContext | None) -> str:
    """/mount 与 /unmount 命令的统一实现。

    交互确认（CONFIRM_REQUIRED）：
      - ctx.confirm_handler 存在（repl）→ 自动询问并重发 --force
      - 无（web）→ 提示用户手动重发 --force
    """
    arg = (arg or "").strip()
    verb = "/unmount" if unmount else "/mount"
    logger.info(f"[cmd] {verb} {arg!r}")
    if unmount:
        if not arg:
            return "⚠️ Usage: /unmount <path>"
        return _mount_wait(mgr, "unmount", {"path": arg}, ctx=ctx)
    if not arg:
        return "⚠️ Usage: /mount <path> [ro|rw|ro/rw] [--force] | /mount list|save|refresh"
    sub = arg.split(None, 1)[0].lower()
    if sub == "list":
        return _mount_wait(mgr, "mount_list", {}, ctx=ctx)
    if sub == "save":
        return _mount_wait(mgr, "mount_save", {}, ctx=ctx)
    if sub == "refresh":
        return _mount_wait(mgr, "mount_refresh", {}, ctx=ctx)
    tokens = arg.split()
    path = tokens[0]
    writable = True
    force = False
    for tok in tokens[1:]:
        if tok == "ro":
            writable = False
        elif tok == "rw":
            writable = True
        elif tok == "ro/rw":
            writable = True          # ro/rw: plan 只读 / build 可写（对齐 permission.txt v2）
        elif tok == "--force":
            force = True
    return _mount_wait(mgr, "mount", {"path": path, "writable": writable, "force": force}, ctx=ctx)


def _mount_wait(mgr, cmd_name: str, args: dict, *, ctx: CommandContext | None, timeout: float = 5.0) -> str:
    """发送 mount 系列命令并等待 _cmd_result（超时返回提示）。"""
    logger.info(f"[cmd] send {cmd_name} args={args!r}")
    event = mgr.send_command_wait(cmd_name, args, timeout=timeout)
    if event is None:
        return "⏳ No response from agent (timeout)."
    data = event.get("data", "")
    if event.get("ok") is False:
        logger.info(f"[cmd] {cmd_name} FAIL {data!r}")
        return f"⚠️ {data or event.get('error', '执行失败')}"
    if isinstance(data, str) and data.startswith("CONFIRM_REQUIRED:"):
        prompt = data[len("CONFIRM_REQUIRED:"):]
        if ctx and ctx.confirm_handler:
            if ctx.confirm_handler(prompt):
                args = dict(args or {})
                args["force"] = True
                return _mount_wait(mgr, cmd_name, args, ctx=ctx, timeout=timeout)
            return "ℹ️ 已取消挂载。"
        return f"⚠️ 需要确认：{prompt}\n   请重新发送 /mount ... --force 确认"
    return f"{data}"


# ────────────────────────────────────────────────────────────────
#  restart 实现
# ────────────────────────────────────────────────────────────────

def parse_restart_overrides(override_str: str) -> dict:
    """解析 /restart 的覆盖参数，返回 {flag: value} 字典。

    支持: --mode web, -s mysession, --port 9090, --resume
    设计考虑: 使用 shlex.split 处理引号，确保带空格的值正确解析。
    """
    if not override_str or not override_str.strip():
        return {}
    try:
        tokens = shlex.split(override_str)
    except ValueError:
        tokens = override_str.split()
    overrides = {}
    i = 0
    while i < len(tokens):
        if tokens[i].startswith('-'):
            key = tokens[i]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                overrides[key] = tokens[i + 1]
                i += 2
            else:
                overrides[key] = None  # boolean flag (e.g., --resume)
                i += 1
        else:
            i += 1
    return overrides


def apply_argv_overrides(original_argv: list, overrides: dict) -> list:
    """将覆盖参数合并到原始 sys.argv，返回新的 argv 列表。

    设计考虑:
      - 已存在的 flag → 替换其值
      - 新 flag → 追加到末尾
      - boolean flag (value=None) → 仅追加 flag，不加值
    """
    result = list(original_argv)
    for flag, value in overrides.items():
        try:
            idx = result.index(flag)
            if value is not None:
                if idx + 1 < len(result) and not result[idx + 1].startswith('-'):
                    result[idx + 1] = value
                else:
                    result.insert(idx + 1, value)
        except ValueError:
            result.append(flag)
            if value is not None:
                result.append(value)
    return result


def restart_process(mgr, *, save_history=None, argv=None) -> None:
    """执行进程重启：停止 agent → 保存历史 → 关闭 socket → 重启。

    跨平台策略:
      - Unix: os.execv 进程替换，无双进程窗口
      - Windows: subprocess.Popen + os._exit(0)
    端口竞争缓解: Web 模式下先调用 close_web_server() 关闭 uvicorn socket
    （uvicorn 默认 SO_REUSEADDR 允许短暂端口复用窗口）。
    """
    new_argv = list(argv) if argv is not None else list(sys.argv)

    # 停止 agent 线程
    try:
        if mgr.focus:
            mgr.stop_agent(mgr.focus)
    except Exception as e:
        logger.warning(f"restart: stop_agent 异常: {e}")

    # 保存 UI 历史（repl 的 readline history）
    try:
        if save_history:
            save_history()
    except Exception as e:
        logger.warning(f"restart: save_history 异常: {e}")

    # 关闭 Web 服务器 socket（如果在 Web 模式）
    try:
        from codes.web import close_web_server
        close_web_server()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"restart: close_web_server 异常: {e}")

    # 重启
    cmd_display = ' '.join(shlex.quote(a) for a in new_argv)
    print(f"  🔄 Restarting: {cmd_display}")
    try:
        if hasattr(os, 'execv'):
            os.execv(sys.executable, [sys.executable] + new_argv)
        else:
            subprocess.Popen([sys.executable] + new_argv)
            os._exit(0)
    except Exception as e:
        print(f"  ❌ Restart failed: {e}", file=sys.stderr)
        logger.error(f"restart 失败: {e}")


# ────────────────────────────────────────────────────────────────
#  解析与调度
# ────────────────────────────────────────────────────────────────

def _parse(cmd_str: str) -> tuple[str, str]:
    """解析 "/cmd arg..." → (name, arg)。与 repl/web 原解析一致。"""
    parts = cmd_str[1:].strip().split(None, 1)
    name = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    return name, arg


def _current_cmd_name() -> str:
    """返回当前正在 dispatch 的命令名（供 plan/build/build-unsafe 复用）。"""
    return _DISPATCH_STACK[-1] if _DISPATCH_STACK else ""


_DISPATCH_STACK: list[str] = []


# ── 状态信息命令（session 级 KV，与 LLM 的 addinfo/listinfo/rminfo 工具同源）──

@cmd("addinfo", help_text="写入/更新本 session 状态信息: /addinfo <key> <value>", ui=("repl", "web"))
def cmd_addinfo(mgr, arg: str, ctx: CommandContext | None) -> str:
    from codes.session_info import add_info
    sess = (ctx.get_session() if (ctx and ctx.get_session) else None) or getattr(mgr, "focus", None)
    if not sess:
        return "⚠️ 无法确定当前 session"
    toks = (arg or "").strip().split(None, 1)
    if len(toks) < 2:
        return "⚠️ Usage: /addinfo <key> <value>"
    return add_info(sess, toks[0], toks[1], by="user")


@cmd("listinfo", help_text="列出本 session 全部状态信息", ui=("repl", "web"))
def cmd_listinfo(mgr, arg: str, ctx: CommandContext | None) -> str:
    from codes.session_info import list_info
    sess = (ctx.get_session() if (ctx and ctx.get_session) else None) or getattr(mgr, "focus", None)
    if not sess:
        return "⚠️ 无法确定当前 session"
    return list_info(sess)


@cmd("rminfo", help_text="删除本 session 状态信息: /rminfo <key>", ui=("repl", "web"))
def cmd_rminfo(mgr, arg: str, ctx: CommandContext | None) -> str:
    from codes.session_info import remove_info
    sess = (ctx.get_session() if (ctx and ctx.get_session) else None) or getattr(mgr, "focus", None)
    if not sess:
        return "⚠️ 无法确定当前 session"
    key = (arg or "").strip()
    if not key:
        return "⚠️ Usage: /rminfo <key>"
    return remove_info(sess, key)


# ────────────────────────────────────────────────────────────────
#  /mail：callagent 邮件总线管理（list/get/pending/cancel/send）
#  零状态机改动：cancel 仅允许 send/failed → rejected（受 _VALID_NEXT 约束）；
#  delivered（在途）/done/dead/rejected（终态）不可取消。
#  写入用 mb.append（自抢锁）+ _apply_line，与 postman 轮线程并发安全；
#  add_status 的无锁 _write_line 仅限 postman 轮内持锁/单测，命令层不得使用。
# ────────────────────────────────────────────────────────────────


@cmd("mail", help_text="/mail — callagent 邮件总线管理: list/get/pending/cancel/send")
def cmd_mail(mgr, arg: str, ctx: CommandContext | None) -> str:
    """callagent mail.jsonl 管理命令（REPL/Web 共用）。

    子命令:
      /mail list [n] [--state s]   最近 n 封邮件（默认10；--state 按整体态过滤）
      /mail get <mid>              单封详情（send 字段 + 收件人状态链）
      /mail pending                待投递/定时未到/重投中 候选
      /mail cancel <mid>           取消未投递或重投中的信（写 rejected 终态；在途不可取消）
      /mail send <to[,to2]> <msg>  手动构建一封邮件（postman ≤1s 自动投递）
    """
    try:
        toks = shlex.split(arg or "")
    except ValueError as e:
        return f"⚠️ Invalid arguments: {e}"
    if not toks:
        return _mail_usage()
    sub = toks[0]
    if sub == "list":
        return _mail_list(toks[1:])
    if sub == "get":
        return _mail_get(toks[1:])
    if sub == "pending":
        return _mail_pending()
    if sub == "cancel":
        return _mail_cancel(toks[1:])
    if sub == "send":
        return _mail_send(mgr, ctx, toks[1:])
    return _mail_usage() + f"\n⚠️ 未知子命令: {sub}"


def _mail_mb():
    """惰性 Mailbox 实例：构造即全量扫描当前总线（XKAGENT_MAIL 可覆盖路径）。"""
    from codes.mailbox import Mailbox
    return Mailbox()


def _mail_usage() -> str:
    return ("用法:\n"
            "  /mail list [n] [--state s]     最近 n 封邮件（默认10）\n"
            "  /mail get <mid>                单封详情\n"
            "  /mail pending                  待投递/定时未到/重投中 候选\n"
            "  /mail cancel <mid>             取消未投递或重投中的信\n"
            "  /mail send <to[,to2]> <msg> [--reply-to X] [--delay 秒] [--at 时间戳]\n"
            "                                [--priority N] [--provider P] [--need-reply] [--from 名]\n"
            "  状态: send/delivered/failed/done/dead/rejected（delivered=在途不可取消）")


def _mail_fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "-"


def _mail_disp_state(st) -> str:
    """展示用状态：per_to 全部 rejected 时显示 rejected。

    背景：_aggregate_state 把 rejected 与 dead 归并为"终态失败"（单收件人
    rejected 聚合为 dead），状态机内部语义正确，但用户视角"取消"显示成
    "dead" 易误解，这里仅展示层还原。
    """
    per = st.get("per_to") or {}
    if per and all((p.get("state") == "rejected") for p in per.values()):
        return "rejected"
    return st.get("state") or "?"


def _mail_list(toks):
    """/mail list [n] [--state s]：最近 n 封邮件（created_at 降序）。"""
    n, state = 10, None
    i = 0
    while i < len(toks):
        tk = toks[i]
        if tk == "--state":
            if i + 1 >= len(toks):
                return "⚠️ --state requires a value"
            state = toks[i + 1]
            i += 2
            continue
        if tk.startswith("--state="):
            state = tk.split("=", 1)[1]
            i += 1
            continue
        if tk.isdigit():
            n = int(tk)
            i += 1
            continue
        return f"⚠️ 未知参数: {tk}"
    mb = _mail_mb()
    items = sorted(mb.status.items(),
                   key=lambda kv: (kv[1]["send"].get("created_at") or 0), reverse=True)
    out = []
    for mid, st in items:
        disp = _mail_disp_state(st)
        if state and disp != state:
            continue
        send = st["send"]
        to_val = send.get("to")
        to_s = ",".join(to_val) if isinstance(to_val, list) else str(to_val)
        body1 = (send.get("body") or "").replace("\n", " ")[:40]
        out.append(f"{mid} {_mail_fmt_ts(send.get('created_at'))} "
                   f"[{_mail_disp_state(st):9s}] {send.get('from')} \u2192 {to_s}  {body1}")
        if len(out) >= n:
            break
    if not out:
        return "（无匹配邮件）"
    return f"最近 {len(out)} 封邮件（{mb.path}）:\n" + "\n".join(out)


def _mail_get(toks):
    """/mail get <mid>：单封详情（send 字段 + 收件人状态链 + 正文）。"""
    if not toks:
        return "⚠️ Usage: /mail get <mid>"
    mid = toks[0]
    st = _mail_mb().get(mid)
    if st is None:
        return f"未找到邮件 {mid}（可能已被 compact 压实归档）"
    send = st["send"]
    to_val = send.get("to")
    to_s = ",".join(to_val) if isinstance(to_val, list) else str(to_val)
    lines = [
        f"id:         {mid}",
        f"from:       {send.get('from')}",
        f"to:         {to_s}",
        f"state:      {_mail_disp_state(st)}",
        f"created:    {_mail_fmt_ts(send.get('created_at'))}",
        f"deliver_at: {_mail_fmt_ts(send.get('deliver_at'))}",
        f"reply_to:   {send.get('reply_to') or '-'}",
        f"need_reply: {send.get('need_reply') or False}",
        f"priority:   {send.get('priority') or 0}",
        f"provider:   {send.get('provider') or '-'}",
    ]
    per_to = st.get("per_to") or {}
    if per_to:
        lines.append("收件人状态:")
        for rcpt, pt in per_to.items():
            last = pt.get("last") or {}
            lines.append(f"  {rcpt}: {pt.get('state')} retry={pt.get('max_retry') or 0} "
                         f"last={_mail_fmt_ts(last.get('at'))}:{last.get('type')}")
    last = st.get("last") or {}
    lines.append(f"last:       {last.get('type')} @ {_mail_fmt_ts(last.get('at'))}")
    body = send.get("body") or ""
    if len(body) > 500:
        body = body[:500] + "…（截断，全文见 mail.jsonl）"
    lines.append("body:")
    lines.append(body)
    return "\n".join(lines)


def _mail_pending():
    """/mail pending：待投递（send 态）+ 重投中（failed 态）候选。"""
    mb = _mail_mb()
    now = time.time()
    items = sorted(mb.status.items(),
                   key=lambda kv: (kv[1]["send"].get("created_at") or 0), reverse=True)
    rows = []
    for mid, st in items:
        state = st["state"]
        send = st["send"]
        to_val = send.get("to")
        to_s = ",".join(to_val) if isinstance(to_val, list) else str(to_val)
        body1 = (send.get("body") or "").replace("\n", " ")[:40]
        if state == "send":
            da = send.get("deliver_at") or 0
            tag = "待投递" if da <= now else f"定时 {_mail_fmt_ts(da)}"
            rows.append(f"{mid} [{tag}] {send.get('from')} \u2192 {to_s}  {body1}")
        elif state == "failed":
            rows.append(f"{mid} [重投中 retry={st.get('max_retry') or 0}] "
                        f"{send.get('from')} \u2192 {to_s}  {body1}")
    if not rows:
        return "（无待投递/重投中邮件）"
    return "\n".join(rows)


def _mail_write_status(mb, line):
    """带锁写状态行 + 本地聚合同步（命令线程与 postman 轮线程并发安全）。"""
    if not mb.append(line):
        return False
    mb._apply_line(line)
    return True


def _mail_cancel(toks):
    """/mail cancel <mid>：send/failed → rejected（终态）；delivered/终态不可取消。"""
    if not toks:
        return "⚠️ Usage: /mail cancel <mid>"
    mid = toks[0]
    mb = _mail_mb()
    st = mb.get(mid)
    if st is None:
        return f"未找到邮件 {mid}"
    send = st["send"]
    to_val = send.get("to")
    if isinstance(to_val, list):
        # 广播：逐收件人独立取消（send/failed → rejected；其余跳过并说明）
        done, in_flight, final = [], [], []
        for rcpt in to_val:
            pt = (st.get("per_to") or {}).get(rcpt) or {}
            # 无 per_to 记录 = 该收件人从未投递（send 态）；回退聚合态
            # 会被其他收件人的 delivered 误导 → 不能回退
            s = pt.get("state") or "send"
            if s in ("send", "failed"):
                ok = _mail_write_status(mb, {"type": "rejected", "id": mid,
                                             "at": time.time(), "to": rcpt})
                st2 = mb.get(mid)
                pt2 = ((st2 or {}).get("per_to") or {}).get(rcpt) or {}
                if ok and pt2.get("state") == "rejected":
                    done.append(rcpt)
                else:
                    final.append(rcpt)
            elif s == "delivered":
                in_flight.append(rcpt)
            else:
                final.append(rcpt)
        msg = f"mid={mid} 取消: {','.join(done) if done else '无'}"
        if in_flight:
            msg += f"；在途不可取消: {','.join(in_flight)}"
        if final:
            msg += f"；未生效/已终态跳过: {','.join(final)}"
        return msg
    state = st["state"]
    if state in ("send", "failed"):
        # 带 to 写状态行（per-收件人迁移）：不带 to 走整体迁移会漏改 per_to，
        # 已建立 per_to 的信（曾 failed）仍会被 claimable 重投 → 取消无效。
        ok = _mail_write_status(mb, {"type": "rejected", "id": mid,
                                     "at": time.time(), "to": send["to"]})
        st2 = mb.get(mid)
        pt2 = ((st2 or {}).get("per_to") or {}).get(send["to"]) or {}
        if ok and pt2.get("state") == "rejected":
            return f"✅ 已取消 {mid}（rejected）"
        if ok:
            cur = st2.get("state") if st2 else "?"
            return (f"⛔ 取消未生效：{mid} 状态已变化（{cur}），可能正在投递；"
                    f"请 /mail get {mid} 确认后续动态")
        return "⛔ 总线忙（锁竞争），请重试"
    if state == "delivered":
        return f"⚠️ {mid} 已投递在途，不可取消（等待 done/dead）"
    return f"⚠️ {mid} 已是终态（{state}），不可取消"


_MAIL_SEND_OPTS = {
    "--reply-to": "reply_to",
    "--delay": "delay",
    "--at": "at",
    "--priority": "priority",
    "--provider": "provider",
    "--from": "from_",
}


def _mail_send(mgr, ctx, toks):
    """/mail send <to[,to2]> <msg>：手动构建一封邮件（校验链路对齐 exec_callagent）。"""
    opts = {"reply_to": None, "delay": 0.0, "at": None,
            "priority": 0, "provider": None, "need_reply": False, "from_": None}
    positional = []
    i = 0
    while i < len(toks):
        tk = toks[i]
        key, val = None, None
        if tk in _MAIL_SEND_OPTS:
            key, val = _MAIL_SEND_OPTS[tk], None
        elif tk in ("--need-reply", "--needreply"):
            opts["need_reply"] = True
            i += 1
            continue
        elif tk.startswith("--"):
            eq = tk.split("=", 1)
            if eq[0] in _MAIL_SEND_OPTS and len(eq) == 2:
                key, val = _MAIL_SEND_OPTS[eq[0]], eq[1]
            else:
                return f"⚠️ 未知选项: {tk}"
        else:
            positional.append(tk)
            i += 1
            continue
        if val is None:
            if i + 1 >= len(toks):
                return f"⚠️ {tk} requires a value"
            val = toks[i + 1]
            i += 2
        else:
            i += 1
        opts[key] = val
    # 位置参数：to（逗号分隔多收件人=广播）+ 消息正文（可含空格，shlex 已拆）
    if not positional:
        return "⚠️ Usage: /mail send <to[,to2]> <message>"
    to_list = [t.strip() for t in positional[0].split(",") if t.strip()]
    if not to_list:
        return "⚠️ to 为空"
    from codes.session_registry import validate_session_name
    for t in to_list:
        err = validate_session_name(t)
        if err:
            return f"⚠️ to '{t}' 非法: {err}"
    message = " ".join(positional[1:]).strip()
    if not message:
        return "⚠️ message 为空"
    # provider 校验（对齐 exec_callagent）
    if opts["provider"]:
        from codes.mailbox import split_mail_provider
        pname, _m = split_mail_provider(opts["provider"])
        if not pname:
            return f"⚠️ provider 格式非法: {opts['provider']!r}"
        try:
            from codes import provider_config
            provider_config.get_provider(pname)
        except ValueError as e:
            return f"⚠️ provider 不存在: {e}"
    # 数值参数解析
    try:
        delay = float(opts["delay"] or 0)
    except (TypeError, ValueError):
        return f"⚠️ --delay 非法: {opts['delay']!r}"
    at = None
    if opts["at"]:
        try:
            at = float(opts["at"])
        except (TypeError, ValueError):
            return f"⚠️ --at 非法: {opts['at']!r}"
    try:
        priority = int(opts["priority"] or 0)
    except (TypeError, ValueError):
        return f"⚠️ --priority 非法: {opts['priority']!r}"
    if len(to_list) > 1 and opts["need_reply"]:
        return "⚠️ 广播邮件不支持 --need-reply（广播=通知型）"
    mail_env = os.environ.get("XKAGENT_MAIL", "").strip().lower()
    if mail_env in ("off", "0", "false", "none"):
        return "⛔ XKAGENT_MAIL=off，邮件功能已禁用（写信不会有人投递）"
    from_ = opts["from_"] or (
        (ctx.get_session() if (ctx and ctx.get_session) else None)
        or getattr(mgr, "focus", None))
    if not from_:
        return "⚠️ 无法确定当前会话（--from <name> 可显式指定）"
    mb = _mail_mb()
    to_val = to_list if len(to_list) > 1 else to_list[0]
    ok, info = mb.add_send(from_=from_, to=to_val, body=message,
                           reply_to=opts["reply_to"] or None,
                           delay_seconds=delay, deliver_at=at,
                           priority=priority, provider=opts["provider"] or None,
                           need_reply=bool(opts["need_reply"]))
    if not ok:
        return f"⛔ 发送失败: {info}"
    if at:
        ts = _mail_fmt_ts(at)
    elif delay > 0:
        ts = _mail_fmt_ts(time.time() + delay)
    else:
        ts = "立即（≤1s 投递）"
    return (f"✅ 已发送 {info}（from={from_}, to={'、'.join(to_list)}）投递: {ts}")


def dispatch(mgr, cmd_str: str, ctx: CommandContext | None = None) -> str:
    """统一命令调度：解析 → 查注册表 → 执行 handler → 返回展示文本。

    设计考虑:
      - 非 "/" 开头（如 "!xxx"）返回空串，由调用方决定处理
      - handler 异常统一捕获并返回错误文本（不向调用方抛异常，
        避免 repl 主循环 / web WS 任务被单个命令拖垮）
    """
    cmd_str = (cmd_str or "").strip()
    if not cmd_str or not cmd_str.startswith("/"):
        return ""
    name, arg = _parse(cmd_str)
    entry = COMMANDS.get(name)
    if not entry:
        return f"❌ Unknown command: /{name}. Try /help."
    _DISPATCH_STACK.append(name)
    session_token = None
    try:
        session = _cur_session(mgr, ctx)
        if session:
            from codes import config as _session_config
            _context, session_token = _session_config.activate_session(session, ensure=False)
        return entry["handler"](mgr, arg, ctx)
    except Exception as e:
        logger.exception(f"Command /{name} error: {e}")
        return f"❌ Command /{name} error: {e}"
    finally:
        if session_token is not None:
            _session_config.reset_session(session_token)
        _DISPATCH_STACK.pop()
