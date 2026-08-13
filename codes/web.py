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
import json
import os
import re
import secrets
import subprocess
import sys
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
                       fork_session, rename_session,
                       sync_session, get_conn, get_chat_messages, parse_user_prefix,
                       get_messages_since, get_agent_state, get_token_state,
                       get_mount_state)
from codes.manager import AgentManager
from codes.commands import dispatch, CommandContext
from codes.llm import friendly_error_hint
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
        parsed = parse_user_prefix(content)
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
            req = (parsed.get("requirement") or "").strip()
            if req:
                # 对齐实时 skill_req 渲染：要求"用户要求调用X" → 💡 Skill loaded: X
                if req.startswith("用户要求调用"):
                    sys_lines.append(f"💡 Skill loaded: {req[len('用户要求调用'):]}")
                else:
                    sys_lines.append(f"要求: {req}")
            if sys_lines:
                msg["sysLines"] = sys_lines
            return msg
        c = content or ""
        return {"role": "user", "displayType": "user", "content": c}
    
    if role == "assistant":
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
_workdir: str | None = None
_current_session: str | None = None
# /api/session/stats TTL 缓存：前端高频轮询，避免每次阻塞 2-7 秒（log 中 7s 慢请求即由此而来）
_STATS_TTL = 8.0   # 5→8s：前端轮询间隔常 >5s 导致缓存频繁失效，每次落入 2~4s 慢路径
_stats_cache: dict[str, tuple[float, dict]] = {}
_SESSIONS_TTL = 8.0   # sessions 列表轮询缓存：前端每 10s 轮询，TTL 8s 保证命中缓存，避免反复扫描目录+锁检查（IO 慢）
_sessions_cache: dict[str, tuple[float, dict]] = {}

# ── 崩溃通知：记录 agent 崩溃（自动重启前捕获），供前端提示用户感知 ──
# 崩溃后自动重启会替换 _AgentProcess 导致 error 丢失，因此必须先记录再重启。
# 前端 ack 后 acked=True，避免 2s 轮询反复弹出；未 ack 前持续返回。
_crash_notices: dict[str, dict] = {}   # session -> {time, error, seq, acked}
_crash_seq: int = 0

# ── Auth globals ──
_hashed_password: str = ""
_auth_username: str = "admin"
_tokens: dict[str, tuple[str, float]] = {}  # token -> (username, creation timestamp)
TOKEN_EXPIRE_SECONDS: int = 0   # 0 = session cookie (close browser = expire)
_AUTH_COOKIE_NAME: str = secrets.token_hex(8)  # M方案: 进程级随机 cookie 名，跨实例隔离（避免多实例同域 cookie 互踢）



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
    """Get or create an agent for a session via the manager.

    使用 manager.resolve_session 解析默认 session（最新优先），并回写
    全局 _current_session，确保 stats 等接口对前端返回一致的当前会话。
    """
    global _current_session
    session = _agent_manager.resolve_session(session or _current_session)
    _current_session = session  # 默认选择回写全局（问题1修复）
    prev_focus = _agent_manager.focus
    # ── 崩溃记录：必须在任何可能触发重启的调用之前（重启会替换 proc 丢失 error） ──
    _record_crash_notice(_agent_manager, session)
    # Ensure agent thread is running
    session_list = [s.session for s in _agent_manager.list_sessions()]
    if session not in session_list:
        logger.info(f"_get_agent: starting agent session={session}, workdir={_workdir}")
        _agent_manager.start_agent(session, wait_ready=True)
        # 对齐 repl：完整切换（switch_focus + resume + 缓存同步）
        _agent_manager.focus_session(session)
    else:
        _agent_manager.switch_focus(session)
        if session != prev_focus:
            # 对齐 repl：仅焦点变化时 resume + 同步缓存
            _agent_manager.focus_session(session)

    # ── 崩溃自动重启说明 ──
    # 注: 原实现此处的 crashed 检测为死代码——switch_focus 内部检测到非 running
    # 会直接 start_agent 替换 proc，此处再查已全部是 running。崩溃信息已在
    # 上方 _record_crash_notice 捕获，重启由 switch_focus/start_agent 完成。
    return _agent_manager


def _ensure_focus_agent(mgr: AgentManager) -> AgentManager:
    """确保焦点 agent 线程存活（WS 路径自愈，P0 修复）。

    设计考虑: WS 聊天/模式切换路径直接走 manager.send_input/send_command，
    不经过 _get_agent（仅 REST 接口调用），因此 agent 崩溃后 WS 交互
    无任何自动恢复路径（用户现象: "No agent session active" / 模式切不动）。
    此函数在 _ws_loop 的 chat/mode 分支前调用：
      1. 线程存活检测——status=running 但线程已死（BaseException 崩溃）→ 重启
      2. crashed 状态检测（对齐 _get_agent）→ switch_focus 触发 start_agent
    """
    focus = mgr.focus
    if focus is None:
        return mgr
    # 0) 崩溃记录：在重启替换 proc 之前捕获 error（同 _get_agent 逻辑）
    _record_crash_notice(mgr, focus)
    # 1) 线程存活检测（防 BaseException 崩溃后 status 残留 running）
    proc = mgr._agents.get(focus)  # noqa: SLF001 同项目内部访问
    if proc is not None and proc.thread is not None and not proc.thread.is_alive():
        logger.warning(f"_ensure_focus_agent: 焦点 agent 线程已死 session={focus}，自动重启")
        mgr._agents.pop(focus, None)
        mgr.start_agent(focus, wait_ready=False)
        mgr.switch_focus(focus)
        return mgr
    # 2) crashed 状态检测（对齐 _get_agent）
    crashed = [s for s in mgr.list_sessions()
               if s.session == mgr.focus and s.status == "crashed"]
    if crashed:
        logger.warning(f"_ensure_focus_agent: Agent 崩溃后重启 session={mgr.focus}")
        mgr.switch_focus(mgr.focus)
    return mgr


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


# ── Auth core functions ──

def _hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()

def _verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash (constant-time comparison)."""
    return secrets.compare_digest(_hash_password(password), hashed)

def _generate_token() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_hex(32)

def _validate_token(token: str) -> str | None:
    """Validate a token; return the bound username, or None if invalid/expired."""
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
    if _path in ("/login", "/api/login", "/api/check-auth") or _path.endswith(("/login", "/api/login", "/api/check-auth")):
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


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """统一请求访问日志：method/path/status/耗时 → log 文件（不落库）。

    设计考虑: middleware 自动覆盖所有 /xxx 路由（含未来新增），杜绝漏记；
    仅写 log 文件（loguru 异步落盘），高频轮询也不会撑爆 sqlite。
    """
    _t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:
        logger.exception(f"REQ {request.method} {request.url.path} 异常")
        raise
    cost_ms = (time.time() - _t0) * 1000
    ip = request.client.host if request.client else "-"
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
    _tokens[token] = (username, datetime.now().timestamp())

    from fastapi.responses import JSONResponse
    logger.info(f"AUTH 登录成功: username={username}")
    resp = JSONResponse(content={"ok": True, "token": token})
    resp.set_cookie(
        key=_AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=None,  # Session cookie (expires when browser closes)
        path="/",
    )
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
    try:
        with open(hist_file, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
    except FileNotFoundError:
        lines = []
    except (UnicodeDecodeError, OSError) as e:
        logger.warning(f"读取历史失败(降级为空): {e}")
        lines = []
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
    try:
        # ① 末尾去重: 读取尾部最后一非空行
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
            return {"ok": False, "reason": "duplicate"}
        # ② 追加写入
        os.makedirs(os.path.dirname(hist_file), exist_ok=True)
        with open(hist_file, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        _log("HIST", f"POST /api/history append: {text[:40]!r}")
        return {"ok": True}
    except OSError as e:
        logger.warning(f"追加历史失败: {e}")
        return {"ok": False, "reason": "io_error"}

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
    current = _current_session
    # 崩溃记录兜底：前端 2s 轮询 /api/sessions 是最高频路径，必须在此主动检测
    # 崩溃并记录（纯浏览/不发消息场景的感知兜底；proc 若已被 manager 内部重启
    # 为 running，此处仍能从 _crash_notices 持久记录返回 crash_notices）。
    _record_crash_notice(_agent_manager)
    # 注：仅包含已启动的 agent 线程；未启动的 session 不在 agents 中（phase 视为 idle）
    agents = {}
    for si in _agent_manager.list_sessions():
        agents[si.session] = {
            "phase": si.phase,       # "starting" | "idle" | "llm"
            "in_tool": si.in_tool,   # 是否有 tool 正在执行
            "turn_active": getattr(si, "turn_active", False),  # 回合级活跃标志（2026-08-10）
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
        for s, n in _crash_notices.items() if not n.get("acked")
    }
    result = {"sessions": sessions, "current": current, "lock_tags": lock_tags,
              "agents": agents, "crash_notices": crash_notices}
    _sessions_cache["default"] = (now, result)
    return result



@app.post("/api/sessions")
async def api_create_session(data: dict):
    """Create a new session via manager (对齐 repl: start_agent + switch)."""
    global _current_session
    name = data.get("name", datetime.now().strftime("session_%Y%m%d_%H%M%S"))
    if not name or not re.match(r'^[a-zA-Z0-9_\-.]+$', str(name)):
        raise HTTPException(400, "Invalid session name")
    ok, msg = add_session(name)
    if not ok:
        raise HTTPException(400, msg)
    # 通过 manager 启动 agent 线程（不再覆盖全局单例）
    _get_agent(name)
    _current_session = name
    logger.info(f"SESSION 创建: {name}")
    _sessions_cache.clear()  # 列表缓存失效，前端立即拿到新 session
    _stats_cache.clear()  # P0: stats 缓存失效，新 session 统计立即准确
    return {"session": name, "message": msg}
@app.post("/api/sessions/{name}/switch")
async def api_switch_session(name: str):
    """Switch to an existing session (对齐 repl：更新 _current_session + 真实 msg_count)."""
    global _current_session
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    agent = _get_agent(name)
    _current_session = name
    # FIX: 2.0→1.0：agent 忙碌时 get_focus_info 必然超时（get_info 命令排队等 LLM 回合结束），
    # 降低超时避免 REST 切换被拖慢；_get_agent 内部 focus_session 已同步缓存
    info = await asyncio.to_thread(agent.get_focus_info, 1.0)
    if info:
        # FIX: REST 切 session 后同步真实 mode/锁状态/技能选择到缓存
        agent._focus_mode = info.get("mode", agent._focus_mode)
        agent._focus_observing = info.get("is_observing", False)
        agent._focus_holder_info = info.get("holder_info", None)
        agent._focus_skill_select = info.get("skill_select_enabled", agent._focus_skill_select)
    msg_count = info.get("msg_count", 0) if info else 0
    logger.info(f"SESSION 切换: {name}")
    _sessions_cache.clear()  # 列表缓存失效，切换后立即重排（选中置顶）
    _stats_cache.clear()  # P0: stats 缓存失效，切换后前端 fetchStatus 不再命中旧 session 数据
    return {"session": name, "messages": msg_count}


def _load_session_messages(name: str, after_id: int, limit: int | None = None) -> tuple:
    """在线程池中执行 DB 读取：连接 + 查询(可选分页) + 关闭连接。

    独立函数以便 asyncio.to_thread 包裹——避免 sqlite 锁等待
    （busy_timeout=10s）阻塞 asyncio 事件循环（check 报告根因1）。
    limit 仅在 after_id=0（全量分页）时传入；增量路径必须完整返回（不截断），
    否则会漏消息导致前端缓存水位线错乱。
    返回 (messages, last_id) 或 (messages, last_id, total)（limit 非 None 时）。
    """
    conn = get_conn(name)
    try:
        if limit is not None:
            # 全量分页：COUNT 轻量统计可见消息总数（与 get_messages_since 过滤一致）。
            # 2026-08-05: 放开 command 渲染后，COUNT 排除集同步去掉 command，
            # 否则 total 与返回消息数不一致会误导 has_more/已渲染条数。
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role NOT IN ('git','compact','drop')"
            ).fetchone()[0]
            raw, last_id = get_messages_since(conn, None, with_id=True, limit=limit, fields=["command", "thinking"], with_time=True)
            return raw, last_id, total
        return get_messages_since(conn, after_id or None, with_id=True, fields=["command", "thinking"], with_time=True)
    finally:
        conn.close()

@app.get("/api/sessions/{name}/messages")
async def api_get_session_messages(name: str, after_id: int = 0, limit: int = Query(50, ge=1, le=500)):
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
    _log("CHAT", f"Loaded {len(msgs)} messages for session '{name}' (after_id={after_id}, last_id={last_id}, total={total})")
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
    global _current_session
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    ok = _agent_manager.stop_agent(name)
    logger.warning(f"SESSION 停止: {name} (ok={ok})")
    _sessions_cache.clear()  # 列表缓存失效，停止后立即从 running 组消失
    _stats_cache.clear()  # stats 缓存失效：停止后统计立即刷新
    if not ok:
        return {"ok": False, "reason": "not_running"}
    switched_to = None
    if name == _current_session:
        switched_to = _pick_next_session(name)
        if switched_to:
            _get_agent(switched_to)   # switch focus（未运行则自动启动）
            _current_session = switched_to
            _stats_cache.clear()  # P0: focus 已切走，旧 key 缓存立即失效
        else:
            _current_session = None   # 无其他会话：resolve_session 兜底
    return {"ok": True, "switched_to": switched_to}

@app.post("/api/sessions/{name}/fork")
async def api_fork_session(name: str, data: dict):
    """Fork（复制）指定会话为新会话（任意源会话，对齐 repl /session fork 语义）。

    设计考虑:
      - 与 /session fork <name> 的区别：repl 版固定 fork 当前 focus 会话，
        此端点可对列表中任意 session 操作（前端 fork 按钮直接调用）。
      - 复用 history.fork_session（WAL checkpoint + 文件复制 + 回滚），
        运行中会话亦可 fork（partial checkpoint 时 .db-wal 一并复制保数据完整）。
      - fork 后不自动切换/不启动 agent（对齐 repl 行为），新会话出现在列表，
        用户点击切换时 _get_agent 自动启动。
    """
    if not session_exists(name):
        raise HTTPException(404, f"Session '{name}' not found")
    target = (data.get("target") or "").strip()
    if not target:
        raise HTTPException(400, "Missing target session name")
    ok, msg = fork_session(name, target)
    if not ok:
        raise HTTPException(400, msg)
    _sessions_cache.clear()   # 列表缓存失效，新 session 立即出现
    logger.info(f"SESSION fork: {name} -> {target}")
    return {"ok": True, "session": target, "message": msg}



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
        }
    except Exception:
        return {"prompt_tokens": 0, "completion_tokens": 0}


def _resolve_effective_model(info: dict) -> str:
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
    try:
        from codes import provider_config as _pc
        return str(_pc.get_provider(prov).get("default_model", "") or "").strip()
    except Exception:
        return ""


@app.post("/api/model/test")
async def api_model_test():
    """测试全部 provider+model 的联通性（复用 /model test 全量测速逻辑）。"""
    try:
        from codes import llm as _llm
        results, elapsed = await asyncio.to_thread(_llm.test_all_connectivity, timeout=15)
        return {"ok": True, "results": results, "elapsed": elapsed}
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
                agent.send_command("set_provider", {"provider": provider})
            agent.send_command("set_model", {"model": model})
            return None
        except Exception as e:
            return f"{name}: {e}"

    if scope == "current":
        agent = _get_agent()
        err = _set_one(agent, _current_session or "current")
        if err:
            return {"ok": False, "error": err}
        _stats_cache.clear()
        return {"ok": True, "scope": "current", "session": _current_session, "model": model, "provider": provider}

    # all：遍历全部已启动 agent 设置
    results = {}
    for si in _agent_manager.list_sessions():
        name = si.session
        try:
            agent = _get_agent(name)
            err = _set_one(agent, name)
            results[name] = "ok" if err is None else err
        except Exception as e:
            results[name] = str(e)
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
    """
    agent = _get_agent(session)   # resolve + 回写 _current_session（stats 权威当前会话）
    effective = _current_session or ""
    cache_key = effective
    now = time.time()
    cached = _stats_cache.get(cache_key)
    if cached and now - cached[0] < _STATS_TTL:
        return cached[1]

    tokens = await asyncio.to_thread(_fetch_token_stats, effective)
    info = await asyncio.to_thread(agent.get_focus_info, 1.0)  # 2.0→1.0：stats 慢请求(日志 2~4s)主要阻塞源之一
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
            "last_prompt_tokens": _last_p,
            "last_completion_tokens": _last_c,
            "model": _resolve_effective_model(info),
            "provider": info.get("provider", "") or "",
            # T5: 透传锁状态（观察者模式 / 持有者信息），供前端渲染 ⏳/🔒 标签
            "observing": info.get("is_observing", False),
            "holder": info.get("holder_info", None),
            # 路径链接：项目根（前端据此识别绝对路径）
            "workdir": _resolve_workdir(),
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
            try:
                from codes import provider_config as _pc2
                _m = str(_pc2.get_provider(_p).get("default_model", "") or "").strip()
            except Exception:
                _m = ""
        result = {"session": effective, "mode": getattr(agent, "_focus_mode", "plan"), "messages": 0,
                "prompt_tokens": tokens["prompt_tokens"],
                "completion_tokens": tokens["completion_tokens"],
                "last_prompt_tokens": _last_p,
                "last_completion_tokens": _last_c,
                "model": _m,
                "provider": _p,
                "observing": False,
                "holder": None,
                "workdir": _resolve_workdir()}
    _stats_cache[cache_key] = (time.time(), result)
    return result
def _resolve_workdir() -> str:
    """项目根目录：优先模块级 _workdir（run_web 设置），否则回退 cwd。"""
    if _workdir:
        return os.path.realpath(_workdir)
    return os.path.realpath(os.getcwd())


def _files_allowed_roots(session: str | None = None) -> list[str]:
    """files 允许根集合：workdir + 全局 permission.txt + 该 session 动态挂载（去重）。

    与 pythonrt 沙箱软边界对齐：/mount 动态挂载（per-session mount_state 表）与
    permission.txt 全局挂载在 files 浏览器同样可见/可访问；根外路径一律 404。
    失效路径（已删除）过滤掉，对齐 _load_dyn_mounts 语义。
    """
    roots = [os.path.realpath(_resolve_workdir())]
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
    return any(t == r or t.startswith(r + os.sep) for r in roots)


def _resolve_files_base(path: str, session: str | None = None) -> tuple[str, list[str]]:
    """解析 files API 请求路径 → (realpath target, 允许根集合)。

    支持绝对路径（须落在允许根内）与相对路径（依次尝试每个根，首个命中生效）。
    """
    roots = _files_allowed_roots(session)
    if os.path.isabs(path):
        target = os.path.realpath(path)
        if not _path_allowed(target, roots):
            raise HTTPException(404, "Path outside allowed root")
        return target, roots
    for r in roots:
        target = os.path.realpath(os.path.join(r, path or ""))
        if _path_allowed(target, roots) and os.path.exists(target):
            return target, roots
    # 兜底：所有根下均不存在该相对路径时，回退首根（workdir）解析，由调用方返回标准 404
    for r in roots:
        target = os.path.realpath(os.path.join(r, path or ""))
        if _path_allowed(target, roots):
            return target, roots
    raise HTTPException(404, "Path outside allowed root")


@app.get("/api/files/list")
async def api_files_list(path: str = Query("", description="目录路径（相对 workdir/挂载根，或落在允许根内的绝对路径）"),
                         session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）")):
    """列出目录内容（FTP 风格），允许根 = workdir + 全局挂载 + 该 session 动态挂载。

    设计考虑：realpath 归一化后对允许根集合逐根前缀校验（防路径穿越，符号链接指向根外仍 404），
    目录项用 os.scandir 惰性迭代避免整目录载入内存。
    """
    sess = session or _current_session
    target, roots = _resolve_files_base(path, sess)
    if not os.path.isdir(target):
        raise HTTPException(404, f"Not a directory: {path or '/'}")
    entries = []
    try:
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
    except OSError as exc:
        raise HTTPException(500, f"Failed to scan directory: {exc}")
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    rel = os.path.relpath(target, _resolve_workdir())
    return {
        "path": "" if rel == "." else rel,
        "parent": None if rel == "." else os.path.dirname(rel),
        "entries": entries,
        "root": target,          # 当前绝对路径（前端拼接子项）
        "roots": roots,          # 允许根集合（前端根选择器）
        "session": sess,
    }


@app.get("/api/files/download")
async def api_files_download(path: str = Query("", description="文件路径（相对 workdir/挂载根，或落在允许根内的绝对路径）"),
                             session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）")):
    """流式下载允许根内文件（FileResponse 流式，避免整文件读入内存），防路径穿越。"""
    sess = session or _current_session
    target, _roots = _resolve_files_base(path, sess)
    if not os.path.isfile(target):
        raise HTTPException(404, f"Not a file: {path}")
    from fastapi.responses import FileResponse
    logger.info(f"FILE 下载: {path} session={sess}")
    return FileResponse(target)

@app.get("/api/files/open")
async def api_files_open(path: str = Query("", description="文件/目录路径（绝对或相对 workdir/挂载根）"),
                         session: str = Query(None, description="session 名（缺省回退当前会话，取其动态挂载）")):
    """打开允许根内文件/目录（新标签页）。文件→302 到下载流；目录→302 到文件浏览器定位。

    设计考虑：支持绝对/相对/裸文件名三档；realpath 对允许根集合逐根前缀校验防穿越；
    302 重定向复用现有 download/files 路由，避免重复实现文件读取；目录跳转带绝对路径+session。
    """
    sess = session or _current_session
    target, _roots = _resolve_files_base(path, sess)
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote
    rel = os.path.relpath(target, _resolve_workdir())
    # URL 编码（safe='/' 保留路径分隔符可读性），防文件名含空格/# 等特殊字符损坏 URL
    rel_q = quote(rel, safe='/')
    abs_q = quote(target, safe='/')
    sess_q = quote(sess or '', safe='')
    logger.info(f"FILE 打开: {path} session={sess}")
    if os.path.isdir(target):
        return RedirectResponse(url=f"../../files?path={abs_q}&session={sess_q}", status_code=302)
    if os.path.isfile(target):
        return RedirectResponse(url=f"../../api/files/download?path={abs_q}&session={sess_q}", status_code=302)
    raise HTTPException(404, f"Not found: {path}")


# ── Upload API: 上传文件至 .xkagent/files/ ──
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


@app.get("/api/files/roots")
async def api_files_roots(session: str = Query(None, description="session 名（缺省回退当前会话）")):
    """返回 files 允许根集合（workdir + 全局挂载 + 该 session 动态挂载），供前端根选择器。"""
    sess = session or _current_session
    return {"roots": _files_allowed_roots(sess),
            "workdir": os.path.realpath(_resolve_workdir()),
            "session": sess}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """上传文件至 <workdir>/.xkagent/files/，返回文件/图片路径。

    设计考虑：文件名 basename 净化防路径穿越；重名加时间戳前缀去重；
    流式分块写入（1MB）防大文件整读内存；auth middleware 自动保护该路由。
    """
    base = os.path.realpath(_resolve_workdir())
    files_dir = os.path.join(base, _config.DATA_DIR_NAME, "files")
    os.makedirs(files_dir, exist_ok=True)
    fname = os.path.basename(file.filename or "upload.bin") or "upload.bin"
    safe_name = fname
    target = os.path.join(files_dir, safe_name)
    if os.path.exists(target):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = f"{ts}_{fname}"
        target = os.path.join(files_dir, safe_name)
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    finally:
        await file.close()
    ext = os.path.splitext(safe_name)[1].lower()
    is_image = ext in IMAGE_EXTS
    rel = os.path.join(_config.DATA_DIR_NAME, "files", safe_name).replace(os.sep, "/")
    from urllib.parse import quote
    url = f"api/files/download?path={quote(rel, safe='/')}"
    logger.info(f"UPLOAD: {rel} ({os.path.getsize(target)} bytes)")
    return {
        "ok": True,
        "filename": safe_name,
        "path": rel,
        "abs_path": target,
        "url": url,
        "is_image": is_image,
        "size": os.path.getsize(target),
    }


@app.get("/api/skills")
async def api_list_skills():
    """List available skills."""
    from codes.skill import SkillLoader
    return {"skills": SkillLoader.list_skills()}


@app.post("/api/chat")
async def api_chat(data: dict):
    """Non-streaming chat (对齐 repl: 累计 stats 事件的真实 token)."""
    logger.info(f"API chat 请求: session={data.get('session', '?')}")
    text = data.get("text", "")
    session = data.get("session")
    _log("CHAT", f"User (REST): {text[:200]}{'...' if len(text) > 200 else ''}")
    agent = _get_agent(session)
    # non-streaming chat via manager
    agent.send_input(text)
    result = ""
    blocked_reason = ""  # T4: 观察者模式拒绝输入时记录原因
    prompt_tokens = 0
    completion_tokens = 0
    deadline = time.time() + 300.0
    while time.time() < deadline:
        evt = agent.read_output(timeout=1.0)
        if evt is None:
            continue
        if evt.get("type") == "_turn_end":
            break
        # T4: 观察者模式拒绝输入（blocked 事件）→ 记录原因，正常返回
        if evt.get("type") == "blocked":
            blocked_reason = evt.get("reason", "session 已被其他进程占用")
        if evt.get("type") == "text":
            result += evt.get("data", "")
        elif evt.get("type") == "stats":
            prompt_tokens += evt.get("prompt_tokens", 0)
            completion_tokens += evt.get("completion_tokens", 0)
    _log("CHAT", f"Assistant (REST): {result[:500]}{'...' if len(result) > 500 else ''}")
    if blocked_reason:
        # T4: 观察者只读 → 返回 blocked 提示（而非空 response）
        return {
            "blocked": True,
            "reason": blocked_reason,
            "session": _current_session or "",
        }
    return {
        "response": result,
        "session": _current_session or "",
        "stats": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }



@app.get("/api/mode")
async def api_get_mode(session: str = Query(None)):
    """Get current mode."""
    agent = _get_agent(session)
    return {"mode": getattr(agent, "_focus_mode", "plan")}


@app.get("/api/skill-select")
async def api_get_skill_select(session: str = Query(None)):
    """Get skill select enabled status."""
    agent = _get_agent(session)
    return {"enabled": getattr(agent, "_focus_skill_select", True)}


@app.post("/api/skill-select")
async def api_set_skill_select(data: dict):
    """Set or toggle skill select enabled status."""
    session = data.get("session")
    agent = _get_agent(session)
    enabled = data.get("enabled")
    if enabled is not None:
        agent.send_command("set_skill_select", {"enabled": enabled})
        agent._focus_skill_select = enabled
    else:
        new_state = not getattr(agent, "_focus_skill_select", True)
        agent.send_command("set_skill_select", {"enabled": new_state})
        agent._focus_skill_select = new_state
    logger.info(f"SKILL_SELECT 切换: session={session} enabled={getattr(agent, '_focus_skill_select', True)}")
    return {"enabled": getattr(agent, "_focus_skill_select", True)}


@app.post("/api/mode")
async def api_set_mode(data: dict):
    """Set or toggle mode."""
    session = data.get("session")
    agent = _get_agent(session)
    mode = data.get("mode")
    if mode and mode in ("plan", "build", "build-unsafe"):
        _ok = agent.send_command("set_mode", {"mode": mode})
        if _ok:
            agent._focus_mode = mode
        else:
            # 命令未送达：回读真实 mode，防 UI 缓存与 agent 实际状态脱节
            info = agent.get_focus_info(timeout=2.0)
            if info:
                agent._focus_mode = info.get("mode", agent._focus_mode)
    else:
        # Toggle
        current_mode = getattr(agent, '_focus_mode', 'plan')
        new_mode = MODE_CYCLE.get(current_mode, "plan")
        _ok = agent.send_command("set_mode", {"mode": new_mode})
        if _ok:
            agent._focus_mode = new_mode
        else:
            # 命令未送达：回读真实 mode，防 UI 缓存与 agent 实际状态脱节
            info = agent.get_focus_info(timeout=2.0)
            if info:
                agent._focus_mode = info.get("mode", agent._focus_mode)
    logger.info(f"MODE 切换: session={session} -> {getattr(agent, '_focus_mode', 'plan')}")
    return {"mode": getattr(agent, "_focus_mode", "plan")}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebSocket — Streaming chat
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.websocket("/ws/{session}")
async def websocket_chat(ws: WebSocket, session: str):
    logger.info(f"WebSocket 连接: session={session}")
    # WebSocket auth check
    if _hashed_password:
        token = ws.cookies.get(_AUTH_COOKIE_NAME) or ws.query_params.get("token")
        if not token or _validate_token(token) != _auth_username:
            await ws.close(code=4001, reason="Unauthorized")
            return
    await ws.accept()
    agent = _get_agent(session)
    await _ws_loop(ws, agent)


@app.websocket("/ws")
async def websocket_chat_default(ws: WebSocket):
    # WebSocket auth check
    if _hashed_password:
        token = ws.cookies.get(_AUTH_COOKIE_NAME) or ws.query_params.get("token")
        if not token or _validate_token(token) != _auth_username:
            await ws.close(code=4001, reason="Unauthorized")
            return
    await ws.accept()
    agent = _get_agent()
    await _ws_loop(ws, agent)


async def _ws_loop(ws: WebSocket, agent: Agent):
    logger.info("WebSocket 循环开始")
    """WebSocket message loop."""
    global _current_session
    send_lock = asyncio.Lock()          # P2: 并发发送互斥（后台 chat 任务 × 主循环响应）
    active_chat_task: asyncio.Task | None = None  # P2: 追踪进行中的 LLM 回合

    async def _send(data: dict) -> None:
        # P2: 带锁发送，避免后台 chat 任务与主循环响应并发写 ws
        async with send_lock:
            await ws.send_text(json.dumps(data))

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
                # P0 修复: WS 路径自愈——chat/mode 前确保焦点 agent 存活
                # （崩溃后自动重启），不再依赖 REST 接口的 _get_agent 触发，
                # 消除 "No agent session active" 与模式切换无响应。
                agent = _ensure_focus_agent(agent)
                if active_chat_task is not None and not active_chat_task.done():
                    # P2: busy 保护：LLM 执行中不再接受新消息
                    await _send({"type": "info", "data": "⏳ Agent 正在处理上一条消息，请稍候"})
                    continue
                # F1c: 新回合开始前清空队列残留（覆盖所有路径：
                # /session 命令切走、switch_session、直接发新消息）
                # 正常回合结束后队列已空（done 已发），drain 立即返回。
                _drain_output_until_turn_end(agent)
                # P2: 后台运行回合，主循环继续接收消息（可切 session / 改 mode）
                active_chat_task = asyncio.create_task(
                    _handle_chat_ws(ws, agent, text, send_lock)
                )

            elif msg_type == "mode":
                agent = _ensure_focus_agent(agent)  # P0: 同 chat 分支，模式切换前自愈
                mode = msg.get("mode")
                if mode and mode in ("plan", "build", "build-unsafe"):
                    _ok = agent.send_command("set_mode", {"mode": mode})
                    if _ok:
                        agent._focus_mode = mode
                    else:
                        # 命令未送达：回读真实 mode，防 UI 缓存与 agent 实际状态脱节
                        info = agent.get_focus_info(timeout=2.0)
                        if info:
                            agent._focus_mode = info.get("mode", agent._focus_mode)
                else:
                    new_mode = MODE_CYCLE.get(getattr(agent, "_focus_mode", "plan"), "plan")
                    _ok = agent.send_command("set_mode", {"mode": new_mode})
                    if _ok:
                        agent._focus_mode = new_mode
                    else:
                        # 命令未送达：回读真实 mode，防 UI 缓存与 agent 实际状态脱节
                        info = agent.get_focus_info(timeout=2.0)
                        if info:
                            agent._focus_mode = info.get("mode", agent._focus_mode)
                _log("SYSTEM", f"Mode: {getattr(agent, '_focus_mode', 'plan')}")
                await _send({
                    "type": "mode_changed",
                    "data": getattr(agent, "_focus_mode", "plan"),
                })

            elif msg_type == "mode_toggle":
                new_mode = MODE_CYCLE.get(getattr(agent, "_focus_mode", "plan"), "plan")
                _ok = agent.send_command("set_mode", {"mode": new_mode})
                if _ok:
                    agent._focus_mode = new_mode
                else:
                    # 命令未送达：回读真实 mode，防 UI 缓存与 agent 实际状态脱节
                    info = agent.get_focus_info(timeout=2.0)
                    if info:
                        agent._focus_mode = info.get("mode", agent._focus_mode)
                _log("SYSTEM", f"Mode: {getattr(agent, '_focus_mode', 'plan')} (toggled)")
                await _send({
                    "type": "mode_changed",
                    "data": getattr(agent, "_focus_mode", "plan"),
                })

            elif msg_type == "skill_select_toggle":
                enabled = msg.get("enabled")
                if enabled is not None:
                    agent.send_command("set_skill_select", {"enabled": enabled})
                    agent._focus_skill_select = enabled
                else:
                    new_state = not getattr(agent, "_focus_skill_select", True)
                    agent.send_command("set_skill_select", {"enabled": new_state})
                    agent._focus_skill_select = new_state
                _log("SYSTEM", f"Skill select: {getattr(agent, '_focus_skill_select', True)}")
                await _send({
                    "type": "skill_select_changed",
                    "data": getattr(agent, "_focus_skill_select", True),
                })

            elif msg_type == "command":
                cmd = msg.get("cmd", "")
                _log("SYSTEM", f"Command: {cmd}")
                # ── /compact: 走 chat 流式路径（复用 run_stream 流式压缩），
                #    而非旧同步命令轮询（曾导致 5s 假超时 + 无流式反馈）──
                if cmd.strip().lower() == "/compact":
                    if active_chat_task is not None and not active_chat_task.done():
                        await _send({"type": "info", "data": "⏳ Agent 正在处理上一条消息，请稍候"})
                        continue
                    _drain_output_until_turn_end(agent)
                    active_chat_task = asyncio.create_task(
                        _handle_chat_ws(ws, agent, COMPACT_MARKER + COMPACT_PROMPT, send_lock)
                    )
                    continue
                before = _current_session
                result = await asyncio.to_thread(_execute_command, agent, cmd)
                # ── 命令历史落库：/xxx 与 !xxx 及回应持久化（T2）──
                _record_web_cmd_history(cmd, result)
                await _send({
                    "type": "command_result",
                    "data": result,
                })
                # F4: /session 命令切换会话后补发 session_switched，
                # 让前端同步更新 session-name（command_result 仅展示文本，
                # 无刷新路径，否则显示停留在旧会话名）。
                if _current_session != before:
                    # FIX: 2.0→1.0：agent 忙碌时 get_info 排队超时是预期，降低切换阻塞
                    info = await asyncio.to_thread(agent.get_focus_info, 1.0)
                    if info:
                        # FIX: /session 切换后同步真实 mode/锁状态/技能选择到缓存（对齐 switch_session 路径）
                        agent._focus_mode = info.get("mode", agent._focus_mode)
                        agent._focus_observing = info.get("is_observing", False)
                        agent._focus_holder_info = info.get("holder_info", None)
                        agent._focus_skill_select = info.get("skill_select_enabled", agent._focus_skill_select)
                    msg_count = info.get("msg_count", 0) if info else 0
                    _stats_cache.clear()  # P0: /session 命令切走后 stats 缓存立即失效
                    await _send({
                        "type": "session_switched",
                        "data": {
                            "session": _current_session,
                            "messages": msg_count,
                            "busy": _session_busy(agent, _current_session),
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
                    if active_chat_task is not None and not active_chat_task.done():
                        active_chat_task.cancel()
                        try:
                            await active_chat_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        active_chat_task = None
                        # 不 drain 旧队列：旧回合仍在产出事件，drain 会丢尾部事件；
                        # 切回后新回合 F1c 清空残留兜底，DB 已落库消息不受影响。
                    _log("SYSTEM", f"Session switch: {name}")
                    mgr = _get_agent(name)   # 切 focus（内部 switch_focus + focus_session）
                    _current_session = name
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
                            "busy": _session_busy(mgr, name),
                        },
                    })
                else:
                    await _send({
                        "type": "error",
                        "data": f"Session '{name}' not found",
                    })
            elif msg_type == "interrupt":
                # 对齐 repl Ctrl+C：请求 agent 线程完全停止（Event + bashkit.cancel），
                # 而非仅 cancel asyncio 转发任务（后者 agent 线程仍在后台跑完、污染下回合）。
                if active_chat_task is not None and not active_chat_task.done():
                    if agent.request_interrupt(_current_session or agent.focus):
                        active_chat_task.cancel()   # 立即停止转发
                        try:
                            await active_chat_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        active_chat_task = None
                        # 排空残留事件至 _turn_end（对齐 repl._drain_until_turn_end）
                        await asyncio.to_thread(_drain_output_until_turn_end, agent, 3.0)
                        await _send({"type": "system", "data": "⏹ 已停止"})
                        await _send({"type": "done", "data": ""})
                    else:
                        await _send({"type": "system", "data": "⚠️ 无法中断：会话未激活或正在收尾"})
                        await _send({"type": "done", "data": ""})
                else:
                    # 无活跃回合：幂等回执 done，前端复位 isStreaming
                    await _send({"type": "done", "data": ""})
    finally:
        logger.info(f"WS 断开: session={agent.session}")
        # P2: 连接断开时取消未完成的 chat 任务
        if active_chat_task is not None and not active_chat_task.done():
            active_chat_task.cancel()
            try:
                await active_chat_task
            except (asyncio.CancelledError, Exception):
                pass


async def _safe_send(ws: WebSocket, data: dict) -> bool:
    """Send JSON to WebSocket, return False if client disconnected."""
    try:
        await ws.send_text(json.dumps(data))
        return True
    except WebSocketDisconnect:
        return False

def _drain_output_until_turn_end(mgr: AgentManager, total_timeout: float = 3.0) -> None:
    """Drain the focused agent's output queue, discarding events until _turn_end.

    对齐 repl._drain_until_turn_end（Ctrl+C 语义）：切换 focus 后，旧回合
    （被 cancel 但 agent 线程仍在后台跑）的残留事件会积压在其队列；若不清空，
    切回后 read_output 会先读到旧事件，污染新回合界面。
    此函数丢弃这些残留（选项 A：与 repl 对齐，接受"切回丢弃旧输出"）。
    仅对当前 focus 的队列有效——调用方须在 focus 未变化时调用。
    """
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        event = mgr.read_output(timeout=0.2)
        if event is None:
            break
        if isinstance(event, dict) and event.get("type") == "_turn_end":
            break


# ── Logging ──
def _log(level: str, msg: str):
    """Print timestamped log to stderr."""
    _now = datetime.now().strftime("%H:%M:%S")
    print(f"  [{_now}] [{level}] {msg}", file=sys.stderr, flush=True)
    logger.info(f"[{level}] {msg}")


async def _handle_chat_ws(ws: WebSocket, mgr: AgentManager, text: str, send_lock: asyncio.Lock):
    """Handle a chat message over WebSocket with streaming (via manager).

    对齐 repl._handle_event 的事件集：补全 stats 成本、tool 字段、
    skill_req、_sync_update；内部事件（_cmd_result/_lock_status）不渲染。

    P2 修复：
    - 本函数作为后台 asyncio 任务运行（_ws_loop 中 create_task），
      read_output 改用 asyncio.to_thread，避免同步阻塞事件循环，
      使主循环能持续接收消息（切 session / 改 mode / 发命令）。
    - target 在启动时捕获，循环内 mgr.focus != target 即停止转发
      （切 session 后旧回合输出积压其队列，不再串台）。
    - 收到取消（切 session）时吞掉 CancelledError，不重发 done。
    """
    _log("CHAT", f"User: {text[:200]}{'...' if len(text) > 200 else ''}")

    async def _send(data: dict) -> bool:
        # P2: 与主循环共享发送锁，避免后台任务与主循环并发写 ws
        async with send_lock:
            return await _safe_send(ws, data)

    # F3: 捕获本回合所属 session（前置到 send_input 之前）
    target = mgr.focus
    if target is None:
        await _send({"type": "error", "data": "No agent session active."})
        return
    # Delegate to the manager's focused agent
    if not mgr.send_input(text):
        await _send({"type": "error", "data": "No agent session active."})
        return
    # F3: send_input 后校验 focus 一致性（极小竞态窗口：create_task 调度期间
    # focus 可能已被 switch_session 改走），不一致则丢弃本回合并提示
    if mgr.focus != target:
        _log("CHAT", f"回合丢弃: focus 已变为 {mgr.focus}（消息已入旧队列）")
        await _send({"type": "system", "data": "⏹ 会话已切换，消息未投递"})
        return
    reply_parts: list[str] = []
    try:
        while True:
            # P2: focus 守卫：切 session 后立即停止本回合转发（避免读错 focus 队列）
            if mgr.focus != target:
                _log("CHAT", f"回合中止: focus {target} -> {mgr.focus}")
                # F2: 补发 done——/session 命令路径切 focus 时本任务静默退出，
                # 若不发 done，前端 isStreaming 永久卡死（send() 拒绝新消息）。
                # switch_session 路径已由前端 session_switched 重置，此处幂等安全。
                await _send({"type": "done", "data": ""})
                return
            # P2: 同步 read_output 放到线程池，避免阻塞事件循环
            event = await asyncio.to_thread(mgr.read_output, 0.2)
            if event is None:
                continue
            t = event.get("type", "")

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
                        _base = os.path.realpath(_resolve_workdir())
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

            elif t == "skill_selected":
                name = event.get("name", "")
                reason = event.get("reason") or ""
                suffix = f"（{reason}）" if reason else ""
                if not await _send({"type": "system", "data": f"🎯 技能选择: {name}{suffix}"}):
                    return

            elif t == "skill_req":
                # 对齐 repl：显示 Skill loaded
                sname = event.get("name", "")
                if sname:
                    if not await _send({"type": "system", "data": f"💡 Skill loaded: {sname}"}):
                        return

            elif t == "no_skill":
                reason = event.get("reason") or ""
                label = "🎯 技能选择: 无"
                if reason:
                    label += f"（{reason[:120]}）"
                if not await _send({"type": "system", "data": label}):
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
                # T7: 观察者状态 → 更新 manager 缓存 + 转发前端渲染 ⏳/🔒 标签
                mgr._focus_observing = event.get("is_observing", False)
                mgr._focus_holder_info = event.get("holder_info", None)
                if not await _send({
                    "type": "lock_status_changed",
                    "data": {
                        "observing": mgr._focus_observing,
                        "holder": mgr._focus_holder_info,
                    },
                }):
                    return

            elif t == "_cmd_result":
                # 对齐 repl：内部状态事件不渲染，但回流真实状态到缓存
                # （FIX: 原实现 pass，get_info/set_mode 结果无法同步缓存，导致状态过期）
                cmd = event.get("cmd")
                data = event.get("data")
                if cmd == "get_info" and isinstance(data, dict):
                    mgr._focus_mode = data.get("mode", mgr._focus_mode)
                    mgr._focus_observing = data.get("is_observing", False)
                    mgr._focus_holder_info = data.get("holder_info", None)
                    mgr._focus_skill_select = data.get("skill_select_enabled", mgr._focus_skill_select)
                elif cmd == "set_mode":
                    if data:
                        mgr._focus_mode = data
                    elif event.get("ok") is False:
                        # 观察者拒绝写命令（无 data）：回读真实 mode，防缓存保留错误状态
                        info = mgr.get_focus_info(timeout=2.0)
                        if info:
                            mgr._focus_mode = info.get("mode", mgr._focus_mode)
                elif cmd == "set_skill_select" and isinstance(data, bool):
                    mgr._focus_skill_select = data


            elif t == "clear_thinking":
                await _send({"type": "clear_thinking"})

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
    full_reply = "".join(reply_parts)
    if full_reply:
        _log("CHAT", f"Assistant: {full_reply[:500]}{'...' if len(full_reply) > 500 else ''}")

    # Send done signal
    await _send({"type": "done", "data": ""})

def _run_bash(cmd: str) -> str:
    """执行 bash 命令并返回输出（对齐 repl 的 !command 快捷方式）。"""
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace")
    except Exception as e:
        return f"❌ Error: {e}"
    parts = []
    if r.stdout:
        parts.append(r.stdout.rstrip())
    if r.stderr:
        parts.append("[stderr]\n" + r.stderr.rstrip())
    parts.append(f"└ exit: {r.returncode}")
    return "\n".join(parts)




def _record_web_cmd_history(cmd: str, result: str) -> None:
    """将 web 端执行的 /xxx 或 !xxx 命令与回应写入当前 session DB（role='command'）。

    设计意图：web 命令不经 agent 队列（_execute_command 为同步直调），观察者 gate
    不拦截，故此处统一落库并 try/except 降级（观察者只读连接写失败仅告警）。
    连接解析：使用 web 侧全局 _current_session（_execute_command 同样依赖它），
    避免依赖 Agent 对象内部字段，跨版本更稳。
    """
    cmd = (cmd or "").strip()
    if not cmd or not _current_session:
        return
    try:
        from codes.history import add_command, get_conn
        add_command(
            get_conn(_current_session), cmd, result,
            kind="bang" if cmd.startswith("!") else "slash",
        )
    except Exception:
        logger.warning(f"记录 command 历史失败: {cmd!r}", exc_info=True)


def _web_cmd_context(mgr) -> CommandContext:
    """构造 Web 侧命令上下文（注入全局会话同步 / 服务器退出能力）。"""
    global _current_session

    def _switch_hook(name: str) -> None:
        global _current_session
        _current_session = name

    def _exit_hook() -> str:
        close_web_server()
        return "👋 服务器已关闭"

    return CommandContext(
        exit_hook=_exit_hook,
        get_session=lambda: _current_session,
        switch_session_hook=_switch_hook,
    )


def _execute_command(agent: AgentManager, cmd: str) -> str:
    """统一命令调度（codes.commands.dispatch），对齐 repl。

    覆盖全部共享命令：help/logfile/clear/drop/session/sessions/model/skills/
    validate/updateembedding/info/skill/plan/build/build-unsafe/mode/mount/
    unmount/turnonskill/turnoffskill/restart/exit/cmds + !xxx bash 快捷方式。
    """
    cmd = cmd.strip()
    if not cmd:
        return ""
    # ── !command: bash 快捷方式（保留 web 侧实现） ──
    if cmd.startswith("!"):
        return _run_bash(cmd[1:].strip())
    return dispatch(agent, cmd, ctx=_web_cmd_context(agent))


# ── Login page HTML ──

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XKAgent - Login</title>
<style>
  :root {
    --bg: #f3f5fb;
    --surface: #ffffff;
    --surface2: #f6f6f6;
    --accent: #0066ff;
    --accent-hover: #0052cc;
    --text: #1c1f23;
    --text-muted: #57606a;
    --border: rgba(28, 31, 35, 0.08);
    --radius: 12px;
    --err: #dc2626;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .login-box {
    background: var(--surface);
    padding: 40px;
    border-radius: 16px;
    border: 1px solid var(--border);
    box-shadow: 0 4px 24px rgba(28, 31, 35, 0.06);
    width: 380px;
    text-align: center;
  }
  .login-box h1 { font-size: 26px; margin-bottom: 8px; color: var(--accent); font-weight: 600; }
  .login-box p { font-size: 14px; color: var(--text-muted); margin-bottom: 28px; }
  .login-box input {
    width: 100%;
    padding: 12px 16px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--surface2);
    color: var(--text);
    font-size: 15px;
    outline: none;
    margin-bottom: 16px;
    transition: border-color .15s;
  }
  .login-box input:focus { border-color: var(--accent); }
  .login-box button {
    width: 100%;
    padding: 12px;
    border: none;
    border-radius: 10px;
    background: var(--accent);
    color: #fff;
    font-size: 15px;
    font-weight: 500;
    cursor: pointer;
    transition: background .15s;
  }
  .login-box button:hover { background: var(--accent-hover); }
  .login-box .error { color: var(--err); font-size: 13px; margin-top: 12px; display: none; }
  .login-box .loading { color: var(--text-muted); font-size: 13px; margin-top: 12px; display: none; }
.thinking-wait { color: #999; font-style: italic; padding: 4px 0; }
</style>
</head>
<body>
<div class="login-box">
  <h1>⚡ XKAgent</h1>
  <p>Enter credentials to continue</p>
  <input type="text" id="username" placeholder="Username" autofocus
         onkeydown="if(event.key==='Enter') login()">
  <input type="password" id="password" placeholder="Password"
         onkeydown="if(event.key==='Enter') login()">
  <button onclick="login()">Login</button>
  <div class="error" id="error-msg">Invalid password</div>
  <div class="loading" id="loading-msg">Authenticating...</div>
</div>
<script>
// ── 相对路径基准：适配反向代理子路径（如 DSW /dsw-xxx/proxy/4096/）──
// 所有 fetch/WS/跳转基于 BASE 拼接，避免绝对路径丢失代理前缀
const BASE = (function(){ var p = location.pathname; return p.endsWith('/') ? p : p.substring(0, p.lastIndexOf('/') + 1); })();
async function login() {
  const user = document.getElementById('username').value.trim();
  const pw = document.getElementById('password').value;
  if (!user || !pw) return;
  document.getElementById('error-msg').style.display = 'none';
  document.getElementById('loading-msg').style.display = 'block';
  try {
    const resp = await fetch(BASE + 'api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pw})
    });
    if (resp.ok) {
      window.location.href = BASE;
    } else {
      document.getElementById('error-msg').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('error-msg').style.display = 'block';
  }
  document.getElementById('loading-msg').style.display = 'none';
}
</script>
</body>
</html>"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML single-page interface (embedded)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FILES_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📂 Files — XKAgent</title>
<style>
  :root {
    --bg: #f3f5fb; --surface: #ffffff; --surface2: #f6f6f6; --border: rgba(28,31,35,.08);
    --text: #1c1f23; --text-muted: #57606a; --accent: #0066ff; --accent-hover: #0052cc;
    --err: #dc2626; --radius: 10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg); color: var(--text); min-height: 100vh;
  }
  .topbar {
    padding: 14px 24px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  .topbar h1 { font-size: 16px; color: var(--accent); font-weight: 600; }
  .crumb { font-size: 13px; color: var(--text-muted); display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
  .crumb a { color: var(--accent); cursor: pointer; text-decoration: none; }
  .crumb a:hover { text-decoration: underline; }
  .actions { margin-left: auto; display: flex; gap: 8px; }
  .actions button {
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 9999px;
    background: var(--surface); color: var(--text); cursor: pointer; font-size: 12px;
    transition: background .15s, color .15s;
  }
  .actions button:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
  #listing { max-width: 960px; margin: 24px auto; padding: 0 24px; }
  table {
    width: 100%; border-collapse: collapse; font-size: 13px;
    background: var(--surface); border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(28,31,35,.05);
  }
  th, td { text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--border); }
  th {
    color: var(--text-muted); font-weight: 600; font-size: 12px;
    position: sticky; top: 0; background: var(--surface2);
  }
  tr:hover td { background: #f7f9fc; }
  .name { cursor: pointer; }
  .name .icon { margin-right: 6px; }
  .dir .name { color: var(--accent); font-weight: 500; }
  .size { color: var(--text-muted); text-align: right; }
  .mtime { color: var(--text-muted); }
  .empty { padding: 48px; text-align: center; color: var(--text-muted); }
  .err { padding: 16px 24px; color: var(--err); font-size: 13px; }
  #loading { padding: 48px; text-align: center; color: var(--text-muted); }
</style>
</head>
<body>
<div class="topbar">
  <h1>📂 Files</h1>
  <div class="crumb" id="crumbs"></div>
  <div class="actions">
    <select id="root-sel" title="允许根（workdir + 挂载）" style="padding:5px 8px;border:1px solid var(--border);border-radius:9999px;font-size:12px;color:var(--text);background:var(--surface);max-width:340px;"></select>
    <button onclick="refresh()" title="Refresh">🔄 Refresh</button>
    <button onclick="window.close()" title="Close this tab">✖ Close</button>
  </div>
</div>
<div id="listing"><div id="loading">Loading...</div></div>
<script>
// ── 相对路径基准：适配反向代理子路径（如 DSW /dsw-xxx/proxy/4096/）──
// 所有 fetch/WS/跳转基于 BASE 拼接，避免绝对路径丢失代理前缀
const BASE = (function(){ var p = location.pathname; return p.endsWith('/') ? p : p.substring(0, p.lastIndexOf('/') + 1); })();
let currentPath = '';
let currentRoot = '';   // 当前所在允许根的绝对路径
let rootsList = [];     // 允许根集合（workdir + 挂载）
const qpInit = new URLSearchParams(location.search);
const currentSession = qpInit.get('session') || '';   // 透传 session（缺省后端回退当前会话）

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function renderCrumbs(root, rel) {
  const crumb = document.getElementById('crumbs');
  crumb.innerHTML = '';
  // 第一级：允许根（workdir 或挂载根），点击回到该根
  const home = document.createElement('a');
  home.textContent = root || '/';
  home.title = root || '/';
  home.onclick = () => load(root || '');
  crumb.appendChild(home);
  const parts = rel ? rel.split('/').filter(Boolean) : [];
  let acc = '';
  parts.forEach((p, i) => {
    const sep = document.createElement('span');
    sep.textContent = ' / ';
    crumb.appendChild(sep);
    acc = acc ? acc + '/' + p : p;
    const a = document.createElement('a');
    a.textContent = p;
    if (i === parts.length - 1) {
      a.style.color = 'var(--text)';
      a.style.cursor = 'default';
    } else {
      a.onclick = () => load(root + '/' + acc);
    }
    crumb.appendChild(a);
  });
}

function load(path) {
  currentPath = path || '';
  const listing = document.getElementById('listing');
  listing.innerHTML = '<div id="loading">Loading...</div>';
  const q = 'path=' + encodeURIComponent(currentPath) + (currentSession ? '&session=' + encodeURIComponent(currentSession) : '');
  fetch(BASE + 'api/files/list?' + q)
    .then(r => {
      if (!r.ok) return r.json().then(d => { throw new Error(d.detail || '加载失败'); });
      return r.json();
    })
    .then(data => {
      currentRoot = data.root || currentPath;
      renderCrumbs(data.root, data.path);
      renderList(data.entries, data.root);
      syncRootSel(data.roots);
    })
    .catch(err => {
      listing.innerHTML = '';
      const errorEl = document.createElement('div');
      errorEl.className = 'err';
      errorEl.textContent = '❌ ' + err.message;
      listing.appendChild(errorEl);
    });
}

function renderList(entries, root) {
  const listing = document.getElementById('listing');
  if (!entries.length) {
    listing.innerHTML = '<div class="empty">(空目录)</div>';
    return;
  }
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Name</th><th class="size">Size</th><th>Modified</th></tr>';
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  const sessQ = currentSession ? '&session=' + encodeURIComponent(currentSession) : '';
  entries.forEach(e => {
    const tr = document.createElement('tr');
    tr.className = e.is_dir ? 'dir' : 'file';
    const tdName = document.createElement('td');
    tdName.className = 'name';
    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.textContent = e.is_dir ? '📁' : '📄';
    tdName.appendChild(icon);
    tdName.appendChild(document.createTextNode(e.name));
    tdName.onclick = () => {
      const abs = (root ? root + '/' : (currentPath ? currentPath + '/' : '')) + e.name;
      if (e.is_dir) {
        load(abs);
      } else {
        window.open(BASE + 'api/files/download?path=' + encodeURIComponent(abs) + sessQ);
      }
    };
    const tdSize = document.createElement('td');
    tdSize.className = 'size';
    tdSize.textContent = e.is_dir ? '' : fmtSize(e.size);
    const tdTime = document.createElement('td');
    tdTime.className = 'mtime';
    tdTime.textContent = fmtTime(e.mtime);
    tr.appendChild(tdName);
    tr.appendChild(tdSize);
    tr.appendChild(tdTime);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  listing.innerHTML = '';
  listing.appendChild(table);
}

function refresh() { load(currentPath); }
function syncRootSel(roots) {
  rootsList = roots || rootsList;
  const sel = document.getElementById('root-sel');
  if (!sel) return;
  sel.innerHTML = '';
  rootsList.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r;
    opt.textContent = r;
    sel.appendChild(opt);
  });
  if (currentRoot) sel.value = currentRoot;
}
function loadRoots() {
  fetch(BASE + 'api/files/roots' + (currentSession ? '?session=' + encodeURIComponent(currentSession) : ''))
    .then(r => r.json())
    .then(data => { syncRootSel(data.roots); })
    .catch(() => {});
}
// 支持 ?path= 参数定位目录（路径链接/文件浏览器入口）；?session= 透传会话
const qp = new URLSearchParams(location.search);
load(qp.get('path') || '');
loadRoots();
</script>
</body>
</html>
"""
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XKAgent Web</title>
<style>
  :root {
    --bg: #f3f5fb;
    --surface: #ffffff;
    --surface2: #f6f6f6;
    --surface3: #eef1f6;
    --text: #1c1f23;
    --text-muted: #57606a;
    --accent: #0066ff;
    --accent-hover: #0052cc;
    --accent-soft: rgba(0, 102, 255, 0.09);
    --warn: #d97706;
    --err: #dc2626;
    --border: rgba(28, 31, 35, 0.08);
    --radius: 12px;
    --tool-bg: #ffffff;
    --thinking-bg: #f6f8fa;
    --thinking-pre-bg: rgba(28, 31, 35, 0.06);
    --on-accent: #ffffff;
    --code-bg: #f6f8fa;
    --link: #0066ff;
    --sidebar-w: 320px;
  }
  html[data-theme="dark"] {
    --bg: #16161a;
    --surface: #232429;
    --surface2: #2e2f35;
    --surface3: #35363c;
    --text: rgba(249, 249, 249, 0.9);
    --text-muted: rgba(249, 249, 249, 0.6);
    --accent: #3295fb;
    --accent-hover: #5aa2fb;
    --accent-soft: rgba(50, 149, 251, 0.15);
    --warn: #ffc234;
    --err: #ff4f42;
    --border: rgba(255, 255, 255, 0.08);
    --tool-bg: #232429;
    --thinking-bg: #1e1f24;
    --thinking-pre-bg: rgba(255, 255, 255, 0.06);
    --on-accent: #ffffff;
    --code-bg: #1d1e22;
    --link: #77b0ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: row; overflow: hidden;
  }
  /* ── 左侧边栏（常驻，可折叠）── */
  #sidebar {
    width: var(--sidebar-w); flex-shrink: 0;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    height: 100vh; overflow-y: auto;
    transition: margin-left .25s ease, width .25s ease;
    z-index: 50;
  }
  body.sidebar-collapsed #sidebar { margin-left: calc(-1 * var(--sidebar-w)); }
  .sidebar-header {
    padding: 14px 16px; display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-header h1 { font-size: 16px; color: var(--accent); font-weight: 600; flex: 1; white-space: nowrap; }
  #btn-sidebar-toggle, #btn-hamburger {
    border: none; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 14px; padding: 4px 8px; border-radius: 8px;
    transition: background .15s, color .15s;
  }
  #btn-sidebar-toggle:hover, #btn-hamburger:hover { background: var(--surface2); color: var(--text); }
  .sidebar-status {
    padding: 10px 16px; font-size: 12px; color: var(--text-muted);
    display: flex; flex-direction: column; gap: 4px;
    border-bottom: 1px solid var(--border);
  }
  .sidebar-status #token-stats { margin-left: 0; }
  #session-label { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  #session-name { font-size: 16px; font-weight: 600; color: var(--text); word-break: break-all; }
  #provider-model { font-size: 11px; color: var(--text-muted); }
  #btn-ping { background: none; border: none; cursor: pointer; font-size: 12px; padding: 0 2px; color: var(--accent, #4a9eff); }
  #btn-ping:hover { opacity: 0.7; }
  /* ── 模型测试弹窗 ── */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; display: flex; align-items: center; justify-content: center; }
  .modal-box { background: var(--bg, #1e1e1e); border: 1px solid var(--border, #444); border-radius: 8px; max-width: 720px; width: 90%; max-height: 80vh; display: flex; flex-direction: column; }
  .modal-head { padding: 12px 16px; border-bottom: 1px solid var(--border, #444); display: flex; justify-content: space-between; align-items: center; }
  .modal-title { font-weight: bold; }
  .modal-close { cursor: pointer; background: none; border: none; font-size: 16px; color: var(--text-muted, #888); }
  .modal-body { padding: 12px 16px; overflow-y: auto; }
  .model-test-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .model-test-table th, .model-test-table td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border, #333); }
  .model-test-table th { color: var(--text-muted, #888); font-weight: normal; }
  .mt-ok { color: #4caf50; } .mt-fail { color: #f44336; }
  .mt-set-btn { font-size: 11px; padding: 2px 8px; margin: 0 4px; cursor: pointer; border: 1px solid var(--border, #555); border-radius: 4px; background: var(--bg2, #2a2a2a); color: inherit; }
  .mt-set-btn:hover { border-color: var(--accent, #4a9eff); }
  /* 消息耗时提示 */
  .msg-latency { font-size: 11px; color: var(--text-muted, #888); margin-left: 8px; }
  .session-input-row {
    padding: 10px 12px; display: flex; gap: 6px;
    border-bottom: 1px solid var(--border);
  }
  .session-input-row input {
    flex: 1; padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px;
    background: var(--surface2); color: var(--text); font-size: 12px; outline: none;
  }
  .session-input-row input:focus { border-color: var(--accent); }
  .session-input-row button {
    padding: 6px 12px; background: var(--accent); border: none; border-radius: 8px;
    color: #fff; cursor: pointer; font-size: 14px;
  }
  .session-input-row button:hover { background: var(--accent-hover); }
  #session-list { flex: 1; overflow-y: auto; padding: 8px; }
  .session-item {
    padding: 8px 10px; cursor: pointer; border-radius: 8px; margin: 2px 0; font-size: 13px;
    display: flex; align-items: center; gap: 4px;
  }
  .session-item > .session-name-text { flex: 1; word-break: break-all; }
  .session-item:hover { background: var(--surface2); }
  .session-item.active { background: var(--accent-soft); border-left: 3px solid var(--accent); }
  .session-stop {
    display: none; border: none; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 12px; padding: 2px 4px; border-radius: 4px;
    flex-shrink: 0;
  }
  .session-stop:hover { color: #f59e0b; background: rgba(245,158,11,.1); }
  .session-fork {
    display: none; border: none; background: transparent; color: var(--text-muted);
    cursor: pointer; font-size: 12px; padding: 2px 4px; border-radius: 4px;
    flex-shrink: 0;
  }
  .session-fork:hover { color: var(--accent); background: rgba(99,102,241,.12); }
  .session-item:hover .session-fork { display: inline-block; }
  .session-item:hover .session-stop { display: inline-block; }
  .session-mode-tag { font-size: 11px; color: var(--muted, #888); margin-left: 6px; }
  .session-lock-tag { font-size: 11px; color: #f59e0b; font-weight: bold; }
  .session-divider { margin: 10px 0 4px; padding-top: 8px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted, #888); text-transform: uppercase; letter-spacing: .05em; }
  .session-status-dot { font-size: 10px; color: #22c55e; margin-right: 4px; }
  .session-phase-tag { font-size: 11px; color: var(--accent); margin-left: 6px; font-weight: bold; }
  .session-pending-tag { font-size: 11px; color: #f59e0b; margin-left: 6px; font-weight: bold; }

  /* ── 主区域 ── */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; height: 100vh; }
  #btn-hamburger { display: none; position: fixed; top: 8px; left: 8px; z-index: 60; font-size: 18px; background: var(--surface); border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  body.sidebar-collapsed #btn-hamburger { display: block; }
  #chat {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 8px;
    width: 100%; max-width: 768px; margin: 0 auto;
  }
  #chat > * { flex-shrink: 0; }   /* F2: 防 flex 压缩子元素导致 scrollHeight 失真 */
  .msg { max-width: 85%; padding: 8px 12px; border-radius: var(--radius); line-height: 1.5; font-size: 14px; white-space: pre-wrap; word-break: break-word; }
  .msg.user {
    align-self: flex-end; background: var(--accent-soft); color: var(--text);
    border-top-right-radius: 4px;
  }
  .msg.assistant { align-self: flex-start; background: transparent; }
  .msg.assistant .msg-head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
  .msg.assistant .view-toggle { font-size: 10px; padding: 1px 8px; border: 1px solid var(--border); border-radius: 10px; background: transparent; color: var(--text-muted); cursor: pointer; opacity: 0.7; }
  .msg.assistant .view-toggle:hover { opacity: 1; }
  .msg.assistant .md-body { white-space: normal; }
  .msg.assistant .md-body pre { background: var(--code-bg); padding: 10px 12px; border-radius: 8px; overflow-x: auto; font-size: 12.5px; line-height: 1.45; border: 1px solid var(--border); }
  .msg.assistant .md-body code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 12.5px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .msg.assistant .md-body pre code { background: none; padding: 0; border: none; }
  .msg.assistant .md-body h1, .msg.assistant .md-body h2, .msg.assistant .md-body h3 { margin: 10px 0 6px; line-height: 1.3; }
  .msg.assistant .md-body h1 { font-size: 17px; }
  .msg.assistant .md-body h2 { font-size: 15.5px; }
  .msg.assistant .md-body h3 { font-size: 14px; }
  .msg.assistant .md-body p { margin: 4px 0; }
  .msg.assistant .md-body ul, .msg.assistant .md-body ol { margin: 4px 0; padding-left: 20px; }
  .msg.assistant .md-body blockquote { margin: 6px 0; padding: 2px 12px; border-left: 3px solid var(--accent); color: var(--text-muted); }
  .msg.assistant .md-body a { color: var(--link); }
  .msg.assistant .md-body a.file-link { text-decoration: underline; cursor: pointer; }
  .msg.assistant .md-body table { border-collapse: collapse; margin: 6px 0; font-size: 13px; }
  .msg.assistant .md-body th, .msg.assistant .md-body td { border: 1px solid var(--border); padding: 4px 10px; }
  .msg.assistant .md-body th { background: var(--surface2); }
  .msg.assistant .md-body hr { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
  .msg.assistant .code-body { white-space: pre-wrap; word-break: break-word; }
  .msg.tool {
    align-self: flex-start; background: var(--tool-bg); font-size: 12px; color: var(--text-muted);
    border: 1px solid var(--border);
  }
  .msg.system { align-self: flex-start; background: transparent; font-size: 12px; color: var(--text-muted); text-align: left; }   /* 2026-08-07: 统一左对齐（历史+实时） */
  .msg.error { align-self: center; background: var(--err); color: #fff; font-size: 12px; }
  .msg .msg-meta { font-size: 11px; opacity: 0.7; margin-bottom: 4px; }
  .msg-meta-outside {
    align-self: flex-end;
    font-size: 11px;
    color: var(--text-muted);
    opacity: 0.7;
    margin-bottom: 2px;
    padding: 0 4px;
  }
  .msg details { cursor: pointer; }
  .msg details summary { font-weight: bold; }
  .msg.tool details pre {
    margin-top: 6px; font-size: 11px; white-space: pre-wrap; word-break: break-all;
    color: var(--text-muted); max-height: 200px; overflow-y: auto;
  }
  .msg details[open] summary { margin-bottom: 4px; }
  /* 2026-08-11: 工具参数 markdown 渲染——不截断（覆盖 .msg.tool details pre 的 max-height） */
  .msg.tool .tool-args-md { margin-top: 6px; font-size: 12px; }
  .msg.tool .tool-args-md pre { max-height: none; overflow: visible; }
  .msg.tool .tool-args-md code { white-space: pre-wrap; word-break: break-word; }
  .msg.thinking-box {
    align-self: flex-start; background: var(--thinking-bg); max-width: 85%;
    padding: 6px 12px; border-radius: var(--radius); border: 1px solid var(--border);
  }
  .msg.thinking-box summary { font-size: 12px; color: var(--text-muted); cursor: pointer; font-weight: bold; }
  .msg.thinking-box pre.thinking-content {
    margin-top: 6px; font-size: 11px; white-space: pre-wrap; word-break: break-all;
    color: var(--text-muted); max-height: 200px; overflow-y: auto;
    background: var(--thinking-pre-bg); padding: 6px 8px; border-radius: 4px;
  }
  .tool-call-info { font-size: 11px; color: var(--warn); }
  .thinking { align-self: flex-start; color: var(--text-muted); font-size: 12px; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 1; } }
  .status-indicator {
    display: inline-block; padding: 5px 14px; border-radius: 9999px;
    font-size: 11px; font-weight: 600;
    background: var(--surface2); color: var(--text-muted);
    border: 1px solid var(--border); user-select: none;
    white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis;
    transition: background .15s, color .15s, border-color .15s;
  }
  .status-indicator.clickable { cursor: pointer; }
  .status-indicator.clickable:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
  .status-indicator.mode-indicator { min-width: 96px; text-align: center; }
  .status-indicator.skill-indicator { min-width: 72px; text-align: center; }
  .status-indicator.skill-indicator.skill-on { color: var(--accent); border-color: var(--accent); }
  .status-indicator.skill-indicator.skill-off { opacity: .55; }
  .status-indicator.lock-indicator { display: none; }
  .status-indicator.lock-indicator.has-lock { display: inline-block; background: rgba(245,158,11,.15); color: #b45309; }
  .status-indicator.lock-indicator.has-lock:hover { background: #b45309; color: #fff; }

  /* ── 底部输入区 + 工具条 ── */
  .input-area {
    padding: 12px 24px 16px; background: var(--surface); border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 8px;
    width: 100%; max-width: 768px; margin: 0 auto;
  }
  .input-row { display: flex; gap: 8px; align-items: flex-end; }
  .input-area textarea {
    flex: 1; padding: 12px 16px; border: 1px solid var(--border); border-radius: 24px;
    background: var(--surface2); color: var(--text); font-family: inherit; font-size: 14px;
    resize: none; min-height: 44px; max-height: 120px;
    transition: border-color .15s;
  }
  .input-area textarea:focus { outline: none; border-color: var(--accent); }
  .input-area .send-btn {
    padding: 10px 22px; border: none; border-radius: 9999px;
    background: var(--accent); color: #fff; cursor: pointer; font-weight: 500;
    transition: background .15s; height: 44px; align-self: flex-end;
  }
  .input-area .send-btn:hover { background: var(--accent-hover); }
  .input-area .send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .input-area .upload-btn {
    padding: 10px 14px; border: 1px solid var(--border); border-radius: 9999px;
    background: var(--surface2); color: var(--text); cursor: pointer; font-size: 15px;
    transition: background .15s, color .15s; height: 44px; align-self: flex-end;
    flex-shrink: 0;
  }
  .input-area .upload-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

  .toolbar {
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    padding: 0 4px; font-size: 12px;
  }
  .toolbar button {
    padding: 4px 12px; border: 1px solid var(--border); border-radius: 9999px;
    background: var(--surface2); color: var(--text); cursor: pointer; font-size: 12px;
    transition: background .15s, color .15s;
  }
  .toolbar button:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
  .toolbar-spacer { flex: 1; }

  /* ── 右侧定位锚点竖条 ── */
  #msg-anchors {
    position: fixed; right: 10px; top: 70px; bottom: 175px;
    display: flex; flex-direction: column; gap: 4px; z-index: 80;
    overflow-y: auto; align-items: center;
  }
  .anchor-dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--accent); opacity: .55; cursor: pointer;
    transition: background .15s, transform .15s;
    flex-shrink: 0;
  }
  .anchor-dot:hover { background: var(--accent); transform: scale(1.4); opacity: 1; }
  .anchor-dot.active { background: var(--accent); transform: scale(1.3); opacity: 1; }

  /* ── 回底圆点 ── */
  #back-to-bottom, #jump-last-user {
    position: fixed; right: 18px; width: 42px; height: 42px;
    border-radius: 50%; border: none; cursor: pointer;
    background: var(--accent); color: #fff; font-size: 16px;
    box-shadow: 0 2px 12px rgba(0,0,0,.15); z-index: 85;
    transition: opacity .15s, transform .15s;
  }
  #back-to-bottom { bottom: 18px; display: none; }
  #jump-last-user { bottom: 68px; background: var(--surface2); color: var(--accent); border: 1px solid var(--border); font-weight: bold; }
  #jump-last-user:hover { background: var(--accent); color: #fff; }
  #back-to-bottom.show { display: block; }
  #back-to-bottom:hover { background: var(--accent-hover); transform: translateY(-2px); }

  /* ── 移动端响应式 ── */
  @media (max-width: 768px) {
    #sidebar { position: fixed; left: 0; top: 0; bottom: 0; box-shadow: 2px 0 12px rgba(0,0,0,.08); }
    body.sidebar-collapsed #sidebar { margin-left: calc(-1 * var(--sidebar-w)); }
    #btn-hamburger { display: block; }
    #chat { padding-top: 44px; }
    .msg { max-width: 92%; }
    #msg-anchors { right: 6px; top: 44px; bottom: 155px; }
    #jump-last-user { bottom: 64px; right: 14px; }
  }

  /* ── 用户友好度增强：info 消息（非错误警示）/ 骨架屏 / 消息时间戳 ── */
  .msg.info {
    align-self: center; background: var(--accent-soft); color: var(--text);
    font-size: 12px; border: 1px solid var(--border);
  }
  .msg-time {
    font-size: 10px; color: var(--text-muted); opacity: 0.65;
    margin-left: 8px; font-weight: normal;
  }
  .msg.system .msg-time { display: block; text-align: left; margin: 2px 0 0; }   /* 2026-08-07: system 时间戳随左对齐 */
  .msg.error .msg-time, .msg.info .msg-time { display: block; text-align: center; margin: 2px 0 0; }
  .crash-notice {
    align-self: stretch;
    margin: 4px 0 8px;
    padding: 8px 12px;
    border: 1px solid #e5534b;
    border-left: 4px solid #e5534b;
    background: rgba(229, 83, 75, 0.08);
    border-radius: var(--radius);
    color: #ffb3ad;
    font-size: 12.5px;
    line-height: 1.5;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .crash-notice .crash-ack {
    background: transparent;
    border: 1px solid #e5534b;
    color: #ffb3ad;
    border-radius: 4px;
    padding: 2px 8px;
    cursor: pointer;
    font-size: 12px;
    flex-shrink: 0;
  }
  .crash-notice .crash-ack:hover { background: rgba(229, 83, 75, 0.15); }
  .session-crash-tag {
    color: #ff6b60;
    font-weight: bold;
    font-size: 12px;
  }
  .loading-skeleton {
    align-self: center; display: flex; flex-direction: column; gap: 8px;
    width: 60%; padding: 12px 16px; border-radius: var(--radius);
    background: var(--surface2); border: 1px solid var(--border);
  }
  .loading-skeleton .sk-line {
    height: 12px; border-radius: 6px;
    background: linear-gradient(90deg, var(--surface3) 25%, var(--surface2) 50%, var(--surface3) 75%);
    background-size: 200% 100%;
    animation: sk-shimmer 1.2s infinite;
  }
  .loading-skeleton .sk-line.short { width: 40%; }
  .loading-skeleton .sk-line.mid { width: 70%; }
  @keyframes sk-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>
</head>
<body>
<div id="sidebar">
  <div class="sidebar-header">
    <h1>⚡ XKAgent</h1>
    <button id="btn-sidebar-toggle" onclick="toggleSidebar()" title="Collapse sidebar">◀</button>
  </div>
  <div class="sidebar-status">
    <span id="session-label"><span id="session-name">-</span></span>
    <span id="provider-model"></span>
    <button id="btn-ping" title="测试所有模型联通性" onclick="openModelTestModal()">⚡</button>
    <span id="token-stats"></span>
  </div>
  <div class="session-input-row">
    <input id="new-session-name" placeholder="New session name">
    <button onclick="createSession()" title="Create session">+</button>
  </div>
  <div id="session-list"></div>
</div>

<div id="main">
  <button id="btn-hamburger" onclick="toggleSidebar()" title="Menu">☰</button>
  <div id="chat"></div>
  <div class="input-area">
    <div class="input-row">
      <textarea id="input" rows="1" placeholder="Enter 发送 · Shift+Enter 换行 · ↑/↓ 顶/底行切历史 · Tab 切 mode"
                onkeydown="return onInputKeydown(event)"></textarea>
      <button id="upload-btn" class="upload-btn" onclick="document.getElementById('file-input').click()"
              title="上传文件到 .xkagent/files/（可多选）">📤</button>
      <input type="file" id="file-input" multiple style="display:none" onchange="uploadFiles(this)">
      <button id="send-btn" class="send-btn" onclick="send()">Send</button>
    </div>
    <div class="toolbar">
      <span id="mode-indicator" class="status-indicator mode-indicator clickable" title="点击切换 mode"></span>
      <span id="skill-indicator" class="status-indicator skill-indicator clickable" title="点击切换 skill"></span>
      <span id="lock-indicator" class="status-indicator lock-indicator" title="回合执行锁状态"></span>
      <button id="btn-theme" onclick="toggleTheme()" title="Toggle light/dark theme">☀️ light</button>
      <span class="toolbar-spacer"></span>
      <button onclick="window.open(BASE + 'files')" title="Open file browser (new tab)">📂 Files</button>
      <button onclick="clearChat()" title="Clear the current browser view without deleting history">🗑️ Clear View</button>
      <button onclick="loadSkills()" title="List skills">📦 Skills</button>
    </div>
  </div>
</div>

<div id="msg-anchors"></div>
<button id="jump-last-user" onclick="jumpToLastUserMsg()" title="跳到最近一条历史输入">⌃</button>
<button id="back-to-bottom" onclick="scrollToBottomForce()" title="Back to bottom">⬇</button>

<script>
// ── 相对路径基准：适配反向代理子路径（如 DSW /dsw-xxx/proxy/4096/）──
// 所有 fetch/WS/跳转基于 BASE 拼接，避免绝对路径丢失代理前缀
const BASE = (function(){ var p = location.pathname; return p.endsWith('/') ? p : p.substring(0, p.lastIndexOf('/') + 1); })();
let ws = null;
let currentSession = '';
let isStreaming = false;
let curMode = 'plan';
let curSkillEnabled = true;
let curLockMsg = null;   // 非空时状态胶囊显示锁占用
let hasWsStats = false;    // v2: 本会话是否收到过 WS stats(当前轮). false=仅看到 db 的上一轮

let sessionCache = {};      // session -> {lastId, messages, ids, nodes}；切回直接复用 DOM 节点（nodes）+ 增量拉取
let loadToken = 0;          // 防竞态：每次 loadSessionMessages 递增，响应时校验仍是当前会话才渲染
const MAX_CACHED_SESSIONS = 5;  // LRU 上限：切换多个会话后释放旧缓存（nodes/messages），防内存无限增长
let reconnectDelay = 1000;  // WS 重连退避基数（1s→2s→4s→...→30s 上限）
let pendingQueue = [];      // 断线期间用户发送的普通消息队列（重连后自动 flush，P0 修复）

function updateModeIndicator() {
  const el = document.getElementById('mode-indicator');
  if (!el) return;
  el.textContent = curMode;
  // 事件只绑定一次：点击=切 mode（保留 Tab 快捷键 toggleMode）
  if (!el.dataset.bound) {
    el.dataset.bound = '1';
    el.onclick = function() { toggleMode(); };
  }
}
function updateSkillIndicator() {
  const el = document.getElementById('skill-indicator');
  if (!el) return;
  el.textContent = curSkillEnabled ? '🎯 skill ON' : '🚫 skill OFF';
  el.classList.toggle('skill-on', curSkillEnabled);
  el.classList.toggle('skill-off', !curSkillEnabled);
  // 事件只绑定一次：点击=切 skill
  if (!el.dataset.bound) {
    el.dataset.bound = '1';
    el.onclick = function() { toggleSkillSelect(); };
  }
}
function updateLockIndicator() {
  const el = document.getElementById('lock-indicator');
  if (!el) return;
  if (curLockMsg) {
    el.textContent = curLockMsg;
    el.classList.add('has-lock');
  } else {
    el.textContent = '';
    el.classList.remove('has-lock');
  }
}
let stickToBottom = true;   // 智能滚动：贴底时自动跟随，用户上卷后不打扰
let olderTriggered = false;   // 滚动到顶触发历史加载的防抖标志（避免连续滚动重复触发）
let currentWorkdir = '';    // 项目根（fetchStatus 获取，用于路径链接化）

// ── 输入历史（↑/↓ 切换，对齐 repl._RawReader 的 _history_up/_history_down 语义）──
let inputHistory = [];    // 历史数组，最新在末尾（与 repl._history 顺序一致）
let histIdx = -1;         // -1=未在浏览；>=0=当前浏览下标（对齐 repl._hist_idx）
let histDraft = '';       // 浏览前的草稿，按 ↓ 越过最新一条后恢复（对齐 repl._hist_pending）
let histLoaded = false;   // 后端历史是否已加载；加载失败则静默降级（不阻塞输入）

function loadInputHistory() {
  fetch(BASE + 'api/history?limit=500')
    .then(r => r.json())
    .then(data => { inputHistory = (data.history || []).slice(); histLoaded = true; })
    .catch(() => { histLoaded = false; });   // 降级：API 不可用时仅禁用 ↑/↓
}

function historyUp() {
  if (!histLoaded || inputHistory.length === 0) return;
  if (histIdx === -1) {                       // 首次 ↑：保存当前草稿，跳到最新一条
    histDraft = document.getElementById('input').value;
    histIdx = inputHistory.length - 1;
  } else if (histIdx > 0) {                   // 继续 ↑：逐条回溯
    histIdx -= 1;
  } else {
    return;                                   // 已到最旧一条：保持不动（对齐 repl）
  }
  setHistoryValue();
}

function historyDown() {
  if (!histLoaded || histIdx === -1) return;  // 未在浏览状态：↓ 不动作（对齐 repl）
  histIdx += 1;
  if (histIdx >= inputHistory.length) {       // 越过最新一条：恢复浏览前草稿
    histIdx = -1;
    document.getElementById('input').value = histDraft;
    histDraft = '';
  } else {
    setHistoryValue();
  }
}

function setHistoryValue() {
  const input = document.getElementById('input');
  input.value = inputHistory[histIdx];
  input.selectionStart = input.selectionEnd = input.value.length;  // 光标移到末尾
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
}

// 光标是否位于输入框第一行（之前无换行符）——↑ 触发历史切换的判定条件
function isCaretAtFirstLine() {
  const input = document.getElementById('input');
  return input.value.slice(0, input.selectionStart).indexOf('\n') === -1;
}
// 光标是否位于输入框最后一行（之后无换行符）——↓ 触发历史切换的判定条件
function isCaretAtLastLine() {
  const input = document.getElementById('input');
  return input.value.slice(input.selectionEnd).indexOf('\n') === -1;
}

function onInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); return false; }
  if (e.key === 'Tab' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); toggleMode(); return false; }
  if (e.key === 'ArrowUp' && !e.shiftKey) {
    // ↑ 仅在第一行或已进入历史浏览时接管为历史切换；多行中间位置让浏览器默认移动光标
    if (histIdx >= 0 || isCaretAtFirstLine()) { e.preventDefault(); historyUp(); return false; }
  }
  if (e.key === 'ArrowDown' && !e.shiftKey) {
    // ↓ 仅在最后一行或已进入历史浏览时接管为历史切换；多行中间位置让浏览器默认移动光标
    if (histIdx >= 0 || isCaretAtLastLine()) { e.preventDefault(); historyDown(); return false; }
  }
  return true;   // 其余按键（含 Shift+Enter 换行、Shift+↑/↓ 选择）交给浏览器默认行为
}

function connect() {
  if (ws) ws.close();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + BASE + 'ws');
  ws.onopen = () => {
    reconnectDelay = 1000;    // 连接成功 → 重置退避
    addMsg('system', '🟢 Connected');
    // P0 修复：断线期间积压的普通消息重连后自动 flush（不丢失用户输入）
    if (pendingQueue.length) {
      const q = pendingQueue.slice();
      pendingQueue = [];
      q.forEach(t => ws.send(JSON.stringify({type:'chat', text:t})));
      addMsg('info', '📤 已自动重发 ' + q.length + ' 条离线消息');
    }
    fetchStatus().then(() => {
      // P0 修复：重连路径保留当前视图（不清空、不滚底），仅增量补新
      if (currentSession) loadSessionMessages(currentSession, {preserveView:true});
    });
    fetchSessions();  // FIX: 连接建立即加载 sessions 列表（问题1：初始为空）
  };
  ws.onclose = () => {
    // P2: 指数退避重连（1s→30s 上限），避免频繁失败时消息刷屏
    addMsg('system', '🔴 连接断开（' + Math.round(reconnectDelay/1000) + 's 后重试）');
    var d = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    setTimeout(connect, d);
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleWsMsg(msg);
    } catch(err) {}
  };
}

function handleWsMsg(msg) {
  const type = msg.type;
  if (type === 'text') {
    isStreaming = true;
    setStopBtnVisible(true);
    collapseLastToolResult();   // 2026-08-10: 文本开始 → 折叠上一个 tool_result 框
    clearThinkingWait();        // 思考阶段结束（或无需等待提示）
    appendStreaming(msg.data);
  } else if (type === 'thinking') {
    // 重构：thinking 显示为可折叠框（无内容时仅占位）
    // P1-1: thinking 即回合开始，置 isStreaming 拦截重复发送（对齐后端 busy）
    isStreaming = true;
    setStopBtnVisible(true);
    collapseLastToolResult();   // 2026-08-10: 新一轮思考开始 → 折叠上一个 tool_result 框
    ensureThinkingEl();
    startThinkingWait();        // 2026-08-10: 思考等待心跳——卡顿可见
  } else if (type === 'thinking_content') {
    // 重构：思考链内容实时追加到折叠框
    clearThinkingWait();        // 2026-08-10: 有内容 → 移除等待占位
    appendThinking(msg.data);
    startThinkingWait();        // 2026-08-10: 重置 15s 窗口，思考流中停顿仍可见
  } else if (type === 'clear_thinking') {
    // 重构：思考完成 → 封板保留折叠框（可展开回顾），不删除
    finalizeThinking();
  } else if (type === 'tool_call') {
    // 重构：tool 调用独立折叠框（tool_call 到达时封板当前文本段，防止顶出）
    collapseLastToolResult();   // 2026-08-10: 工具开始 → 折叠上一个 tool_result 框
    clearThinkingWait();
    appendToolBox('tool_call', msg.data);
  } else if (type === 'tool_progress') {
    // 方案2: 工具执行进度实时追加到当前 tool 折叠框
    appendToolProgress(msg.data);
  } else if (type === 'tool_result') {
    // 重构：tool 结果独立折叠框
    appendToolBox('tool_result', msg.data);
  } else if (type === 'stats') {
    // 对齐 repl：显示本轮（单次 LLM 调用）token 用量，不再取会话累积值
    hasWsStats = true;   // v2: 已收到当前轮 stats -> 后续 updateStats 不回退上一轮
    updateStatsFrom(msg.data || {});
  } else if (type === 'done') {
    // 重构：done → 封板 thinking 与文本段（thinking 保留可展开）
    collapseLastToolResult();   // 2026-08-10: 回合结束 → 折叠最后一个 tool_result 框
    clearThinkingWait();
    finalizeThinking();
    var _lastAsst = lastAssistantEl;  // finalizeTextEl 末尾置 null，先保存引用
    // 2026-08-10: 实时回合结束，给 assistant 头部补耗时提示（距上条 user 消息）
    if (_lastAsst && lastUserMsgTs) {
      var latHead = _lastAsst.querySelector('.msg-head');
      var latStr2 = formatLatency(lastUserMsgTs, new Date().toISOString());
      if (latHead && latStr2 && !latHead.querySelector('.msg-latency')) {
        var latSpan2 = document.createElement('span');
        latSpan2.className = 'msg-latency';
        latSpan2.textContent = '⏱ ' + latStr2;
        latHead.appendChild(latSpan2);
      }
    }
    finalizeTextEl();
    isStreaming = false;
    setStopBtnVisible(false);
    updateStats();
    scrollToBottomIfSticky();   // 2026-08-12: done 后贴底才补滚（markdown 高度变化）；用户上卷不打扰
    fetchSessions();  // 需求1：回合完成即时刷新列表（状态→idle，忙标记清除）
    // A+B: 回合完成 → 静默增量记账（推进 lastId/ids），防止后续增量拉取重渲本回合已实时渲染的消息
    if (currentSession) syncCacheAfterDone(currentSession);
  } else if (type === 'skill_select_changed') {
    updateSkillSelectUI(msg.data);
  } else if (type === 'mode_changed') {
    setMode(msg.data);
  } else if (type === 'lock_status_changed') {
    // T7: 观察者模式锁状态 → 更新工具栏 ⏳/🔒 标签
    updateLockTag(msg.data);
  } else if (type === 'session_switched') {
    // P2: 切换 session 后重置流式状态（旧回合可能被取消且未发 done，
    // 若不清除 isStreaming 将导致前端无法再发送新消息）
    clearThinkingWait();
    collapseLastToolResult();
    finalizeThinking();
    finalizeTextEl();
    isStreaming = !!msg.data.busy;   // 目标 session 正在执行 → 输入框显示 Stop（问题1修复）
    setStopBtnVisible(isStreaming);
    currentSession = msg.data.session;
    document.getElementById('session-name').textContent = currentSession;
    delete pendingSessions[currentSession];  // 已进入该会话：清除待查看标记
    touchRecent(currentSession);  // 需求2：ws 路径同步 MRU（与 switchSession 对齐）
    hasWsStats = false;  // v2: 切 session 重置——新会话未收到 stats 前显示其上一轮(db last_*)
    loadSessionMessages(currentSession);
    fetchSessions();
    fetchStatus();  // FIX: 切 session 后刷新 mode 标签（显示新 session 真实 mode）
  } else if (type === 'command_result') {
    if (msg.data) { addMsg("tool", "💻 " + msg.data); }
  } else if (type === 'error') {
    // 重构：error → 封板 thinking 与文本段
    collapseLastToolResult();   // 2026-08-10: 错误 → 折叠 tool_result 框
    clearThinkingWait();
    finalizeThinking();
    finalizeTextEl();
    isStreaming = false;
    setStopBtnVisible(false);
    addMsg('error', '❌ ' + msg.data);
  } else if (type === 'system') {
    addMsg('system', msg.data);
  } else if (type === 'info') {
    // P1: 非错误提示（如 busy / 离线排队 / 处理中）— 浅色样式而非红色 ❌
    addMsg('info', msg.data);
  }
}

let lastAssistantEl = null;   // 当前活跃的文本段（无全局累积框，每轮 LLM 输出一段）
let thinkingEl = null;        // 当前活跃的 thinking 折叠框

// ── 2026-08-10: 实时追踪最新消息（需求1）──
let lastToolCallDetails = null;    // 当前活跃的 tool_call 折叠框（tool_result 到达时折叠）
let lastToolResultDetails = null;  // 当前活跃的 tool_result 折叠框（下一条消息到达时折叠）

function collapseLastToolResult() {
  // 当前 tool_result 消息结束（thinking/text/tool_call/done 到达）→ 折叠回去
  if (lastToolResultDetails) {
    lastToolResultDetails.open = false;
    lastToolResultDetails = null;
  }
}

// ── 思考等待心跳（需求2）：LLM 卡顿（无 chunk）时前端显示等待占位，避免"死寂"──
let thinkingWaitTimer = null;
let thinkingWaitEl = null;
let thinkingWaitStart = 0;
const THINKING_WAIT_THRESHOLD_MS = 15000;   // 15s 无内容 → 显示等待占位

function startThinkingWait() {
  clearThinkingWaitTimer();
  thinkingWaitStart = Date.now();
  thinkingWaitTimer = setTimeout(function tick() {
    const el = ensureThinkingEl();
    if (!el) return;
    const pre = el.querySelector('pre.thinking-content');
    if (!pre) return;
    if (!thinkingWaitEl || !thinkingWaitEl.parentNode) {
      thinkingWaitEl = document.createElement('div');
      thinkingWaitEl.className = 'thinking-wait';
      pre.appendChild(thinkingWaitEl);
    }
    const secs = Math.round((Date.now() - thinkingWaitStart) / 1000);
    thinkingWaitEl.textContent = '⏳ 模型思考中…已等待 ' + secs + 's';
    scrollToBottomIfSticky();
    thinkingWaitTimer = setTimeout(tick, 1000);   // 每秒刷新秒数
  }, THINKING_WAIT_THRESHOLD_MS);
}
function clearThinkingWaitTimer() {
  if (thinkingWaitTimer) { clearTimeout(thinkingWaitTimer); thinkingWaitTimer = null; }
}
function clearThinkingWait() {
  clearThinkingWaitTimer();
  if (thinkingWaitEl && thinkingWaitEl.parentNode) {
    thinkingWaitEl.parentNode.removeChild(thinkingWaitEl);
  }
  thinkingWaitEl = null;
}

// ── Thinking 折叠框（重构：像 tool 一样可折叠展示）──
function ensureThinkingEl() {
  // 不存在或已封板 → 新建 thinking 折叠框
  if (!thinkingEl || thinkingEl.dataset.finalized === 'true') {
    finalizeTextEl();  // thinking 是新一轮的开始，先封板上一段文本
    const chat = document.getElementById('chat');
    thinkingEl = document.createElement('div');
    thinkingEl.className = 'msg thinking-box';
    thinkingEl.dataset.finalized = 'false';
    const details = document.createElement('details');
    details.open = true;  // 2026-08-10: 实时渲染——思考进行中自动展开，结束后 finalizeThinking 折叠
    const summary = document.createElement('summary');
    summary.textContent = '🧠 Thinking' + fmtTimeSuffix();
    const pre = document.createElement('pre');
    pre.className = 'thinking-content';
    pre.textContent = '';
    details.appendChild(summary);
    details.appendChild(pre);
    thinkingEl.appendChild(details);
    chat.appendChild(thinkingEl);
    appendAnchor(thinkingEl);
  }
  return thinkingEl;
}

function appendThinking(text) {
  const el = ensureThinkingEl();
  const pre = el.querySelector('pre.thinking-content');
  // P2 性能优化：appendChild 追加文本节点，避免 += 每次重建整个 text node（长思考链卡顿）
  pre.appendChild(document.createTextNode(text));
  scrollToBottomIfSticky();   // 2026-08-12: 贴底才跟随，用户上卷不打扰
}

function finalizeThinking() {
  if (thinkingEl) {
    // P2-2: 无思考链内容时显示占位文案（模型无 reasoning_content 时避免空框）
    const pre = thinkingEl.querySelector('pre.thinking-content');
    if (pre && !pre.textContent) pre.textContent = '(no thinking content)';
    // 2026-08-10: 思考结束（clear_thinking/done/error）→ 折叠回去，保留可展开回顾
    const det = thinkingEl.querySelector('details');
    if (det) det.open = false;
    // A+B: thinking 封板 → 完整思考链记账（增量去重；前端占位文本不记）
    var _tfinal = pre ? (pre.textContent || '') : '';
    if (_tfinal && _tfinal !== '(no thinking content)') trackRendered('thinking', _tfinal);
    thinkingEl.dataset.finalized = 'true';  // 保留框，可展开回顾
    thinkingEl = null;
    clearThinkingWait();
  }
}
// ── Markdown 渲染（内联轻量渲染器，零依赖；先 escape 防 XSS，URL 协议白名单）──
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function mdInline(t) {
  // 行内：code → bold → italic → strikethrough → link（顺序保证 code 内不被二次处理）
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  t = t.replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>');
  t = t.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|mailto:[^)\s]+|#[^\s)]*)\)/g,
                '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return t;
}
function mdBlock(raw) {
  // 逐块解析：代码围栏 / 表格 / 标题 / 引用 / 列表 / 分隔线 / 段落
  const lines = String(raw).replace(/\r\n/g, '\n').split('\n');
  let html = '', i = 0, listStack = [];
  const closeLists = (to) => { while (listStack.length > to) { html += '</' + listStack.pop() + '>'; } };
  const esc = escapeHtml;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      closeLists(0);
      const lang = fence[1];
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      const cls = lang ? ' class="lang-' + esc(lang) + '"' : '';
      html += '<pre' + cls + '><code>' + esc(buf.join('\n')) + '</code></pre>\n';
      continue;
    }
    if (i + 1 < lines.length && /^\|.*\|$/.test(line) && /^\|[\s:|-]+\|$/.test(lines[i + 1])) {
      closeLists(0);
      const headers = line.split('|').slice(1, -1).map(s => s.trim());
      const aligns = lines[i + 1].split('|').slice(1, -1).map(s =>
        s.includes(':') ? (s.startsWith(':') && s.endsWith(':') ? ' style="text-align:center"' :
                           (s.startsWith(':') ? ' style="text-align:left"' : ' style="text-align:right"')) : '');
      html += '<table><thead><tr>';
      headers.forEach((h, idx) => { html += '<th' + aligns[idx] + '>' + mdInline(esc(h)) + '</th>'; });
      html += '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && /^\|.*\|$/.test(lines[i])) {
        const cells = lines[i].split('|').slice(1, -1).map(s => s.trim());
        html += '<tr>';
        cells.forEach((c, idx) => { html += '<td' + (aligns[idx] || '') + '>' + mdInline(esc(c)) + '</td>'; });
        html += '</tr>';
        i++;
      }
      html += '</tbody></table>\n';
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { closeLists(0); html += '<h' + h[1].length + '>' + mdInline(esc(h[2])) + '</h' + h[1].length + '>\n'; i++; continue; }
    if (/^>\s?/.test(line)) {
      closeLists(0);
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i++; }
      html += '<blockquote>' + mdInline(esc(buf.join(' '))) + '</blockquote>\n';
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      const tag = ul ? 'ul' : 'ol';
      if (listStack[listStack.length - 1] !== tag) { closeLists(0); html += '<' + tag + '>'; listStack.push(tag); }
      html += '<li>' + mdInline(esc((ul || ol)[1])) + '</li>\n';
      i++;
      continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { closeLists(0); html += '<hr>\n'; i++; continue; }
    if (!line.trim()) { closeLists(0); html += '\n'; i++; continue; }
    closeLists(0);
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i]) && !/^\|.*\|$/.test(lines[i]) &&
           !/^(#{1,3})\s/.test(lines[i]) && !/^>\s?/.test(lines[i]) &&
           !/^\s*[-*+]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])) {
      buf.push(lines[i]); i++;
    }
    html += '<p>' + mdInline(esc(buf.join(' '))) + '</p>\n';
  }
  closeLists(0);
  return html;
}
function mdToHtml(raw) { return mdBlock(raw); }
// ── 路径链接化：将回复中的项目路径/文件名转为可点击链接（新标签打开）──
// 三档匹配：绝对(项目根前缀) / 相对(含目录分隔) / 裸文件名(后缀白名单)
// 设计：DOM 后处理（TreeWalker 文本节点），零侵入 mdToHtml；
//       跳过 code/pre/a 内部，避免破坏代码块与已有链接；href 仅指向自家 /api/files/open。
const FILE_EXT_SET = 'markdown|ipynb|webp|yaml|json|html|docx|jpeg|conf|toml|java|tsx|jsx|txt|tex|rst|log|css|htm|xml|sql|ini|cfg|png|jpg|gif|svg|bmp|ico|pdf|csv|yml|php|cpp|md|py|js|ts|go|rs|rb|sh|c|h';
const BARE_FILE_RE = new RegExp('[\\p{L}\\p{N}_.-]+\\.(?:' + FILE_EXT_SET + ')(?![\\p{L}\\p{N}_])', 'giu');
const REL_PATH_RE = /(?:\.{0,2}\/)?[\p{L}\p{N}_.-]+\/[\p{L}\p{N}_.\/-]*/gu;
// URL 禁区：https?:// 或 // 起头的连续非空白段（防止 URL 端口/路径被误链为本地文件）
// 只链文件：路径必须以白名单扩展名结尾（目录/无扩展名不链）。
const FILE_TAIL_RE = new RegExp('\\.(?:' + FILE_EXT_SET + ')$', 'i');
const URL_SPAN_RE = /(?:https?:\/\/|\/\/)[^\s，。；、）)\]'"`]+/gi;

function collectPathMatches(text) {
  const cands = [];
  // 0) URL 禁区：匹配 https?:// 或 // 起头的连续段，候选与其重叠则丢弃
  const urlSpans = [];
  let um;
  while ((um = URL_SPAN_RE.exec(text))) urlSpans.push([um.index, um.index + um[0].length]);
  const inUrlSpan = (s, e) => urlSpans.some(([us, ue]) => s < ue && e > us);
  // 左边界：前一字符为 ASCII 文件名字符（字母/数字/_/./-）→ 视为粘连，丢弃
  const hasLB = (s) => s <= 0 || !/[A-Za-z0-9_.\/-]/.test(text[s - 1]);
  // 1) 绝对路径：项目根前缀（最精确，避免误链任意 /xxx）
  if (currentWorkdir) {
    const esc = currentWorkdir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const absRe = new RegExp(esc + '[\\p{L}\\p{N}._\\/-]*', 'gu');   // * 允许 workdir 本身匹配
    let m;
    while ((m = absRe.exec(text))) {
      if (!inUrlSpan(m.index, m.index + m[0].length)) {
        if (FILE_TAIL_RE.test(trimPathTail(m[0]))) cands.push({s: m.index, e: m.index + m[0].length, p: m[0]});
      }
    }
  }
  // 2) 相对路径：至少含一个目录分隔符；排除 / 开头的本地API路径（/api/...）
  let m;
  while ((m = REL_PATH_RE.exec(text))) {
    if (m[0].startsWith('/')) continue;  // /xxx 形式（非项目根前缀）不链
    if (hasLB(m.index) && !inUrlSpan(m.index, m.index + m[0].length)) {
      if (FILE_TAIL_RE.test(trimPathTail(m[0]))) cands.push({s: m.index, e: m.index + m[0].length, p: m[0]});
    }
  }
  // 3) 裸文件名：文本/代码/图片等常见后缀白名单
  while ((m = BARE_FILE_RE.exec(text))) {
    if (hasLB(m.index) && !inUrlSpan(m.index, m.index + m[0].length)) {
      cands.push({s: m.index, e: m.index + m[0].length, p: m[0]});
    }
  }
  // 排序 + 重叠去重（保留最长匹配，避免绝对/相对/裸名互相覆盖）
  cands.sort((a, b) => a.s - b.s || (b.e - b.s) - (a.e - a.s));
  const merged = [];
  for (const c of cands) {
    const last = merged[merged.length - 1];
    if (last && c.s < last.e) {
      if (c.e > last.e) { last.e = c.e; last.p = c.p; }
      continue;
    }
    merged.push(c);
  }
  return merged;
}

function trimPathTail(p) {
  // 0) 剥离中文连接词前缀（和manager.py → manager.py；说明文档.md 不受影响：
  //    仅当中文后紧跟 ASCII 字母数字时才剥离，中文文件名后是 . 则不剥离）
  p = p.replace(/^[\u4e00-\u9fff]+(?=[A-Za-z0-9_])/u, '');
  // 1) 截断到第一个白名单扩展名：处理中文/连字符/句号粘连（如 web.py和x.py → web.py）。
  const re = new RegExp('[\\p{L}\\p{N}_.-]+?\\.(?:' + FILE_EXT_SET + ')', 'u');   // 非贪婪：截到第一个扩展名
  const m = p.match(re);
  if (m) return p.slice(0, m.index + m[0].length);
  return p;
}

function linkifyTextNode(node) {
  const text = node.nodeValue || '';
  const parent = node.parentElement;
  if (!text || !parent) return;
  // 跳过代码块/行内代码/已有链接内部（避免破坏 markdown 渲染结构）
  if (parent.closest('pre, a')) return;  // 跳过代码块/已有链接；行内 code 内的路径也链接（LLM 惯用反引号包裹路径）
  const frag = document.createDocumentFragment();
  let rest = text;
  let done = 0;
  // 循环：截断粘连后剩余文本重新匹配（web.py和manager.py → 两个链接）
  while (rest.length) {
    const matches = collectPathMatches(rest);
    if (!matches.length) break;
    const mt = matches[0];               // 最左匹配（去重后有序）
    mt.p = trimPathTail(mt.p);           // 截断粘连（web.py和x.py → web.py）
    if (!mt.p.length) { frag.appendChild(document.createTextNode(rest)); done += rest.length; break; }
    mt.e = mt.s + mt.p.length;
    if (mt.s > 0) frag.appendChild(document.createTextNode(rest.slice(0, mt.s)));
    const a = document.createElement('a');
    a.className = 'file-link';
    a.href = BASE + 'api/files/open?path=' + encodeURIComponent(mt.p);
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = mt.p;
    frag.appendChild(a);
    done += mt.e;
    rest = rest.slice(mt.e);
  }
  if (done < text.length) frag.appendChild(document.createTextNode(text.slice(done)));
  parent.replaceChild(frag, node);
}

function linkifyFilePaths(container) {
  if (!container) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(linkifyTextNode);
}

// ── 文本段（每轮 LLM 输出独立成框，打字机效果）──
function ensureTextEl() {
  if (!lastAssistantEl || lastAssistantEl.dataset.finalized === 'true') {
    const chat = document.getElementById('chat');
    lastAssistantEl = document.createElement('div');
    lastAssistantEl.className = 'msg assistant';
    lastAssistantEl.dataset.finalized = 'false';
    // markdown 渲染：dataset 保存原文与当前视图（text=markdown / code=原文）
    lastAssistantEl.dataset.raw = '';
    lastAssistantEl.dataset.mode = getDefaultViewMode();
    const head = document.createElement('div');
    head.className = 'msg-head';
    const btn = document.createElement('button');
    btn.className = 'view-toggle';
    btn.type = 'button';
    btn.textContent = lastAssistantEl.dataset.mode === 'text' ? 'TXT' : 'CODE';
    btn.onclick = function() { toggleMsgView(lastAssistantEl); };
    head.appendChild(btn);
    const tspan = document.createElement('span');
    tspan.className = 'msg-time';
    tspan.textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
    head.appendChild(tspan);
    lastAssistantEl.appendChild(head);
    const body = document.createElement('div');
    body.className = lastAssistantEl.dataset.mode === 'text' ? 'md-body' : 'code-body';
    lastAssistantEl.appendChild(body);
    chat.appendChild(lastAssistantEl);
    appendAnchor(lastAssistantEl);
  }
  return lastAssistantEl;
}

function appendStreaming(text) {
  const el = ensureTextEl();
  el.dataset.raw += text;   // 原文累积，done 后统一渲染
  const body = el.querySelector('.md-body, .code-body');
  if (body) {
    if (el.dataset.mode === 'text') {
      // 流式阶段以纯文本打字机展示；done 时 finalizeTextEl 统一 markdown 渲染
      body.appendChild(document.createTextNode(text));
    } else {
      body.textContent = el.dataset.raw;
    }
  }
  scrollToBottomIfSticky();   // 2026-08-12: 文本流式贴底才跟随（8-10 force 过度修复已撤销）
}

function finalizeTextEl() {
  if (lastAssistantEl) {
    // markdown 渲染：text 模式在收尾时把累积原文渲染为 HTML
    if (lastAssistantEl.dataset.mode === 'text') {
      const body = lastAssistantEl.querySelector('.md-body');
      const raw = lastAssistantEl.dataset.raw;
      const el = lastAssistantEl;
      const render = function() {
        if (body) body.innerHTML = mdToHtml(raw);
        if (body) linkifyFilePaths(body);   // 路径链接化（done 后）
      };
      // P2 性能优化：超长消息（>8KB）延迟到 rAF 渲染，避免 markdown 解析阻塞主线程导致卡顿
      if (raw.length > 8192 && window.requestAnimationFrame) {
        requestAnimationFrame(render);
      } else {
        render();
      }
    }
    // A+B: assistant 封板 → 完整文本记账（增量内容级去重）
    if (lastAssistantEl.dataset.raw) trackRendered('assistant', lastAssistantEl.dataset.raw);
    lastAssistantEl.dataset.finalized = 'true';
    lastAssistantEl = null;
  }
}

// ── TXT/CODE 视图切换（TXT=markdown 渲染，CODE=原文；偏好存 localStorage）──
function getDefaultViewMode() {
  try { return (localStorage.getItem('mdViewMode') || 'text') === 'code' ? 'code' : 'text'; } catch(e) { return 'text'; }
}
function toggleMsgView(el) {
  const isText = el.dataset.mode === 'text';
  el.dataset.mode = isText ? 'code' : 'text';
  try { localStorage.setItem('mdViewMode', el.dataset.mode); } catch(e) {}
  const btn = el.querySelector('.view-toggle');
  if (btn) btn.textContent = el.dataset.mode === 'text' ? 'TXT' : 'CODE';
  const body = el.querySelector('.md-body, .code-body');
  if (!body) return;
  if (el.dataset.mode === 'text') {
    body.className = 'md-body';
    body.innerHTML = mdToHtml(el.dataset.raw);
    linkifyFilePaths(body);   // 切回 TXT 时路径链接化
  } else {
    body.className = 'code-body';
    body.textContent = el.dataset.raw;
  }
}

// ── 方案2: 工具执行进度渲染（rAF 节流批量追加，行数上限防卡）──
var lastToolBoxProgressEl = null;
var pendingProgressLines = [];
var progressRafScheduled = false;
var TOOL_PROGRESS_MAX_LINES = 500;   // 行数上限：超限丢弃后续行（进度尽力而为）

// ── 2026-08-11: 工具参数 markdown 模板渲染（替代裸 JSON.stringify）──
// 设计：args 逐参数转 markdown 模板，复用 mdToHtml 渲染：
//   标量 → **key**: 值；长文本/代码 → **key**: + ```lang 代码块（不截断）；
//   对象/数组 → **key**: + ```json 代码块
function mdCodeBlock(text, lang) {
  // 防围栏冲突：行首 ``` 加零宽空格拆开，避免提前闭合代码块
  var safe = String(text).replace(/^```/gm, '`\u200b``');
  return '```' + (lang || '') + '\n' + safe + '\n```';
}
function toolArgsToMarkdown(name, args, fileContent) {
  if (!args || typeof args !== 'object') {
    return mdCodeBlock(args === undefined ? '' : String(args), 'json');
  }
  var parts = [];
  var keys = Object.keys(args);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var v = args[k];
    var label = '**' + k + '**:';
    if (v === null || v === undefined) { parts.push(label + ' ' + v); continue; }
    if (typeof v === 'string') {
      // pythonrt + .py 文件 → 优先用后端预读内容
      if (k === 'code_or_filepath' && name === 'pythonrt' && v.trim().endsWith('.py') && fileContent) {
        parts.push(label + '\n' + mdCodeBlock(fileContent, 'python'));
        continue;
      }
      if (v.indexOf('\n') !== -1 || v.length > 120) {
        parts.push(label + '\n' + mdCodeBlock(v, k === 'code_or_filepath' ? 'python' : ''));
      } else {
        parts.push(label + ' `' + v + '`');
      }
      continue;
    }
    if (typeof v === 'object') {
      parts.push(label + '\n' + mdCodeBlock(JSON.stringify(v, null, 2), 'json'));
      continue;
    }
    parts.push(label + ' ' + v);
  }
  return parts.join('\n\n');
}

function summaryBodyFromOutput(output) {
  if (!output) return '';
  return String(output).split('\n').filter(function(line) {
    return !/^📌/.test(line) && !/^✅ 已持久化/.test(line) && !/^📄/.test(line) && !/^🏷️/.test(line);
  }).join('\n').trim();
}

function appendToolProgress(data) {
  if (!data || !data.line) return;
  pendingProgressLines.push(data.line);
  if (!progressRafScheduled) {
    progressRafScheduled = true;
    if (window.requestAnimationFrame) requestAnimationFrame(flushProgressLines);
    else flushProgressLines();
  }
}

function flushProgressLines() {
  progressRafScheduled = false;
  if (!lastToolBoxProgressEl) { pendingProgressLines = []; return; }
  var pre = lastToolBoxProgressEl;
  if (pendingProgressLines.length > 0 && pre.style.display === 'none') {
    pre.style.display = '';   // 首次有进度时显示进度区
  }
  for (var i = 0; i < pendingProgressLines.length; i++) {
    if (pre.childElementCount >= TOOL_PROGRESS_MAX_LINES) break;
    var div = document.createElement('div');
    div.className = 'progress-line';
    div.textContent = pendingProgressLines[i];
    pre.appendChild(div);
  }
  pendingProgressLines = [];
  scrollToBottomIfSticky();   // 2026-08-12: 进度贴底才跟随，用户上卷不打扰
}

function appendToolBox(kind, data) {
  // tool_call 到达时先封板当前文本段（finalizeTextEl），保证每轮 LLM 输出独立成框，
  // 文本框按序排列、不会被 tool 框顶出视口。
  if (kind === 'tool_call') finalizeTextEl();
  const chat = document.getElementById('chat');
  const el = document.createElement('div');
  el.className = 'msg tool';
  const details = document.createElement('details');
  details.open = (kind === 'tool_call' || kind === 'tool_result'); // 2026-08-10: 实时渲染自动展开，结束后折叠
  const summary = document.createElement('summary');
  if (kind === 'tool_call') {
    var stepStr = (data.total && data.total > 1) ? ' (' + ((data.index || 0) + 1) + '/' + data.total + ')' : '';
    var modeTag = data.mode ? ' [' + data.mode + ']' : '';
    summary.textContent = '🔧 ' + (data.name || 'tool') + stepStr + modeTag + fmtTimeSuffix();
    details.appendChild(summary);
    if (data.args) {
      // 2026-08-11: markdown 模板渲染（复用 mdToHtml），替代裸 JSON.stringify
      var mdDiv = document.createElement('div');
      mdDiv.className = 'tool-args-md';
      mdDiv.innerHTML = mdToHtml(toolArgsToMarkdown(data.name, data.args, data.file_content));
      details.appendChild(mdDiv);
    }
    // 方案2: 进度区（初始隐藏，tool_progress 事件逐行填充，实时可见执行中输出）
    var progPre = document.createElement('pre');
    progPre.className = 'tool-progress';
    progPre.style.display = 'none';
    details.appendChild(progPre);
    lastToolBoxProgressEl = progPre;
    lastToolCallDetails = details;   // 2026-08-10: 记录当前 tool_call 框（tool_result 到达时折叠）
  } else {  // tool_result
    var elapsedStr = (data.elapsed !== undefined) ? ' (' + data.elapsed.toFixed(2) + 's)' : '';
    summary.textContent = '💻 Exit: ' + (data.exit_code !== undefined ? data.exit_code : '?') + elapsedStr + fmtTimeSuffix();
    details.appendChild(summary);
    var pre = document.createElement('pre');
    var out = data.stdout || '(no output)';
    if (data.stderr) out += '\n[stderr]\n' + data.stderr;
    if (data.error) out += '\n[error] ' + data.error;
    pre.textContent = out;
    details.appendChild(pre);
    // 2026-08-10: 工具执行结束 → 折叠 tool_call 框（含进度区），结果框展开
    if (lastToolCallDetails) { lastToolCallDetails.open = false; lastToolCallDetails = null; }
    // 2026-08-11: summary/exit 是收尾结果 → 不参与"下一条消息到达时折叠"（保持展开可见）
    var isTerminal = (data.name === 'summary' || data.name === 'exit');
    if (!isTerminal) { lastToolResultDetails = details; }   // 记录结果框，下一条消息到达时折叠
    // 2026-08-12: summary 正文渲染——在折叠框后追加类似 assistant 的正文块，直接显示 content
    // stdout 格式: 📌key / ✅已持久化 / 📄title / content / 🏷️tags，按行过滤提取正文
    var _content = '';
    var _cdiv = null;
    if (data.name === 'summary' && data.stdout) {
      _content = summaryBodyFromOutput(data.stdout);
      if (_content) {
        _cdiv = document.createElement('div');
        _cdiv.className = 'msg assistant summary-content';   // 复用正文样式
        var _cbody = document.createElement('div');
        _cbody.className = 'md-body';
        _cbody.innerHTML = mdToHtml(_content);   // markdown 渲染（与正文一致）
        linkifyFilePaths(_cbody);               // 路径链接化
        _cdiv.appendChild(_cbody);
      }
    }
  }
  el.appendChild(details);
  // 2026-08-12: 折叠框追加后再追加 summary 正文块（顺序：details 在前，正文在后）
  if (_cdiv) el.appendChild(_cdiv);
  chat.appendChild(el);
  appendAnchor(el);
  scrollToBottomIfSticky();   // 2026-08-12: 工具框贴底才跟随，用户上卷不打扰
}

function addMsg(role, text) {
  const chat = document.getElementById('chat');
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.textContent = text;
  // A+B: user 实时回显记账（供增量拉取内容级去重，消除双渲染）
  if (role === 'user') { el.dataset.raw = text; trackRendered('user', text); }
  // P2: 时间戳 — system/error/info/tool 消息显示 HH:MM（用户友好度）
  if (role === 'system' || role === 'error' || role === 'info' || role === 'tool') {
    const t = document.createElement('span');
    t.className = 'msg-time';
    t.textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
    el.appendChild(t);
  }
  chat.appendChild(el);
  appendAnchor(el);
  scrollToBottomIfSticky();
  return el;   // 修复：返回节点供调用方操作（loading 提示/移除等；原实现返回 undefined 导致 loadingEl 崩溃）
}

// ── 智能滚动：仅贴底时自动跟随；用户上卷(距底>40px)后流式内容不再强拉到底部 ──
function scrollToBottomIfSticky(force) {
  // 2026-08-12: force 参数已无调用方（实时路径全部改贴底跟随），保留作防御性接口
  if (force) stickToBottom = true;
  if (!stickToBottom) return;
  const chat = document.getElementById('chat');
  if (!chat) return;
  // 2026-08-10 修复: force 时同步立即滚底（读 scrollHeight 强制 reflow，值必然准确）
  // + rAF 补滚（覆盖异步布局/图片加载后的偏移）——原实现仅单次 rAF，
  // 高频流式 chunk 下 rAF 可能被合并/跳过导致滚动丢失（"强制滚底没起作用"根因之一）。
  chat.scrollTop = chat.scrollHeight;
  if (window.requestAnimationFrame) {
    requestAnimationFrame(function() { chat.scrollTop = chat.scrollHeight; });
  }
}
// F4a: 强制滚底（同步立即 + rAF 补滚）。用于历史加载完成后——append 后 DOM 已更新，
// 同步赋值 scrollHeight 必然准确；rAF 仅作保险，即使 rAF 失败同步滚动也已生效。
// 不依赖 stickToBottom（加载历史后无条件滚底显示最新消息）。
function forceScrollToBottom() {
  const chat = document.getElementById('chat');
  if (!chat) return;
  // 首次同步立即无条件滚底（DOM 刚更新，scrollHeight 必然准确）。
  // 修复: 原实现 doScroll() 首次调用就被 F8（距底>40px→return）拦截，
  // 因 clearChat 后 scrollTop=0，加载新消息后 scrollHeight-clientHeight 必然>40，
  // 导致切换 session 后消息停在顶部不滚底。
  chat.scrollTop = chat.scrollHeight;
  // F7b: 后续补滚 try-catch——scrollTop 赋值永不抛异常，
  // 保证后续 rAF/setTimeout 补滚必然注册（否则异常会短路整条补滚链）
  const doScroll = function() {
    try {
      // F8: 用户已上卷（距底>40px）→ 停止补滚，尊重用户操作（场景N修复）。
      // 仅 rAF/setTimeout 补滚阶段执行此判断——首次同步已在上方无条件完成。
      if (chat.scrollHeight - chat.scrollTop - chat.clientHeight > 40) return;
      chat.scrollTop = chat.scrollHeight;
    } catch(e) { /* 忽略：任何异常都不允许中断补滚链 */ }
  };
  // ②同步补滚（首次已在上方执行）
  if (window.requestAnimationFrame) {
    requestAnimationFrame(doScroll);   // ②rAF 补滚（下一帧，布局稳定后）
  }
  // F7c: ③-⑧六级递进补滚——覆盖字体/图片/异步资源/浏览器 scroll restoration 任意时机。
  // F7a: 已移除 scrollIntoView（其可能在 fetch 微任务回调抛异常短路补滚链）。
  setTimeout(doScroll, 50);
  setTimeout(doScroll, 200);
  setTimeout(doScroll, 500);
  setTimeout(doScroll, 1000);
  setTimeout(doScroll, 1500);
  setTimeout(doScroll, 2000);
}
function onChatScroll() {
  const chat = document.getElementById('chat');
  const dist = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
  stickToBottom = dist < 40;
  // 滚到顶 → 触发历史消息补渲染（olderTriggered 防抖：只在上次未触发过时执行一次）
  if (chat.scrollTop <= 2 && !olderTriggered) {
    olderTriggered = true;
    renderOlderBatch(currentSession);
  } else if (chat.scrollTop > 2) {
    olderTriggered = false;
  }
  updateAnchorHighlight();
  const btn = document.getElementById('back-to-bottom');
  if (btn) btn.classList.toggle('show', dist > 300);
}
function clearChat() {
  const chat = document.getElementById('chat');
  chat.innerHTML = '';
  chat.scrollTop = 0;   // F4b: 重置滚动位置，杜绝浏览器恢复旧位置（刷新后停在顶部）
  resetAnchors();
  lastAssistantEl = null;
  thinkingEl = null;
}

function sendStop() {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({type: 'interrupt'}));
}


// ── 右侧定位锚点：每条消息一个 dot，点击跳转；滚动时高亮当前消息 ──
let anchorCounter = 0;
const MAX_ANCHORS = 50;
function appendAnchor(el) {
  if (!el || !el.classList || !el.classList.contains('msg')) return;
  if (!el.classList.contains('user')) return;   // 只给 user 消息建锚点
  const anchors = document.getElementById('msg-anchors');
  if (!anchors) return;
  const idx = anchorCounter++;
  el.dataset.anchorIdx = idx;
  const dot = document.createElement('div');
  dot.className = 'anchor-dot' + (el.classList.contains('user') ? ' user' : '');
  dot.dataset.idx = idx;   // 修复：dot 记录稳定标识，与 el.dataset.anchorIdx 对应（updateAnchorHighlight 精确匹配）
  dot.title = (el.classList.contains('user') ? '👤 历史输入 ' : '') + '跳到消息 ' + (idx + 1);
  dot.onclick = function() {
    el.scrollIntoView({block: 'start', behavior: 'smooth'});
  };
  anchors.appendChild(dot);
  // 上限：超出的移除最旧锚点，避免长会话锚点条过密
  while (anchors.children.length > MAX_ANCHORS) {
    anchors.removeChild(anchors.firstChild);
  }
}
function jumpToLastUserMsg() {
  const chat = document.getElementById('chat');
  if (!chat) return;
  const users = chat.querySelectorAll('.msg.user');
  if (users.length) {
    users[users.length - 1].scrollIntoView({block: 'start', behavior: 'smooth'});
  }
}
function resetAnchors() {
  anchorCounter = 0;
  const anchors = document.getElementById('msg-anchors');
  if (anchors) anchors.innerHTML = '';
}
function updateAnchorHighlight() {
  const chat = document.getElementById('chat');
  const anchors = document.getElementById('msg-anchors');
  if (!chat || !anchors) return;
  const dots = anchors.querySelectorAll('.anchor-dot');
  const msgs = chat.querySelectorAll('.msg.user');
  const st = chat.scrollTop;
  let activeIdx = null;   // 修复：稳定标识——视口内最后一条 user 消息的 anchorIdx
  let activePos = 0;      // 位置退化：旧节点无 anchorIdx 时按位置对齐（容错）
  for (let i = 0; i < msgs.length; i++) {
    if (msgs[i].offsetTop - chat.offsetTop <= st + 80) {
      activePos = i;
      if (msgs[i].dataset.anchorIdx != null) activeIdx = msgs[i].dataset.anchorIdx;
    }
  }
  dots.forEach(function(d, i) {
    const hit = activeIdx != null ? (d.dataset.idx === activeIdx) : (i === activePos);
    d.classList.toggle('active', hit);
  });
}
function scrollToBottomForce() {
  const chat = document.getElementById('chat');
  if (chat) chat.scrollTop = chat.scrollHeight;
  const btn = document.getElementById('back-to-bottom');
  if (btn) btn.classList.remove('show');
  stickToBottom = true;
}

function setStopBtnVisible(show) {
  const sendBtn = document.getElementById('send-btn');
  if (sendBtn) {
    sendBtn.textContent = show ? '⏹' : 'Send';
    sendBtn.title = show ? '停止生成' : '发送';
    sendBtn.onclick = show ? sendStop : send;
  }
}

function send() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || isStreaming) {
    // 2026-08-12 方案③: 被 busy 拦截时给出可见提示（此前静默丢弃，用户误以为无反馈）
    if (text && isStreaming) {
      addMsg('info', '⏳ 正在处理中，请稍候再发送（当前回合未结束）');
      input.focus();
    }
    return;
  }
  input.value = '';

  // ── 输入历史：内存入队（末尾去重，对齐 repl._add_history）+ 异步写回后端 ──
  if (text && (inputHistory.length === 0 || inputHistory[inputHistory.length - 1] !== text)) {
    inputHistory.push(text);
  }
  histIdx = -1;    // 提交后重置浏览游标（对齐 repl: 新输入从最新历史重新开始）
  histDraft = '';
  fetch(BASE + 'api/history', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text})
  }).catch(() => {});   // 写回失败不阻塞发送（历史仅尽力持久化）
  input.style.height = 'auto';

  stickToBottom = true;   // 用户主动发送 → 恢复贴底跟随
  addMsg('user', text);
  lastUserMsgTs = new Date().toISOString();   // 2026-08-10: 记录实时发送时间（耗时提示基准）
  finalizeTextEl();

  if (ws && ws.readyState === WebSocket.OPEN) {
    if (text.startsWith('!') || text.startsWith('/')) {
      ws.send(JSON.stringify({type: 'command', cmd: text}));
    } else {
      ws.send(JSON.stringify({type: 'chat', text: text}));
    }
  } else {
    // P0 修复：断线时普通消息入队（重连后自动 flush），命令消息提示暂不可用
    if (text.startsWith('!') || text.startsWith('/')) {
      addMsg('error', '❌ 离线中，命令暂不可用（重连后重试）');
    } else {
      pendingQueue.push(text);
      addMsg('info', '⏳ 离线中，消息已排队，重连后自动发送');
    }
    connect();
  }
  input.focus();               // 4B: 发送后自动聚焦
}
// ── 文件上传：上传到 .xkagent/files/，返回路径并插入输入框 ──
const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico'];
function formatSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
async function uploadFiles(input) {
  const files = input.files;
  if (!files || files.length === 0) return;
  for (let i = 0; i < files.length; i++) {
    const fd = new FormData();
    fd.append('file', files[i]);
    addMsg('info', '⏳ 上传中: ' + files[i].name);
    try {
      const resp = await fetch(BASE + 'api/upload', {method: 'POST', body: fd});
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        addMsg('error', '❌ 上传失败: ' + (data.detail || data.error || ('HTTP ' + resp.status)));
        continue;
      }
      const inputEl = document.getElementById('input');
      const sep = (inputEl.value && !inputEl.value.endsWith(' ')) ? ' ' : '';
      inputEl.value += sep + data.path;
      inputEl.style.height = 'auto';
      const msgEl = addMsg('tool', '📎 已上传: ' + data.path + ' (' + formatSize(data.size) + (data.is_image ? ', 图片' : '') + ')');
      const link = document.createElement('a');
      link.href = BASE + data.url;
      link.target = '_blank';
      link.textContent = ' 打开';
      link.style.marginLeft = '8px';
      msgEl.appendChild(link);
      if (data.is_image) {
        const img = document.createElement('img');
        img.src = BASE + data.url;
        img.style.maxWidth = '180px'; img.style.maxHeight = '120px';
        img.style.borderRadius = '8px'; img.style.marginTop = '4px';
        img.style.cursor = 'pointer';
        img.onclick = function() { window.open(BASE + data.url); };
        msgEl.appendChild(img);
      }
    } catch (e) {
      addMsg('error', '❌ 上传异常: ' + e.message);
    }
  }
  input.value = '';
}




function toggleTheme() {
  const root = document.documentElement;
  const isDark = root.dataset.theme === 'dark';
  if (isDark) { delete root.dataset.theme; } else { root.dataset.theme = 'dark'; }
  try { localStorage.setItem('theme', isDark ? 'light' : 'dark'); } catch(e) {}
  updateThemeButton(!isDark);
}
function updateThemeButton(isDark) {
  const btn = document.getElementById('btn-theme');
  if (btn) btn.textContent = isDark ? '🌙 dark' : '☀️ light';
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('theme'); } catch(e) {}
  let isDark = false;
  if (saved === 'dark') isDark = true;
  else if (saved === 'light') isDark = false;
  else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) isDark = true;
  if (isDark) {
    document.documentElement.dataset.theme = 'dark';
    updateThemeButton(true);
  } else {
    updateThemeButton(false);
  }
}
function toggleSkillSelect() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'skill_select_toggle'}));
  }
}
function toggleMode() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'mode_toggle'}));
  }
}


function updateSkillSelectUI(enabled) {
  curSkillEnabled = !!enabled;
  updateSkillIndicator();
}
function modeLabel(mode) {
  const icons = {plan: '📐 plan', build: '🔧 build', 'build-unsafe': '🔥 build-unsafe'};
  return icons[mode] || mode;
}
function setMode(mode) {
  curMode = modeLabel(mode);
  updateModeIndicator();
}

function toggleSidebar() {
  const body = document.body;
  const collapsed = body.classList.toggle('sidebar-collapsed');
  try { localStorage.setItem('sidebarCollapsed', collapsed ? '1' : '0'); } catch(e) {}
  if (!collapsed) fetchSessions();
}
function initSidebar() {
  let collapsed = window.innerWidth < 768;
  try {
    const saved = localStorage.getItem('sidebarCollapsed');
    if (saved !== null) collapsed = saved === '1';
  } catch(e) {}
  if (collapsed) document.body.classList.add('sidebar-collapsed');
}

let sessionPrevBusy = {};   // session -> 上一轮是否忙（llm/in_tool），用于忙→闲检测
let pendingSessions = {};   // session -> true：回合已完成、等待用户查看
let lastSessionsSig = '';   // 上次渲染签名（防轮询闪烁）
let lastUserMsgTs = null;    // 最后一条 user 消息时间（assistant 耗时提示基准）
let recentOrder = [];       // MRU：最近选中的 session 在前（running 块按此排序，需求2）

function touchRecent(name) {
  // MRU 置顶：name 移到数组头部，其余保持原顺序（V8 稳定）
  recentOrder = [name, ...recentOrder.filter(x => x !== name)];
}

function fetchSessions() {
  fetch(BASE + 'api/sessions').then(r => r.json()).then(data => {
    // 忙→闲检测：非当前 session 回合完成 → 标记待查看
    // busy = llm 处理中 或 tool 执行中（starting 初始化不算回合执行，避免误标）
    const isBusy = s => {
      const ag = data.agents && data.agents[s];
      // 回合级判定（2026-08-10）：phase 在技能选择/tool 间隙临时复位 idle 会
      // 误判回合完成；turn_active 仅回合真正结束才复位，是权威忙闲信号。
      return !!ag && ag.status === 'running' && ag.turn_active === true;
    };
    data.sessions.forEach(s => {
      const ag = data.agents && data.agents[s];
      const busy = isBusy(s);
      const idleNow = !!ag && ag.status === 'running' && !busy;  // 仍在运行但已空闲 = 回合完成（基于 turn_active）
      if (sessionPrevBusy[s] === true && idleNow && s !== data.current) {
        pendingSessions[s] = true;   // 忙→闲 且非当前 → 待查看
      }
      sessionPrevBusy[s] = busy;
    });
    // 首次加载：用后端顺序初始化 MRU（current 置前），避免 recentOrder 为空
    if (recentOrder.length === 0 && data.sessions.length > 0) {
      recentOrder = [data.current, ...data.sessions.filter(s => s !== data.current)];
    }
    // 兜底校准（2026-08-12）：每次轮询响应都先同步 Send/Stop 按钮状态（口径与后端
    // _session_busy 一致：status==='running' && turn_active）。必须在 sig 短路之前执行——
    // 切走/刷新/断线后 done 丢失导致 isStreaming 卡 True，即使 agents 数据未变
    // （回合完成后 turn_active 复位为 false 但被 10s 低频轮询拖慢），按钮也要尽快恢复。
    if (data.current) {
      const agB = data.agents && data.agents[data.current];
      const busyNow = !!(agB && agB.status === 'running' && agB.turn_active);
      if (busyNow !== isStreaming) {
        isStreaming = busyNow;
        setStopBtnVisible(busyNow);
      }
    }
    // 当前会话不在流式执行时，每轮轮询都补拉 DB 增量；不能被侧边栏签名短路。
    var pollSession = data.current || currentSession;
    var pollCache = sessionCache[pollSession];
    var pollAgent = data.agents && data.agents[pollSession];
    if (pollCache && pollCache.ids && pollCache.ids.size && (!pollAgent || pollAgent.turn_active !== true)) {
      fetchIncremental(pollSession, pollCache, loadToken, null);
    }
    // 签名比较：sessions/current/agents/lock_tags/pending/recentOrder 均未变则跳过重渲染（防轮询闪烁）
    const sig = JSON.stringify([data.sessions, data.current, data.agents, data.lock_tags, pendingSessions, recentOrder]);
    if (sig === lastSessionsSig) return;
    lastSessionsSig = sig;
    const list = document.getElementById('session-list');
    list.innerHTML = '';
    // 对齐 repl /sessions：running 置顶，其余用分隔标题隔开
    const isRunning = s => data.agents && data.agents[s] && data.agents[s].status === 'running';
    const running = data.sessions.filter(isRunning);
    // 需求2：running 块按 MRU（最近选中）排序——当前选中第 1，上一次选中的第 2，以此类推
    // recentOrder 前端实时维护，不受后端 2s TTL 缓存影响；未在 MRU 的 session 稳定排序兜底置后
    const mruIdx = s => { const i = recentOrder.indexOf(s); return i === -1 ? 999 : i; };
    running.sort((a, b) => mruIdx(a) - mruIdx(b));
    const others = data.sessions.filter(s => !isRunning(s));
    const renderItem = s => {
      const el = document.createElement('div');
      el.className = 'session-item' + (s === data.current ? ' active' : '');
      // 对齐 repl /sessions 的 [running] 标记：running 会话前加绿色圆点
      if (isRunning(s)) {
        const dot = document.createElement('span');
        dot.className = 'session-status-dot';
        dot.textContent = '●';
        el.appendChild(dot);
      }
      // 会话名（独立 span，避免后续 textContent 清空徽标导致样式丢失）
      const nameSpan = document.createElement('span');
      nameSpan.className = 'session-name-text';
      nameSpan.textContent = s;
      el.appendChild(nameSpan);
      // 对齐 repl /sessions 的 🔒：他进程持锁时追加锁标记
      const lockTag = data.lock_tags && data.lock_tags[s];
      if (lockTag) {
        const lockSpan = document.createElement('span');
        lockSpan.className = 'session-lock-tag';
        lockSpan.textContent = ' ' + lockTag;
        el.appendChild(lockSpan);
      }

      // 对齐 repl /sessions 的 phase/in_tool 徽标：llm处理中⏳ / tool执行中🔧 / 初始化⏳starting（空闲不加）
      const ag = data.agents && data.agents[s];
      if (ag) {
        let phaseTag = "";
        if (ag.in_tool) phaseTag = "🔧tool";
        else if (ag.phase === "llm") phaseTag = "⏳llm";
        else if (ag.phase === "starting") phaseTag = "⏳starting";
        if (phaseTag) {
          const phaseSpan = document.createElement("span");
          phaseSpan.className = "session-phase-tag";
          phaseSpan.textContent = " " + phaseTag;
          el.appendChild(phaseSpan);
        }
        // 运行中会话显示 mode 徽标（复用输入框 modeLabel；仅 running 展示，空闲不占位）
        if (isRunning(s) && ag.mode) {
          const modeSpan = document.createElement("span");
          modeSpan.className = "session-mode-tag";
          modeSpan.textContent = " " + modeLabel(ag.mode);
          el.appendChild(modeSpan);
        }
      }
      // 崩溃符号：crashed 状态或 crash_notices 未 ack（崩溃过）都显示
      const cn = data.crash_notices && data.crash_notices[s];
      if ((ag && ag.status === 'crashed') || cn) {
        const crashSpan = document.createElement('span');
        crashSpan.className = 'session-crash-tag';
        crashSpan.textContent = ' 🔴crashed';
        crashSpan.title = '崩溃时间: ' + (cn ? cn.time : '') + '\n' + (cn ? (cn.error || '') : (ag && ag.error ? ag.error : ''));
        el.appendChild(crashSpan);
      }
      // 需求1：🔔 待查看 徽标（非当前 session 回合完成后提示用户查看）
      if (pendingSessions[s]) {
        const pendSpan = document.createElement('span');
        pendSpan.className = 'session-pending-tag';
        pendSpan.textContent = ' 🔔待查看';
        el.appendChild(pendSpan);
      }
      el.onclick = () => switchSession(s);
      // 面板 stop 按钮：停止运行会话的 agent 线程（保留数据，切换时自动恢复）。
      // 所有 running 会话（含当前）均显示；当前会话停止后自动切换到列表最靠前的
      // 其他会话（后端已切 focus 并返回 switched_to，避免 focus 置空失焦）。
      // 他进程持锁的会话不在本进程 agents 中 → isRunning=false → 天然不显示，
      // 不会误停他人持有的会话。
      // ── fork 快捷按钮：复制该会话为副本（所有 session 显示，hover 可见）──
      const forkBtn = document.createElement('button');
      forkBtn.className = 'session-fork';
      forkBtn.textContent = '\u29c9';
      forkBtn.title = '复制会话（fork）';
      forkBtn.onclick = function(ev) {
        ev.stopPropagation();
        const target = prompt('复制会话 "' + s + '" 为新会话名:', s + '_copy');
        if (!target) return;
        fetch(BASE + 'api/sessions/' + encodeURIComponent(s) + '/fork', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({target: target})
        })
          .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
          .then(function(res) {
            if (!res.ok) { alert('Fork 失败: ' + ((res.data && res.data.detail) || res.status)); return; }
            fetchSessions();   // 刷新列表，新 session 出现
          })
          .catch(function(e) { alert('Fork 失败: ' + e.message); });
      };
      el.appendChild(forkBtn);
      if (isRunning(s)) {
        const stopBtn = document.createElement('button');
        stopBtn.className = 'session-stop';
        stopBtn.textContent = '⏹';
        stopBtn.title = (s === data.current)
          ? '停止当前会话（保留数据，自动切换到下一个会话）'
          : '停止会话（保留数据，切换时自动恢复）';
        stopBtn.onclick = function(ev) {
          ev.stopPropagation();
          fetch(BASE + 'api/sessions/' + encodeURIComponent(s) + '/stop', {method: 'POST'})
            .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
            .then(function(res) {
              if (!res.ok) { alert('停止失败: ' + res.status); return; }
              if (s === currentSession) {
                // 停止的是当前会话
                if (res.data && res.data.switched_to) {
                  switchSession(res.data.switched_to);   // WS 幂等重切 + 乐观高亮 + session_switched 刷新
                } else {
                  // 无其他会话可切：线程已停不会再有 done，复位流式状态
                  isStreaming = false;
                  setStopBtnVisible(false);
                  fetchSessions();
                }
              } else {
                fetchSessions();
              }
            })
            .catch(function(e) { alert('停止失败: ' + e.message); });
        };
        el.appendChild(stopBtn);
      }
      list.appendChild(el);
    };
    running.forEach(renderItem);
    if (others.length) {
      const sep = document.createElement('div');
      sep.className = 'session-divider';
      sep.textContent = 'Other sessions (not running)';
      list.appendChild(sep);
      others.forEach(renderItem);
    }
    if (data.current) {
      currentSession = data.current;
      document.getElementById('session-name').textContent = currentSession;
    }
  }).catch(function(err) {
    // 会话列表加载失败不阻塞页面：仅记录，避免未捕获异常中断后续初始化
    console.error('fetchSessions error:', err);
  });
}

function switchSession(name) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'switch_session', session: name}));
  } else {
    fetchSessions();  // 离线：WS 不可用时仅靠 2s 轮询刷新（权威切换需 WS 往返）
  }
  delete pendingSessions[name];  // 用户已查看该会话：清除待查看标记
  // ── 崩溃通知自动 ack：切换到该 session 即视为已查看 → 徽标消失（对齐待查看模式） ──
  // 仅清理前端侧边栏徽标（_crash_notices.acked=true），不影响 agent 运行状态。
  fetch(BASE + 'api/sessions/' + encodeURIComponent(name) + '/crash/ack', {method: 'POST'}).catch(function(){});
  touchRecent(name);  // 需求2：更新 MRU 顺序（当前选中置顶，上次选中排第 2）
  // ── 乐观高亮：立即更新 current 与标题，不等 WS 往返（面板即时响应，问题2修复）──
  currentSession = name;
  document.getElementById('session-name').textContent = name;
  document.querySelectorAll('.session-item').forEach(function(el) {
    const n = el.querySelector('.session-name-text');
    el.classList.toggle('active', !!n && n.textContent === name);
  });
  // 移动端切会话后收起边栏
  if (window.innerWidth < 768) document.body.classList.add('sidebar-collapsed');
  // 移除立即 fetchSessions：WS 已连接时权威刷新由后端 session_switched 分支完成
  // （后端已清 sessions 缓存，返回最新 current）；此处立即请求会命中 stale 缓存并覆盖乐观高亮
}
function showSessionError(msg) {
  const panel = document.getElementById('sidebar');
  let err = document.getElementById('session-create-error');
  if (!msg) {
    if (err) err.remove();
    return;
  }
  if (!err) {
    err = document.createElement('div');
    err.id = 'session-create-error';
    err.style.cssText = 'color:var(--err);font-size:12px;margin:0 0 8px 0;';
    const row = panel.querySelector('.session-input-row');
    panel.insertBefore(err, row);
  }
  err.textContent = msg;
}

function createSession() {
  const name = document.getElementById('new-session-name').value.trim();
  if (!name) {
    showSessionError('请输入 session 名称');
    return;
  }
  fetch(BASE + 'api/sessions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})})
    .then(r => {
      if (!r.ok) {
        return r.json().then(data => { throw new Error(data.detail || '创建失败'); });
      }
      return r.json();
    })
    .then(() => {
      showSessionError('');
      document.getElementById('new-session-name').value = '';
      if (window.innerWidth < 768) document.body.classList.add('sidebar-collapsed');
      touchRecent(name);  // 需求2：新创建的 session 置顶
      fetchSessions();  // 刷新列表 + current 标签（后端已把新 session 设为 current）
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: 'switch_session', session: name}));
      }
    })
    .catch(err => showSessionError('❌ ' + err.message));
}


 document.getElementById('new-session-name').addEventListener('input', () => showSessionError(''));
function loadSkills() {
  fetch(BASE + 'api/skills').then(r => r.json()).then(data => {
    if (data.skills && data.skills.length) {
      addMsg('tool', '📦 Skills: ' + data.skills.join(', '));
    } else {
      addMsg('tool', '📦 No skills available');
    }
  });
}

const RENDER_TAIL_COUNT = 20;   // P2: 10→20 条，首屏上下文更充足（现代浏览器渲染成本可控）   // 历史加载只渲染最新 N 条"可见"消息（正文+tool/command，2026-08-05），更早的通过顶部按钮按需补渲染

// 2026-08-05: 用户要求历史渲染包含 tool 执行/结果及 /xxx、!xxx 命令与结果。
// 后端已过滤 git/compact/drop 内部角色，到前端的消息全部需要渲染，恒返回 true。
// 保留函数签名：后续如需按类型选择性渲染，仅需在此调整。
function isVisibleMsg(m) {
  return true;
}

// 从 msgs（按 id 升序）取最新 n 条可见消息，保持原顺序（旧→新）返回。
// 设计：历史消息可能上千条，全量渲染代价高；只取尾部最新 n 条保证首屏秒开，
// 更早的由用户点击"加载更早"再补渲染（renderOlderBatch）。
function lastVisibleTail(msgs, n) {
  var tail = [];
  for (var i = msgs.length - 1; i >= 0 && tail.length < n; i--) {
    if (isVisibleMsg(msgs[i])) tail.unshift(msgs[i]);
  }
  return tail;
}

// 顶部"加载更早"按钮：历史只渲染最新 RENDER_TAIL_COUNT 条正文，点击补渲染更早的一批
function ensureOlderButton() {
  var btn = document.getElementById('older-btn');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'older-btn';
    btn.type = 'button';
    btn.textContent = '⬆ 加载更早的消息';
    btn.onclick = function() { renderOlderBatch(currentSession); };
  }
  var chat = document.getElementById('chat');
  if (btn.parentNode !== chat) chat.insertBefore(btn, chat.firstChild);
  updateOlderButton();
  return btn;
}
function updateOlderButton() {
  var btn = document.getElementById('older-btn');
  if (!btn) return;
  var cached = sessionCache[currentSession];
  if (cached && cached.messages && cached.renderedIds) {
    // P2: 显示剩余未渲染数量，用户对"还有多少历史"有预期
    var unrendered = cached.messages.filter(function(m) {
      return isVisibleMsg(m) && !cached.renderedIds.has(m._id);
    });
    if (unrendered.length) {
      btn.textContent = '⬆ 加载更早的消息（还有 ' + unrendered.length + ' 条）';
      btn.style.display = '';
    } else {
      btn.style.display = 'none';
    }
  } else {
    btn.style.display = 'none';
  }
}

// 历史消息中的 tool_call / tool_result / command 折叠框渲染（2026-08-05 新增）。
// 设计：历史数据的字段结构（displayType + toolCalls/content/result）与流式
// appendToolBox 的 data 结构（{name,args,index,total}/{exit_code,stdout,stderr}）
// 不同，故独立实现，不复用 appendToolBox，避免字段错位。
// 渲染风格与流式保持一致（.msg.tool 折叠框），默认收起、点开看详情。
function renderToolHistory(msg, chat) {
  var produced = [];
  var dt = msg.displayType;

  if (dt === 'tool_call') {
    var el = document.createElement('div');
    el.className = 'msg tool';
    var details = document.createElement('details');
    details.open = false; // 默认收起，点开看详情
    var summary = document.createElement('summary');
    summary.textContent = '🔧 ' + (msg.content || 'tool') + fmtTimeSuffix(msg.created_at);
    details.appendChild(summary);
    if (msg.toolCalls && msg.toolCalls.length) {
      // 2026-08-11: markdown 模板渲染；历史 args 为 JSON 字符串 → parse（失败 json 兜底）；
      // pythonrt .py 文件 → fetch 补读文件内容（历史消息未存 file_content）
      msg.toolCalls.forEach(function(tc) {
        var argsObj = {};
        try { argsObj = JSON.parse(tc.args || '{}'); } catch(e) { argsObj = { raw: tc.args }; }
        var mdDiv = document.createElement('div');
        mdDiv.className = 'tool-args-md';
        mdDiv.innerHTML = mdToHtml(toolArgsToMarkdown(tc.name, argsObj, null));
        details.appendChild(mdDiv);
        if (tc.name === 'pythonrt' && argsObj && typeof argsObj === 'object') {
          var fp = String(argsObj.code_or_filepath || '').trim();
          if (fp.endsWith('.py')) {
            fetch(BASE + 'api/files/download?path=' + encodeURIComponent(fp))
              .then(function(r) { return r.ok ? r.text() : null; })
              .then(function(txt) { if (txt) mdDiv.innerHTML = mdToHtml(toolArgsToMarkdown(tc.name, argsObj, txt)); })
              .catch(function() {});
          }
        }
      });
    }
    el.appendChild(details);
    chat.appendChild(el);
    produced.push(el);
    return produced;
  }

  if (dt === 'tool_result') {
    var el2 = document.createElement('div');
    el2.className = 'msg tool';
    var details2 = document.createElement('details');
    // 2026-08-11: summary/exit 历史结果默认展开（exec_summary/exec_exit stdout 前缀特征），其余折叠
    var isTerminal = /^(📌|🔚|✅ 已持久化|\[exit requested)/.test(msg.content || '');
    details2.open = isTerminal; // summary/exit 默认展开，其余收起
    var summary2 = document.createElement('summary');
    summary2.textContent = '💻 Tool Result' + fmtTimeSuffix(msg.created_at);
    details2.appendChild(summary2);
    var pre2 = document.createElement('pre');
    pre2.textContent = msg.content || '(no output)';
    details2.appendChild(pre2);
    el2.appendChild(details2);
    var historySummary = /^(📌|✅ 已持久化|📄)/.test(msg.content || '') ? summaryBodyFromOutput(msg.content) : '';
    if (historySummary) {
      var historySummaryBody = document.createElement('div');
      historySummaryBody.className = 'msg assistant summary-content';
      var historySummaryMd = document.createElement('div');
      historySummaryMd.className = 'md-body';
      historySummaryMd.innerHTML = mdToHtml(historySummary);
      linkifyFilePaths(historySummaryMd);
      historySummaryBody.appendChild(historySummaryMd);
      el2.appendChild(historySummaryBody);
    }
    chat.appendChild(el2);
    produced.push(el2);
    return produced;
  }

  if (dt === 'thinking') {
    // 历史 thinking（思考链）：折叠框，与实时 ensureThinkingEl 同构
    var tel = document.createElement('div');
    tel.className = 'msg thinking-box';
    var tdetails = document.createElement('details');
    tdetails.open = false; // 默认收起，点开看思考链
    var tsummary = document.createElement('summary');
    tsummary.textContent = '🧠 Thinking' + fmtTimeSuffix(msg.created_at);
    tdetails.appendChild(tsummary);
    var tpre = document.createElement('pre');
    tpre.className = 'thinking-content';
    tpre.textContent = msg.content || '(no thinking content)';
    tdetails.appendChild(tpre);
    tel.appendChild(tdetails);
    chat.appendChild(tel);
    produced.push(tel);
    return produced;
  }

  // command: /xxx 或 !xxx —— 拆成两个节点（与实时路径一致）：
  //   ① 命令原文 → .msg.user（user 输入，参与锚点）
  //   ② 命令结果 → .msg.tool 折叠框（有 result 时才渲染）
  var userEl = document.createElement('div');
  userEl.className = 'msg user';
  var contentDiv = document.createElement('div');
  contentDiv.textContent = msg.content || '';
  userEl.appendChild(contentDiv);
    userEl.dataset.raw = msg.content || '';
  var tSpan = document.createElement('span');
  tSpan.className = 'msg-time';
  tSpan.textContent = fmtTimeSuffix(msg.created_at);
  userEl.appendChild(tSpan);
  chat.appendChild(userEl);
  appendAnchor(userEl);   // 修复：历史命令原文是 user 输入，补建锚点（与实时 addMsg('user') 一致）
  produced.push(userEl);

  var tag = msg.kind === 'bang' ? '💻' : '🔧';
  var status = '';
  if (msg.ok === false) status = ' ❌';
  else if (msg.exit_code !== undefined && msg.exit_code !== null && msg.exit_code !== 0) status = ' (exit=' + msg.exit_code + ')';
  if (msg.result) {
    var el3 = document.createElement('div');
    el3.className = 'msg tool';
    var details3 = document.createElement('details');
    details3.open = false; // 默认收起，点开看详情
    var summary3 = document.createElement('summary');
    summary3.textContent = tag + ' 命令结果' + status + fmtTimeSuffix(msg.created_at);
    details3.appendChild(summary3);
    var pre3 = document.createElement('pre');
    pre3.textContent = msg.result;
    details3.appendChild(pre3);
    el3.appendChild(details3);
    chat.appendChild(el3);
    produced.push(el3);
  }
  return produced;
}

function renderMessage(msg, container) {
  // 单条消息渲染。全量渲染：user/assistant/其他正文 + tool_call/tool_result/command 折叠框
  // （2026-08-05 用户要求历史包含 tool 与命令及其结果）。
  // container 传入时（批量渲染/增量渲染）节点挂到容器（DocumentFragment），
  // 由调用方统一一次性 append 到 #chat；单条渲染时直接 append 并按需滚动。
  // 返回本消息产生的顶层节点数组（user 消息可能产生 meta + el 两个节点），
  // 供 DOM 缓存复用（sessionCache[session].nodes）。
  var dt = msg.displayType || msg.role;
  if (dt === 'tool_call' || dt === 'tool_result' || dt === 'command' || dt === 'thinking') {
    // 历史 tool_call/tool_result/command/thinking → 折叠框渲染（独立结构，不复用流式 appendToolBox）
    return renderToolHistory(msg, container || document.getElementById('chat'));
  }
  var chat = container || document.getElementById('chat');
  var el;
  var produced = [];
  var afterNodes = [];

  if (dt === 'user') {
    if (msg.timestamp || msg.mode) {
      var meta = document.createElement('div');
      meta.className = 'msg-meta-outside';
      meta.textContent = (msg.timestamp || '') + (msg.mode ? '  │  ' + msg.mode : '');
      chat.appendChild(meta);
      produced.push(meta);
    }
    el = document.createElement('div');
    el.className = 'msg user';
    var contentDiv = document.createElement('div');
    contentDiv.textContent = msg.content || '';
    el.appendChild(contentDiv);
    el.dataset.raw = msg.content || '';
    // 2026-08-10: 记录最后 user 消息时间（供 assistant 耗时提示），优先用 DB created_at / 解析的 timestamp
    lastUserMsgTs = msg.created_at || msg.timestamp || lastUserMsgTs;
    appendAnchor(el);   // 修复：历史 user 消息也建锚点（与实时 addMsg('user') 一致）
    // 2026-08-11: 系统信息渲染到 user 消息之后（与实时 addMsg('system') 顺序同构）
    if (msg.sysLines && msg.sysLines.length) {
      msg.sysLines.forEach(function(line) {
        var sysEl = document.createElement('div');
        sysEl.className = 'msg system';   // 2026-08-07: 统一左对齐（基础样式已左对齐）
        sysEl.textContent = line;
        var t = document.createElement('span');
        t.className = 'msg-time';
        t.textContent = new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
        sysEl.appendChild(t);
        afterNodes.push(sysEl);
      });
    }

  } else if (dt === 'assistant') {
    // assistant：markdown 渲染 + TXT/CODE 切换（历史消息复用流式同构框）
    el = document.createElement('div');
    el.className = 'msg assistant';
    el.dataset.raw = msg.content || '';
    el.dataset.mode = getDefaultViewMode();
    const head = document.createElement('div');
    head.className = 'msg-head';
    const btn = document.createElement('button');
    btn.className = 'view-toggle';
    btn.type = 'button';
    btn.textContent = el.dataset.mode === 'text' ? 'TXT' : 'CODE';
    btn.onclick = function() { toggleMsgView(el); };
    head.appendChild(btn);
    // P2: assistant 消息头部显示时间戳 + 距上条用户消息耗时（2026-08-10 增强）
    const tspan = document.createElement('span');
    tspan.className = 'msg-time';
    tspan.textContent = formatClockTs(msg.created_at) || new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
    head.appendChild(tspan);
    // 耗时提示：距上条 user 消息 X分Y秒
    const latStr = formatLatency(lastUserMsgTs, msg.created_at || new Date().toISOString());
    if (latStr) {
      const latSpan = document.createElement('span');
      latSpan.className = 'msg-latency';
      latSpan.textContent = '⏱ ' + latStr;
      head.appendChild(latSpan);
    }
    el.appendChild(head);
    const body = document.createElement('div');
    body.className = el.dataset.mode === 'text' ? 'md-body' : 'code-body';
    if (el.dataset.mode === 'text') {
      body.innerHTML = mdToHtml(el.dataset.raw);
      linkifyFilePaths(body);   // 历史消息路径链接化
    } else {
      body.textContent = el.dataset.raw;
    }
    el.appendChild(body);

  } else {
    el = document.createElement('div');
    el.className = 'msg ' + (msg.role || '');
    el.textContent = msg.content || '';
  }

  chat.appendChild(el);
  produced.push(el);
  afterNodes.forEach(function(node) { chat.appendChild(node); produced.push(node); });
  if (!container) scrollToBottomIfSticky();
  return produced;
}

// ── A+B 双渲染去重（2026-08-12）：实时渲染记账 + 增量内容级去重 ──
// 根因：实时渲染（addMsg/appendStreaming/appendToolBox）不回写 sessionCache，
// 增量拉取（fetchIncremental）按 cached.ids 过滤时把已实时渲染的消息判为 fresh 重渲。
// 方案：
//   B - syncCacheAfterDone：回合 done 后静默增量记账（推进 lastId/ids），
//       后续轮询/重连按已推进的 lastId 只拉真正新增，机制上消除重拉。
//   A - trackRendered/rebuildRenderedContent + fetchIncremental 过滤：
//       实时渲染的 user/assistant/thinking 按 role+正文 计数记账，增量拉取按计数跳过，
//       兜底 B 失效（done 前刷新/时序错位）的残留重复。
function trackRendered(role, content) {
  if (!currentSession) return;
  var c = sessionCache[currentSession];
  if (!c) return;
  if (!c.renderedContent) c.renderedContent = new Map();
  var key = role + '\u0001' + (content || '');
  c.renderedContent.set(key, (c.renderedContent.get(key) || 0) + 1);
}

// 重建内容记账：以当前 #chat DOM 实际渲染为准（切换会话/复用 DOM 节点后调用）
function rebuildRenderedContent() {
  if (!currentSession) return;
  var c = sessionCache[currentSession];
  if (!c) return;
  var map = new Map();
  var chat = document.getElementById('chat');
  var bump = function(role, content) {
    if (!content) return;
    var key = role + '\u0001' + content;
    map.set(key, (map.get(key) || 0) + 1);
  };
  if (chat) {
    chat.querySelectorAll('.msg.user').forEach(function(el) { bump('user', el.dataset.raw || ''); });
    chat.querySelectorAll('.msg.assistant').forEach(function(el) { bump('assistant', el.dataset.raw || ''); });
    chat.querySelectorAll('.msg.thinking-box').forEach(function(el) {
      var pre = el.querySelector('pre.thinking-content');
      var t = pre ? (pre.textContent || '') : '';
      if (t && t !== '(no thinking content)') bump('thinking', t);
    });
  }
  c.renderedContent = map;
}

// B: 回合完成 → 静默增量记账（只推进缓存水位，不渲染——实时已渲染）
function syncCacheAfterDone(session) {
  var c = sessionCache[session];
  if (!c || !c.ids || c.ids.size === 0) return;
  fetch(BASE + 'api/sessions/' + encodeURIComponent(session) + '/messages?after_id=' + c.lastId)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data || !data.messages || !data.messages.length) return;
      var cur = sessionCache[session];
      if (!cur) return;
      var freshAll = data.messages.filter(function(m) { return !cur.ids.has(m._id); });
      if (freshAll.length) {
        cur.messages = cur.messages.concat(freshAll);
        freshAll.forEach(function(m) { cur.ids.add(m._id); });
      }
      cur.lastId = data.last_id || cur.lastId;
    })
    .catch(function() {});
}

// 增量拉取：after_id=lastId → 去重 → 渲染新增消息（正文 + tool/command），数据缓存全部更新
function fetchIncremental(session, cached, token, loadingEl) {
  fetch(BASE + 'api/sessions/' + encodeURIComponent(session) + '/messages?after_id=' + cached.lastId)
    .then(r => r.json()).then(function(data) {
      if (token !== loadToken) { updateCacheOnly(session, data); return; }  // 已切走：仅更新缓存，不渲染
      renderCrashNotice(session, data.crash_notice);   // 崩溃通知：崩溃自动重启后提示用户
      var freshAll = (data.messages || []).filter(function(m) { return !cached.ids.has(m._id); });
      // A: 内容级去重——实时渲染已覆盖的 user/assistant/thinking 不再重复渲染（按计数消耗），
      // 但仍计入数据缓存（freshAll），避免下次增量重复拉取。
      var fresh = freshAll.filter(function(m) {
        if (!cached.renderedContent || (m.role !== 'user' && m.role !== 'assistant' && m.role !== 'thinking')) return true;
        var content = m.content || '';
        if (m.role === 'user') {
          // DB 存完整前缀（时间/模式/建议技能/正文），实时渲染的是正文 → 提取正文做 key
          var m2 = /正文:\n([\s\S]*)$/.exec(content);
          if (m2) content = m2[1];
        }
        var key = m.role + '\u0001' + content;
        var n = cached.renderedContent.get(key) || 0;
        if (n > 0) { cached.renderedContent.set(key, n - 1); return false; }
        return true;
      });
      var visFresh = fresh.filter(isVisibleMsg);   // 全部渲染（含 tool/command）
      if (loadingEl) loadingEl.textContent = visFresh.length
        ? '✅ 已加载 ' + visFresh.length + ' 条新消息'
        : '✅ 已是最新（' + cached.renderedIds.size + ' 条正文）';
      if (visFresh.length) {
        var frag = document.createDocumentFragment();
        visFresh.forEach(function(m) {
          var produced = renderMessage(m, frag);
          if (cached.nodes) cached.nodes.push.apply(cached.nodes, produced);
          if (cached.renderedIds) cached.renderedIds.add(m._id);
        });
        document.getElementById('chat').appendChild(frag);
      }
      if (freshAll.length) {   // 数据缓存：全部 freshAll（含 tool/result 与被内容去重跳过的）
        cached.messages = cached.messages.concat(freshAll);
        freshAll.forEach(function(m) { cached.ids.add(m._id); });
        cached.lastId = data.last_id || cached.lastId;
      }
      if (data.session) document.getElementById('session-name').textContent = data.session;
      if (loadingEl) loadingEl.remove();   // F9: 移除 loading 提示条（分支1/2 残留）
      // F4c 修订（2026-08-10）：仅在"确实渲染了新消息"时滚底——fetchSessions
      // 2s 轮询会频繁调用本函数（无新消息），无条件滚底会把用户阅读中的历史
      // 拉回底部；preserveView 重连路径（用户正在阅读）也受益，不再被强制滚底。
      // 2026-08-12: 尊重 stickToBottom——正常加载路径 L4275 已置 true 行为不变；
      // preserveView 重连路径（用户正在阅读历史）不再被强制拉回底部。
      if (visFresh.length && stickToBottom) scrollToBottomIfSticky();
    }).catch(function(err) {
      console.error('incremental load error:', err);
      // P2: catch 分支补 remove——原实现只改文案不移除，loading 条残留
      if (loadingEl) {
        loadingEl.textContent = '⚠️ 增量加载失败（已显示缓存）';
        setTimeout(function() { loadingEl.remove(); }, 2000);
      }
    });
}

// 骨架屏：加载中占位（3 条 shimmer 线条），优于孤立文本提示（用户友好度）
// 崩溃通知横幅：agent 崩溃并自动重启后，在消息区顶部提示用户（含错误与时间）
function renderCrashNotice(session, notice) {
  if (!notice) return;
  var chat = document.getElementById('chat');
  var existing = document.querySelector('.crash-notice[data-session="' + session + '"]');
  if (existing) return;   // 已展示过，不重复
  var el = document.createElement('div');
  el.className = 'crash-notice';
  el.setAttribute('data-session', session);
  var ts = notice.time || '';
  var err = notice.error || 'unknown';
  var txt = document.createElement('span');
  txt.textContent = '⚠️ Agent 崩溃并已自动重启（' + ts + '｜' + err + '）。崩溃期间发送的消息可能丢失，请重发。';
  var ackBtn = document.createElement('button');
  ackBtn.className = 'crash-ack';
  ackBtn.textContent = '知道了';
  ackBtn.onclick = function() {
    fetch(BASE + 'api/sessions/' + encodeURIComponent(session) + '/crash/ack', {method: 'POST'})
      .then(function(r) { return r.json(); })
      .then(function() { if (el.parentNode) el.parentNode.removeChild(el); })
      .catch(function() { if (el.parentNode) el.parentNode.removeChild(el); });
  };
  el.appendChild(txt);
  el.appendChild(ackBtn);
  chat.insertBefore(el, chat.firstChild);
}

function makeLoadingSkeleton() {
  var el = document.createElement('div');
  el.className = 'loading-skeleton';
  el.innerHTML = '<div class="sk-line"></div><div class="sk-line mid"></div><div class="sk-line short"></div>';
  return el;
}

// LRU 缓存写入：超出 MAX_CACHED_SESSIONS 时淘汰最早未使用的 session 缓存。
// 被淘汰的 session 切回时会重新拉取（数据仍在后端），仅释放前端内存，可接受。
function cacheSession(session, entry) {
  delete sessionCache[session];   // 先删后插 → 更新为"最近使用"
  sessionCache[session] = entry;
  var keys = Object.keys(sessionCache);
  while (keys.length > MAX_CACHED_SESSIONS) {
    var victim = keys.shift();
    delete sessionCache[victim];
  }
}

function loadSessionMessages(session, opts) {
  // 方案A++：per-session 数据缓存 + DOM 节点缓存 + 增量拉取 + 仅渲染最新 RENDER_TAIL_COUNT 条消息。
  // - 全量渲染（user/assistant/正文 + tool_call/tool_result/command 折叠框，2026-08-05）
  // - 从最新开始，先渲染最新 RENDER_TAIL_COUNT 条可见消息；更早的通过顶部按钮补渲染
  // - loadToken 防竞态：切走/切回并发时，仅当前会话的响应允许渲染 DOM
  // - cached.ids（Set）：全部消息 _id 去重（数据层）；cached.renderedIds（Set）：已渲染正文 _id 去重（渲染层）
  // - cached.nodes（Array）：已渲染正文的顶层 DOM 节点数组，切回直接复用（零重渲染）
  // - opts.preserveView=true（P0 重连路径）：不清空现有 DOM、不强制滚底，仅增量补新，
  //   避免断线重连时用户正在阅读的历史被清空/拉回底部
  opts = opts || {};
  var cached = sessionCache[session];
  if (opts.preserveView && cached && cached.nodes && cached.nodes.length) {
    // ── 重连 + 已有缓存：保留视图，仅增量拉取补新（不清空、不滚底）──
    var lEl = makeLoadingSkeleton();
    document.getElementById('chat').appendChild(lEl);
    fetchIncremental(session, cached, ++loadToken, lEl);
    return;
  }
  clearChat();
  stickToBottom = true;   // 加载历史后强制滚底显示最新消息
  var token = ++loadToken;
  var loadingEl = makeLoadingSkeleton();
  document.getElementById('chat').appendChild(loadingEl);
  ensureOlderButton();   // 顶部"加载更早"按钮（先于消息插入）

  if (cached && cached.nodes && cached.nodes.length) {
    // ── 分支1：DOM 节点缓存命中 → 零重渲染直接复用 + 增量拉取 ──
    var frag = document.createDocumentFragment();
    cached.nodes.forEach(function(n) { frag.appendChild(n); });
    document.getElementById('chat').appendChild(frag);
    // 修复：缓存复用路径 —— clearChat→resetAnchors 已清空锚点条，
    // 为历史 user 消息逐个补建锚点（cached.nodes 顺序 = 消息顺序（旧→新），append 顺序一致）
    cached.nodes.forEach(function(n) {
      if (n.classList && n.classList.contains('msg') && n.classList.contains('user')) {
        appendAnchor(n);
      }
    });
    updateOlderButton();
    forceScrollToBottom();   // F4c: 节点插入后立即滚底（同步+rAF 双保险）
    rebuildRenderedContent();   // A+B: DOM 恢复后重建内容记账（增量内容级去重基准）
    fetchIncremental(session, cached, token, loadingEl);
  } else if (cached) {
    // ── 分支2：旧格式缓存（无 nodes/renderedIds）→ 渲染最新 RENDER_TAIL_COUNT 条消息并补齐缓存 ──
    cached.nodes = [];
    cached.renderedIds = cached.renderedIds || new Set();
    cached.renderedContent = cached.renderedContent || new Map();
    var tail2 = lastVisibleTail(cached.messages, RENDER_TAIL_COUNT);
    var frag2 = document.createDocumentFragment();
    tail2.forEach(function(m) {
      var produced = renderMessage(m, frag2);
      cached.nodes.push.apply(cached.nodes, produced);
      cached.renderedIds.add(m._id);
    });
    document.getElementById('chat').appendChild(frag2);
    updateOlderButton();
    forceScrollToBottom();   // F4c: 节点插入后立即滚底（同步+rAF 双保险）
    rebuildRenderedContent();   // A+B: DOM 恢复后重建内容记账（增量内容级去重基准）
    fetchIncremental(session, cached, token, loadingEl);
  } else {
    // ── 分支3：无缓存 → 分页全量拉取（limit=100，P1 后端分页）→ 渲染最新 RENDER_TAIL_COUNT 条正文并写入缓存 ──
    fetch(BASE + 'api/sessions/' + encodeURIComponent(session) + '/messages?limit=100').then(r => r.json()).then(function(data) {
      if (token !== loadToken) { updateCacheOnly(session, data); return; }  // 已切走：仅更新缓存，不渲染
      renderCrashNotice(session, data.crash_notice);   // 崩溃通知：崩溃自动重启后提示用户
      var msgs = data.messages || [];
      var nodes = [];
      var renderedIds = new Set();
      var tail = lastVisibleTail(msgs, RENDER_TAIL_COUNT);
      var frag = document.createDocumentFragment();
      tail.forEach(function(m) {
        var produced = renderMessage(m, frag);
        nodes.push.apply(nodes, produced);
        renderedIds.add(m._id);
      });
      cacheSession(session, {
        lastId: data.last_id || 0,
        messages: msgs.slice(),
        ids: new Set(msgs.map(function(m) { return m._id; })),
        nodes: nodes,
        renderedIds: renderedIds,
        renderedContent: new Map(),
      });
      document.getElementById('chat').appendChild(frag);
      if (data.session) document.getElementById('session-name').textContent = data.session;
      // P1: 展示总数与已渲染条数（后端 total 字段）——用户友好度
      if (data.total !== undefined) {
        loadingEl.textContent = '✅ 共 ' + data.total + ' 条消息，已显示最新 ' + tail.length + ' 条';
      } else {
        loadingEl.textContent = '✅ 已渲染最新 ' + tail.length + ' 条';
      }
      updateOlderButton();
      loadingEl.remove();   // F4d: 先移除 loading 条，消除 scrollHeight 计算干扰
      forceScrollToBottom();   // F4c: 历史加载完成 → 强制滚底（同步+rAF 双保险）
      rebuildRenderedContent();   // A+B: 初始渲染后重建内容记账
    }).catch(function(err) {
      console.error('loadSessionMessages error:', err);
      if (loadingEl) loadingEl.remove();
      // P1: 失败提示带重试按钮（用户友好度）
      var errMsg = addMsg('error', '❌ 加载会话消息失败 ');
      var retryBtn = document.createElement('button');
      retryBtn.textContent = '重试';
      retryBtn.style.cssText = 'margin-left:6px;padding:1px 10px;border:none;border-radius:8px;background:#fff;color:#dc2626;cursor:pointer;font-size:11px;';
      retryBtn.onclick = function() { loadSessionMessages(session); };
      errMsg.appendChild(retryBtn);
    });
  }
}

// 点击"加载更早"：补渲染紧邻已渲染区间的更早 RENDER_TAIL_COUNT 条正文，
// 插入到消息顶部并保持滚动视口位置（不跳动）
function renderOlderBatch(session) {
  var cached = sessionCache[session];
  if (!cached || !cached.messages) return;
  var unrendered = cached.messages.filter(function(m) {
    return isVisibleMsg(m) && !cached.renderedIds.has(m._id);
  });
  if (!unrendered.length) { updateOlderButton(); return; }
  var batch = unrendered.slice(-RENDER_TAIL_COUNT);   // 紧邻已渲染区间的上一批（旧→新）
  var frag = document.createDocumentFragment();
  var producedAll = [];
  var anchorsStart = anchorCounter;   // 修复：记录渲染前锚点计数器，用于定位本批新增 dot
  batch.forEach(function(m) {
    producedAll.push.apply(producedAll, renderMessage(m, frag));
    cached.renderedIds.add(m._id);
  });
  var chat = document.getElementById('chat');
  var btn = ensureOlderButton();
  var distToBottom = chat.scrollHeight - chat.scrollTop;   // 加载前距底距离（用于恢复视口）
  chat.insertBefore(frag, btn.nextSibling);
  if (producedAll.length) cached.nodes = producedAll.concat(cached.nodes);  // 更早节点插到 nodes 前部
  // 修复：本批新增 dot（idx >= anchorsStart）整体前移到锚点条头部，
  // 保持 dot 顺序与消息顺序一致（旧→新），避免 updateAnchorHighlight 高亮错位
  var anchors = document.getElementById('msg-anchors');
  if (anchors) {
    var newDots = [];
    for (var ai = 0; ai < anchors.children.length; ai++) {
      var ad = anchors.children[ai];
      if (ad.classList && ad.classList.contains('anchor-dot') && ad.dataset.idx !== undefined && parseInt(ad.dataset.idx, 10) >= anchorsStart) {
        newDots.push(ad);
      }
    }
    if (newDots.length) {
      newDots.forEach(function(d) { anchors.removeChild(d); });
      var ref = anchors.firstChild;   // 原有最旧 dot
      if (ref) {
        newDots.forEach(function(d) { anchors.insertBefore(d, ref); });   // 依次插到 ref 前 → 保持旧→新
      } else {
        newDots.forEach(function(d) { anchors.appendChild(d); });
      }
    }
  }
  chat.scrollTop = chat.scrollHeight - distToBottom;       // 恢复视口，避免跳动
  updateOlderButton();
}

// 竞态兜底：请求返回时已切到其他 session → 只更新数据缓存不渲染 DOM，
// 避免旧会话消息污染当前视图；下次切回直接命中缓存（按 _id 去重）
function updateCacheOnly(session, data) {
  if (!data || !data.messages) return;
  var c = sessionCache[session];
  if (c) {
    var fresh = data.messages.filter(function(m) { return !c.ids.has(m._id); });
    c.messages = c.messages.concat(fresh);
    fresh.forEach(function(m) { c.ids.add(m._id); });
    c.lastId = data.last_id || c.lastId;
  } else {
    sessionCache[session] = {
      lastId: data.last_id || 0,
      messages: data.messages.slice(),
      ids: new Set(data.messages.map(function(m) { return m._id; })),
      nodes: [],
      renderedIds: new Set(),
      renderedContent: new Map(),
    };
  }
}

function updateLockTag(data) {
  const observing = data && data.observing;
  if (observing) {
    // T7: 观察者模式 → 显示 ⏳ 持有者（对齐 repl 的 [⏳pid:tid]）
    const holder = data.holder || {};
    const name = holder.holder || holder.holder_name || '未知进程';
    curLockMsg = '⏳ ' + name;
  } else {
    curLockMsg = null;
  }
  updateLockIndicator();
}

function openModelTestModal() {
  // 关闭已存在的弹窗
  closeModelTestModal();
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'model-test-overlay';
  overlay.innerHTML =
    '<div class="modal-box">' +
    '  <div class="modal-head"><span class="modal-title">⚡ 模型联通性测试</span>' +
    '    <button class="modal-close" onclick="closeModelTestModal()">✕</button></div>' +
    '  <div class="modal-body"><div id="model-test-content" style="color:var(--text-muted,#888)">⏳ 测试中（每个模型约需数秒）...</div></div>' +
    '</div>';
  overlay.addEventListener('click', function(e) { if (e.target === overlay) closeModelTestModal(); });
  document.body.appendChild(overlay);
  fetch(BASE + 'api/model/test', {method: 'POST'}).then(function(r) { return r.json(); }).then(function(data) {
    var content = document.getElementById('model-test-content');
    if (!content) return;
    if (!data.ok) { content.textContent = '❌ 测试失败: ' + (data.error || 'unknown'); return; }
    var results = data.results || [];
    // 排序：成功项按延迟从小到大（升序），失败项排最后（保持原顺序）
    var okList = results.filter(function(r) { return r.ok; })
      .sort(function(a, b) { return (a.latency_ms || 0) - (b.latency_ms || 0); });
    var failList = results.filter(function(r) { return !r.ok; });
    var ordered = okList.concat(failList);
    var okN = okList.length;
    var html = '<table class="model-test-table"><tr><th>Provider.Model</th><th>状态</th><th>延迟</th></tr>';
    ordered.forEach(function(r) {
      var name = (r.provider || '?') + '.' + (r.model || '?');
      var status = r.ok
        ? '<span class="mt-ok">✅</span>'
        : '<span class="mt-fail">❌ ' + escapeHtml(String(r.error || 'failed').slice(0, 40)) + '</span>';
      var lat = r.ok ? (r.latency_ms + 'ms') : '-';
      html += '<tr><td>' + escapeHtml(name) + '</td><td>' + status + '</td><td>' + lat + '</td></tr>';
    });
    html += '</table><div style="margin-top:8px;font-size:12px;color:var(--text-muted,#888)">' +
      okN + '/' + results.length + ' 可用，总耗时 ' + (data.elapsed || 0).toFixed(1) + 's</div>';
    content.innerHTML = html;
  }).catch(function(err) {
    var content = document.getElementById('model-test-content');
    if (content) content.textContent = '❌ 请求失败: ' + err;
  });
}

function closeModelTestModal() {
  var overlay = document.getElementById('model-test-overlay');
  if (overlay) overlay.remove();
}

function setSessionModel(provider, model, scope) {
  var body = {model: model, scope: scope};
  if (provider) body.provider = provider;
  fetch(BASE + 'api/model/set', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(function(r) { return r.json(); }).then(function(data) {
    var msg = data.ok
      ? (scope === 'current'
          ? '✅ 当前 session 已设为 ' + (provider ? provider + ' · ' : '') + model
          : '✅ 已设置 ' + data.ok_count + '/' + data.total + ' 个 session')
      : '❌ 设置失败: ' + (data.error || 'unknown');
    alert(msg);
    if (data.ok) {
      closeModelTestModal();
      fetchStatus();      // 刷新顶部 provider-model 显示
      fetchSessions();    // 刷新 session 列表（模式/状态可能变化）
    }
  }).catch(function(err) { alert('❌ 请求失败: ' + err); });
}

function formatClockTs(ts) {
  // 解析 DB created_at "YYYY-MM-DD HH:MM:SS" → HH:MM
  if (!ts) return '';
  var d = new Date(ts);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
}

function fmtTimeSuffix(ts) {
  // 折叠框 summary 时间后缀：有 created_at 用真实消息时间，否则用本地当前时间（与实时路径一致）
  var t = formatClockTs(ts) || new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
  return t ? '  ' + t : '';
}

function formatLatency(userTs, msgTs) {
  // 计算 msgTs 距 userTs 的 X分Y秒
  if (!userTs || !msgTs) return '';
  var u = new Date(userTs), m = new Date(msgTs);
  if (isNaN(u.getTime()) || isNaN(m.getTime())) return '';
  var diff = Math.max(0, (m.getTime() - u.getTime()) / 1000);
  if (diff < 1) return '';
  var min = Math.floor(diff / 60), sec = Math.floor(diff % 60);
  return (min > 0 ? min + ' 分 ' : '') + sec + ' 秒';
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fetchStatus() {
  return fetch(BASE + 'api/session/stats').then(r => r.json()).then(data => {
    // P0 防御：响应 session 与前端当前 session 不一致（切换竞态/缓存残留）时不覆盖标题与 currentSession
    if (currentSession && data.session && data.session !== currentSession) {
      return;
    }
    currentWorkdir = data.workdir || '';   // 路径链接：项目根
    document.getElementById('session-name').textContent = data.session || '-';
    setMode(data.mode);
    updateLockTag(data);   // T9: 初始化/轮询渲染锁标签
    updateStatsFrom(data, false); // 3: 初始加载不显示累计值（token 仅由 WS stats 单轮值驱动）
    currentSession = data.session;
  });
}

function updateStatsFrom(data, showTokens) {
  // v2 方案A(完善)：token 区优先由 WS stats 事件(当前轮单轮值)驱动；
  // showTokens=false(fetchStatus/updateStats 的会话累计值)时：
  //   - 已收到过当前轮 stats(hasWsStats=true) -> 不覆盖(保持当前轮显示, done 后一致)
  //   - 未收到(刚进入 session) -> 显示 db 持久化的最近一轮单轮值 last_*("上一轮")
  // 累计值 prompt_tokens/completion_tokens 始终不用于显示(避免大太多)。
  const el = document.getElementById('token-stats');
  if (showTokens !== false) {
    const total = (data.prompt_tokens || 0) + (data.completion_tokens || 0);
    if (el) el.textContent = '📊 ' + total.toLocaleString() + ' tokens';
  } else if (!hasWsStats) {
    const lastTotal = (data.last_prompt_tokens || 0) + (data.last_completion_tokens || 0);
    if (lastTotal > 0 && el) el.textContent = '📊 上轮 ' + lastTotal.toLocaleString() + ' tokens';
    // lastTotal===0：新 session 无历史 -> 不动(保持空白)
  }
  updateProviderModel(data);
}
function updateProviderModel(data) {
  const el = document.getElementById('provider-model');
  if (!el) return;
  const hasP = data.provider !== undefined && data.provider !== null;
  const hasM = data.model !== undefined && data.model !== null;
  if (!hasP && !hasM) return;  // ws stats 事件无此字段，保持现状不覆盖
  const prov = hasP ? String(data.provider).trim() : '';
  const model = hasM ? String(data.model).trim() : '';
  el.textContent = (prov || model) ? (prov + (model ? ' · ' : '') + model) : '';
}
function updateStats() {
  fetch(BASE + 'api/session/stats').then(r => r.json()).then(data => updateStatsFrom(data, false)).catch(function(){});
}

// Auto-resize textarea
document.getElementById('input').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// Keyboard shortcut: Ctrl+Enter for newline
document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    // Already handled by textarea onkeydown
  }
  // 4G: Esc 收起侧边栏
  if (e.key === 'Escape' && !document.body.classList.contains('sidebar-collapsed')) {
    document.body.classList.add('sidebar-collapsed');
    try { localStorage.setItem('sidebarCollapsed', '1'); } catch(_) {}
  }
});

// F5c: 禁用浏览器滚动位置恢复（刷新后不再自动回到顶部/旧位置，让滚底生效）
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
// F5c/F6b: window load 兜底——浏览器 scroll restoration 之后强制滚底（最终保险）。
// F6b: 多级延迟补滚（load + 300/800/1500ms）——覆盖浏览器 restoration 任意时机，
// 且 currentSession 可能在 load 后才被 fetchStatus 赋值，延迟补滚保证最终命中。
window.addEventListener('load', function() {
  forceScrollToBottom();
  setTimeout(forceScrollToBottom, 300);
  setTimeout(forceScrollToBottom, 800);
  setTimeout(forceScrollToBottom, 1500);
});
initTheme();
initSidebar();
fetchSessions();  // FIX: 页面初始化即加载 sessions 列表（问题1：初始为空）
setInterval(fetchSessions, 2000);  // 需求1：每 2s 轮询刷新 session 列表（与后端 /api/sessions 2s TTL 匹配；切走/刷新/断线场景 done 丢失需轮询兜底恢复按钮）
updateModeIndicator();
updateSkillIndicator();
updateLockIndicator();
loadInputHistory();   // 页面加载即拉取共享历史（失败静默降级）
document.getElementById('chat').addEventListener('scroll', onChatScroll);
connect();
</script>
</body>
</html>
"""


@app.get("/login")
async def login_page():
    """Serve the login page."""
    return HTMLResponse(LOGIN_PAGE)


@app.get("/files")
async def files_page():
    """FTP 风格文件浏览器页面（新标签页打开，auth 保护）。"""
    return HTMLResponse(FILES_PAGE)
@app.get("/")
async def root():
    """Serve the single-page web interface."""
    return HTMLResponse(HTML_PAGE)


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


def run_web(args):
    logger.info("Web 服务启动")

    global _workdir, _hashed_password, _auth_username
    _workdir = args.workdir
    _auth_username = args.user
    if args.password:
        from hashlib import sha256
        _hashed_password = sha256(args.password.encode()).hexdigest()
        print(f"  🔒 Auth enabled: user={_auth_username}, password=***")
    else:
        print(f"  🔓 Auth disabled (no --password set)")

    # ── 默认 session：以最新 session 作为默认（对齐 repl --resume，问题1修复）──
    global _current_session
    _current_session = _agent_manager.resolve_session()
    if _current_session:
        print(f"  💬 Default session: {_current_session}")
        # ── 后台预热默认 session 的 agent，避免首请求初始化阻塞（问题2修复）──
        _agent_manager.prewarm(_current_session)

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
