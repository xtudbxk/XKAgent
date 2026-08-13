"""
manager.py — 多 Agent 线程管理器

管理 N 个 agent 线程，每个线程绑定一个 session。
通过 queue.Queue 与 agent 线程通信，支持聚焦切换、不活跃回收、崩溃恢复。

用法:
from __future__ import annotations
    from codes.manager import AgentManager

    mgr = AgentManager()
    mgr.start_agent("sess_A")
    mgr.start_agent("sess_B")
    mgr.switch_focus("sess_B")
    mgr.send_input("你好")  # 发给 sess_B
    mgr.switch_focus("sess_A")
    mgr.send_input("世界")  # 发给 sess_A

    output = mgr.read_output()  # 读取当前聚焦 agent 的输出
"""

import os
import sys
import queue
import time
import threading
from datetime import datetime
from typing import Any


# ── Data classes ──


from codes._log import logger
from codes.history import list_sessions
class SessionInfo:
    """Information about a managed agent session."""
    def __init__(self, session: str, pid: int, tid: int,
                 status: str, last_active: float, msg_count: int,
                 phase: str = "idle", in_tool: bool = False,
                 mode: str = "plan", turn_active: bool = False,
                 error: str | None = None):
        self.session = session
        self.pid = pid
        self.tid = tid
        self.status = status       # "running" | "stopped" | "crashed"
        self.last_active = last_active
        self.msg_count = msg_count
        self.phase = phase             # "starting" | "idle" | "llm"（agent 实时阶段）
        self.in_tool = in_tool         # 是否有 tool 正在执行
        self.mode = mode                 # "plan" | "build" | "build-unsafe"（agent 实时模式）
        self.turn_active = turn_active   # 回合级活跃标志（agent._turn_active 透传，2026-08-10）
        self.error = error                 # 崩溃错误信息（crashed 时非 None，供 web 前端提示）

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "pid": self.pid,
            "tid": self.tid,
            "status": self.status,
            "last_active": datetime.fromtimestamp(self.last_active).isoformat(),
            "msg_count": self.msg_count,
            "phase": self.phase,
            "in_tool": self.in_tool,
            "mode": self.mode,
            "turn_active": self.turn_active,
            "error": self.error,
        }

    def __repr__(self) -> str:
        return (f"SessionInfo(session={self.session!r}, pid={self.pid}, "
                f"tid={self.tid}, status={self.status!r})")


class _AgentProcess:
    """Internal holder for an agent thread and its communication queues."""
    def __init__(self, session: str):
        self.session = session
        self.input_queue: queue.Queue = queue.Queue()
        self.output_queue: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.status: str = "stopped"   # "running" | "stopped" | "crashed"
        self.last_active: float = time.time()
        self.error: str | None = None
        # agent 实例引用（线程内创建后回填）。跨线程只读调用其线程安全方法
        # （request_interrupt / interrupt_tool），Event 与 pythonrt worker 轮询退出 均线程安全。
        self.agent_ref: Any | None = None


# ── Manager ──

class AgentManager:
    """Manager for multiple agent threads, each bound to one session."""

    def __init__(self, embedding_model: Any = None):
        self._agents: dict[str, _AgentProcess] = {}
        self._focus: str | None = None
        self._lock = threading.RLock()
        self._embedding_model = embedding_model  # shared reference (optional)


        # ── Session state cache (set by repl.py / web.py after get_focus_info) ──
        self._focus_mode: str = "plan"
        self._focus_observing: bool = False
        self._focus_holder_info: dict | None = None
        self._focus_skill_select: bool = True

    # ── Public API ──

    def start_agent(self, session: str, wait_ready: bool = True) -> bool:
        logger.info(f"启动 Agent: session={session}, wait_ready={wait_ready}")
        """Start a new agent thread bound to *session*.

        Args:
            session: 会话名。
            wait_ready: True 时同步等待 agent 就绪（默认，repl 交互场景）；
                        False 时后台等待就绪、立即返回（web 事件循环场景，
                        避免首次请求同步等待）。

        设计考虑：wait_for_ready 放在锁外执行，避免持锁 15s 阻塞
        其他线程的 start_agent / switch_focus 调用（并发安全关键点）。
        """
        _t_start = __import__("time").time()
        with self._lock:
            if session in self._agents and self._agents[session].status == "running":
                return False  # already running

            proc = _AgentProcess(session)

            # Build the agent thread — it will import Agent and run forever
            def _target(session=session, proc=proc):
                _run_agent_thread(session, proc, self._embedding_model)

            t = threading.Thread(
                target=_target,
                name=f"agent-{session}",
                daemon=True,
            )
            proc.thread = t
            proc.status = "running"
            self._agents[session] = proc
            t.start()

            # 首个启动的 session 设为焦点（原逻辑在等待就绪之后，提前到锁内）
            if self._focus is None:
                self._focus = session

        # ── 等待就绪（锁外）：首次启动可能耗时数秒 ──
        if wait_ready:
            self.wait_for_ready(session, timeout=15.0)
        else:
            threading.Thread(
                target=self.wait_for_ready,
                args=(session, 15.0),
                daemon=True,
                name=f"wait-ready-{session}",
            ).start()

        _t_elapsed = __import__("time").time() - _t_start
        print(f"  [timing] start_agent({session}): {_t_elapsed:.2f}s")
        return True

    def request_interrupt(self, session: str | None = None) -> bool:
        """请求中断指定 session 的 agent 当前 turn（完全停止）。

        主线程 Ctrl+C 时调用：设置 agent 的 interrupt_event，
        run_pythonrt 主线程每 50ms 轮询该事件，检测到后 SIGKILL 终止
        pythonrt worker 子进程并返回 exit_code=130。
        返回 True 表示已转发。
        """
        session = session or self._focus
        if session is None:
            return False
        proc = self._agents.get(session)
        if proc is None or proc.agent_ref is None:
            return False
        try:
            logger.info(f"[DIAG] request_interrupt: Event.set() at {__import__('time').time():.3f} (session={session})")
            proc.agent_ref.request_interrupt()   # 设置中断事件
            proc.agent_ref.interrupt_tool()      # 若在 tool 执行中，直调 bash.cancel()
            return True
        except Exception as e:
            logger.warning(f"request_interrupt 失败: session={session} error={e}")
            return False

    def stop_agent(self, session: str, join_timeout: float = 3.0) -> bool:
        logger.info(f"停止 Agent: session={session}")
        """Stop an agent thread by sending None sentinel.

        T2 修复: 增加锁外 join 等待（默认 3s），确保旧线程的 finally
        （agent.close → _release_lock）执行完毕、flock fd 已释放后再返回，
        避免 stop 后立即重启同名 agent 时同进程自锁误报"被占用"。
        """
        with self._lock:
            proc = self._agents.get(session)
            if proc is None:
                return False
            proc.input_queue.put(None)  # sentinel → run_forever will exit
            proc.status = "stopped"
            del self._agents[session]
            if self._focus == session:
                self._focus = None
        # T2: 锁外 join，与 start_agent 的 wait_for_ready 放锁外同理，
        # 避免持锁阻塞其他线程的 start_agent / switch_focus。
        if proc.thread is not None and proc.thread.is_alive():
            proc.thread.join(timeout=join_timeout)
            if proc.thread.is_alive():
                logger.warning(
                    f"stop_agent: 线程未在 {join_timeout}s 内退出 session={session}，锁可能残留"
                )
                # 2026-08-13: /session stop 退出时兜底清理 mkdir 锁（线程未退出也清）
                try:
                    from codes.lock import cleanup as _cleanup_lock
                    _ok_c, _msg_c = _cleanup_lock(session)
                    logger.info(f"stop_agent: 兜底清理锁 session={session} ok={_ok_c} ({_msg_c})")
                except Exception as _e:
                    logger.warning(f"stop_agent: 清理锁失败 session={session}: {_e}")
        return True

    def switch_focus(self, session: str) -> bool:
        """Switch the current focus to *session*.

        If the session's agent thread is not running, it will be started.
        Returns True on success, False if session is empty.
        """
        if not session:
            return False

        # Auto-start if not running
        with self._lock:
            proc = self._agents.get(session)
            if proc is None or proc.status != "running":
                ok = self.start_agent(session)  # RLock allows re-entry
                if not ok:
                    return False

        self._focus = session
        return True

    def focus_session(self, session: str, resume: bool = True) -> dict | None:
        """聚焦 + 恢复 + 同步缓存的统一入口（repl/web 共用）。

        收敛 repl._switch_to_session 与 web._sync_focus_cache 的重复逻辑：
        switch_focus → resume → get_focus_info → 更新缓存。
        仅当焦点变化时调用（调用方保证），避免常规请求每次 2s 查询延迟。
        """
        self.switch_focus(session)
        if resume:
            self.send_command("resume")
        # FIX: 降低超时（1.0s）并移除重试，避免 agent 忙碌时 switch 阻塞 4s+
        # 原: 2.0s + retry(0.3s + 2.0s) = 最多 4.3s 阻塞
        # 新: 1.0s 单次 = 最多 1.0s 阻塞
        # agent 忙时 get_focus_info 超时是预期行为，不阻塞 switch 流程
        info = self.get_focus_info(timeout=1.0)
        if info:
            self._focus_mode = info.get("mode", "plan")
            self._focus_observing = info.get("is_observing", False)
            self._focus_holder_info = info.get("holder_info", None)
            self._focus_skill_select = info.get("skill_select_enabled", self._focus_skill_select)
        else:
            # T5 修复(决策A): get_focus_info 超时（目标 agent 未就绪/队列积压）
            # 时回退到文件级锁探测，避免缓存保留旧 session 的占用状态，
            # 导致切换到空闲 session 后仍误报"被占用"。
            from codes.lock import is_locked as _is_locked
            locked, meta = _is_locked(session)
            self._focus_observing = locked
            self._focus_holder_info = meta if isinstance(meta, dict) else None
        return info

    @staticmethod
    def resolve_session(session: str | None = None) -> str:
        """解析默认 session：显式指定 > 最新 session > 新建时间戳会话。

        统一 repl/web 的 session 选择策略（消除两处手写重复）。
        """
        if session:
            return session
        sessions = list_sessions()
        return sessions[0] if sessions else datetime.now().strftime("session_%Y%m%d_%H%M%S")

    def prewarm(self, session: str | None = None) -> None:
        """后台预热 agent 线程（daemon），避免首个请求同步等待初始化。

        幂等：agent 已在运行则跳过。失败仅告警，绝不阻断启动流程。
        """
        name = self.resolve_session(session)

        def _run() -> None:
            try:
                logger.info(f"prewarm: 预热 agent session={name}")
                self.start_agent(name, wait_ready=True)
            except Exception:
                logger.exception(f"prewarm: agent 预热失败 session={name}")

        threading.Thread(target=_run, daemon=True, name=f"prewarm-{name}").start()

    def send_input(self, text: str) -> bool:
        """Send input text to the currently focused agent.

        Returns True if sent, False if no agent is focused.
        """
        session = self._focus
        if session is None:
            return False
        proc = self._agents.get(session)
        if proc is None or proc.status != "running":
            return False
        # P0 修复: 线程存活检测——status=running 但线程已死（BaseException 崩溃、
        # 未被 except 捕获的异常）时自动重启，避免消息被静默吞掉
        # （用户现象: "No agent session active" / 发消息无响应）。
        if proc.thread is not None and not proc.thread.is_alive():
            logger.warning(f"send_input: agent 线程已死 session={session}，自动重启")
            self._agents.pop(session, None)
            if not self.start_agent(session, wait_ready=False):
                return False
            proc = self._agents.get(session)
            if proc is None or proc.status != "running":
                return False
        proc.input_queue.put(text)
        proc.last_active = time.time()
        return True

    def read_output(self, timeout: float = 0.1) -> dict | None:
        """Read one output event from the currently focused agent (non-blocking).

        Returns a dict (the event) or None if nothing is available within *timeout*.
        """
        session = self._focus
        if session is None:
            return None
        proc = self._agents.get(session)
        if proc is None or proc.status != "running":
            return None
        try:
            event = proc.output_queue.get(timeout=timeout)
            return event
        except queue.Empty:
            return None

    def read_output_blocking(self, timeout: float | None = None) -> dict | None:
        """Read one output event, blocking until available or *timeout* elapses.

        timeout=None means block forever.
        """
        session = self._focus
        if session is None:
            return None
        proc = self._agents.get(session)
        if proc is None:
            return None
        try:
            event = proc.output_queue.get(timeout=timeout)
            return event
        except queue.Empty:
            return None

    def send_command(self, command: str, args: dict | None = None) -> bool:
        """Send a control command to the currently focused agent thread.

        Args:
            command: Command name (e.g. 'clear', 'compact', 'set_mode', 'resume').
            args: Optional dict of arguments for the command.

        Returns:
            True if the command was queued, False if no agent is focused.
        """
        session = self._focus
        if session is None:
            return False
        proc = self._agents.get(session)
        if proc is None or proc.status != "running":
            return False
        # P0 修复: 与 send_input 相同的线程存活检测 + 自动重启，
        # 保证 set_mode / get_info 等控制命令在 agent 崩溃后仍可用
        # （用户现象: 模式切换按钮点了没反应）。
        if proc.thread is not None and not proc.thread.is_alive():
            logger.warning(f"send_command({command}): agent 线程已死 session={session}，自动重启")
            self._agents.pop(session, None)
            if not self.start_agent(session, wait_ready=False):
                return False
            proc = self._agents.get(session)
            if proc is None or proc.status != "running":
                return False
        proc.input_queue.put({"_cmd": command, "_args": args or {}})
        return True

    def get_focus_info(self, timeout: float = 2.0) -> dict | None:
        """Request and return info dict from the currently focused agent.

        Sends a 'get_info' command and waits for the response.
        Returns None if no agent is focused or if the request times out.
        """
        ok = self.send_command("get_info")
        if not ok:
            return None
        # Read events until we get the _cmd_result for get_info
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.read_output(timeout=0.5)
            if event is None:
                continue
            if (isinstance(event, dict) and event.get("type") == "_cmd_result"
                    and event.get("cmd") == "get_info"):
                return event.get("data")
        return None

    def wait_for_ready(self, session: str | None = None, timeout: float = 15.0) -> bool:
        """Wait for an agent thread to signal readiness via the output queue.

        Blocks up to *timeout* seconds until a {"type": "_ready"} event is received.
        Returns True if ready, False on timeout.
        """
        if session is None:
            session = self._focus
        if session is None:
            return False
        proc = self._agents.get(session)
        if proc is None:
            return False
        deadline = __import__('time').time() + timeout
        while __import__('time').time() < deadline:
            try:
                event = proc.output_queue.get(timeout=0.5)
                if isinstance(event, dict) and event.get("type") == "_ready":
                    return True
            except Exception:
                continue
        return False

    def get_focus_latest_rounds(self, n: int = 3, timeout: float = 2.0) -> str | None:
        """Request latest rounds summary from the focused agent."""
        ok = self.send_command("get_latest_rounds", {"n": n})
        if not ok:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            event = self.read_output(timeout=0.5)
            if event is None:
                continue
            if (isinstance(event, dict) and event.get("type") == "_cmd_result"
                    and event.get("cmd") == "get_latest_rounds"):
                return event.get("data")
        return None

    def list_sessions(self) -> list[SessionInfo]:
        """List all managed sessions with their status."""
        result = []
        with self._lock:
            for session, proc in self._agents.items():
                tid = proc.thread.ident if proc.thread else 0
                # agent_ref 为 None（agent 线程尚未创建完成）时 phase 回退 "starting"
                ref = proc.agent_ref
                phase = getattr(ref, "phase", "starting") if ref else "starting"
                in_tool = bool(getattr(ref, "in_tool", False)) if ref else False
                mode = getattr(ref, "mode", "plan") if ref else "plan"
                # 回合级活跃标志透传（2026-08-10 修复）：agent._turn_active 仅回合
                # 真正结束才复位（run_forever finally），是 web 忙闲判定的权威信号
                turn_active = bool(getattr(ref, "_turn_active", False)) if ref else False
                result.append(SessionInfo(
                    session=session,
                    pid=os.getpid(),
                    tid=tid or 0,
                    status=proc.status,
                    last_active=proc.last_active,
                    msg_count=proc.input_queue.qsize(),
                    phase=phase,
                    in_tool=in_tool,
                    mode=mode,
                    turn_active=turn_active,
                    error=proc.error,
                ))
        return result

    @property
    def focus(self) -> str | None:
        """Return the currently focused session name, or None."""
        return self._focus

    @focus.setter
    def focus(self, session: str):
        """Set the focus to *session*. Same as switch_focus()."""
        self.switch_focus(session)

    def cleanup_idle(self, timeout: float = 300.0):
        """Stop agent threads that have been idle for more than *timeout* seconds.

        The currently focused agent is never cleaned up.
        Call this periodically (e.g. every 30s) from a background timer.
        """
        now = time.time()
        with self._lock:
            to_stop = []
            for session, proc in self._agents.items():
                if session == self._focus:
                    continue
                if proc.status == "running" and (now - proc.last_active) > timeout:
                    to_stop.append(session)

        for session in to_stop:
            # Release lock before stopping to avoid deadlock
            self.stop_agent(session)

    def close(self):
        """Stop all agent threads."""
        with self._lock:
            sessions = list(self._agents.keys())
        for session in sessions:
            self.stop_agent(session)
        self._agents.clear()
        self._focus = None


# ── Thread target (imports Agent inside the thread) ──

def _run_agent_thread(session: str, proc: _AgentProcess,
                      embedding_model: Any = None):
    """Target function for each agent thread.

    Imports Agent lazily inside the thread to avoid heavy import at manager load time.
    """
    agent = None  # T1: 提前声明，finally 中安全引用
    try:
        logger.info(f"_run_agent_thread: session={session}")
        # Lazy import to avoid circular deps and heavy imports in the main thread
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from codes.agent import Agent

        agent = Agent(
            session=session,
            mode="web",
            input_queue=proc.input_queue,
            output_queue=proc.output_queue,
        )
        proc.agent_ref = agent  # 回填引用，供主线程 request_interrupt 使用
        if embedding_model is not None:
            try:
                agent.embed_model = embedding_model
            except Exception:
                pass  # optional

        agent.run_forever()

    except BaseException as e:  # P1 修复: Exception→BaseException，捕获 CancelledError/SystemExit 等
        proc.status = "crashed"
        proc.error = str(e)
        # P1 修复: 崩溃写当前进程日志文件（含 traceback），不再只打 stderr
        logger.exception(f"Agent 线程崩溃 session={session}: {e}")
    finally:
        # T1 修复: 无论正常退出还是异常崩溃，都释放 flock 锁并关闭 DB。
        # flock 锁绑定 fd，线程退出不会自动释放 → 残留 fd 导致同进程
        # 重启同名 agent 时 acquire 自锁、误报"被占用"。
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        # P1 修复: 线程退出后状态与真实一致（防 BaseException 路径 status 残留 running，
        # 导致 send_input 假成功 / 自动重启逻辑失效）。
        if proc.status == "running":
            proc.status = "stopped"
