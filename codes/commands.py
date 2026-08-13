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
        names: 命令名（第一个为主名，其余为别名，如 turnonskill/skillson）
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
    if not mgr.send_command(cmd_name, args):
        return {"ok": False, "error": "无聚焦 agent 或 agent 未运行"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = mgr.read_output(timeout=1.0)
        if event is None:
            continue
        if (isinstance(event, dict) and event.get("type") == "_cmd_result"
                and event.get("cmd") == cmd_name):
            return event
    return None


def _set_mode(mgr, new_mode: str) -> str:
    """切换模式：send_command + 失败回读真实 mode（P0 修复语义）。

    设计考虑: 命令未送达时回读 get_info 防止 UI 缓存与 agent 实际状态脱节
    （web.py 原 33 处 _focus_mode 读写均遵循此模式）。
    """
    _ok = mgr.send_command("set_mode", {"mode": new_mode})
    if _ok:
        mgr._focus_mode = new_mode
        return ""
    info = mgr.get_focus_info(timeout=2.0)
    if info:
        mgr._focus_mode = info.get("mode", mgr._focus_mode)
    return f"⚠️ 模式切换失败：当前仍为 [{getattr(mgr, '_focus_mode', 'plan')}] mode"


def _set_skill_select(mgr, enabled: bool) -> str:
    """切换技能自动选择：同步 agent 线程 + 本地缓存。"""
    mgr.send_command("set_skill_select", {"enabled": enabled})
    mgr._focus_skill_select = enabled
    return f"✅ Skill auto-select: {'ON' if enabled else 'OFF'}"


def _cur_session(mgr, ctx: CommandContext | None) -> str | None:
    """解析当前 session 名（cmds 等命令需要）。"""
    if ctx and ctx.get_session:
        return ctx.get_session()
    return mgr.focus


def _switch_session(mgr, name: str, ctx: CommandContext | None) -> None:
    """后台启动 + 切换焦点 + UI 同步（统一 repl/web 的 session 切换）。"""
    mgr.start_agent(name, wait_ready=False)
    mgr.switch_focus(name)
    if ctx and ctx.switch_session_hook:
        ctx.switch_session_hook(name)


# ────────────────────────────────────────────────────────────────
#  命令实现
# ────────────────────────────────────────────────────────────────

@cmd("help", help_text="/help — 帮助")
def cmd_help(mgr, arg: str, ctx: CommandContext | None) -> str:
    """生成统一帮助文本（注册表元数据 + 特殊命令 + UI 补充）。"""
    lines = [
        "/help — 帮助",
        "/clear — 清空会话",
        "/drop — 丢弃历史（清内存，保留DB记录）",
        "/compact — 压缩会话历史",
        "/session — 会话信息 + token 统计",
        "/session <name> — 切换会话",
        "/session add <name> — 新建会话",
        "/session fork <name> — 复制当前会话",
        "/session rename <name> — 重命名当前会话",
        "/session remove <name> — 删除会话",
        "/session sync [name] — 同步会话（WAL checkpoint）",
        "/sessions — 列出所有会话",
        "/model [name] — 显示/切换模型 | /model test — 全量测速 | /model test <name> — 测试指定模型",
        "/skills /showskills — 列出技能",
        "/updateskillembedding — 更新技能向量索引",
        "/validate [names] — 校验技能",
        "/skill <name> — 加载技能",
        "/plan /build /build-unsafe — 切换模式",
        "/mode — 循环切换 mode",
        "/logfile — 查看本次运行的日志文件尾部",
        "/mount <path> [ro|rw|ro/rw] [--force] — 挂载路径到 pythonrt（软边界）",
        "/mount list|save|refresh — 查看/持久化/重载动态挂载",
        "/unmount <path> — 卸载动态挂载",
        "/turnonskill /turnoffskill — 技能自动选择开关",
        "/session stop <name> — 停止会话 agent",
        "/restart [overrides] — 重启进程",
        "/exit — 退出",
        "!<command> — 执行 bash",
    ]
    if ctx and ctx.help_extra:
        lines.append("")
        lines.append(ctx.help_extra)
    return "\n".join(lines)


@cmd("logfile", help_text="/logfile — 查看本次运行的日志文件尾部")
def cmd_logfile(mgr, arg: str, ctx: CommandContext | None) -> str:
    """查看本次进程对应的日志文件（尾部 60 行）。"""
    from codes._log import get_log_file, tail_log_file
    log_path = get_log_file()
    tail = tail_log_file(60)
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
        return "\n".join(lines) or "(no command history yet)"
    except Exception as e:
        return f"❌ /cmds error: {e}"


@cmd("clear", help_text="/clear — 清空会话")
def cmd_clear(mgr, arg: str, ctx: CommandContext | None) -> str:
    """清空会话（T3: 观察者模式下 agent 拒绝执行时如实报告）。"""
    result = run_cmd(mgr, "clear", timeout=5.0)
    if result and result.get("ok") is False:
        return f"⚠️ {result.get('error', '观察者只读模式：session 已被其他进程占用')}"
    return "✅ Session cleared."


@cmd("drop", help_text="/drop — 丢弃历史（清内存，保留DB记录）")
def cmd_drop(mgr, arg: str, ctx: CommandContext | None) -> str:
    """丢弃历史（T3: 观察者模式下 agent 拒绝执行时如实报告）。"""
    result = run_cmd(mgr, "drop", timeout=5.0)
    if result and result.get("ok") is False:
        return f"⚠️ {result.get('error', '观察者只读模式：session 已被其他进程占用')}"
    return "✅ History dropped."


@cmd("session", help_text="/session — 会话信息与子命令（add/fork/remove/stop/rename/sync/切换）")
def cmd_session(mgr, arg: str, ctx: CommandContext | None) -> str:
    """会话管理子命令（统一 repl/web；交互确认与多选通过 ctx 注入）。"""
    if not arg:
        # 裸 /session：会话信息 + token 统计
        result = run_cmd(mgr, "get_session_stats", timeout=3.0)
        if result and result.get("data"):
            return result["data"]
        return f"Session: {_cur_session(mgr, ctx) or '?'} (stats unavailable)"

    sub_parts = arg.split()
    sub_cmd = sub_parts[0]
    sub_args = sub_parts[1:]
    current_focus = mgr.focus or ""

    if sub_cmd == "add":
        no_switch = "--no-switch" in sub_args
        name_args = [a for a in sub_args if not a.startswith("--")]
        if not name_args:
            return "⚠️ Usage: /session add <name> [--no-switch]"
        name = name_args[0]
        ok, msg = add_session(name)
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
        dst = sub_args[0]
        ok, msg = fork_session(src, dst)
        return f"{'✅' if ok else '❌'} {msg}"

    elif sub_cmd == "remove":
        if not sub_args:
            return "⚠️ Usage: /session remove <name>"
        name = sub_args[0]
        if not session_exists(name):
            return f"❌ Session '{name}' does not exist."
        if name == current_focus:
            return "⚠️ Cannot remove the currently active session."
        if ctx and ctx.confirm_handler and not ctx.confirm_handler(
                f"Are you sure you want to remove '{name}'? (y/N): "):
            return "ℹ️ Cancelled."
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

    elif sub_cmd == "rename":
        if not sub_args:
            return "⚠️ Usage: /session rename <name>"
        old = current_focus
        new = sub_args[0]
        if ctx and ctx.confirm_handler and not ctx.confirm_handler(
                f"Rename current session -> '{new}'? (y/N): "):
            return "ℹ️ Cancelled."
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
        mgr.send_command("set_provider", {"provider": pname})
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
        mgr.send_command("set_provider", {"provider": prov})
    send_kwargs = {"model": resolved}
    if effort_arg:
        send_kwargs["reasoning_effort"] = effort_arg
    mgr.send_command("set_model", send_kwargs)
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
    """重建全部范围向量索引（FAISS）。"""
    from codes.search import update_embeddings
    res = update_embeddings(verbose=True)
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
        path = os.path.abspath(os.path.expanduser(rest))
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


@cmd("turnonskill", "skillson", help_text="/turnonskill — 技能自动选择开关 ON")
def cmd_turnonskill(mgr, arg: str, ctx: CommandContext | None) -> str:
    return _set_skill_select(mgr, True)


@cmd("turnoffskill", "skillsoff", help_text="/turnoffskill — 技能自动选择开关 OFF")
def cmd_turnoffskill(mgr, arg: str, ctx: CommandContext | None) -> str:
    return _set_skill_select(mgr, False)


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
    if not mgr.send_command(cmd_name, args):
        return "❌ No active agent."
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = mgr.read_output(timeout=1.0)
        if event is None:
            continue
        if (isinstance(event, dict) and event.get("type") == "_cmd_result"
                and event.get("cmd") == cmd_name):
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
    return "⏳ No response from agent (timeout)."


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
    try:
        return entry["handler"](mgr, arg, ctx)
    except Exception as e:
        logger.exception(f"Command /{name} error: {e}")
        return f"❌ Command /{name} error: {e}"
    finally:
        _DISPATCH_STACK.pop()
