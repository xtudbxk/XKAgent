"""XKAgent — Web interface (FastAPI + WebSocket + embedded HTML single page).

Usage:
    python -m codes.main --mode web                  # http://localhost:7860
    python -m codes.main --mode web --port 9090      # custom port
    python -m codes.main --mode web --host 0.0.0.0   # all interfaces
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

# ── FastAPI / uvicorn (runtime dependency) ──

from codes._log import logger
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query, UploadFile, File
    from fastapi.responses import HTMLResponse
except ImportError:
    print("  ❌ 需要安装 FastAPI: pip install fastapi uvicorn")
    sys.exit(1)

# ── Project imports ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codes.agent import Agent, MODE_CYCLE, COMPACT_MARKER, COMPACT_PROMPT, _parse_permission_file
from codes.history import (list_sessions, session_exists, add_session,
                       fork_session, prepare_rerun, rename_session,
                       sync_session, get_conn, get_chat_messages, parse_user_prefix,
                       get_messages_since, get_messages_before, get_agent_state, get_token_state,
                       get_mount_state)
from codes.manager import AgentManager
from codes.commands import dispatch, CommandContext
from codes.llm import friendly_error_hint
from codes.path_guard import root_contains
from codes.web_static import load_page
from codes import config as _config  # 与 repl.py 同源: 共享 .xkagent/history.txt

app = FastAPI(title="XKAgent Web")



def _format_permission_text(perm: str) -> str:
    """路径访问权限按 ';' 拆行展示（前端 .msg.system 已 pre-wrap，自动换行）。

    输入: '项目根(...)=只读; /tmp=可写(不持久化); .xkagent/=只读; 挂载: ...'
    输出: '🔒 路径访问权限:\n    项目根(...)=只读\n    /tmp=可写(不持久化)...'
    """
    parts = [p.strip() for p in perm.split(";") if p.strip()]
    if not parts:
        return ""
    return "🔒 路径访问权限:\n" + "\n".join("    " + p for p in parts)


def _format_message_for_display(m: dict) -> dict:
    """Format a message dict with displayType, timestamp, mode, etc."""
    role = m.get("role", "")
    content = m.get("content")
    
    if content is None:
        tool_calls = m.get("tool_calls")
        if tool_calls:
            return {
                "role": "assistant",
                "displayType": "tool_call",
                "content": ", ".join(
                    tc.get("function", {}).get("name", "") for tc in tool_calls
                ),
                "toolCalls": [
                    {
                        "name": tc.get("function", {}).get("name", ""),
                        "args": tc.get("function", {}).get("arguments", "{}"),
                    }
                    for tc in tool_calls
                ],
            }
        return {"role": role, "displayType": role, "content": ""}
    
    if role == "user":
        try:
            parsed = parse_user_prefix(content)
        except Exception:
            # 防御：content 非 str（如多模态 list）时解析抛异常 → 按无前缀处理，避免历史加载 500
            parsed = None
        if parsed:
            b = parsed["body"]
            msg = {
                "role": "user",
                "displayType": "user",
                "timestamp": parsed["timestamp"],
                "mode": parsed["mode"],
                "content": b,
            }
            # 2026-08-07: 历史渲染按实时方案补系统信息（建议技能/推荐信息/要求），
            # 文本与实时 _send({"type":"system",...}) 完全一致，前端按 .msg.system 渲染。
            sys_lines = []
            ag = (parsed.get("agent_name") or "").strip()
            if ag:
                sys_lines.append(f"🤖 当前会话: {ag}")
            perm = (parsed.get("permission") or "").strip()
            if perm:
                sys_lines.append(_format_permission_text(perm))
            skills = (parsed.get("suggested_skills") or "").strip()
            if skills:
                # 2026-08-11: 建议技能多行统一渲染（首行+缩进续行 → 标题+缩进行，与权限/推荐信息风格一致）
                _skill_lines = [ln.strip() for ln in skills.split("\n") if ln.strip()]
                if len(_skill_lines) == 1:
                    sys_lines.append(f"📋 建议技能: {_skill_lines[0]}")
                else:
                    sys_lines.append("📋 建议技能:\n" + "\n".join("    " + ln for ln in _skill_lines))
            rec = (parsed.get("recommended_info") or "").strip()
            if rec:
                items = [ln.strip() for ln in rec.split("\n") if ln.strip()]
                sys_lines.append(f"🧠 推荐信息: {len(items)} 条\n" + "\n".join(items))
            sinfo = (parsed.get("status_info") or "").strip()
            if sinfo:
                # 2026-09-04: 状态信息（session 级 KV）逐行渲染为系统行
                sys_lines.append("📌 状态信息:\n" + "\n".join("    " + ln for ln in sinfo.split("\n") if ln.strip()))
            req = (parsed.get("requirement") or "").strip()
            if req:
                # 对齐实时 skill_req 渲染：要求"用户要求调用X" → 💡 Skill loaded: X
                if req.startswith("用户要求调用"):
                    sys_lines.append(f"💡 Skill loaded: {req[len('用户要求调用'):]}")
                else:
                    sys_lines.append(f"要求: {req}")
            alarm = (parsed.get("system_alarm") or "").strip()
            if alarm:
                sys_lines.append(f"⚠️ 系统告警: {alarm}")
            tcon = (parsed.get("tool_constraint") or "").strip()
            if tcon:
                sys_lines.append(f"⚠️ 工具约束: {tcon}")
            ctx = parsed.get("skill_context") or ""
            if ctx:
                # 旧格式 [skill context: X] 消息 → 历史兼容渲染（实时 skill_selected 事件已移除）
                sys_lines.append(f"🎯 技能选择: {ctx}")
            for _fname, _fval in (parsed.get("extra_fields") or {}).items():
                # v2 通用字段（2026-09-03）：未知头部字段通用渲染，新字段零改动可见
                if not _fval:
                    continue
                _fv_lines = str(_fval).split("\n")
                if len(_fv_lines) == 1:
                    sys_lines.append(f"🏷 {_fname}: {_fv_lines[0]}")
                else:
                    sys_lines.append(f"🏷 {_fname}:\n" + "\n".join("    " + ln for ln in _fv_lines))
            if sys_lines:
                msg["sysLines"] = sys_lines
            return msg
        c = content or ""
        return {"role": "user", "displayType": "user", "content": c}
    
    if role == "assistant":
        tool_calls = m.get("tool_calls")
        if tool_calls:
            # 2026-08-15 修复：带 tool_calls 的 assistant 过渡语正文不能丢——
            # displayType 用 assistant 走正文渲染（保留 content 原文），toolCalls 附加供前端补 tool 折叠框。
            return {
                "role": "assistant",
                "displayType": "assistant",
                "content": content or "",
                "toolCalls": [
                    {
                        "name": tc.get("function", {}).get("name", ""),
                        "args": tc.get("function", {}).get("arguments", "{}"),
                    }
                    for tc in tool_calls
                ],
            }
        return {"role": "assistant", "displayType": "assistant", "content": content}
    
    if role == "tool":
        # tool 原文完整返回：agent/工具输出（含子 agent JSON 结果）需完整可见，
        # 不截断（2026-08-05 用户要求去除截断）
        return {"role": "tool", "displayType": "tool_result", "content": content or ""}

    if role == "thinking":
        # 思考链（agent 落库的 role='thinking' 消息）：历史加载时折叠展示。
        # 仅用于 web 展示；get_chat_messages（LLM 上下文）已在 history 排除集剔除。
        return {"role": "thinking", "displayType": "thinking", "content": content or ""}
    
    if role == "command":
        # /xxx 与 !xxx 命令及结果（web 端执行 / agent._record_command 落库）。
        # 2026-08-05: 历史加载放开 command 渲染；cmd_text 兼容 agent 旧格式
        # （extras.cmd 存命令原文，content 仅是展示文本）。
        cmd_text = m.get("cmd_text") or (content or "")
        result = m.get("result") or ""
        return {
            "role": "command",
            "displayType": "command",
            "content": cmd_text,
            "result": result,
            "kind": m.get("kind", ""),
            "exit_code": m.get("exit_code"),
            "ok": m.get("ok"),
        }
    
    return {"role": role, "displayType": role, "content": content or ""}





# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_agent_manager = AgentManager()
_web_server = None  # uvicorn.Server 实例引用，用于优雅关闭
_current_session: str | None = None
_session_lock = threading.RLock()
_session_ui_cache: dict[str, dict] = {}  # per-session mode/lock/skill 快照（不依赖 focus 缓存）
# /api/session/stats TTL 缓存：前端高频轮询，避免每次阻塞 2-7 秒（log 中 7s 慢请求即由此而来）
_STATS_TTL = 8.0   # 5→8s：前端轮询间隔常 >5s 导致缓存频繁失效，每次落入 2~4s 慢路径
_stats_cache: dict[str, tuple[float, dict]] = {}
_SESSIONS_TTL = 8.0   # sessions 列表轮询缓存：前端每 10s 轮询，TTL 8s 保证命中缓存，避免反复扫描目录+锁检查（IO 慢）
_sessions_cache: dict[str, tuple[float, dict]] = {}

# ── 会话级消息队列（2026-08-30）：busy 时 chat 消息入队，回合结束后自动续跑 ──
# 设计：队列放 web 层而非 agent 内核——manager.send_input 会覆盖 active_turn_id，
# 且第二个回合无 WS 转发任务会导致前端看不到实时流。仅内存不持久化：web 重启丢失。
# v1.1：每条消息带 qid（uuid）支持单条取消；快照供前端刷新/切会话后重建排队视图。
CHAT_QUEUE_MAX = 10   # 每 session 排队上限，超出拒绝（前端 error 提示）
_chat_queues: dict[str, list] = {}   # session -> [{"qid": str, "text": str, "ts": float}, ...]（FIFO）
_chat_queue_lock = threading.Lock()
# 2026-09-04 修复：转发循环空闲兜底阈值（秒）。正常回合有流式事件（thinking/text），
# 连续无事件超过该值基本只有卡死残留（如 _safe_send 挂起导致 blocked 分支未收尾）。
_CHAT_FORWARD_IDLE_TIMEOUT = 900.0


def _enqueue_chat(session: str, text: str) -> dict:
    """消息入队，返回 {"qid", "position"}（position 为 1-based）；队列满抛 ValueError。"""
    import uuid
    with _chat_queue_lock:
        q = _chat_queues.setdefault(session, [])
        if len(q) >= CHAT_QUEUE_MAX:
            raise ValueError(f"排队消息已达上限（{CHAT_QUEUE_MAX} 条），请等待处理或停止当前回合")
        item = {"qid": uuid.uuid4().hex, "text": text, "ts": time.time()}
        q.append(item)
        return {"qid": item["qid"], "position": len(q)}


def _pop_chat_queue(session: str | None) -> dict | None:
    """弹出队头消息（FIFO），返回 {"qid","text"}；队列空返回 None。"""
    if not session:
        return None
    with _chat_queue_lock:
        q = _chat_queues.get(session)
        if not q:
            return None
        item = q.pop(0)
        if not q:
            _chat_queues.pop(session, None)
        return {"qid": item["qid"], "text": item["text"]}


def _cancel_chat_item(session: str, qid: str) -> tuple:
    """按 qid 取消单条排队消息，返回 (ok, text)；已开始处理/不存在返回 (False, None)。"""
    with _chat_queue_lock:
        q = _chat_queues.get(session)
        if not q:
            return False, None
        for _i, _it in enumerate(q):
            if _it["qid"] == qid:
                _text = _it["text"]
                q.pop(_i)
                if not q:
                    _chat_queues.pop(session, None)
                return True, _text
        return False, None


def _chat_queue_snapshot(session: str | None) -> list:
    """队列快照 [{qid,text,position}]（供前端刷新/切会话后重建排队视图与取消入口）。"""
    if not session:
        return []
    with _chat_queue_lock:
        q = _chat_queues.get(session)
        if not q:
            return []
        return [{"qid": _it["qid"], "text": _it["text"], "position": _i + 1}
                for _i, _it in enumerate(q)]


def _clear_chat_queue(session: str | None) -> int:
    """清空指定 session 的排队消息，返回清除条数（Stop/rerun/stop_agent 时调用）。"""
    if not session:
        return 0
    with _chat_queue_lock:
        q = _chat_queues.pop(session, None)
        return len(q) if q else 0


def _chat_queue_size(session: str | None) -> int:
    if not session:
        return 0
    with _chat_queue_lock:
        return len(_chat_queues.get(session) or [])

# ── 崩溃通知：记录 agent 崩溃（自动重启前捕获），供前端提示用户感知 ──
# 崩溃后自动重启会替换 _AgentProcess 导致 error 丢失，因此必须先记录再重启。
# 前端 ack 后 acked=True，避免 2s 轮询反复弹出；未 ack 前持续返回。
_crash_notices: dict[str, dict] = {}   # session -> {time, error, seq, acked}
_crash_seq: int = 0

# ── Auth globals ──
_hashed_password: str = ""
_auth_username: str = "admin"
_tokens: dict[str, tuple[str, float]] = {}  # token -> (username, creation timestamp)
TOKEN_EXPIRE_SECONDS: int = 12 * 60 * 60
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024
_AUTH_EXEMPT_PATHS = frozenset({
    "/login", "/api/login", "/api/check-auth",
    # 2026-09-07: 静态/探测端点免 401 噪音（项目无此二路由，免鉴权后落 404；API 鉴权保留）
    "/favicon.ico", "/metrics",
})


def _normalize_auth_path(path: str) -> str:
    """Strip reverse-proxy prefix; exact-match auth whitelist paths."""
    p = (path or "/").split("?", 1)[0].rstrip("/") or "/"
    m = re.match(r"^/dsw-[^/]+/proxy/\d+(/.*)?$", p)
    if m:
        p = m.group(1) or "/"
        p = p.rstrip("/") or "/"
    return p


def _is_auth_exempt(path: str) -> bool:
    return _normalize_auth_path(path) in _AUTH_EXEMPT_PATHS


_AUTH_COOKIE_NAME: str = secrets.token_hex(8)  # M方案: 进程级随机 cookie 名，跨实例隔离（避免多实例同域 cookie 互踢）


def _get_current_session() -> str | None:
    with _session_lock:
        return _current_session


def _set_current_session(name: str | None) -> None:
    global _current_session
    with _session_lock:
        _current_session = name


def _cache_session_ui(session: str | None, info: dict) -> None:
    if not session or not isinstance(info, dict):
        return
    with _session_lock:
        entry = dict(_session_ui_cache.get(session, {}))
        for key in ("mode", "is_observing", "holder_info"):
            if key in info:
                entry[key] = info[key]
        entry.setdefault("mode", "plan")
        entry.setdefault("is_observing", False)
        entry.setdefault("holder_info", None)
        _session_ui_cache[session] = entry


def _ui_cache(session: str | None) -> dict:
    if not session:
        return {}
    with _session_lock:
        return dict(_session_ui_cache.get(session, {}))


def _focus_session(session: str) -> AgentManager:
    """切换 manager focus + 同步 Web 当前 session（仅显式切 session 时调用）。"""
    from codes.session_registry import validate_session_name
    error = validate_session_name(session)
    if error:
        raise HTTPException(400, error)
    session = _agent_manager.resolve_session(session)
    _set_current_session(session)
    prev_focus = _agent_manager.focus
    _record_crash_notice(_agent_manager, session)
    session_list = [s.session for s in _agent_manager.list_sessions()]
    if session not in session_list:
        logger.info(f"_focus_session: starting agent session={session}")
        _agent_manager.start_agent(session, wait_ready=False)
        _agent_manager.focus_session(session)
    else:
        _agent_manager.switch_focus(session)
        if session != prev_focus:
            _agent_manager.focus_session(session)
    return _agent_manager



def _record_crash_notice(mgr: AgentManager, session: str | None = None) -> None:
    """崩溃检测：在自动重启替换 proc 之前，读取并记录崩溃信息（供前端提示）。

    关键：manager 的 switch_focus/start_agent 检测到非 running 时会直接替换
    _AgentProcess（旧 proc 连同 error 一起丢失），因此必须在任何可能触发重启
    的调用之前，先遍历 list_sessions 检查 crashed 状态并捕获 error。
    记录后前端 2s 轮询（messages/sessions）即可拿到 crash_notice 展示给用户。
    """
    global _crash_seq
    for si in mgr.list_sessions():
        if session is not None and si.session != session:
            continue
        if si.status != "crashed":
            continue
        proc = mgr._agents.get(si.session)  # noqa: SLF001 同项目内部访问
        err = (proc.error if proc and proc.error else None) or si.error or "unknown"
        _crash_seq += 1
        # 保留已 ack 状态：proc 崩溃期间（未重启）每次轮询都会重新记录，
        # 若不保留 acked，用户点"知道了"后横幅会反复弹出（ack 失效）。
        _prev = _crash_notices.get(si.session)
        _crash_notices[si.session] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": err,
            "seq": _crash_seq,
            "acked": bool(_prev and _prev.get("acked")),
        }
        logger.warning(f"记录崩溃通知: session={si.session} error={err}")
        # 崩溃通知变化 → 失效 sessions 列表缓存，前端尽快看到侧边栏 🔴 徽标
        _sessions_cache.clear()
        _stats_cache.clear()


def _get_agent(session: str | None = None) -> AgentManager:
    """显式切换 focus 并回写当前 session（创建/切换 session 用）。"""
    if session:
        return _focus_session(session)
    resolved = _agent_manager.resolve_session(_get_current_session())
    return _focus_session(resolved)


def _ensure_agent_running(session: str | None = None) -> tuple[AgentManager, str]:
    """启动 session 对应 agent，但不切换全局 focus（供只读 API 使用）。"""
    if session:
        from codes.session_registry import validate_session_name
        error = validate_session_name(session)
        if error:
            raise HTTPException(400, error)
    session = session or _get_current_session() or _agent_manager.resolve_session(None)
    names = [s.session for s in _agent_manager.list_sessions()]
    if session not in names:
        _agent_manager.start_agent(session, wait_ready=False)
    return _agent_manager, session


def _ensure_session_agent(mgr: AgentManager, session: str | None) -> AgentManager:
    """确保指定 session 的 agent 线程存活（不切换 focus）。"""
    if not session:
        return mgr
    _record_crash_notice(mgr, session)
    with mgr._lock:  # noqa: SLF001
        proc = mgr._agents.get(session)
    if proc is None or proc.status != "running":
        mgr.start_agent(session, wait_ready=False)
        return mgr
    if proc.thread is not None and not proc.thread.is_alive():
        logger.warning(f"_ensure_session_agent: agent 线程已死 session={session}，自动重启")
        mgr.stop_agent(session)
        mgr.start_agent(session, wait_ready=False)
        return mgr
    if proc.status == "crashed":
        logger.warning(f"_ensure_session_agent: Agent 崩溃后重启 session={session}")
        mgr.start_agent(session, wait_ready=False)
    return mgr


def _session_mode(mgr: AgentManager, session: str | None) -> str:
    info = _get_session_info(mgr, session, 1.0) if session else None
    if info and info.get("mode"):
        return info["mode"]
    if session and mgr.focus == session:
        return getattr(mgr, "_focus_mode", "plan")
    return "plan"


def _session_busy(mgr, name: str) -> bool:
    """判断 session 是否正在执行中（llm 处理或 tool 执行）。

    内存遍历 manager 的 SessionInfo（零 IO），供 session_switched 推送
    目标 session 的实时执行状态，前端据此决定输入框 Send/Stop。
    与 /api/sessions 前端 isBusy 判定保持同一口径（status=running 且 llm/in_tool）。
    """
    for si in mgr.list_sessions():
        if si.session == name:
            # 回合级活跃判定（2026-08-10）：phase 在技能选择/tool 间隙会临时
            # 复位 idle，导致"LLM 还在执行却谎报空闲"；_turn_active 仅回合
            # 真正结束才复位，是权威信号。
            return si.status == "running" and getattr(si, "turn_active", False)
    return False


def _should_update_focus_cache(mgr: AgentManager, session: str) -> bool:
    """后台转发时仅 focus session 才更新 Web 侧全局 _focus_* 缓存。"""
    return bool(session) and session == mgr.focus


def _get_session_info(mgr: AgentManager, session: str, timeout: float = 1.0) -> dict | None:
    """读取指定 session 的 get_info，不切换 focus（供 stats 等只读 API）。"""
    event = mgr.send_command_wait("get_info", None, session, timeout)
    if event and isinstance(event.get("data"), dict):
        info = event["data"]
        _cache_session_ui(session, info)
        return info
    return None


# ── Auth core functions ──

def _hash_password(password: str) -> str:
    """使用带盐 PBKDF2 保存密码，格式内含参数便于后续升级。"""
    iterations = 310_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

def _verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash (constant-time comparison)."""
    try:
        algorithm, rounds, salt_hex, digest_hex = hashed.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False

def _generate_token() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_hex(32)

def _prune_expired_tokens() -> None:
    """清理过期 token，避免长跑进程 _tokens 无界增长。"""
    if TOKEN_EXPIRE_SECONDS <= 0 or not _tokens:
        return
    now = datetime.now()
    expired = [
        t for t, (_, created) in _tokens.items()
        if (now - datetime.fromtimestamp(created)).total_seconds() > TOKEN_EXPIRE_SECONDS
    ]
    for t in expired:
        _tokens.pop(t, None)


def _validate_token(token: str) -> str | None:
    """Validate a token; return the bound username, or None if invalid/expired."""
    _prune_expired_tokens()
    if token not in _tokens:
        return None
    username, created = _tokens[token]
    if TOKEN_EXPIRE_SECONDS > 0:
        elapsed = (datetime.now() - datetime.fromtimestamp(created)).total_seconds()
        if elapsed > TOKEN_EXPIRE_SECONDS:
            del _tokens[token]
            return None
    return username

def _parse_basic_auth(header: str):
    """Parse 'Authorization: Basic base64(user:pass)' -> (user, pass) or None."""
    try:
        if not header.startswith("Basic "):
            return None
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
        return (user, pwd)
    except Exception:
        return None

def _get_token_from_request(request):
    """Extract token from Cookie or Authorization header."""
    cookie = request.cookies.get(_AUTH_COOKIE_NAME)
    if cookie and _validate_token(cookie) == _auth_username:
        return cookie
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if _validate_token(token) == _auth_username:
            return token
    return None

def _is_authenticated(request):
    """Check if the request is authenticated (token or valid Basic Auth)."""
    if _get_token_from_request(request):
        return True
    auth = request.headers.get("Authorization", "")
    parsed = _parse_basic_auth(auth)
    if parsed:
        user, pwd = parsed
        if user == _auth_username and _verify_password(pwd, _hashed_password):
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Auth middleware ──

@app.middleware("http")
async def auth_middleware(request, call_next):
    # Whitelist: paths that don't require authentication
    # 适配反向代理子路径（如 DSW /dsw-xxx/proxy/4096/）：代理可能保留或重写前缀，
    # 用 endswith 匹配，避免代理保留前缀时白名单失效导致 /login 被 401 拦截
    _path = request.url.path
    if _is_auth_exempt(_path):
        return await call_next(request)

    # Check authentication
    if _hashed_password and not _is_authenticated(request):
        # HTML request -> redirect to login
        accept = request.headers.get("accept", "")
        if "text/html" in accept or request.url.path == "/" or _path.endswith("/"):
            from fastapi.responses import RedirectResponse
            # 相对路径跳转（无前导斜杠）：浏览器基于当前地址栏 URL 解析，
            # 天然适配任意反向代理前缀（如 /dsw-xxx/proxy/4096/ -> /dsw-xxx/proxy/4096/login）；
            # 代理将前缀重写为 / 时（path=/），相对 login 也基于浏览器 URL 正确解析，
            # 不会像绝对路径 /login 那样丢失前缀跳转到网关根路径
            return RedirectResponse(url="login")
        # API request -> 401
        ip = request.client.host if request.client else "-"
        logger.warning(f"AUTH 401 拒绝: path={request.url.path} ip={ip}")
        from fastapi.responses import JSONResponse
        # 注意：不返回 WWW-Authenticate: Basic 头！该头会让浏览器弹出原生
        # Basic Auth 弹框（显示页面域名），而 XKAgent 采用 JSON+cookie 登录，
        # 弹框输入永远无法通过，形成"反复弹框"死循环。改为纯 JSON 401，
        # 由前端 JS fetch 捕获后显示错误提示。
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "message": "Please provide valid credentials"},
        )

    return await call_next(request)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── Access log middleware ──


def _is_client_disconnect(exc: Exception) -> bool:
    """判断异常链中是否存在 anyio 客户端断连特征（WouldBlock/EndOfStream）。"""
    cur = exc
    seen = 0
    while cur is not None and seen < 8:
        name = type(cur).__name__
        if "EndOfStream" in name or "WouldBlock" in name:
            return True
        cur = getattr(cur, "__cause__", None)
        seen += 1
    return False


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """统一请求访问日志：method/path/status/耗时 → log 文件（不落库）。

    设计考虑: middleware 自动覆盖所有 /xxx 路由（含未来新增），杜绝漏记；
    仅写 log 文件（loguru 异步落盘），高频轮询也不会撑爆 sqlite。
    """
    _t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception as _exc:
        if _is_client_disconnect(_exc):
            # 客户端提前断开（WS/HTTP 中止）→ anyio 抛 WouldBlock/EndOfStream，
            # 属正常现象，降级为 debug 避免刷 ERROR 日志。
            logger.debug(f"REQ {request.method} {request.url.path} 客户端断连")
        else:
            logger.exception(f"REQ {request.method} {request.url.path} 异常")
        raise
    cost_ms = (time.time() - _t0) * 1000
    ip = request.client.host if request.client else "-"
    if resp.status_code >= 400:
        # 2026-09-09: 4xx/5xx 记录完整 query，便于定位坏 URL（如硬编码的非法 session）
        _q = request.url.query or ""
        if len(_q) > 500:
            _q = _q[:500] + "…(truncated)"
        _qs = ("?" + _q) if _q else ""
        logger.warning(f"REQ {request.method} {request.url.path}{_qs} -> {resp.status_code} {cost_ms:.0f}ms ip={ip}")
    else:
        logger.info(f"REQ {request.method} {request.url.path} -> {resp.status_code} {cost_ms:.0f}ms ip={ip}")
    return resp

#  REST API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ── Auth API endpoints ──

@app.get("/api/check-auth")
async def api_check_auth(request: Request):
    """Check if the current request is authenticated."""
    if not _hashed_password:
        return {"authenticated": True, "reason": "auth_disabled"}
    ok = _is_authenticated(request)
    return {"authenticated": ok}

@app.post("/api/login")
async def api_login(request: Request):
    """Login endpoint. Accepts JSON {"username": "...", "password": "..."} or Authorization: Basic."""
    if not _hashed_password:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": "Auth disabled"})

    username = None
    password = None

    # Try JSON body first
    try:
        body = await request.json()
        username = body.get("username")
        password = body.get("password")
    except Exception:
        pass

    # Try Authorization: Basic
    if not password:
        auth = request.headers.get("Authorization", "")
        parsed = _parse_basic_auth(auth)
        if parsed:
            username, password = parsed


    if (not username) or (username != _auth_username) or not password or not _verify_password(password, _hashed_password):
        ip = request.client.host if request.client else "-"
        logger.warning(f"AUTH 登录失败: username={username} ip={ip}")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid credentials"},
        )

    # Generate token
    token = _generate_token()
    _prune_expired_tokens()
    _tokens[token] = (username, datetime.now().timestamp())

    from fastapi.responses import JSONResponse
    logger.info(f"AUTH 登录成功: username={username}")
    resp = JSONResponse(content={"ok": True, "token": token})
    secure = request.url.scheme == "https" or os.environ.get("XKAGENT_COOKIE_SECURE") == "1"
    resp.set_cookie(
        key=_AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=TOKEN_EXPIRE_SECONDS,
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    """撤销当前 token 并清除认证 cookie。"""
    token = request.cookies.get(_AUTH_COOKIE_NAME)
    if token:
        _tokens.pop(token, None)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        _tokens.pop(auth[7:], None)
    from fastapi.responses import JSONResponse
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(_AUTH_COOKIE_NAME, path="/")
    return resp

# ── 输入历史 API（与 repl.py 共享 .xkagent/history.txt）──

_HISTORY_FILE_NAME = "history.txt"
_HISTORY_TAIL_BYTES = 4096  # 末尾去重只需读尾部，避免全量读大文件


def _get_history_file() -> "os.PathLike[str]":
    """获取共享命令历史文件路径（与 repl._get_history_file 同源，保证路径一致）。"""
    return _config.get_data_dir() / _HISTORY_FILE_NAME


@app.get("/api/history")
async def api_get_history(limit: int = Query(200, ge=1, le=5000)):
    """读取共享命令历史，返回最近 limit 条（最新在末尾，与 repl._history 顺序一致）。

    设计: 只读不写，避免与 repl 运行中的内存态冲突；
    前端一次性拉取并在本地维护游标，浏览过程零请求。
    """
    hist_file = _get_history_file()
    def _read():
        try:
            with open(hist_file, "r", encoding="utf-8") as f:
                return [line.rstrip("\n") for line in f if line.strip()]
        except FileNotFoundError:
            return []
        except (UnicodeDecodeError, OSError) as e:
            logger.warning(f"读取历史失败(降级为空): {e}")
            return []
    lines = await asyncio.to_thread(_read)
    _log("HIST", f"GET /api/history -> {len(lines)} lines (limit={limit})")
    return {"history": lines[-limit:]}


@app.post("/api/history")
async def api_append_history(data: dict):
    """追加一条命令历史（O_APPEND 追加 + 末尾去重）。

    对齐 repl._add_history 语义: 空行不入库、与末尾重复不入库。
    并发考虑: repl 仅在退出时全量落盘，web 用追加模式写入，
    冲突窗口极小；O_APPEND 保证多进程下 write 原子性。
    """
    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    hist_file = _get_history_file()
    def _append():
        last = ""
        try:
            with open(hist_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - _HISTORY_TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace")
                non_empty = [l for l in tail.splitlines() if l.strip()]
                if non_empty:
                    last = non_empty[-1]
        except FileNotFoundError:
            pass
        if last == text:
            return "duplicate"
        os.makedirs(os.path.dirname(hist_file), exist_ok=True)
        with open(hist_file, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        return "ok"
    try:
        status = await asyncio.to_thread(_append)
    except OSError as e:
        logger.warning(f"追加历史失败: {e}")
        return {"ok": False, "reason": "io_error"}
    if status == "duplicate":
        return {"ok": False, "reason": "duplicate"}
    _log("HIST", f"POST /api/history append: {text[:40]!r}")
    return {"ok": True}

def _collect_sessions_info() -> tuple:
    """线程池中执行：扫描 session 列表 + 锁检查（均为文件系统 IO）。

    独立函数以便 asyncio.to_thread 包裹——list_sessions() 扫描目录、
    _is_locked() 检查锁文件均为同步 IO，可能阻塞事件循环
    （check 报告：/api/sessions 慢时段实测 9~15s）。
    """
    sessions = list_sessions()
    from codes.lock import is_locked as _is_locked
    from codes.lock import is_same_process as _is_same_process
    lock_tags = {}
    for s in sessions:
        locked, meta = _is_locked(s)
        if locked and isinstance(meta, dict) and not _is_same_process(meta):
            lock_tags[s] = "\U0001f512"
    return sessions, lock_tags


@app.get("/api/autocompactlimit")
async def api_get_autocompactlimit(session: str = Query("")):
    """读取指定 session 的 autocompactlimit（get_info 字段透传）。

    复用 _get_session_info（send_command_wait("get_info")，不切换 focus）；
    agent 未运行/超时 → value=null（前端隐藏 indicator，不报错）。
    """
    target = session or _get_current_session()
    if not target:
        return {"ok": True, "value": None}
    info = await asyncio.to_thread(_get_session_info, _agent_manager, target, 1.5)
    value = info.get("autocompactlimit") if isinstance(info, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        value = None
    return {"ok": True, "value": value, "session": target}

@app.get("/api/sessions")
async def api_list_sessions():
    logger.info("API: 列出 session")
    """List all sessions."""
    # 2s TTL 缓存：前端每 2s 轮询，命中缓存时零 IO 开销（目录扫描+锁检查慢）
    now = time.time()
    cached = _sessions_cache.get("default")
    if cached and now - cached[0] < _SESSIONS_TTL:
        return cached[1]
    # 文件 IO（目录扫描 + 锁检查）放入线程池，避免阻塞事件循环
    sessions, lock_tags = await asyncio.to_thread(_collect_sessions_info)
    current = _get_current_session()
    # 崩溃记录兜底：前端 2s 轮询 /api/sessions 是最高频路径，必须在此主动检测
    # 崩溃并记录（纯浏览/不发消息场景的感知兜底；proc 若已被 manager 内部重启
    # 为 running，此处仍能从 _crash_notices 持久记录返回 crash_notices）。
    _record_crash_notice(_agent_manager)
    # 注：仅包含已启动的 agent 线程；未启动的 session 不在 agents 中（phase 视为 idle）
    session_set = set(sessions)
    agents = {}
    for si in _agent_manager.list_sessions():
        if si.session not in session_set:
            continue
        agents[si.session] = {
            "phase": si.phase,       # "starting" | "idle" | "llm"
            "in_tool": si.in_tool,   # 是否有 tool 正在执行
            "turn_active": getattr(si, "turn_active", False),  # 回合级活跃标志（2026-08-10）
            # 2026-09-04 修复：WS 转发任务活跃度（与 turn_active 独立）——转发任务卡死
            # 时 agent 回合已结束但任务未结束，前端据此显示 busy，避免"静默排队"
            "chat_forward_busy": _chat_forward_busy_global(si.session),
            # 2026-09-04 修复：排队消息数（跨 session 可见——前端侧边栏徽标）
            "queue_count": _chat_queue_size(si.session),
            "status": si.status,     # "running" | "stopped" | "crashed"
            "error": si.error,       # 崩溃错误信息（crashed 时非 None，供前端提示）
            "mode": si.mode,         # "plan" | "build" | "build-unsafe"（供面板徽标显示）
        }
    # 对齐 repl /sessions：running 的会话置顶，其余保持原有 mtime 顺序
    # 稳定排序保证组内相对顺序不变；未启动的 session 视为非 running
    sessions.sort(key=lambda s: not (agents.get(s, {}).get("status") == "running"))
    # 崩溃通知透传（未 ack 的）：供侧边栏 🔴crashed 徽标与消息区警告横幅
    crash_notices = {
        s: {"time": n["time"], "error": n["error"], "seq": n["seq"]}
        for s, n in _crash_notices.items() if not n.get("acked") and s in session_set
    }
    workdirs = {}
    for session_name in sessions:
        try:
            workdirs[session_name] = _resolve_workdir(session_name)
        except Exception:
            workdirs[session_name] = ""
    # 2026-09-10: 展示标题（title）随列表一并下发，前端零额外请求。
    # title 未设置 → 回退 session name（display = title or name，唯一 fallback 规则）。
    from codes import session_registry as _sreg
    # 2026-09-10: 单次 list_contexts() 构建 titles——避免逐 session get() 造成 N 次
    # registry 文件读取（/api/sessions 是 2s 高频轮询端点，读文件次数必须 O(1)）。
    titles = {}
    pins = []
    try:
        _contexts = _sreg.list_contexts()
        for _ctx in _contexts:
            titles[_ctx.name] = _ctx.title or _ctx.name
        # 2026-09-10: 置顶列表（最近置顶在前；仅保留仍在列表中的会话，与 sessions 口径一致）
        pins = [c.name for c in sorted(_contexts, key=lambda c: c.pinned_at or 0.0, reverse=True)
                if c.pinned and c.name in session_set]
    except Exception:
        pass
    # 目录发现的未注册 .msgz 会话无 context → 回退 name（与 get() None 分支同语义）
    for session_name in sessions:
        titles.setdefault(session_name, session_name)
    result = {"sessions": sessions, "current": current, "lock_tags": lock_tags,
              "agents": agents, "crash_notices": crash_notices, "workdirs": workdirs,
              "titles": titles, "pins": pins}
    _sessions_cache["default"] = (now, result)
    return result



@app.post("/api/sessions")
async def api_create_session(data: dict):
    """Create a new session via manager (对齐 repl: start_agent + switch)."""
    name = data.get("name", datetime.now().strftime("session_%Y%m%d_%H%M%S"))
    if not name or not re.match(r'^[a-zA-Z0-9_\-.]+$', str(name)):
        raise HTTPException(400, "Invalid session name")
    # title 可选：展示名（支持中文/空格/emoji），不参与路径；未设置则展示回退 name
    title = data.get("title")
    from codes.session_registry import validate_session_title as _vtitle
    _terr = _vtitle(title)
    if _terr:
        raise HTTPException(400, _terr)
    # 支持自定义 workdir（对齐 repl /session add --workdir）：
    # 透传 add_session(name, workdir)，validate_workdir 校验失败时
    # add_session 内部捕获 ValueError 返回 (False, msg)，此处转 400。
    ok, msg = add_session(name, workdir=data.get("workdir") or None, title=title)
    if not ok:
        raise HTTPException(400, msg)
    _get_agent(name)
    logger.info(f"SESSION 创建: {name}")
    _sessions_cache.clear()  # 列表缓存失效，前端立即拿到新 session
    _stats_cache.clear()  # P0: stats 缓存失效，新 session 统计立即准确
    return {"session": name, "message": msg}
@app.post("/api/sessions/{name}/switch")
async def api_switch_session(name: str):
    """Switch to an existing session (对齐 repl：更新 _current_session + 真实 msg_count)."""
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    agent = _get_agent(name)
    # FIX: 2.0→1.0：agent 忙碌时 get_focus_info 必然超时（get_info 命令排队等 LLM 回合结束），
    # 降低超时避免 REST 切换被拖慢；_get_agent 内部 focus_session 已同步缓存
    info = await asyncio.to_thread(agent.get_focus_info, 1.0)
    if info:
        agent._focus_mode = info.get("mode", agent._focus_mode)
        agent._focus_observing = info.get("is_observing", False)
        agent._focus_holder_info = info.get("holder_info", None)
        _cache_session_ui(name, info)
    msg_count = info.get("msg_count", 0) if info else 0
    logger.info(f"SESSION 切换: {name}")
    _sessions_cache.clear()  # 列表缓存失效，切换后立即重排（选中置顶）
    _stats_cache.clear()  # P0: stats 缓存失效，切换后前端 fetchStatus 不再命中旧 session 数据
    return {"session": name, "messages": msg_count}


@app.post("/api/sessions/{name}/title")
async def api_set_session_title(name: str, data: dict):
    """设置/清空 session 展示标题（title）。

    title 仅用于展示（支持中文/空格/emoji），不参与磁盘路径与调用寻址；
    空串/空白 → 清除（展示回退 session name）。name 仍须为合法 ASCII key。
    """
    from codes.session_registry import set_title as _set_title
    from codes.session_registry import validate_session_title as _vtitle
    data = data or {}   # 空 body 防御：视为清除标题
    error = _vtitle(data.get("title"))
    if error:
        raise HTTPException(400, error)
    try:
        context = await asyncio.to_thread(_set_title, name, data.get("title"))
    except KeyError:
        raise HTTPException(404, f"Session '{name}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _sessions_cache.clear()  # 列表缓存失效，前端立即拿到新标题
    logger.info(f"SESSION 标题更新: {name} -> {context.title!r}")
    return {"session": name, "title": context.title,
            "display": context.display_name, "message": "title updated"}


# ── 2026-08-23: 最近消息缓存（读路径降载 + EIO 降级）──
# 前端 2s 轮询 messages（实测 17 次/分钟）全走 DB；EIO 时 sqlite 连接初始化
# （-shm/-wal 写）即失败。缓存最近 N 条 + last_id，按 db 文件 mtime+size 失效；
# EIO 期间命中缓存可绕过 DB（前端继续可用）；stat 失败时降级信任缓存。
_MSG_CACHE_MAX_ROWS = 200
_MSG_CACHE_MAX_SESSIONS = 100
_msg_cache: dict[str, tuple] = {}   # name -> (mtime_ns, size, last_id, rows)
_msg_cache_lock = threading.Lock()


def _msg_cache_db_path(name: str) -> str:
    """（2026-08-25 msgz 迁移）缓存已禁用，函数保留仅供诊断；
    路径跟随 msgz 存储（.msgz 单文件）。"""
    return os.path.join(str(_config.get_default_workdir()), ".xkagent", "historys", name + ".msgz")


def _try_msg_cache(name: str, after_id: int):
    """增量轮询缓存（已禁用 2026-08-25）。

    根因：msgz 已替代 sqlite（内存主数据 + 原子落盘），sqlite EIO 降级
    用的消息缓存不再需要；且 .db 文件已迁移移除，os.stat(.db) 恒失败
    → key=None → 缓存永不失效 → 增量轮询返回旧数据 → 切回 session 后
    历史消息丢失。直接禁用：每次走 DB（msgz 内存读，快），彻底消除
    脏缓存风险。
    """
    return None


def _fill_msg_cache(name: str, raw: list, last_id: int):
    """填充消息缓存（已禁用 2026-08-25，与 _try_msg_cache 同步禁用）。"""
    return


def _load_session_messages(name: str, after_id: int, limit: int | None = None, before_id: int = 0) -> tuple:
    """在线程池中执行 DB 读取：连接 + 查询(可选分页) + 关闭连接。

    独立函数以便 asyncio.to_thread 包裹——msgz 内存读 O(n) 快，
    线程池隔离避免阻塞 asyncio 事件循环（原 sqlite busy_timeout 已随迁移消除）。
    limit 仅在 after_id=0（全量分页）时传入；增量路径必须完整返回（不截断），
    否则会漏消息导致前端缓存水位线错乱。
    返回 (messages, last_id) 或 (messages, last_id, total)（limit 非 None 时）。
    """
    # 2026-08-23: 增量轮询路径（前端主形态）→ 先试内存缓存（命中可绕过 DB，EIO 降级）
    if after_id > 0 and before_id == 0 and limit is None:
        cached = _try_msg_cache(name, after_id)
        if cached is not None:
            return cached
    conn = get_conn(name)
    try:
        if before_id > 0:
            # "加载更早"向前分页：id < before_id 的最新 limit 条，不受 compact cutoff 限制
            return get_messages_before(conn, before_id, limit=limit or 20,
                                       fields=["command", "thinking"], with_id=True, with_time=True)
        if limit is not None:
            # 全量分页：COUNT 轻量统计可见消息总数（与 get_messages_since 过滤一致）。
            # 2026-08-05: 放开 command 渲染后，COUNT 排除集同步去掉 command，
            # 否则 total 与返回消息数不一致会误导 has_more/已渲染条数。
            total = conn.count_visible(include_compact=True)   # 与 ignore_cutoff=True 返回集一致（含 compact marker，防 has_more/"加载更早"计数错乱）
            raw, last_id = get_messages_since(conn, None, with_id=True, limit=limit, fields=["command", "thinking"], with_time=True, ignore_cutoff=True)
            return raw, last_id, total
        raw, last_id = get_messages_since(conn, after_id or None, with_id=True, fields=["command", "thinking"], with_time=True, ignore_cutoff=True)
        if after_id > 0:
            _fill_msg_cache(name, raw, last_id)   # 2026-08-23: 填充缓存供下次轮询命中
        return raw, last_id
    finally:
        conn.close()

@app.get("/api/sessions/{name}/messages")
async def api_get_session_messages(name: str, after_id: int = 0, limit: int = Query(50, ge=1, le=500), before_id: int = 0):
    """Get messages for a session, optionally incremental.

    - after_id=0（默认）→ 全量分页：返回最近 limit 条 + total 总数 + has_more
      （兼容旧前端：未传 limit 时默认 50，前端可自行调整）
    - after_id>0 → 增量：只返回 id > after_id 的消息（前端缓存游标），完整不截断
    - 响应带 last_id：本次返回的最大消息 id，前端存为下次增量游标
    - 响应带 total：该 session 可见消息总数（仅全量路径），前端用于展示"共 N 条"

    Returns structured messages with displayType, timestamp, mode fields.
    User messages have system prefix parsed out (timestamp/mode extracted).
    对齐 repl: 从 history 读取真实消息，而非空列表占位。
    """
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    # 崩溃通知：前端 2s 轮询 messages 是用户感知崩溃最快的路径（先记录再渲染）
    _record_crash_notice(_agent_manager, name)
    # DB 操作（连接+查询+关闭）整体放入线程池，避免 sqlite 锁等待阻塞事件循环
    if before_id > 0:
        # "加载更早"向前分页：id < before_id 的最新 limit 条，不受 compact cutoff 限制
        raw, has_more, oldest_id = await asyncio.to_thread(_load_session_messages, name, 0, limit, before_id)
        msgs = [_format_message_for_display(m) for m in raw]
        for _m, _raw in zip(msgs, raw):
            _m["_id"] = _raw.get("_id")
            _m["created_at"] = _raw.get("created_at", "")
        _log("CHAT", f"Loaded {len(msgs)} older messages for session '{name}' (before_id={before_id}, has_more={has_more})")
        resp = {"session": name, "messages": msgs, "has_more": has_more, "oldest_id": oldest_id}
        notice = _crash_notices.get(name)
        if notice and not notice.get("acked"):
            resp["crash_notice"] = {
                "time": notice["time"], "error": notice["error"], "seq": notice["seq"],
            }
        return resp
    if after_id == 0:
        # 全量分页路径：limit 生效；增量路径不传 limit（必须完整返回防漏消息）
        raw, last_id, total = await asyncio.to_thread(_load_session_messages, name, after_id, limit)
        has_more = total > len(raw)
    else:
        raw, last_id = await asyncio.to_thread(_load_session_messages, name, after_id)
        total, has_more = None, False
    msgs = [_format_message_for_display(m) for m in raw]
    # 透传 _id：_format_message_for_display 会重建 dict 丢失内部字段，
    # 前端增量去重依赖 _id，此处按原始消息补齐
    for _m, _raw in zip(msgs, raw):
        _m["_id"] = _raw.get("_id")
        _m["created_at"] = _raw.get("created_at", "")
    # 日志降级（2026-08-22）：无新消息时前端 2s 轮询会重复请求空结果（last_id=0 被 || 短路），
    # 无条件打 INFO 会每 2s 刷一条日志。有消息才打 INFO，空结果降级 DEBUG。
    if msgs:
        _log("CHAT", f"Loaded {len(msgs)} messages for session '{name}' (after_id={after_id}, last_id={last_id}, total={total})")
    else:
        logger.debug(f"Loaded 0 messages for session '{name}' (after_id={after_id}, last_id={last_id})")
    resp = {"session": name, "messages": msgs, "last_id": last_id}
    # 崩溃通知透传：前端据此弹出警告横幅（未 ack 才返回，ack 后清除）
    notice = _crash_notices.get(name)
    if notice and not notice.get("acked"):
        resp["crash_notice"] = {
            "time": notice["time"], "error": notice["error"], "seq": notice["seq"],
        }
    if total is not None:
        resp["total"] = total
        resp["has_more"] = has_more
    return resp





@app.post("/api/sessions/{name}/crash/ack")
async def api_ack_crash_notice(name: str):
    """前端确认崩溃通知后调用：标记 acked，避免 2s 轮询反复弹出。"""
    notice = _crash_notices.get(name)
    if notice:
        notice["acked"] = True
        logger.info(f"崩溃通知已确认: session={name}")
    return {"ok": True}


def _pick_next_session(exclude: str) -> str | None:
    """停止当前会话后，选择列表最靠前的其他会话（running 优先，保持 mtime 顺序）。

    与 /api/sessions 排序口径一致：running 会话置顶、组内保持 history mtime 顺序；
    排除被停止的会话自身。无其他会话时返回 None。
    """
    sessions = list_sessions()
    agents = {si.session: si.status for si in _agent_manager.list_sessions()}
    sessions.sort(key=lambda s: not (agents.get(s) == "running"))
    for s in sessions:
        if s != exclude:
            return s
    return None


@app.post("/api/sessions/{name}/stop")
async def api_stop_session(name: str):
    """Stop a session's agent thread（保留数据，可恢复；对齐 repl 的 /session stop 命令）。

    与 DELETE（stop + 删数据，不可逆）语义区分：此处仅停线程，
    会话消息历史保留，切回时 switch_focus 自动重启 agent 线程。

    若停止的是当前会话：stop_agent 会把 focus 置空，此处自动切换到列表最靠前的
    其他会话并同步 _current_session（防止 stats 轮询经 resolve_session 重启刚停止的会话），
    返回 switched_to 供前端同步 UI；无其他会话时置 _current_session=None 交由 resolve_session 兜底。
    """
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    ok = _agent_manager.stop_agent(name)
    logger.warning(f"SESSION 停止: {name} (ok={ok})")
    _sessions_cache.clear()
    _stats_cache.clear()
    # 2026-08-30: 会话停止 → 排队消息一并清空（对齐 interrupt 的 Stop 语义）
    queue_cleared = _clear_chat_queue(name)
    if queue_cleared:
        logger.info(f"SESSION 停止清空排队消息: {name} count={queue_cleared}")
    if not ok:
        return {"ok": False, "reason": "not_running", "queue_cleared": queue_cleared}
    switched_to = None
    if name == _get_current_session():
        switched_to = _pick_next_session(name)
        if switched_to:
            await asyncio.to_thread(_agent_manager.focus_session, switched_to)
            _set_current_session(switched_to)
            _stats_cache.clear()
        else:
            _set_current_session(None)
    return {"ok": True, "switched_to": switched_to, "queue_cleared": queue_cleared}

@app.post("/api/sessions/{name}/fork")
async def api_fork_session(name: str, data: dict):
    """Fork（复制）指定会话为新会话（任意源会话，对齐 repl /session fork 语义）。

    设计考虑:
      - 与 /session fork <name> 的区别：repl 版固定 fork 当前 focus 会话，
        此端点可对列表中任意 session 操作（前端 fork 按钮直接调用）。
      - 复用 history.fork_session（msgz 文件复制：先 sync 落盘再 shutil.copy2），
        运行中会话亦可 fork（内存 store 先落盘保证最新数据）。
      - fork 后不自动切换/不启动 agent（对齐 repl 行为），新会话出现在列表，
        用户点击切换时 _get_agent 自动启动。
      - 可选 cutoff（消息 id）：仅 fork 该消息及其上方历史/LLM 回复（截断复制），
        不传则完整复制（向后兼容）。
    """
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    target = (data.get("target") or "").strip()
    if not target:
        raise HTTPException(400, "Missing target session name")
    cutoff = data.get("cutoff")
    if cutoff is not None:
        try:
            cutoff = int(cutoff)
        except (TypeError, ValueError):
            raise HTTPException(400, "cutoff must be an integer message id")
    ok, msg = fork_session(name, target, cutoff)
    if not ok:
        raise HTTPException(400, msg)
    _sessions_cache.clear()   # 列表缓存失效，新 session 立即出现
    logger.info(f"SESSION fork: {name} -> {target}" + (f" (until msg {cutoff})" if cutoff is not None else ""))
    return {"ok": True, "session": target, "message": msg}



@app.post("/api/sessions/{name}/pin")
async def api_set_session_pin(name: str, data: dict):
    """置顶/取消置顶 session（侧边栏「📌 置顶」分区固定显示；对齐 title 端点的异常/缓存语义）。

    置顶状态持久化在 registry 记录（pinned/pinned_at 字段，与 title 同文件同锁）；
    返回更新后的完整置顶列表，供前端立即重排列表。
    """
    from codes.session_registry import set_pin as _set_pin
    from codes.session_registry import list_pins as _list_pins
    data = data or {}   # 空 body 防御：默认置顶
    pinned = bool(data.get("pinned", True))
    try:
        await asyncio.to_thread(_set_pin, name, pinned)
    except KeyError:
        raise HTTPException(404, f"Session '{name}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    _sessions_cache.clear()   # 列表缓存失效，前端立即拿到新置顶顺序
    pins = await asyncio.to_thread(_list_pins)
    logger.info(f"SESSION 置顶更新: {name} pinned={pinned}")
    return {"session": name, "pinned": pinned, "pins": pins, "message": "pin updated"}


def _fetch_token_stats(session: str) -> dict:
    """从 session db 读取累计 token 用量（权威值，agent 每轮持久化）。

    设计考虑: 原实现 send_command("get_session_stats") + read_output 轮询抢队列，
    与 WS 聊天循环并发消费同一输出队列（回合转发/drain 会吞掉 _cmd_result、
    agent 忙碌时命令排队 1s 超时），导致 token 统计时有时无或为 0（P1-B）。
    改为直接读 db：agent 每轮结束 set_token_state 持久化 total_*（agent.py），
    零竞态、零排队、零超时；与 last_* 同源同 session，杜绝跨 session 错位。
    """
    try:
        _ts = get_token_state(session)
        return {
            "prompt_tokens": _ts.get("prompt_tokens", 0) or 0,
            "completion_tokens": _ts.get("completion_tokens", 0) or 0,
            "reasoning_tokens": _ts.get("reasoning_tokens", 0) or 0,
        }
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


def _resolve_effective_model(info: dict, session: str | None = None) -> str:
    """解析当前实际生效的模型名。

    设计考虑：agent.get_info 返回的 model 是 self.model（用户显式 /model 设置过才非空）；
    未显式设置时真实生效的是 provider 的 default_model（对齐 repl /model 命令的 eff 解析）。
    """
    model = (info.get("model") or "").strip()
    if model:
        return model
    prov = (info.get("provider") or "").strip()
    if not prov:
        return ""
    token = None
    try:
        if session:
            _context, token = _config.activate_session(session, ensure=False)
        from codes import provider_config as _pc
        return str(_pc.get_provider(prov).get("default_model", "") or "").strip()
    except Exception:
        return ""
    finally:
        if token is not None:
            _config.reset_session(token)


@app.post("/api/model/test")
async def api_model_test(session: str = Query(None)):
    """测试全部 provider+model 的联通性（复用 /model test 全量测速逻辑）。"""
    token = None
    try:
        effective = _checked_session(session) or _get_current_session()
        if effective:
            _context, token = _config.activate_session(effective, ensure=False)
        from codes import llm as _llm
        results, elapsed = await asyncio.to_thread(_llm.test_all_connectivity, timeout=15)
        return {"ok": True, "results": results, "elapsed": elapsed}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if token is not None:
            _config.reset_session(token)


@app.get("/api/model/list")
async def api_model_list(session: str = Query(None)):
    # 列出 config 全部 provider+model（不测速），供前端下拉框选择切换。
    # current 复用 api_session_stats 的实际生效模型解析（含 default_model 兜底）。
    try:
        from codes import llm as _llm
        models = [{"provider": p, "model": m} for p, m in _llm._iter_test_targets()]
        current = None
        try:
            stats = await api_session_stats(session=session)
            if stats.get("provider") or stats.get("model"):
                current = {"provider": stats.get("provider") or "", "model": stats.get("model") or ""}
        except Exception:
            current = None
        return {"ok": True, "models": models, "current": current}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/model/set")
async def api_model_set(data: dict):
    """设置模型：scope="current"（当前 session）或 "all"（全部 session）。

    body: {"model": "模型名", "provider": "provider名(可选)", "scope": "current"|"all"}
    """
    model = str(data.get("model") or "").strip()
    provider = str(data.get("provider") or "").strip() or None
    scope = str(data.get("scope") or "current").strip()
    if not model:
        return {"ok": False, "error": "model 必填"}
    if scope not in ("current", "all"):
        return {"ok": False, "error": f"scope 必须为 current 或 all，收到 {scope!r}"}

    def _set_one(agent, name: str) -> str | None:
        """对单个 agent 设置 provider/model，返回错误信息或 None。"""
        try:
            if provider:
                ev = agent.send_command_wait("set_provider", {"provider": provider},
                                             session=name, timeout=2.0)
                if not ev or ev.get("ok") is False:
                    return f"{name}: provider switch failed"
            # 拆分 :effort 后缀：model 存纯名，effort 走 reasoning_effort 临时覆盖
            # （对齐 commands.py /model 命令语义，否则 web 侧 effort 只能靠配置推导）
            from codes import provider_config as _pc
            model_part, effort_arg = _pc.split_model_effort(str(model))
            send_kwargs = {"model": model_part}
            if effort_arg:
                send_kwargs["reasoning_effort"] = effort_arg
            ev = agent.send_command_wait("set_model", send_kwargs,
                                         session=name, timeout=2.0)
            if not ev or ev.get("ok") is False:
                return f"{name}: model switch failed"
            return None
        except Exception as e:
            return f"{name}: {e}"

    if scope == "current":
        agent = await asyncio.to_thread(_get_agent)
        err = await asyncio.to_thread(_set_one, agent, _get_current_session() or "current")
        if err:
            return {"ok": False, "error": err}
        _stats_cache.clear()
        return {"ok": True, "scope": "current", "session": _get_current_session(), "model": model, "provider": provider}

    # all：全部 registry session 写 DB；已运行 agent 再发 set_* 命令
    def _set_all():
        from codes.history import list_sessions, set_agent_state, get_agent_state
        results = {}
        running = {si.session for si in _agent_manager.list_sessions() if si.status == "running"}
        for name in list_sessions():
            try:
                if name in running:
                    err = _set_one(_agent_manager, name)
                    results[name] = "ok" if err is None else err
                else:
                    cur_p, _cur_m = get_agent_state(name)
                    set_agent_state(name, provider=provider or cur_p or None, model=model)
                    results[name] = "ok (db)"
            except Exception as e:
                results[name] = str(e)
        return results
    results = await asyncio.to_thread(_set_all)
    _sessions_cache.clear()
    _stats_cache.clear()
    ok_count = sum(1 for v in results.values() if v == "ok")
    return {"ok": True, "scope": "all", "results": results, "ok_count": ok_count, "total": len(results)}


@app.get("/api/session/stats")
async def api_session_stats(session: str = Query(None)):
    """Get current session stats（8s TTL 缓存：前端轮询秒回，避免每次阻塞 2-7s）。

    P0 修复: cache_key 从固定 "" 改为实际生效 session（_get_agent 回写后的
    _current_session），切换 session 后 key 自然变化不再命中旧缓存；
    各切换路径同时 _stats_cache.clear() 双保险立即失效。
    P1-C: tokens 与 last_* 统一读 effective 的 db，返回 session 也用 effective。
    UX 修复: 使用 _ensure_agent_running + send_command_wait(get_info, session=...)
    代替 _get_agent，避免 stats 轮询悄悄 switch_focus 导致 chat 进错 session。
    """
    agent, effective = await asyncio.to_thread(_ensure_agent_running, session)
    cache_key = effective
    now = time.time()
    cached = _stats_cache.get(cache_key)
    if cached and now - cached[0] < _STATS_TTL:
        return cached[1]

    tokens = await asyncio.to_thread(_fetch_token_stats, effective)
    info = await asyncio.to_thread(_get_session_info, agent, effective, 1.0)
    # v2: 最近一轮单轮值（last_*）与累计值同源读 effective 的 db
    try:
        _ts = get_token_state(effective)
        _last_p = _ts.get("last_prompt_tokens", 0) or 0
        _last_c = _ts.get("last_completion_tokens", 0) or 0
    except Exception:
        _last_p, _last_c = 0, 0
    if info:
        result = {
            "session": effective,
            "mode": info.get("mode", "plan"),
            "messages": info.get("msg_count", 0),
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
            "reasoning_tokens": tokens["reasoning_tokens"],
            "last_prompt_tokens": _last_p,
            "last_completion_tokens": _last_c,
            "model": _resolve_effective_model(info, effective),
            "provider": info.get("provider", "") or "",
            # T5: 透传锁状态（观察者模式 / 持有者信息），供前端渲染 ⏳/🔒 标签
            "observing": info.get("is_observing", False),
            "holder": info.get("holder_info", None),
            # 路径链接：项目根（前端据此识别绝对路径）
            "workdir": _resolve_workdir(effective),
        }
    else:
        # info 为 None（agent 忙碌/未就绪，get_focus_info 超时）时，从 session db 读取
        # 持久化的真实 provider/model，不再硬编码 "default"（用户现象: 忙碌 session 切换
        # 时侧边栏显示 ⚙️ default）。model 为空时解析 provider 的 default_model。
        try:
            _p, _m = get_agent_state(effective)
        except Exception:
            _p, _m = "", ""
        if not _m and _p:
            _m = _resolve_effective_model({"provider": _p}, effective)
        _ui = _ui_cache(effective)
        _mode = _ui.get("mode") or ("plan" if agent.focus != effective else getattr(agent, "_focus_mode", "plan"))
        result = {"session": effective, "mode": _mode, "messages": 0,
                "prompt_tokens": tokens["prompt_tokens"],
                "completion_tokens": tokens["completion_tokens"],
                "reasoning_tokens": tokens["reasoning_tokens"],
                "last_prompt_tokens": _last_p,
                "last_completion_tokens": _last_c,
                "model": _m,
                "provider": _p,
                "observing": _ui.get("is_observing", False),
                "holder": _ui.get("holder_info"),
                "workdir": _resolve_workdir(effective)}
    _stats_cache[cache_key] = (time.time(), result)
    return result
def _resolve_workdir(session: str | None = None) -> str:
    """解析 session 创建时固定的 workdir；无 session 时回退启动默认目录。"""
    effective = session or _get_current_session()
    if effective:
        from codes.session_registry import get as _get_session_context
        context = _get_session_context(effective)
        if context is not None:
            return str(context.workdir)
    return str(_config.get_default_workdir().resolve())


def _checked_session(session: str | None) -> str | None:
    """校验可选 session 名；非法名称直接 400，避免 files/stats 路径穿越。"""
    if not session:
        return None
    from codes.session_registry import validate_session_name
    error = validate_session_name(session)
    if error:
        raise HTTPException(400, error)
    return session


def _checked_session_soft(session: str | None) -> str | None:
    """文件类端点专用：非法 session 名不再 400，warning 后视同缺省（回退当前会话）。

    设计考虑（2026-09-09）：历史消息/书签里硬编码的绝对 URL 可能携带非法 session 值
    （如 LLM 误写 ``{sess}``），严格 400 会让浏览器地址栏显示裸 JSON。session 仅用于
    挑选允许根、不参与路径拼接，忽略非法值回退当前会话不会扩大访问面。
    """
    if not session:
        return None
    from codes.session_registry import validate_session_name
    error = validate_session_name(session)
    if error:
        logger.warning(f"files: 忽略非法 session={session!r}（{error}），回退当前会话")
        return None
    return session


_FILE_ERROR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · XKAgent</title>
<style>
  :root {{ --bg:#f3f5fb; --surface:#fff; --text:#1c1f23; --muted:#57606a; --border:rgba(28,31,35,.08); --accent:#0066ff; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
         background:var(--bg); color:var(--text); min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }}
  .card {{ max-width:640px; width:100%; background:var(--surface); border:1px solid var(--border); border-radius:12px;
          padding:28px 30px; box-shadow:0 2px 12px rgba(28,31,35,.06); }}
  .icon {{ font-size:34px; line-height:1; }}
  h1 {{ font-size:19px; margin:12px 0 8px; }}
  .name {{ display:inline-block; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:14px;
          background:#f6f8fa; border:1px solid var(--border); border-radius:6px; padding:3px 8px; word-break:break-all; }}
  .path {{ color:var(--muted); font-size:12.5px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
          word-break:break-all; margin-top:10px; }}
  .hint {{ color:var(--muted); font-size:13px; margin-top:14px; line-height:1.6; }}
  a.btn {{ display:inline-block; margin-top:20px; color:var(--accent); text-decoration:none; font-size:13.5px; }}
  a.btn:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">📄</div>
  <h1>{title}</h1>
  <div class="name">{name}</div>
  <div class="path">原始路径：{path}</div>
  <div class="hint">{hint}</div>
  <a class="btn" href="javascript:history.back()">← 返回上一页</a>
</div>
</body>
</html>
"""


def _wants_html(request: Request) -> bool:
    """浏览器地址栏导航（Accept 含 text/html）→ 友好 HTML；XHR/fetch/img 调用保持 JSON。"""
    try:
        return "text/html" in (request.headers.get("accept") or "").lower()
    except Exception:
        return False


def _friendly_file_error(request: Request, path: str, status: int, detail: str):
    """文件类端点错误响应：浏览器导航返回友好 HTML（显式显示文件名），API 调用仍返回 JSON。

    设计考虑：历史消息里硬编码的绝对 URL（坏 session / 已删除文件）被点击时浏览器会
    直接打开 API URL，裸 JSON 对用户毫无意义。
    """
    from fastapi.responses import JSONResponse
    if not _wants_html(request):
        return JSONResponse({"detail": detail}, status_code=status)
    raw = (path or "").strip()
    name = os.path.basename(raw.rstrip("/")) or (raw or "(未指定文件名)")
    return HTMLResponse(
        _FILE_ERROR_HTML.format(
            title="文件未找到",
            name=html.escape(name),
            path=html.escape(raw or "-"),
            hint=html.escape(detail or "该文件可能已被移动、重命名或删除。"),
        ),
        status_code=status,
    )


def _files_allowed_roots(session: str | None = None) -> list[str]:
    """files 允许根集合：workdir + /tmp + 全局 permission.txt + 该 session 动态挂载（去重）。

    与 pythonrt 沙箱软边界对齐：/tmp（沙箱恒可写、非持久化）与 /mount 动态挂载
    （per-session mount_state 表）及 permission.txt 全局挂载在 files 浏览器同样
    可见/可访问；根外路径一律 404。
    失效路径（已删除）过滤掉，对齐 _load_dyn_mounts 语义。
    """
    roots = [os.path.realpath(_resolve_workdir(session))]
    rp_tmp = os.path.realpath("/tmp")
    if rp_tmp not in roots:
        roots.append(rp_tmp)  # /tmp 恒为可浏览根（对齐沙箱软边界）
    try:
        for p, _w in _parse_permission_file():
            rp = os.path.realpath(p)
            if rp not in roots:
                roots.append(rp)
    except Exception as e:
        logger.warning(f"files allowed roots: permission.txt 解析失败: {e}")
    if session:
        try:
            for m in get_mount_state(session):
                rp = os.path.realpath(m.get("path", ""))
                if rp and rp not in roots:
                    roots.append(rp)
        except Exception as e:
            logger.warning(f"files allowed roots: session={session} mount_state 读取失败: {e}")
    return [r for r in roots if os.path.exists(r)]


def _path_allowed(target: str, roots: list[str]) -> bool:
    """target（已 realpath）是否落在任一允许根内（前缀校验，防路径穿越）。"""
    t = os.path.realpath(target)
    return any(root_contains(r, t) for r in roots)


def _files_rel_in_root(target: str, root: str) -> str:
    """Return API-relative path within root ('' = root itself)."""
    t, r = os.path.realpath(target), os.path.realpath(root)
    if t == r:
        return ""
    rel = os.path.relpath(t, r)
    return "" if rel == "." else rel.replace("\\", "/")


def _resolve_files_target(path: str, session: str | None, root_index: int = 0) -> tuple[str, list[str], int]:
    """解析 files API 请求路径 → (realpath target, 允许根集合, root_index)。"""
    roots = _files_allowed_roots(session)
    if not roots:
        raise HTTPException(404, "No allowed roots")
    idx = max(0, min(root_index, len(roots) - 1))
    base = roots[idx]
    if os.path.isabs(path):
        target = os.path.realpath(path)
    else:
        target = os.path.realpath(os.path.join(base, path or ""))
    if not _path_allowed(target, roots):
        raise HTTPException(404, "Path outside allowed root")
    for i, r in enumerate(roots):
        if root_contains(r, target):
            idx = i
            break
    return target, roots, idx


def _resolve_files_base(path: str, session: str | None = None,
                        root_index: int = 0) -> tuple[str, list[str], int]:
    """兼容旧调用：解析 files API 请求路径。"""
    return _resolve_files_target(path, session, root_index)


# ── 路径解析（resolve）：LLM 路径片段 → 真实文件（多根尝试 / 前缀补全 / 按名搜索）──
# 2026-09-09: LLM 回复中的路径常为裸文件名/半路径/跨根相对路径；精确解析失败时按成本
# 递增逐级回退，所有命中仍受允许根校验（_path_allowed），不越权。
_FILE_REF_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".trash", "venv", ".venv",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", ".ipynb_checkpoints",
})
_FILE_REF_SKIP_REL = (".xkagent/historys", ".xkagent/logs", ".xkagent/search_index")
_FILE_REF_PREFIXES = (".xkagent/files", ".xkagent/docs/{session}", ".xkagent", "files")
_FILE_REF_MAX_DEPTH = 6
_FILE_REF_MAX_DIRS = 3000
_FILE_REF_MAX_RESULTS = 20
_FILE_REF_TIME_BUDGET = 1.5
_FILE_REF_CACHE_TTL = 60.0
_FILE_REF_CACHE_MAX = 512
_FILE_REF_CACHE: dict = {}


def _normalize_file_ref(ref: str) -> str:
    """规范化 LLM 路径片段：去包裹引号/反引号、file://、URL 解码、反斜杠、./ 前缀、~ 展开。"""
    s = (ref or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ("\"", "'", "`"):
        s = s[1:-1].strip()
    if s.startswith("file://"):
        s = s[7:]
    try:
        from urllib.parse import unquote
        s = unquote(s)
    except Exception:
        pass
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s.startswith("~/"):
        s = os.path.expanduser(s)
    return s


def _search_file_in_roots(ref: str, roots: list, session: str | None = None,
                          limit: int = _FILE_REF_MAX_RESULTS) -> list:
    """在允许根内按 basename / 路径后缀搜索（BFS 按目录 mtime 新→旧，带预算与缓存）。

    Returns: [(real_path, mtime, root_index)]，按启发式排序（目录优先级 > 尾部匹配 > mtime 新）。
    """
    ref_n = _normalize_file_ref(ref)
    base_name = os.path.basename(ref_n)
    if not ref_n or not base_name:
        return []
    suffix = ref_n.lstrip("/")
    roots_key = tuple(roots)
    ck = (roots_key, ref_n, session or "")
    now = time.monotonic()
    cached = _FILE_REF_CACHE.get(ck)
    if cached and cached[0] > now:
        return cached[1]
    hits = []
    deadline = time.monotonic() + _FILE_REF_TIME_BUDGET
    visited = 0
    for ri, root in enumerate(roots):
        queue = [(root, 0)]
        while queue and time.monotonic() < deadline and visited < _FILE_REF_MAX_DIRS:
            d, depth = queue.pop(0)
            visited += 1
            subdirs = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                nm = e.name
                                if nm in _FILE_REF_SKIP_DIRS:
                                    continue
                                if nm.startswith(".") and nm != ".xkagent":
                                    continue
                                rel = os.path.relpath(e.path, root).replace(os.sep, "/")
                                if any(rel == s or rel.startswith(s + "/") for s in _FILE_REF_SKIP_REL):
                                    continue
                                subdirs.append((e.stat().st_mtime, e.path))
                            elif e.is_file(follow_symlinks=False):
                                if e.name != base_name:
                                    continue
                                pl = e.path.replace(os.sep, "/")
                                if suffix and not (pl.endswith("/" + suffix) or pl.endswith(suffix)):
                                    continue
                                if _path_allowed(e.path, roots):
                                    hits.append((e.path, e.stat().st_mtime, ri))
                        except OSError:
                            continue
            except OSError:
                continue
            if depth < _FILE_REF_MAX_DEPTH:
                subdirs.sort(reverse=True)
                queue.extend((p, depth + 1) for _, p in subdirs)

    def _score(h):
        p, mt, _ri = h
        pl = p.replace(os.sep, "/")
        s = 0.0
        if "/.xkagent/files/" in pl:
            s += 300
        elif "/.xkagent/docs/" in pl:
            s += 200
        elif "/.xkagent/" in pl:
            s += 100
        if session and ("/.xkagent/docs/%s/" % session) in pl:
            s += 60
        if suffix and pl.endswith("/" + suffix):
            s += 40
        s -= pl.count("/")
        return (s, mt)

    hits.sort(key=_score, reverse=True)
    out = hits[:limit]
    if len(_FILE_REF_CACHE) >= _FILE_REF_CACHE_MAX:
        _FILE_REF_CACHE.clear()
    _FILE_REF_CACHE[ck] = (now + _FILE_REF_CACHE_TTL, out)
    return out


def _resolve_files_deep(ref: str, session: str | None, root_index: int = 0):
    """LLM 路径片段 → (target, roots, root_index, candidates)。

    逐级回退：精确（绝对/指定根）→ 多根尝试 → 前缀补全 → 按名搜索；找不到抛 404。
    """
    roots = _files_allowed_roots(session)
    if not roots:
        raise HTTPException(404, "No allowed roots")
    ref_n = _normalize_file_ref(ref)
    if not ref_n:
        raise HTTPException(404, "Empty path")
    idx0 = max(0, min(root_index, len(roots) - 1))
    # 1) 精确解析（保持既有语义：绝对路径 / 指定根相对路径）
    if os.path.isabs(ref_n):
        cand = os.path.realpath(ref_n)
        if os.path.exists(cand) and _path_allowed(cand, roots):
            for i, r in enumerate(roots):
                if root_contains(r, cand):
                    return cand, roots, i, []
    else:
        cand = os.path.realpath(os.path.join(roots[idx0], ref_n))
        if os.path.exists(cand) and _path_allowed(cand, roots):
            return cand, roots, idx0, []
    # 2) 多根尝试（相对路径跨根：workdir ↔ 挂载根）
    if not os.path.isabs(ref_n):
        for i, r in enumerate(roots):
            if i == idx0:
                continue
            cand = os.path.realpath(os.path.join(r, ref_n))
            if os.path.exists(cand) and _path_allowed(cand, roots):
                return cand, roots, i, []
    # 3) 前缀补全（.xkagent/files、session docs 等常见产出目录）
    prefixes = [p.format(session=session or "") for p in _FILE_REF_PREFIXES]
    for i, r in enumerate(roots):
        for pre in prefixes:
            cand = os.path.realpath(os.path.join(r, pre, ref_n))
            if os.path.exists(cand) and _path_allowed(cand, roots):
                return cand, roots, i, []
    # 4) 搜索兜底（裸文件名 / 半路径）
    hits = _search_file_in_roots(ref_n, roots, session)
    if hits:
        p, _mt, ri = hits[0]
        return p, roots, ri, hits
    raise HTTPException(404, f"File not found: {ref}")


def _files_candidate_dict(hit, roots) -> dict:
    """搜索结果 → 前端候选结构（path 相对该根，root_index 用于构造 URL）。"""
    p, mt, ri = hit
    return {"path": _files_rel_in_root(p, roots[ri]), "root_index": ri,
            "name": os.path.basename(p), "mtime": mt}


@app.get("/api/files/list")
async def api_files_list(path: str = Query("", description="目录路径（相对当前 root_index 根）"),
                         root_index: int = Query(0, description="允许根索引"),
                         session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）")):
    """列出目录内容（FTP 风格），允许根 = workdir + 全局挂载 + 该 session 动态挂载。"""
    sess = _checked_session_soft(session) or _get_current_session()
    target, roots, idx = _resolve_files_target(path, sess, root_index)
    if not os.path.isdir(target):
        raise HTTPException(404, f"Not a directory: {path or '/'}")
    def _scan():
        entries = []
        with os.scandir(target) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "is_dir": is_dir,
                    "size": None if is_dir else st.st_size,
                    "mtime": st.st_mtime,
                })
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return entries
    try:
        entries = await asyncio.to_thread(_scan)
    except OSError as exc:
        raise HTTPException(500, f"Failed to scan directory: {exc}")
    rel = _files_rel_in_root(target, roots[idx])
    parent = os.path.dirname(rel).replace("\\", "/") if rel else None
    if parent == ".":
        parent = ""
    can_up = bool(rel)
    return {
        "path": rel,
        "parent": parent,
        "can_up": can_up,
        "root_index": idx,
        "entries": entries,
        "roots": [{"index": i, "label": os.path.basename(r) or r} for i, r in enumerate(roots)],
        "session": sess,
    }


@app.get("/api/files/download")
async def api_files_download(request: Request,
                             path: str = Query("", description="文件路径（相对 root_index 根）"),
                             root_index: int = Query(0, description="允许根索引"),
                             session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）"),
                             preview: bool = Query(False, description="在线预览：inline 展示（图片/HTML/PDF 浏览器直接渲染）而非下载"),
                             resolve: bool = Query(False, description="路径解析：精确失败时多根尝试/前缀补全/按名搜索（LLM 路径片段）")):
    """流式下载允许根内文件（FileResponse 流式，避免整文件读入内存），防路径穿越。

    2026-09-09：session 宽松化（非法值忽略）；找不到文件时浏览器导航返回友好 HTML。
    """
    sess = _checked_session_soft(session) or _get_current_session()
    try:
        if resolve:
            target, _roots, _idx, _cands = await asyncio.to_thread(_resolve_files_deep, path, sess, root_index)
        else:
            target, _roots, _idx = _resolve_files_target(path, sess, root_index)
    except HTTPException as exc:
        return _friendly_file_error(request, path, exc.status_code, str(exc.detail))
    if not os.path.isfile(target):
        return _friendly_file_error(request, path, 404, f"Not a file: {path}")
    from fastapi.responses import FileResponse
    logger.info(f"FILE 下载: {path} session={sess}")
    fname = os.path.basename(target)
    return FileResponse(
        target,
        filename=fname,
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline" if preview else "attachment",
    )

@app.get("/api/files/open")
async def api_files_open(request: Request,
                         path: str = Query("", description="文件/目录路径（相对 root_index 根）"),
                         root_index: int = Query(0, description="允许根索引"),
                         session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）"),
                         resolve: bool = Query(False, description="路径解析：精确失败时多根尝试/前缀补全/按名搜索（LLM 路径片段）")):
    """打开允许根内文件/目录（新标签页）。文件→302 到下载流；目录→302 到文件浏览器定位。

    2026-09-09：session 宽松化；找不到时浏览器导航返回友好 HTML（显示文件名）。
    """
    sess = _checked_session_soft(session) or _get_current_session()
    try:
        if resolve:
            target, roots, idx, _cands = await asyncio.to_thread(_resolve_files_deep, path, sess, root_index)
        else:
            target, roots, idx = _resolve_files_target(path, sess, root_index)
    except HTTPException as exc:
        return _friendly_file_error(request, path, exc.status_code, str(exc.detail))
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote
    rel = _files_rel_in_root(target, roots[idx])
    rel_q = quote(rel, safe='/')
    sess_q = quote(sess or '', safe='')
    ri_q = str(idx)
    logger.info(f"FILE 打开: {path} session={sess}")
    if os.path.isdir(target):
        return RedirectResponse(
            url=f"../../files?path={rel_q}&root_index={ri_q}&session={sess_q}", status_code=302)
    if os.path.isfile(target):
        return RedirectResponse(
            url=f"../../api/files/download?path={rel_q}&root_index={ri_q}&session={sess_q}", status_code=302)
    return _friendly_file_error(request, path, 404, f"Not found: {path}")


# ── Upload API: 上传文件至 .xkagent/files/ ──
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


@app.get("/api/files/roots")
async def api_files_roots(session: str = Query(None, description="session 名（缺省回退当前会话）")):
    """返回 files 允许根集合（workdir + 全局挂载 + 该 session 动态挂载），供前端根选择器。"""
    sess = _checked_session_soft(session) or _get_current_session()
    return {"roots": [{"index": i, "label": os.path.basename(r) or r} for i, r in enumerate(_files_allowed_roots(sess))],
            "session": sess}


@app.get("/api/files/resolve")
async def api_files_resolve(ref: str = Query(..., description="LLM 给出的路径片段（绝对/相对/裸文件名）"),
                            root_index: int = Query(0, description="优先尝试的允许根索引"),
                            session: str = Query(None, description="session 名（缺省回退当前会话）"),
                            limit: int = Query(10, ge=1, le=50, description="最多返回候选数")):
    """把 LLM 回复中的路径片段解析为真实文件（多根/前缀补全/按名搜索），供前端定位。

    2026-09-09 新增：未命中返回 404 + 候选列表（可能为空）。
    """
    from fastapi.responses import JSONResponse
    sess = _checked_session_soft(session) or _get_current_session()
    roots = _files_allowed_roots(sess)
    if not roots:
        raise HTTPException(404, "No allowed roots")
    try:
        target, roots2, idx, hits = await asyncio.to_thread(_resolve_files_deep, ref, sess, root_index)
    except HTTPException:
        hits = await asyncio.to_thread(_search_file_in_roots, ref, roots, sess, limit)
        return JSONResponse(status_code=404, content={
            "ok": False, "ref": ref, "resolved": None,
            "candidates": [_files_candidate_dict(h, roots) for h in hits[:limit]],
        })
    rel = _files_rel_in_root(target, roots2[idx])
    resolved = {"path": rel, "root_index": idx, "name": os.path.basename(target)}
    cands = [_files_candidate_dict(h, roots2) for h in hits][:limit]
    if not any(c["path"] == rel and c["root_index"] == idx for c in cands):
        cands.insert(0, resolved)
    return {"ok": True, "ref": ref, "resolved": resolved, "candidates": cands[:limit]}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), session: str = Query(None)):
    """上传文件至 <workdir>/.xkagent/files/，返回文件/图片路径。

    设计考虑：文件名 basename 净化防路径穿越；重名加时间戳前缀去重；
    流式分块写入（1MB）防大文件整读内存；auth middleware 自动保护该路由。
    """
    sess = _checked_session_soft(session) or _get_current_session()
    base = os.path.realpath(_resolve_workdir(sess))
    files_dir = os.path.join(base, _config.DATA_DIR_NAME, "files")
    os.makedirs(files_dir, exist_ok=True)
    fname = os.path.basename(file.filename or "upload.bin") or "upload.bin"
    safe_name = fname
    target = os.path.join(files_dir, safe_name)
    if os.path.exists(target):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = f"{ts}_{fname}"
        target = os.path.join(files_dir, safe_name)
    total = 0
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
                out.write(chunk)
    except Exception:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    finally:
        await file.close()
    ext = os.path.splitext(safe_name)[1].lower()
    is_image = ext in IMAGE_EXTS
    rel = os.path.join(_config.DATA_DIR_NAME, "files", safe_name).replace(os.sep, "/")
    from urllib.parse import quote
    url = f"api/files/download?path={quote(rel, safe='/')}&root_index=0&session={quote(sess or '', safe='')}"
    logger.info(f"UPLOAD: {rel} session={sess} ({os.path.getsize(target)} bytes)")
    return {
        "ok": True,
        "filename": safe_name,
        "path": rel,
        "url": url,
        "is_image": is_image,
        "size": os.path.getsize(target),
    }


@app.get("/api/skills")
async def api_list_skills(session: str = Query(None)):
    """List available skills."""
    from codes.skill import SkillLoader
    token = None
    try:
        effective = _checked_session(session) or _get_current_session()
        if effective:
            _context, token = _config.activate_session(effective, ensure=False)
        return {"skills": SkillLoader.list_skills()}
    finally:
        if token is not None:
            _config.reset_session(token)


@app.post("/api/chat")
async def api_chat(data: dict):
    """Non-streaming chat (对齐 repl: 累计 stats 事件的真实 token)."""
    logger.info(f"API chat 请求: session={data.get('session', '?')}")
    text = data.get("text", "")
    session = data.get("session")
    _log("CHAT", f"User (REST): {text[:200]}{'...' if len(text) > 200 else ''}")
    agent, target = await asyncio.to_thread(_ensure_agent_running, session)
    agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
    if not target:
        raise HTTPException(503, "No agent session active")
    subscription = agent.subscribe(target, maxsize=2048)
    if subscription is None:
        raise HTTPException(503, "Agent event stream unavailable")
    if not agent.send_input(text, session=target):
        agent.unsubscribe(subscription)
        raise HTTPException(503, "Agent session unavailable")
    result = ""
    blocked_reason = ""  # T4: 观察者模式拒绝输入时记录原因
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0  # B3 修复（2026-08-22）：与 WS 路径一致，累计 reasoning
    deadline = time.time() + 300.0
    try:
        while time.time() < deadline:
            evt = await asyncio.to_thread(agent.read_output_for, target, 1.0, subscription)
            if evt is None:
                continue
            if evt.get("type") == "_turn_end":
                break
            if evt.get("type") == "blocked":
                blocked_reason = evt.get("reason", "session 已被其他进程占用")
            if evt.get("type") == "text":
                result += evt.get("data", "")
            elif evt.get("type") == "stats":
                prompt_tokens += evt.get("prompt_tokens", 0)
                completion_tokens += evt.get("completion_tokens", 0)
                reasoning_tokens += evt.get("reasoning_tokens", 0)
    finally:
        agent.unsubscribe(subscription)
    _log("CHAT", f"Assistant (REST): {result[:500]}{'...' if len(result) > 500 else ''}")
    if blocked_reason:
        # T4: 观察者只读 → 返回 blocked 提示（而非空 response）
        return {
            "blocked": True,
            "reason": blocked_reason,
            "session": target or "",
        }
    return {
        "response": result,
        "session": target or "",
        "stats": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
        },
    }


@app.get("/api/chat/queue")
async def api_chat_queue(session: str = Query(None)):
    """排队消息快照：qid/text/position，供前端刷新/切会话后重建排队视图与取消入口。"""
    target = session or _get_current_session()
    items = await asyncio.to_thread(_chat_queue_snapshot, target)
    return {"session": target or "", "items": items}



@app.get("/api/mode")
async def api_get_mode(session: str = Query(None)):
    """Get mode for the requested session without stealing global focus."""
    mgr, sess = await asyncio.to_thread(_ensure_agent_running, session)
    event = await asyncio.to_thread(mgr.send_command_wait, "get_info", None, sess, 1.0)
    if event and isinstance(event.get("data"), dict):
        return {"mode": event["data"].get("mode", "plan")}
    return {"mode": _ui_cache(sess).get("mode", "plan")}




async def _apply_control(agent: AgentManager, command: str, args: dict,
                         cache_attr: str, info_key: str,
                         session: str | None = None):
    """等待控制命令真实结果后再更新 Web 缓存，避免乐观状态漂移。"""
    target = session or agent.focus
    event = await asyncio.to_thread(agent.send_command_wait, command, args, target, 2.0)
    if event and event.get("ok", True):
        value = event.get("data")
        if value is not None and target:
            if target == agent.focus:
                setattr(agent, cache_attr, value)
            if info_key == "mode":
                _cache_session_ui(target, {"mode": value})
        return value
    info = await asyncio.to_thread(_get_session_info, agent, target, 1.0) if target else None
    if info and info_key in info and target and target == agent.focus:
        setattr(agent, cache_attr, info[info_key])
    if info and info_key in info:
        return info[info_key]
    return getattr(agent, cache_attr, None)




@app.post("/api/mode")
async def api_set_mode(data: dict):
    """Set or toggle mode."""
    session = data.get("session")
    agent, target = await asyncio.to_thread(_ensure_agent_running, session)
    agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
    mode = data.get("mode")
    if mode and mode in ("plan", "build", "build-unsafe"):
        new_mode = mode
    else:
        new_mode = MODE_CYCLE.get(_session_mode(agent, target), "plan")
    new_mode = await _apply_control(agent, "set_mode", {"mode": new_mode}, "_focus_mode", "mode", target)
    logger.info(f"MODE 切换: session={target} -> {new_mode}")
    return {"mode": new_mode or getattr(agent, "_focus_mode", "plan")}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebSocket — Streaming chat
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.websocket("/ws/{session}")
async def websocket_chat(ws: WebSocket, session: str):
    logger.info(f"WebSocket 连接: session={session}")
    # WebSocket auth check
    if _hashed_password:
        token = ws.cookies.get(_AUTH_COOKIE_NAME)
        if not token or _validate_token(token) != _auth_username:
            await ws.close(code=4001, reason="Unauthorized")
            return
    try:
        _checked_session(session)
    except HTTPException as e:
        await ws.close(code=4002, reason=str(e.detail)[:120])
        return
    await ws.accept()
    _, sess = await asyncio.to_thread(_ensure_agent_running, session)
    await _ws_loop(ws, _agent_manager, ws_session=sess)


@app.websocket("/ws")
async def websocket_chat_default(ws: WebSocket):
    # WebSocket auth check
    if _hashed_password:
        token = ws.cookies.get(_AUTH_COOKIE_NAME)
        if not token or _validate_token(token) != _auth_username:
            await ws.close(code=4001, reason="Unauthorized")
            return
    await ws.accept()
    await _ws_loop(ws, _agent_manager, ws_session=_get_current_session())


async def _ws_loop(ws: WebSocket, agent: AgentManager, ws_session: str | None = None):
    logger.info("WebSocket 循环开始")
    """WebSocket message loop."""
    send_lock = asyncio.Lock()
    chat_tasks: dict[str, asyncio.Task] = {}

    async def _send(data: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(data))

    def _ws_target_session(mgr: AgentManager, msg: dict | None = None) -> str | None:
        if msg:
            explicit = (msg.get("session") or "").strip()
            if explicit:
                return explicit
        return _get_current_session() or mgr.focus

    async def _target_busy(mgr: AgentManager, session: str | None) -> bool:
        """指定 session 是否忙（turn_active 或 WS 仍在转发）。"""
        if not session:
            return False
        busy = await asyncio.to_thread(_session_busy, mgr, session)
        return busy or _chat_forward_busy(chat_tasks, session)

    # 2026-08-30: 连接建立时若该 session 有残留排队消息且空闲 → 自动续跑
    # （覆盖"链式任务随旧连接断开而中止"的场景：刷新/重连后队列继续被消费）
    _boot_session = ws_session or _get_current_session() or agent.focus
    if _boot_session and _chat_queue_size(_boot_session) > 0:
        try:
            # 稍等前端完成首屏历史渲染，避免 chat_dequeued 补渲染的气泡被 loadSessionMessages 清空
            await asyncio.sleep(2.0)
            _boot_busy = await asyncio.to_thread(_session_busy, agent, _boot_session)
            if not _boot_busy:
                _boot_item = await asyncio.to_thread(_pop_chat_queue, _boot_session)
                if _boot_item is not None:
                    await _send({"type": "chat_dequeued",
                                 "data": {"qid": _boot_item["qid"], "text": _boot_item["text"]},
                                 "session": _boot_session})
                    _register_chat_task(chat_tasks, _boot_session, asyncio.create_task(
                        _handle_chat_chain(ws, agent, _boot_item["text"], send_lock, _boot_session)))
                    _log("CHAT", f"chat queue auto-drain: session={_boot_session}")
        except Exception:
            logger.exception("chat queue auto-drain failed: session=%s", _boot_session)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send({"type": "error", "data": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "chat")

            if msg_type == "chat":
                text = msg.get("text", "")
                if not text:
                    continue
                target = _ws_target_session(agent, msg)
                agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
                if not target:
                    await _send({"type": "error", "data": "No agent session active."})
                    continue
                if await _target_busy(agent, target):
                    # 2026-08-30: busy 不再丢弃消息 → 入会话级 FIFO 队列，回合结束后链式续跑
                    try:
                        qitem = await asyncio.to_thread(_enqueue_chat, target, text)
                    except ValueError as e:
                        # 队列段错误走 info：error 分支会封板流式文本段并复位按钮，干扰进行中的回合
                        await _send({"type": "info", "data": str(e), "session": target})
                        continue
                    await _send({"type": "chat_queued",
                                 "data": {"qid": qitem["qid"], "position": qitem["position"],
                                          "text": text},
                                 "session": target})
                    _log("CHAT", f"Message queued: session={target} position={qitem['position']}")
                    continue
                # 空闲但队列非空（重连/刷新残留）→ 新消息也入队，从队头按 FIFO 续跑
                if _chat_queue_size(target) > 0:
                    try:
                        await asyncio.to_thread(_enqueue_chat, target, text)
                    except ValueError as e:
                        await _send({"type": "info", "data": str(e), "session": target})
                        continue
                    _fitem = await asyncio.to_thread(_pop_chat_queue, target)
                    if _fitem is None:
                        continue   # 队列已被其他连接的链式任务接管（FIFO 保持）
                    first = _fitem["text"]
                else:
                    first = text
                await asyncio.to_thread(_drain_output_until_turn_end, agent, target)
                task = asyncio.create_task(
                    _handle_chat_chain(ws, agent, first, send_lock, target)
                )
                _register_chat_task(chat_tasks, target, task)

            elif msg_type == "chat_cancel":
                # 2026-08-30(v1.1): 取消单条排队消息（qid 来自 chat_queued 事件 / 队列快照）
                target = _ws_target_session(agent, msg)
                qid = (msg.get("qid") or "").strip()
                if not target or not qid:
                    await _send({"type": "info", "data": "chat_cancel 缺少 qid/session",
                                 "session": target or ""})
                    continue
                cres = await asyncio.to_thread(_cancel_chat_item, target, qid)
                cok = bool(cres[0])
                ctext = cres[1]
                if cok:
                    await _send({"type": "chat_cancelled",
                                 "data": {"qid": qid, "text": ctext},
                                 "session": target})
                    _log("CHAT", f"Queued message cancelled: session={target} qid={qid[:8]}")
                else:
                    await _send({"type": "info",
                                 "data": "该消息已开始处理或不在队列中，无法取消",
                                 "session": target})

            elif msg_type == "chat_queue_clear":
                # 2026-08-30(v1.1): 清空排队队列但不停当前回合（与 interrupt 的 Stop 语义区分）
                target = _ws_target_session(agent, msg)
                n = await asyncio.to_thread(_clear_chat_queue, target) if target else 0
                if n:
                    await _send({"type": "chat_queue_cleared", "data": {"count": n},
                                 "session": target})
                    _log("CHAT", f"chat queue cleared: session={target} count={n}")
                else:
                    await _send({"type": "info", "data": "当前没有排队消息",
                                 "session": target or ""})

            elif msg_type == "rerun":
                # 从指定 user 消息重新开始：截断历史后复用普通 chat 的流式执行链路。
                target = _ws_target_session(agent, msg)
                if not target or not session_exists(target):
                    await _send({"type": "error", "data": "Session not found",
                                 "session": target or ""})
                    continue
                agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
                if await _target_busy(agent, target):
                    await _send({"type": "error",
                                 "data": "⏳ Agent 正在处理上一条消息，请稍候",
                                 "session": target})
                    continue

                raw_message_id = msg.get("message_id")
                try:
                    if isinstance(raw_message_id, bool):
                        raise ValueError
                    message_id = int(raw_message_id)
                    if message_id <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    await _send({"type": "error",
                                 "data": "message_id must be a positive integer",
                                 "session": target})
                    continue

                # get_info 是现有锁状态查询；观察者模式不能改写共享历史。
                info_evt = await asyncio.to_thread(
                    agent.send_command_wait, "get_info", None, target, 2.0)
                info = info_evt.get("data") if isinstance(info_evt, dict) else None
                if not isinstance(info, dict):
                    await _send({"type": "error",
                                 "data": "无法确认 session 锁状态，请稍后重试",
                                 "session": target})
                    continue
                if info.get("is_observing") or not info.get("is_locked", True):
                    await _send({"type": "error",
                                 "data": "session 当前为只读/未持有写锁，无法重新生成",
                                 "session": target})
                    continue

                try:
                    ok, detail, replay = await asyncio.to_thread(
                        prepare_rerun, target, message_id)
                except Exception as e:
                    logger.exception("SESSION rerun 准备失败: %s", e)
                    await _send({"type": "error", "data": f"Rerun failed: {e}",
                                 "session": target})
                    continue
                if not ok or not replay:
                    await _send({"type": "error", "data": detail,
                                 "session": target})
                    continue

                _sessions_cache.clear()
                _stats_cache.clear()
                # 2026-08-30: rerun 物理截断历史 → 排队消息基于旧分支，语义已失效，一并清空
                _rq_cleared = _clear_chat_queue(target)
                if _rq_cleared:
                    await _send({"type": "chat_queue_cleared", "data": {"count": _rq_cleared},
                                 "session": target})
                logger.info("SESSION rerun: %s from message %s (removed=%s, queue_cleared=%s)",
                            target, message_id, replay.get("removed", 0), _rq_cleared)
                # 前端先清空旧分支；新的 user 消息由 data.text 显示，随后 DB 正常落库。
                await _send({
                    "type": "history_reset",
                    "data": {
                        "message_id": message_id,
                        "last_id": replay.get("last_id", 0),
                        "removed": replay.get("removed", 0),
                        "text": replay.get("text", ""),
                    },
                    "session": target,
                })
                await asyncio.to_thread(_drain_output_until_turn_end, agent, target)
                task = asyncio.create_task(
                    _handle_chat_ws(ws, agent, replay["text"], send_lock, target)
                )
                _register_chat_task(chat_tasks, target, task)

            elif msg_type == "mode":
                target = _ws_target_session(agent, msg)
                agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
                mode = msg.get("mode")
                if mode and mode in ("plan", "build", "build-unsafe"):
                    new_mode = mode
                else:
                    new_mode = MODE_CYCLE.get(_session_mode(agent, target), "plan")
                new_mode = await _apply_control(agent, "set_mode", {"mode": new_mode}, "_focus_mode", "mode", target)
                _log("SYSTEM", f"Mode ({target}): {new_mode}")
                await _send({"type": "mode_changed", "data": new_mode, "session": target or ""})

            elif msg_type == "mode_toggle":
                target = _ws_target_session(agent, msg)
                agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
                new_mode = MODE_CYCLE.get(_session_mode(agent, target), "plan")
                new_mode = await _apply_control(agent, "set_mode", {"mode": new_mode}, "_focus_mode", "mode", target)
                _log("SYSTEM", f"Mode ({target}): {new_mode} (toggled)")
                await _send({"type": "mode_changed", "data": new_mode, "session": target or ""})


            elif msg_type == "command":
                cmd = msg.get("cmd", "")
                _log("SYSTEM", f"Command: {cmd}")
                # ── /compact: 走 chat 流式路径（复用 run_stream 流式压缩），
                #    而非旧同步命令轮询（曾导致 5s 假超时 + 无流式反馈）。
                #    2026-09-02: 改走 _handle_chat_chain——compact 也是一回合，
                #    结束后自动续跑排队队列（修复 /compact 完成后队列滞留）──
                if cmd.strip().lower() == "/compact":
                    target = _ws_target_session(agent, msg)
                    agent = await asyncio.to_thread(_ensure_session_agent, agent, target)
                    if await _target_busy(agent, target):
                        await _send({"type": "info", "data": "⏳ Agent 正在处理上一条消息，请稍候",
                                     "session": target or ""})
                        continue
                    if not target:
                        await _send({"type": "error", "data": "No agent session active."})
                        continue
                    await asyncio.to_thread(_drain_output_until_turn_end, agent, target)
                    task = asyncio.create_task(
                        _handle_chat_chain(ws, agent, COMPACT_MARKER + COMPACT_PROMPT, send_lock, target)
                    )
                    _register_chat_task(chat_tasks, target, task)
                    continue
                before = _get_current_session()
                cmd_sess = _ws_target_session(agent, msg)
                result = await asyncio.to_thread(_execute_command, agent, cmd, cmd_sess)
                _record_web_cmd_history(cmd, result, cmd_sess)
                await _send({
                    "type": "command_result",
                    "data": result,
                    # A3 修复（2026-08-22）：注入 session 标识，前端据此分流——
                    # 切走后到达的命令结果不再污染当前视图（handleBackgroundEvent 丢弃非当前 session 事件）
                    "session": cmd_sess or "",
                })
                # F4: /session 命令切换会话后补发 session_switched，
                # 让前端同步更新 session-name（command_result 仅展示文本，
                # 无刷新路径，否则显示停留在旧会话名）。
                if _get_current_session() != before:
                    cur = _get_current_session()
                    info = await asyncio.to_thread(_get_session_info, agent, cur, 1.0) if cur else None
                    if info and cur == agent.focus:
                        agent._focus_mode = info.get("mode", agent._focus_mode)
                        agent._focus_observing = info.get("is_observing", False)
                        agent._focus_holder_info = info.get("holder_info", None)
                    msg_count = info.get("msg_count", 0) if info else 0
                    _stats_cache.clear()
                    await _send({
                        "type": "session_switched",
                        "data": {
                            "session": cur,
                            "messages": msg_count,
                            "busy": _session_ws_busy(agent, cur, chat_tasks),
                        },
                    })

            elif msg_type == "switch_session":
                name = msg.get("session", "")
                if session_exists(name):
                    # P2: LLM 执行中切 session → 仅取消 WS 转发任务，不中断 agent 后台执行。
                    # 语义（2026-08-10 修订2，对齐 repl Ctrl+N/P）：旧 agent 线程继续
                    # 跑完整个回合（run_forever 单线程消费 input_queue），事件落 DB；
                    # 切回该 session 时前端从 DB 增量拉取可见完整结果。
                    # 卡死残留风险已由 llm.py 2026-08-10 读超时修复兜底；
                    # 用户仍可用 interrupt 按钮手动强杀旧回合。
                    # C1：切走保留旧 session 的后台转发任务；busy 仅看目标 session。
                    for sess, task in list(chat_tasks.items()):
                        if sess != name and not task.done():
                            _log("CHAT", f"切走保留后台转发: session={sess} task={task}")
                    _log("SYSTEM", f"Session switch: {name}")
                    try:
                        mgr = await asyncio.to_thread(_focus_session, name)
                    except Exception as _sw_err:
                        # 2026-08-23: 切换目标 session 的 DB 不可用（IOERR 冷却/存储故障）
                        # → 立即通知前端（error 事件会 clearSessionSwitchPending），
                        # 避免前端 8s "切换会话超时"；不阻塞 WS 处理循环。
                        _log("SYSTEM", f"Session switch 失败: {name} -> {_sw_err}")
                        await _send({"type": "error",
                                     "data": f"会话 {name} 数据库暂不可用（存储 IO 故障冷却中），请稍后重试"})
                        return
                    agent = mgr
                    # FIX: 移除新 focus 的 drain（队列理论干净，drain 阻塞 3s 是主要瓶颈）
                    # FIX: 移除重复的 get_focus_info（focus_session 内部已调用并更新缓存）
                    # focus_session 已同步 mode/锁状态/技能选择到 mgr 缓存
                    # msg_count: focus_session 不存储 msg_count，发送 0（前端会从 server 重新加载消息）
                    msg_count = 0
                    _sessions_cache.clear()   # 对齐 REST 切换路径（L542）：清列表缓存，前端立即拿到新 current
                    _stats_cache.clear()   # P0: stats 缓存失效，避免前端 fetchStatus 命中旧 session 数据
                    await _send({
                        "type": "session_switched",
                        "data": {
                            "session": name,
                            "messages": msg_count,
                            "busy": _session_ws_busy(mgr, name, chat_tasks),
                        },
                    })
                else:
                    await _send({
                        "type": "error",
                        "data": f"Session '{name}' not found",
                    })
            elif msg_type == "interrupt":
                target = (msg.get("session") or "").strip() or _get_current_session() or agent.focus
                stopped = False
                if target and agent.request_interrupt(target):
                    stopped = True
                had_forward = bool(target and _chat_forward_busy(chat_tasks, target))
                # 2026-08-30: 停止语义 = 中断当前回合 + 清空该 session 排队队列
                # （先 clear 后 cancel：_handle_chat_ws 会吞掉 CancelledError，若先 cancel
                #   链式任务可能在吞异常后继续 pop；先清空则 pop 必为 None，循环自然退出）
                _cleared = _clear_chat_queue(target)
                if _cleared:
                    await _send({"type": "chat_queue_cleared", "data": {"count": _cleared},
                                 "session": target})
                if target and (had_forward or stopped):
                    await _cancel_chat_task(chat_tasks, target)
                    await asyncio.to_thread(_drain_output_until_turn_end, agent, target, 3.0)
                note = "⏹ 已停止" if stopped else "⚠️ 无活跃回合可中断"
                if _cleared:
                    note += f"，已清空 {_cleared} 条排队消息"
                payload = {"type": "system", "data": note}
                if target:
                    payload["session"] = target
                await _send(payload)
                if stopped or had_forward:
                    done_payload = {"type": "done", "data": ""}
                    if target:
                        done_payload["session"] = target
                    await _send(done_payload)
    finally:
        logger.info(f"WS 断开: session={agent.focus}")
        for sess in list(chat_tasks.keys()):
            await _cancel_chat_task(chat_tasks, sess)


async def _safe_send(ws: WebSocket, data: dict) -> bool:
    """Send JSON to WebSocket, return False if client disconnected."""
    try:
        # 10s 写超时：浏览器标签页休眠/网络中断时 ws.send_text 可能长期阻塞，
        # 导致回合订阅消费停滞、事件队列溢出（subscriber overflow 根因之一）。
        # 2026-09-04 修复（Task-406 卡死根因）：wait_for 直接包裹 send_text 时，
        # 超时后需取消并等待底层任务结束；若 websockets drain 吞掉 CancelledError
        # 继续阻塞，wait_for 会永久挂起（10s 超时形同虚设，转发任务卡死 → busy 恒真）。
        # shield 隔离后：超时只取消 shield 协程，底层任务泄漏但 _safe_send 必返回。
        await asyncio.wait_for(asyncio.shield(ws.send_text(json.dumps(data))), timeout=10.0)
        return True
    except (WebSocketDisconnect, asyncio.TimeoutError, ConnectionError, OSError):
        return False

def _drain_output_until_turn_end(mgr: AgentManager, session: str | None = None,
                                 total_timeout: float = 3.0) -> None:
    """等待指定 session 的回合结束，不消费任何订阅者的流式事件。"""
    session = session or mgr.focus
    if session:
        mgr.wait_for_turn_end(session=session, timeout=total_timeout)


def _chat_forward_busy(chat_tasks: dict[str, asyncio.Task], session: str | None) -> bool:
    """该 session 是否仍有 WS 转发任务在跑（与 agent turn_active 独立）。"""
    if not session:
        return False
    task = chat_tasks.get(session)
    return task is not None and not task.done()


def _session_ws_busy(mgr: AgentManager, session: str | None,
                     chat_tasks: dict[str, asyncio.Task] | None = None) -> bool:
    """session 是否忙：agent 回合进行中或 WS 仍在转发（与 _focus_busy 同口径）。"""
    if not session:
        return False
    if _session_busy(mgr, session):
        return True
    return chat_tasks is not None and _chat_forward_busy(chat_tasks, session)


# 2026-09-04 修复（busy 口径对齐）：跨 WS 连接的活跃转发任务计数。
# chat_tasks 是 per-connection 局部变量，/api/sessions 无法访问；
# 用模块级计数汇总，供前端 busy 判定（转发任务卡死时 turn_active 已复位
# false 但任务未结束 → 前端需显示 busy，避免"静默排队"）。
_chat_forward_count: dict[str, int] = {}
_chat_forward_lock = threading.Lock()


def _register_chat_task(chat_tasks: dict[str, asyncio.Task], session: str,
                        task: asyncio.Task) -> None:
    """登记 per-session 转发任务，完成后自动清理。"""
    def _cleanup(t: asyncio.Task) -> None:
        if chat_tasks.get(session) is t:
            chat_tasks.pop(session, None)
        with _chat_forward_lock:
            _n = _chat_forward_count.get(session, 0) - 1
            if _n <= 0:
                _chat_forward_count.pop(session, None)
            else:
                _chat_forward_count[session] = _n
    with _chat_forward_lock:
        _chat_forward_count[session] = _chat_forward_count.get(session, 0) + 1
    chat_tasks[session] = task
    task.add_done_callback(_cleanup)


def _chat_forward_busy_global(session: str | None) -> bool:
    """跨连接汇总：该 session 是否有活跃 WS 转发任务（供 /api/sessions busy 判定）。"""
    if not session:
        return False
    with _chat_forward_lock:
        return _chat_forward_count.get(session, 0) > 0


async def _cancel_chat_task(chat_tasks: dict[str, asyncio.Task],
                            session: str | None) -> None:
    """取消并 await 指定 session 的 WS 转发任务。"""
    if not session:
        return
    task = chat_tasks.pop(session, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _handle_chat_chain(ws: WebSocket, mgr: AgentManager, first_text: str,
                             send_lock: asyncio.Lock, session: str | None = None) -> None:
    """链式执行：当前回合结束后自动消费该 session 的排队消息（FIFO）。

    2026-08-30: 配合 _chat_queues 实现"busy 时排队、空闲后依次处理"。
    - 全程占用 chat_tasks[session]（_chat_forward_busy 恒真）→ 链式期间新消息继续入队；
    - 每条排队消息都是独立完整回合（独立技能选择/工具调用/落库），非拼接 prompt；
    - _handle_chat_ws 先 send_input 再转发：ws 断开仅影响实时转发（早退），
      回合仍由 agent 执行并落库（P2 语义），故队列继续消费不丢消息；
    - CancelledError 向上传播（interrupt 取消链式任务 → 队列已由 interrupt 分支先行清空）。
    """
    target = session
    text = first_text
    while text is not None:
        try:
            await _handle_chat_ws(ws, mgr, text, send_lock, target)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat chain turn failed: session=%s", target)
        # 下一回合前确认 session 未被其他连接的回合抢占（防御：避免 send_input
        # 在他人回合进行中覆盖 active_turn_id）。短暂 busy 视为回合收尾时序，重试等待。
        _waited = 0.0
        while target and await asyncio.to_thread(_session_busy, mgr, target) and _waited < 2.0:
            await asyncio.sleep(0.3)
            _waited += 0.3
        if target and await asyncio.to_thread(_session_busy, mgr, target):
            _log("CHAT", f"chat chain 停止续跑（session 仍被占用，队列保留）: session={target}")
            # 2026-09-02(A2): 滞留不再静默——队列非空时通知前端，避免用户误判卡死
            # 后用 Stop 清队（interrupt 会丢弃排队消息）。恢复：发任意新消息（chat 分支
            # FIFO 续跑）或刷新页面（WS 连接建立 auto-drain）。
            if _chat_queue_size(target) > 0:
                try:
                    async with send_lock:
                        await ws.send_text(json.dumps({
                            "type": "info",
                            "data": "⏳ 会话仍被占用，已暂停排队消息续跑（队列已保留）；"
                                    "发送任意新消息或刷新页面可恢复",
                            "session": target or ""}))
                except Exception:
                    logger.debug("chain 滞留通知发送失败（ws 可能已断开）")
            break
        nitem = await asyncio.to_thread(_pop_chat_queue, target)
        if nitem is None:
            break
        text = nitem["text"]
        try:
            async with send_lock:
                await ws.send_text(json.dumps({
                    "type": "chat_dequeued",
                    "data": {"qid": nitem["qid"], "text": text},
                    "session": target or ""}))
        except Exception:
            logger.debug("chat_dequeued 发送失败（ws 可能已断开），继续处理队列")
        _log("CHAT", f"Queued message dequeued: session={target}")


# ── Logging ──
def _log(level: str, msg: str):
    """Print timestamped log to stderr."""
    _now = datetime.now().strftime("%H:%M:%S")
    print(f"  [{_now}] [{level}] {msg}", file=sys.stderr, flush=True)
    logger.info(f"[{level}] {msg}")


async def _handle_chat_ws(ws: WebSocket, mgr: AgentManager, text: str, send_lock: asyncio.Lock,
                          session: str | None = None):
    """Handle a chat message over WebSocket with streaming (via manager).

    对齐 repl._handle_event 的事件集：补全 stats 成本、tool 字段、
    skill_req、_sync_update；内部事件（_cmd_result/_lock_status）不渲染。

    P2 修复：
    - 本函数作为后台 asyncio 任务运行（_ws_loop 中 create_task），
      read_output 改用 asyncio.to_thread，避免同步阻塞事件循环。
    - target 在启动时捕获；C1 切走后仍按 target 读队列继续转发（非 focus）。
    - 收到取消（interrupt）时吞掉 CancelledError，不重发 done。
    """
    async def _send(data: dict) -> bool:
        # P2: 与主循环共享发送锁，避免后台任务与主循环并发写 ws
        # C1: 统一注入 session 标识，前端据此区分当前/后台 session 事件
        # （仅注入一次，避免嵌套 dict 重复污染）
        if "session" not in data:
            data = dict(data)
            data["session"] = target
        async with send_lock:
            return await _safe_send(ws, data)

    # F3: 捕获本回合所属 session（_ws_loop 在 create_task 前锁定，避免 switch 竞态）
    target = session or mgr.focus
    if target is None:
        await _send({"type": "error", "data": "No agent session active."})
        return
    _session_context, session_token = _config.activate_session(target, ensure=False)
    _log("CHAT", f"User: {text[:200]}{'...' if len(text) > 200 else ''}")
    subscription = mgr.subscribe(target, maxsize=4096)
    if subscription is None:
        _config.reset_session(session_token)
        await _send({"type": "error", "data": "Agent event stream unavailable."})
        return
    # Delegate to the manager's focused agent（显式 session=target，切 focus 后仍投递正确队列）
    if not mgr.send_input(text, session=target):
        mgr.unsubscribe(subscription)
        _config.reset_session(session_token)
        await _send({"type": "error", "data": "No agent session active."})
        return
    reply_parts: list[str] = []
    # 2026-09-04 修复：转发循环空闲兜底——subscription 未关闭但长时间无事件
    # （_safe_send 修复前 blocked 分支挂起导致 unsubscribe 未执行）→ 主动收尾，
    # 避免 _chat_forward_busy 恒真拖死队列消费。阈值保守（正常回合有流式事件）。
    _idle_deadline = time.monotonic() + _CHAT_FORWARD_IDLE_TIMEOUT
    try:
        while True:
            # C1（2026-08-21）：focus 守卫改为"后台转发"而非"中止"。
            # 切走后 mgr.focus != target，但本回合仍按 target 读队列继续转发，
            # 事件附带 session 标识，前端对非当前 session 只更新缓存不渲染 DOM。
            # 切回 target 时前端缓存已含后台事件，实时流无缝继续。
            # （原实现：focus 变化即 return，切回后无恢复机制 = 实时渲染中断根因之一）
            # C1（2026-08-21）：按 target session 读队列，而非 focus。
            # 切走后 mgr.focus 已变化，read_output 会读错队列；read_output_for
            # 固定读本回合所属 session 的队列，实现"切走后后台继续接收"。
            event = await asyncio.to_thread(mgr.read_output_for, target, 0.2, subscription)
            if event is None:
                if subscription.closed:
                    await _send({"type": "error", "data": "Agent event stream ended unexpectedly."})
                    await _send({"type": "done", "data": ""})
                    return
                # 空闲兜底：subscription 未关闭但长时间无事件 → 强制收尾（防转发任务卡死残留）
                if time.monotonic() >= _idle_deadline:
                    _log("CHAT", f"回合转发空闲超时({int(_CHAT_FORWARD_IDLE_TIMEOUT)}s)，强制收尾: session={target}")
                    await _send({"type": "done", "data": ""})
                    return
                continue
            _idle_deadline = time.monotonic() + _CHAT_FORWARD_IDLE_TIMEOUT  # 有事件则重置空闲计时
            t = event.get("type", "")
            if t == "_dropped_events":
                await _send({"type": "stream_gap", "data": event.get("data", {})})
                await _send({"type": "system", "data": "⚠️ 事件消费者过慢，部分实时输出已丢弃；正在从数据库补全。"})
                continue

            if t == "thinking":
                if not await _send({"type": "thinking", "data": ""}):
                    return

            elif t == "thinking_content":
                # 重构：思考链内容（agent 从 llm reasoning chunk 累积而来）
                tdata = event.get("data", "")
                if tdata:
                    if not await _send({"type": "thinking_content", "data": tdata}):
                        return

            elif t == "text":
                data = event.get("data", "")
                if data:
                    reply_parts.append(data)
                    if not await _send({"type": "text", "data": data}):
                        return

            elif t == "tool_call":
                # 2026-08-11: pythonrt 传 .py 文件路径 → 预读文件内容供前端 markdown 渲染
                # （防穿越同 api_files_open：realpath 前缀校验；>512KB 不预读避免 WS 消息过大）
                _tc_name = event.get("name", "")
                _tc_args = event.get("args", {})
                _tc_data = {
                    "name": _tc_name,
                    "args": _tc_args,
                    "index": event.get("index", 0),
                    "total": event.get("total", 1),
                    "mode": event.get("mode", "plan"),
                }
                if _tc_name == "pythonrt" and isinstance(_tc_args, dict):
                    _fp = str(_tc_args.get("code_or_filepath") or "").strip()
                    if _fp.endswith(".py") and not _fp.startswith(("http://", "https://")):
                        _base = os.path.realpath(_resolve_workdir(target))
                        _full = os.path.realpath(os.path.join(_base, _fp))
                        if _full == _base or _full.startswith(_base + os.sep):
                            try:
                                if os.path.isfile(_full) and os.path.getsize(_full) <= 512 * 1024:
                                    with open(_full, encoding="utf-8", errors="replace") as _f:
                                        _tc_data["file_content"] = _f.read()
                                elif os.path.isfile(_full):
                                    _tc_data["file_content"] = "# 文件过大（%d bytes），未预读" % os.path.getsize(_full)
                            except OSError:
                                pass
                if not await _send({"type": "tool_call", "data": _tc_data}):
                    return

            elif t == "tool_progress":
                # 方案2: 工具执行进度——尽力而为转发（发送失败不中断回合，
                # tool_result 全量结果兜底），避免进度洪泛拖垮关键事件。
                try:
                    await _send({
                        "type": "tool_progress",
                        "data": {
                            "name": event.get("name", ""),
                            "line": event.get("line", ""),
                            "idx": event.get("idx", 0),
                            "stream": event.get("stream", "stdout"),
                        },
                    })
                except Exception:
                    pass

            elif t == "tool_result":
                if not await _send({
                    "type": "tool_result",
                    "data": {
                        "name": event.get("name", ""),
                        "exit_code": event.get("exit_code"),
                        "stdout": event.get("stdout", ""),
                        "stderr": event.get("stderr", ""),
                        "elapsed": event.get("elapsed", 0),
                        "error": event.get("error", ""),
                    },
                }):
                    return

            elif t == "stats":
                pt = event.get("prompt_tokens", 0)
                ct = event.get("completion_tokens", 0)
                rt = event.get("reasoning_tokens", 0)
                model = event.get("model", "")
                # 对齐 repl：估算成本
                try:
                    from codes.agent import _estimate_cost
                    cost = _estimate_cost(model, pt, ct)
                except Exception:
                    cost = 0.0
                if not await _send({
                    "type": "stats",
                    "data": {
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "reasoning_tokens": rt,
                        "cost": cost,
                        "model": model,
                    },
                }):
                    return

            elif t == "permission":
                # 方案B: 实时路径访问权限提示（对齐 suggested_skills 系统事件，拆行展示）
                pdata = event.get("data", "")
                if pdata:
                    if not await _send({"type": "system", "data": _format_permission_text(pdata)}):
                        return

            elif t == "suggested_skills":
                skills = event.get("skills", [])
                if skills:
                    if not await _send({"type": "system", "data": f"📋 建议技能: {', '.join(str(s) for s in skills)}"}):
                        return

            elif t == "recommended_info":
                items = event.get("items", [])
                if items:
                    lines = f"🧠 推荐信息: {len(items)} 条\n" + "\n".join(
                        f"[{it.get('scope','')}] {it.get('path','')} | {it.get('snippet','')}"
                        for it in items)
                    if not await _send({"type": "system", "data": lines}):
                        return

            elif t == "skill_req":
                # 对齐 repl：显示 Skill loaded
                sname = event.get("name", "")
                if sname:
                    if not await _send({"type": "system", "data": f"💡 Skill loaded: {sname}"}):
                        return

            elif t == "_sync_update":
                # 对齐 repl：打印同步消息
                msgs = event.get("messages", [])
                for m in msgs:
                    role = m.get("role", "?")
                    content = m.get("content", "") or ""
                    if role == "user":
                        parsed = parse_user_prefix(content)
                        if parsed:
                            content = parsed["body"]
                    prefix = "🧑" if role == "user" else "🤖" if role == "assistant" else "🔧"
                    if not await _send({"type": "system", "data": f"📥 {prefix} [sync][{role}] {content[:500]}"}):
                        return

            elif t == "blocked":
                # T7: 观察者模式拒绝输入（agent T2 gate）→ 前端系统消息，结束回合
                reason = event.get("reason", "session 已被其他进程占用")
                _log("CHAT", f"回合被拒(观察者只读): {reason}")
                await _send({"type": "system", "data": f"⚠️ [blocked] {reason}"})
                await _send({"type": "done", "data": ""})
                return

            elif t == "_lock_status":
                _cache_session_ui(target, {
                    "is_observing": event.get("is_observing", False),
                    "holder_info": event.get("holder_info"),
                })
                if _should_update_focus_cache(mgr, target):
                    mgr._focus_observing = event.get("is_observing", False)
                    mgr._focus_holder_info = event.get("holder_info", None)
                if not await _send({
                    "type": "lock_status_changed",
                    "data": {
                        "observing": event.get("is_observing", False),
                        "holder": event.get("holder_info", None),
                    },
                    "session": target,
                }):
                    return

            elif t == "_cmd_result":
                if isinstance(event.get("data"), dict) and target:
                    _cache_session_ui(target, event["data"])
                # 对齐 repl：内部状态事件不渲染，但回流真实状态到缓存
                # （FIX: 原实现 pass，get_info/set_mode 结果无法同步缓存，导致状态过期）
                if not _should_update_focus_cache(mgr, target):
                    pass
                else:
                    cmd = event.get("cmd")
                    data = event.get("data")
                    if cmd == "get_info" and isinstance(data, dict):
                        mgr._focus_mode = data.get("mode", mgr._focus_mode)
                        mgr._focus_observing = data.get("is_observing", False)
                        mgr._focus_holder_info = data.get("holder_info", None)
                    elif cmd == "set_mode":
                        if data:
                            mgr._focus_mode = data
                        elif event.get("ok") is False:
                            # 观察者拒绝写命令（无 data）：回读真实 mode，防缓存保留错误状态
                            info = await asyncio.to_thread(mgr.get_focus_info, 2.0)
                            if info:
                                mgr._focus_mode = info.get("mode", mgr._focus_mode)


            elif t == "clear_thinking":
                await _send({"type": "clear_thinking"})

            elif t == "info":
                # 2026-09-11: 非错误提示转发（对齐 repl.py 的 info 分支）——
                # 如 autocompact 自动压缩通知 / 上下文超限修剪提示（前端 addMsg('info') 渲染）
                await _send({"type": "info", "data": event.get("data", "")})

            elif t == "error":
                err_msg = event.get("data", "Unknown error")
                _log("CHAT", f"Error: {err_msg}")
                hint = friendly_error_hint(err_msg)
                if hint:
                    await _send({"type": "system", "data": hint})
                await _send({"type": "error", "data": err_msg})
                await _send({"type": "done", "data": ""})
                return
            elif t == "turn_end_by_tool":
                # 回合由 summary/exit 等工具结束：展示结束原因/结论（key），
                # 避免"像 error 一样直接结束"的观感。system 消息前端可见。
                _tname = event.get("name", "")
                _tkey = event.get("key", "")
                note = f"🔚 回合由 {_tname} 工具结束" + (f"：{_tkey}" if _tkey else "")
                if not await _send({"type": "system", "data": note}):
                    return

            elif t == "_turn_end":
                # 对齐 repl：回合结束（agent.run_forever 只发 _turn_end，不发 done）
                break
            elif t == "done":
                break
    except asyncio.CancelledError:
        # P2: 切 session 取消本任务：静默退出，不重发 done
        _log("CHAT", f"回合取消: {target}")
        return
    finally:
        full_reply = "".join(reply_parts)
        if full_reply:
            _log("CHAT", f"Assistant: {full_reply[:500]}{'...' if len(full_reply) > 500 else ''}")
        mgr.unsubscribe(subscription)
        _config.reset_session(session_token)

    # Send done signal
    await _send({"type": "done", "data": ""})

def _run_bash(cmd: str, cwd: str | None = None) -> str:
    """执行 bash 命令并返回输出（对齐 repl 的 !command 快捷方式）。"""
    if not cmd:
        return ""
    # workdir 目录失效（已删除/不可访问）时回退 None（继承 server cwd，保持原行为）
    if cwd and not os.path.isdir(cwd):
        cwd = None
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           errors="replace", timeout=30, cwd=cwd)
    except subprocess.TimeoutExpired:
        return "❌ Error: command timed out after 30s"
    except Exception as e:
        return f"❌ Error: {e}"
    parts = []
    if r.stdout:
        parts.append(r.stdout.rstrip())
    if r.stderr:
        parts.append("[stderr]\n" + r.stderr.rstrip())
    parts.append(f"└ exit: {r.returncode}")
    return "\n".join(parts)




_WEB_WRITE_CMD_PREFIXES = (
    "/clear", "/drop", "/compact", "/mount", "/unmount", "/mode", "/model",
    "/plan", "/build", "/build-unsafe", "/info", "/autocompactlimit",
    "/session add", "/session remove", "/session fork", "/session rename",
    "/session stop", "/session sync", "/session title",
    "/addinfo", "/rminfo",
    "/skill", "/validate", "/updateembedding",
)


def _web_observer_blocks(mgr: AgentManager, session: str | None, cmd: str) -> str | None:
    cmd = (cmd or "").strip()
    if not cmd.startswith("/"):
        return None
    low = cmd.lower()
    if not any(low == p or low.startswith(p + " ") for p in _WEB_WRITE_CMD_PREFIXES):
        return None
    if not session:
        return None
    evt = mgr.send_command_wait("get_info", session=session, timeout=2.0)
    if evt and isinstance(evt.get("data"), dict) and evt["data"].get("is_observing"):
        return "⚠️ session 已被其他进程占用（观察者只读模式），写命令已拒绝"
    return None


def _record_web_cmd_history(cmd: str, result: str, session: str | None = None) -> None:
    """将 web 端执行的 /xxx 或 !xxx 命令与回应写入 session DB（role='command'）。"""
    cmd = (cmd or "").strip()
    sess = session or _get_current_session()
    if not cmd or not sess:
        return
    try:
        from codes.history import add_command, get_conn
        add_command(
            get_conn(sess), cmd, result,
            kind="bang" if cmd.startswith("!") else "slash",
        )
    except Exception:
        logger.warning(f"记录 command 历史失败: {cmd!r}", exc_info=True)


def _web_cmd_context(mgr, session: str | None = None) -> CommandContext:
    """构造 Web 侧命令上下文（注入全局会话同步 / 服务器退出能力）。"""
    def _switch_hook(name: str) -> None:
        _set_current_session(name)

    def _exit_hook() -> str:
        close_web_server()
        return "👋 服务器已关闭"

    return CommandContext(
        exit_hook=_exit_hook,
        get_session=lambda: session or _get_current_session(),
        switch_session_hook=_switch_hook,
    )


def _execute_command(agent: AgentManager, cmd: str, session: str | None = None) -> str:
    """统一命令调度（codes.commands.dispatch），对齐 repl。"""
    cmd = cmd.strip()
    if not cmd:
        return ""
    sess = session or _get_current_session()
    if cmd.startswith("/"):
        block = _web_observer_blocks(agent, sess, cmd)
        if block:
            return block
    if cmd.startswith("!"):
        return _run_bash(cmd[1:].strip(), cwd=_resolve_workdir(sess))
    return dispatch(agent, cmd, ctx=_web_cmd_context(agent, sess))


# ── Static HTML pages (web_static/*.html, loaded via load_page) ──


@app.get("/login")
async def login_page():
    """Serve the login page."""
    return HTMLResponse(load_page("login.html"))


@app.get("/files")
async def files_page():
    """FTP 风格文件浏览器页面（新标签页打开，auth 保护）。"""
    return HTMLResponse(load_page("files.html"))

@app.get("/viewer.html")
async def viewer_page():
    """统一文件预览页（md 渲染 / pdf·html 嵌入 / 图片 / 文本，auth 保护）。"""
    return HTMLResponse(load_page("viewer.html"))


@app.get("/")
async def root():
    """Serve the single-page web interface."""
    return HTMLResponse(load_page("index.html"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def close_web_server():
    """关闭 Web 服务器 socket，释放端口（供 /restart 调用）。"""
    global _web_server
    srv = _web_server
    if srv is not None:
        _web_server = None
        try:
            srv.should_exit = True
            logger.info("Web server shutdown requested")
        except Exception as e:
            logger.warning(f"close_web_server 异常: {e}")
    # 2026-08-13: /exit 退出 Web 时兜底清理本进程持有的 mkdir 锁
    try:
        from codes.lock import cleanup_all as _cleanup_all
        _n = _cleanup_all()
        if _n:
            logger.info(f"close_web_server: 兜底清理 {_n} 个残留锁")
    except Exception:
        pass


def run_web(args):
    logger.info("Web 服务启动")

    global _hashed_password, _auth_username
    _auth_username = args.user
    if args.password:
        _hashed_password = _hash_password(args.password)
        print(f"  🔒 Auth enabled: user={_auth_username}, password=***")
    else:
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("Binding Web UI to a non-loopback host requires --password")
        print(f"  🔓 Auth disabled (no --password set)")

    # ── 默认 session：以最新 session 作为默认（对齐 repl --resume，问题1修复）──
    _set_current_session(_agent_manager.resolve_session())
    if _get_current_session():
        print(f"  💬 Default session: {_get_current_session()}")
        _agent_manager.prewarm(_get_current_session())

    import uvicorn
    print(f"  ⚡ XKAgent Web — http://{args.host}:{args.port}")
    print(f"  📐 Mode: plan (switch with mode button or /mode)")
    print(f"  💡 /help for commands  |  Tab toggles mode")
    print()
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    global _web_server
    _web_server = server
    try:
        server.run()
    finally:
        _web_server = None


# Web 统一由 codes.main / codes.web_main 调度；本模块不再保留失效的独立入口。
