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
import uuid
from datetime import datetime
from typing import Any


# ── Data classes ──


from codes._log import logger
from codes.history import list_sessions
from codes.mailbox import split_mail_provider


def _split_mail_effort(model: str | None) -> tuple[str | None, str | None]:
    """拆分 mail provider 的 model 部分 '模型名:effort' → (纯模型名, effort)。

    provider 参数支持 p/model:effort 语法（如 my-provider/gpt-5.4:max）：
    split_mail_provider 拆出 model 部分后此处剥离 :effort 后缀，
    effort 经 send_input → _effort 透传给 agent 层 turn 级临时覆盖。
    """
    if not model:
        return model, None
    from codes.provider_config import split_model_effort
    pure, eff = split_model_effort(model)
    return pure, eff


_DEMUX_STOP = object()


class EventSubscription:
    """Independent bounded event stream for one session."""
    def __init__(self, session: str, maxsize: int):
        self.session = session
        self.queue: queue.Queue = queue.Queue(maxsize=max(1, maxsize))
        self.closed = False
        self.last_overflow_warn = 0.0  # 溢出告警限频（monotonic 秒）


class SessionInfo:
    """Information about a managed agent session."""
    def __init__(self, session: str, pid: int, tid: int,
                 status: str, last_active: float, msg_count: int,
                 phase: str = "idle", in_tool: bool = False,
                 mode: str = "plan", turn_active: bool = False,
                 error: str | None = None, workdir: str = ""):
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
        self.workdir = workdir

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
            "workdir": self.workdir,
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
        self.demux_thread: threading.Thread | None = None
        self.status: str = "stopped"   # "running" | "stopped" | "crashed"
        self.last_active: float = time.time()
        self.error: str | None = None
        self.ready_event = threading.Event()
        self.active_turn_id: str | None = None
        self.last_turn_id: str | None = None
        self.turn_events: dict[str, threading.Event] = {}
        self.waiters: dict[str, queue.Queue] = {}
        self.subscriptions: set[EventSubscription] = set()
        self.default_subscription = EventSubscription(session, 16384)  # 2026-09-07: 后台会话事件缓冲扩容 16x（原 1024 在慢消费时数秒即满 -> subscriber overflow 丢事件）
        self.subscriptions.add(self.default_subscription)
        self.bus_lock = threading.RLock()
        self.demux_stopped = threading.Event()
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


        # ── mail 邮差槽（惰性启动：首次 start_agent 时 _ensure_mail_postman）──
        self._mail_postman: "MailPostman | None" = None
        # ── 空闲会话回收线程槽（独立于 MailPostman；XKAGENT_IDLE_CLEANUP=off 禁用）──
        self._idle_cleanup_thread: "threading.Thread | None" = None
        self._idle_cleanup_stop = threading.Event()

        # ── Session state cache (set by repl.py / web.py after get_focus_info) ──
        self._focus_mode: str = "plan"
        self._focus_observing: bool = False
        self._focus_holder_info: dict | None = None

    # ── Public API ──

    def start_agent(self, session: str, wait_ready: bool = True) -> bool:
        from codes.session_registry import validate_session_name
        err = validate_session_name(session)
        if err:
            logger.warning(f"start_agent 拒绝非法 session 名: {session!r} ({err})")
            return False
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
        join_timeout = 3.0
        with self._lock:
            proc = self._agents.get(session)
            if proc is not None:
                if proc.status == "running":
                    return False
                stopping_thread = proc.thread if proc.status == "stopping" else None
                if proc.status != "stopping":
                    self._agents.pop(session, None)
            else:
                stopping_thread = None
        if stopping_thread is not None and stopping_thread.is_alive():
            stopping_thread.join(timeout=join_timeout)
            with self._lock:
                stale = self._agents.pop(session, None)
            if stale is not None:
                self._stop_event_bus(stale, "agent stopped (waited)")
        with self._lock:
            if session in self._agents and self._agents[session].status == "running":
                return False
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
            proc.demux_thread = threading.Thread(
                target=self._demux_loop, args=(proc,), name=f"demux-{session}", daemon=True)
            proc.demux_thread.start()
            t.start()
            self._ensure_mail_postman()   # mail 邮差惰性启动（首次）
            self._ensure_idle_cleanup_thread()   # 空闲会话回收线程（独立 30s 轮询）

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
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
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
            proc.status = "stopping"
            if self._focus == session:
                self._focus = None
        self.request_interrupt(session)
        if proc.thread is not None and proc.thread.is_alive():
            proc.thread.join(timeout=join_timeout)
            if proc.thread.is_alive():
                logger.warning(
                    f"stop_agent: 线程未在 {join_timeout}s 内退出 session={session}，锁可能残留"
                )
                try:
                    from codes.lock import cleanup as _cleanup_lock
                    _ok_c, _msg_c = _cleanup_lock(session)
                    logger.info(f"stop_agent: 兜底清理锁 session={session} ok={_ok_c} ({_msg_c})")
                except Exception as _e:
                    logger.warning(f"stop_agent: 清理锁失败 session={session}: {_e}")
        with self._lock:
            if self._agents.get(session) is proc:
                if proc.thread is None or not proc.thread.is_alive():
                    self._agents.pop(session, None)
                else:
                    proc.status = "stopping"
        if proc.thread is None or not proc.thread.is_alive():
            self._stop_event_bus(proc, "agent stopped")
        else:
            proc.error = proc.error or "agent stop requested"
            self._unblock_waiters(proc, proc.error)
        return True

    def switch_focus(self, session: str) -> bool:
        """Switch the current focus to *session*.

        If the session's agent thread is not running, it will be started.
        Returns True on success, False if session is empty.
        """
        if not session:
            return False

        # Auto-start if not running. Never wait for readiness while holding _lock.
        with self._lock:
            proc = self._agents.get(session)
            needs_start = proc is None or proc.status != "running"
        if needs_start and not self.start_agent(session):
            return False
        with self._lock:
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
        else:
            # T5 修复(决策A): get_focus_info 超时（目标 agent 未就绪/队列积压）
            # 时回退到文件级锁探测，避免缓存保留旧 session 的占用状态，
            # 导致切换到空闲 session 后仍误报"被占用"。
            from codes.lock import is_locked as _is_locked, is_same_process as _is_same_process
            locked, meta = _is_locked(session)
            observing = bool(locked and isinstance(meta, dict) and not _is_same_process(meta))
            self._focus_observing = observing
            self._focus_holder_info = meta if observing else None
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

    def send_input(self, text: str, session: str | None = None,
                    provider: str | None = None, model: str | None = None,
                    mail_meta: dict | None = None,
                    effort: str | None = None) -> bool:
        """Send input text to an agent session (default: current focus).

        Returns turn_id (str) if sent, False if no agent is focused/running.

        provider/model：可选收信方本次任务的 LLM provider/model 覆盖（mail callagent
        投递用；写入 input item 的 _provider/_model，由 agent 层 turn 级临时覆盖，
        不影响会话自身持久配置）。
        effort：可选 reasoning_effort 临时覆盖（provider 参数 'p/model:effort' 拆出），
        写入 input item 的 _effort，由 agent 层 turn 级临时覆盖（_cmd_effort）。
        mail_meta：可选 callagent 邮件信封元信息 {"id","from","reply_to","need_reply"}；写入
        input item 的 _mail_meta，由 agent 层包装进指令（need_reply=true 时含回信指引）。
        返回值自 mail v2.3.1 起为 turn_id（向后兼容：真值语义不变，web/repl 不读返回值）。
        """
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
            if proc is None or proc.status != "running":
                return False
            dead = proc.thread is not None and not proc.thread.is_alive()
        # P0 修复: 线程存活检测——status=running 但线程已死（BaseException 崩溃、
        # 未被 except 捕获的异常）时自动重启，避免消息被静默吞掉
        # （用户现象: "No agent session active" / 发消息无响应）。
        if dead:
            logger.warning(f"send_input: agent 线程已死 session={session}，自动重启")
            with self._lock:
                if self._agents.get(session) is proc:
                    self._agents.pop(session, None)
            self._stop_event_bus(proc, "agent restarted")
            if not self.start_agent(session, wait_ready=False):
                return False
        turn_id = uuid.uuid4().hex
        with self._lock:
            proc = self._agents.get(session)
            if proc is None or proc.status != "running":
                return False
            with proc.bus_lock:
                proc.turn_events = {
                    key: event for key, event in proc.turn_events.items() if not event.is_set()
                }
                proc.active_turn_id = turn_id
                proc.last_turn_id = turn_id
                proc.turn_events[turn_id] = threading.Event()
            _item = {"_input": text, "_turn_id": turn_id}
            if provider:
                _item["_provider"] = provider
            if model:
                _item["_model"] = model
            if effort:
                _item["_effort"] = effort
            if mail_meta:
                _item["_mail_meta"] = mail_meta
            proc.input_queue.put(_item)
            proc.last_active = time.time()
        return turn_id

    def read_output(self, timeout: float = 0.1,
                    subscription: EventSubscription | None = None) -> dict | None:
        """Read from the focused session's legacy default subscription."""
        if subscription is not None:
            return self.read_subscription(subscription, timeout)
        with self._lock:
            session = self._focus
        return self.read_output_for(session, timeout) if session else None

    def read_output_for(self, session: str, timeout: float = 0.1,
                        subscription: EventSubscription | None = None) -> dict | None:
        """Read from a session's legacy default subscription (C1).

        C1（后台接收数据）：与 read_output 不同，本方法不依赖当前 focus——
        切走后旧回合仍在后台执行并持续向自己 session 的订阅产出事件，
        web 的 _handle_chat_ws 用它按 target 读取，实现"切走后后台继续接收"。
        """
        if subscription is not None:
            return self.read_subscription(subscription, timeout)
        if not session:
            return None
        with self._lock:
            proc = self._agents.get(session)
        if proc is None:
            return None
        return self.read_subscription(proc.default_subscription, timeout)

    def read_output_blocking(self, timeout: float | None = None) -> dict | None:
        """Read the focused default subscription, blocking until timeout."""
        with self._lock:
            session = self._focus
            proc = self._agents.get(session) if session else None
        if proc is None:
            return None
        return self.read_subscription(proc.default_subscription, timeout)

    def subscribe(self, session: str | None = None, maxsize: int = 4096) -> EventSubscription | None:
        """Create an independent bounded subscription for a session."""
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
        if proc is None or proc.status != "running":
            return None
        subscription = EventSubscription(session, maxsize)
        with proc.bus_lock:
            proc.subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription | None) -> bool:
        """Remove and close a subscription."""
        if subscription is None:
            return False
        with self._lock:
            proc = self._agents.get(subscription.session)
        if proc is not None:
            with proc.bus_lock:
                proc.subscriptions.discard(subscription)
        subscription.closed = True
        return True

    @staticmethod
    def read_subscription(subscription: EventSubscription | None,
                          timeout: float | None = 0.1) -> dict | None:
        """Read one event from an explicit subscription."""
        if subscription is None:
            return None
        try:
            return subscription.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_command(self, command: str, args: dict | None = None,
                     session: str | None = None, request_id: str | None = None) -> bool:
        """Send a control command to the currently focused agent thread.

        Args:
            command: Command name (e.g. 'clear', 'compact', 'set_mode', 'resume').
            args: Optional dict of arguments for the command.

        Returns:
            True if the command was queued, False if no agent is focused.
        """
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
            if proc is None or proc.status != "running":
                return False
            dead = proc.thread is not None and not proc.thread.is_alive()
        # P0 修复: 与 send_input 相同的线程存活检测 + 自动重启，
        # 保证 set_mode / get_info 等控制命令在 agent 崩溃后仍可用
        # （用户现象: 模式切换按钮点了没反应）。
        if dead:
            logger.warning(f"send_command({command}): agent 线程已死 session={session}，自动重启")
            with self._lock:
                if self._agents.get(session) is proc:
                    self._agents.pop(session, None)
            self._stop_event_bus(proc, "agent restarted")
            if not self.start_agent(session, wait_ready=False):
                return False
        payload = {"_cmd": command, "_args": args or {}}
        if request_id:
            payload["_request_id"] = request_id
        with self._lock:
            proc = self._agents.get(session)
            if proc is None or proc.status != "running":
                return False
            proc.input_queue.put(payload)
        return True

    def send_command_wait(self, command: str, args: dict | None = None,
                          session: str | None = None, timeout: float = 2.0) -> dict | None:
        """Send a correlated command and wait for its exact result."""
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
            dead = bool(proc and proc.status == "running" and proc.thread
                        and not proc.thread.is_alive())
            if dead and self._agents.get(session) is proc:
                self._agents.pop(session, None)
        if dead:
            self._stop_event_bus(proc, "agent restarted")
            if not self.start_agent(session, wait_ready=False):
                return None
        request_id = uuid.uuid4().hex
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            session = session or self._focus
            proc = self._agents.get(session) if session else None
            if proc is None or proc.status != "running":
                return None
            with proc.bus_lock:
                proc.waiters[request_id] = waiter
            proc.input_queue.put({
                "_cmd": command, "_args": args or {}, "_request_id": request_id})
        try:
            try:
                result = waiter.get(timeout=timeout)
            except queue.Empty:
                return None
            return result if result.get("type") != "_manager_error" else None
        finally:
            with proc.bus_lock:
                proc.waiters.pop(request_id, None)

    def get_focus_info(self, timeout: float = 2.0) -> dict | None:
        """Request and return info dict from the currently focused agent.

        Sends a 'get_info' command and waits for the response.
        Returns None if no agent is focused or if the request times out.
        """
        event = self.send_command_wait("get_info", timeout=timeout)
        return event.get("data") if event else None

    def wait_for_ready(self, session: str | None = None, timeout: float = 15.0) -> bool:
        """Wait for the demultiplexer to observe the agent's ready event."""
        with self._lock:
            session = session or self._focus
            if session is None:
                return False
            proc = self._agents.get(session)
        if proc is None:
            return False
        return proc.ready_event.wait(timeout)

    def wait_for_turn_end(self, session: str | None = None, turn_id: str | None = None,
                          timeout: float | None = None) -> bool:
        """Wait for one identified turn without consuming stream events."""
        session = session or self._focus
        proc = self._agents.get(session) if session else None
        if proc is None:
            return False
        with proc.bus_lock:
            turn_id = turn_id or proc.active_turn_id or proc.last_turn_id
            event = proc.turn_events.get(turn_id) if turn_id else None
        if event is None:
            return False
        return event.wait(timeout)

    def get_focus_latest_rounds(self, n: int = 3, timeout: float = 2.0) -> str | None:
        """Request latest rounds summary from the focused agent."""
        event = self.send_command_wait("get_latest_rounds", {"n": n}, timeout=timeout)
        return event.get("data") if event else None

    def _demux_loop(self, proc: _AgentProcess) -> None:
        """Sole consumer of an agent's raw output queue."""
        try:
            while True:
                event = proc.output_queue.get()
                if event is _DEMUX_STOP:
                    break
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "_ready":
                    proc.ready_event.set()
                elif event_type == "_turn_end":
                    turn_id = event.get("turn_id")
                    with proc.bus_lock:
                        turn_event = proc.turn_events.get(turn_id)
                        if turn_event:
                            turn_event.set()
                        if proc.active_turn_id == turn_id:
                            proc.active_turn_id = None
                elif event_type == "_cmd_result":
                    request_id = event.get("request_id")
                    with proc.bus_lock:
                        waiter = proc.waiters.get(request_id)
                    if waiter is not None:
                        try:
                            waiter.put_nowait(event)
                        except queue.Full:
                            pass
                self._publish(proc, event)
        finally:
            self._unblock_waiters(proc, proc.error or "event bus stopped")
            with proc.bus_lock:
                for subscription in proc.subscriptions:
                    subscription.closed = True
            proc.demux_stopped.set()

    @staticmethod
    def _publish(proc: _AgentProcess, event: dict) -> None:
        with proc.bus_lock:
            subscriptions = tuple(proc.subscriptions)
        for subscription in subscriptions:
            if subscription.closed:
                continue
            try:
                subscription.queue.put_nowait(event)
            except queue.Full:
                dropped = 1
                while True:
                    try:
                        subscription.queue.get_nowait()
                        dropped += 1
                    except queue.Empty:
                        break
                marker = {
                    "type": "_dropped_events",
                    "data": {"count": dropped, "policy": "buffer_reset"},
                    "session": event.get("session", proc.session),
                    "turn_id": event.get("turn_id"),
                    "seq": event.get("seq"),
                }
                try:
                    subscription.queue.put_nowait(marker)
                except queue.Full:
                    pass
                # 限频告警：慢/死消费者持续溢出时避免日志刷屏（每订阅 60s 至多 1 条）
                now = time.monotonic()
                if now - subscription.last_overflow_warn >= 60.0:
                    logger.warning(
                        f"event subscriber overflow: session={proc.session} dropped={dropped}")
                    subscription.last_overflow_warn = now

    @staticmethod
    def _unblock_waiters(proc: _AgentProcess, error: str) -> None:
        with proc.bus_lock:
            waiters = tuple(proc.waiters.values())
            turn_events = tuple(proc.turn_events.values())
        for waiter in waiters:
            try:
                waiter.put_nowait({"type": "_manager_error", "error": error})
            except queue.Full:
                pass
        for turn_event in turn_events:
            turn_event.set()

    def _stop_event_bus(self, proc: _AgentProcess, error: str) -> None:
        proc.error = proc.error or error
        proc.output_queue.put(_DEMUX_STOP)
        self._unblock_waiters(proc, proc.error)
        if proc.demux_thread and proc.demux_thread.is_alive():
            proc.demux_thread.join(timeout=1.0)
        with proc.bus_lock:
            for subscription in proc.subscriptions:
                subscription.closed = True

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
                    msg_count=len(getattr(ref, "messages", [])) if ref else 0,
                    phase=phase,
                    in_tool=in_tool,
                    mode=mode,
                    turn_active=turn_active,
                    error=proc.error,
                    workdir=str(getattr(ref, "cwd", "")) if ref else "",
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

    def cleanup_idle(self, timeout: float | None = None,
                     max_alive: int | None = None) -> int:
        """容量 LRU 回收空闲会话（v0902.1 策略）。

        - 运行数量 ≤ max_alive（默认 AGENT_MAX_ALIVE=30）→ 不回收任何会话；
        - 超量后按 last_active 升序回收最老的"不活跃"会话（超几个收几个）；
        - "不活跃"= 非 focus + 非观察者 + 无任务执行（_turn_active/in_tool/phase==llm
          三元组全否，覆盖长程 tool 等待期）+ 距上次输入超过 timeout
          （默认 IDLE_RECLAIM_SECONDS=300s）；
        - 候选不足（全部活跃/近期有交互）→ 不强制回收（宁超上限）；
        - 回收动作 stop_agent（优雅停止，历史不丢，下次交互自动拉起）。
        返回本次回收会话数。
        """
        now = time.time()
        _grace = (_env_float(IDLE_RECLAIM_ENV, IDLE_RECLAIM_SECONDS)
                  if timeout is None else float(timeout))
        _max = (_env_int(IDLE_MAX_ALIVE_ENV, AGENT_MAX_ALIVE)
                if max_alive is None else int(max_alive))
        with self._lock:
            procs = list(self._agents.items())
        if len(procs) <= _max:
            return 0
        surplus = len(procs) - _max
        cands: list[tuple[float, str, Any]] = []
        for session, proc in procs:
            if session == self._focus:
                continue
            if proc.status != "running":
                continue
            ar = proc.agent_ref
            if ar is not None:
                if getattr(ar, "_observing", False):
                    continue
                if (getattr(ar, "_turn_active", False)
                        or getattr(ar, "in_tool", False)
                        or getattr(ar, "phase", "") == "llm"):
                    continue
            last = float(getattr(proc, "last_active", 0.0) or 0.0)
            if now - last <= _grace:
                continue
            cands.append((last, session, proc))
        cands.sort(key=lambda t: t[0])
        victims = [(last, session, proc) for last, session, proc in cands[:surplus]]
        for last, session, proc in victims:
            self.stop_agent(session)
        if victims:
            logger.info("idle cleanup: 回收 %d 个会话 (运行 %d > 上限 %d, 候选 %d)",
                        len(victims), len(procs), _max, len(cands))
        return len(victims)

    def close(self):
        """Stop all agent threads."""
        if self._mail_postman is not None:
            self._mail_postman.stop()
            self._mail_postman = None
        self._idle_cleanup_stop.set()
        if self._idle_cleanup_thread is not None:
            self._idle_cleanup_thread.join(timeout=2.0)
            self._idle_cleanup_thread = None
        with self._lock:
            sessions = list(self._agents.keys())
        for session in sessions:
            self.stop_agent(session)
        with self._lock:
            self._agents.clear()
            self._focus = None

    def _ensure_mail_postman(self):
        """惰性启动 MailPostman（首次 start_agent 时；XKAGENT_MAIL=off 自动禁用）。"""
        if self._mail_postman is not None:
            return
        try:
            pm = MailPostman(self)
            if pm.start():
                self._mail_postman = pm
        except Exception:
            logger.exception("MailPostman 启动失败，邮件功能暂停")

    def _ensure_idle_cleanup_thread(self):
        """惰性启动空闲会话回收线程（30s 轮询；与 MailPostman 解耦）。

        XKAGENT_IDLE_CLEANUP=off/0/false/none → 禁用（不启线程，无任何自动回收，
        与 v0827 行为对齐）；线程异常自愈：单轮异常捕获，下轮继续。
        """
        if (self._idle_cleanup_thread is not None
                and self._idle_cleanup_thread.is_alive()):
            return
        env = os.environ.get(IDLE_CLEANUP_ENV, "").strip().lower()
        if env in ("off", "0", "false", "none"):
            return
        self._idle_cleanup_stop.clear()

        def _loop():
            while not self._idle_cleanup_stop.wait(IDLE_CLEANUP_INTERVAL):
                try:
                    self.cleanup_idle()
                except Exception:
                    logger.exception("idle cleanup 异常（自愈，下轮重试）")

        self._idle_cleanup_thread = threading.Thread(
            target=_loop, daemon=True, name="idle-cleanup")
        self._idle_cleanup_thread.start()
        logger.info("idle cleanup 线程启动: interval=%s", IDLE_CLEANUP_INTERVAL)


MAIL_SCAN_INTERVAL = 1.0        # 轮次间隔（秒）
MAIL_TURN_TIMEOUT = 600.0       # 二级保险：turn 累计无结果（心跳新鲜场景）
MAIL_MAX_RETRY = 2              # 重投上限（耗尽 → dead）
MAIL_CROSS_WAIT = 600.0         # 他进程持锁等待上限
AGENT_MAX_ALIVE = 30            # 同时运行的 agent 会话上限（超量才按 LRU 回收）
# 空闲会话回收策略（v0902.1：容量 LRU，非时间一刀切）
IDLE_RECLAIM_SECONDS = 300.0    # "不活跃"判定：无交互 + 无任务执行超过此秒数
IDLE_CLEANUP_INTERVAL = 30.0    # 独立回收线程轮询周期（秒）
IDLE_CLEANUP_ENV = "XKAGENT_IDLE_CLEANUP"    # off/0/false/none 禁用自动回收
IDLE_MAX_ALIVE_ENV = "XKAGENT_MAX_AGENTS"    # 覆盖 AGENT_MAX_ALIVE
IDLE_RECLAIM_ENV = "XKAGENT_IDLE_RECLAIM"    # 覆盖 IDLE_RECLAIM_SECONDS


def _env_float(name: str, default: float) -> float:
    """读取 env 数值（非法回退默认）。"""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """读取 env 整数（非法回退默认）。"""
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except (TypeError, ValueError):
        return default
MAIL_SUBSCRIBE_MAX = 65536      # 事件订阅容量（防 buffer_reset 丢 error 事件）
MAIL_STARTING_TIMEOUT = 20.0    # 两阶段投递 ready 等待上限（秒）
MAIL_RETRY_COOLDOWN = 0.2       # failed 后重投冷却期（秒，跨轮等待）
MAIL_COMPACT_THRESHOLD = 5000   # mail.jsonl 聚合行数超阈值 → 压实归档（毫秒级代价）


class MailPostman:
    """mail.jsonl 轮次邮差（v2.3.1 §4/§5）：每轮抢轮次锁 → tail → 盯梢三分支 → 认领。

    惰性启动（AgentManager 首次 start_agent 时）；XKAGENT_MAIL=off 禁用；
    线程自愈：_run 全捕获 + 轮内预算（防慢轮被误判 lease 残留 5s）。
    """

    def __init__(self, manager: "AgentManager"):
        self.manager = manager
        self.mailbox = None                   # 惰性起 Mailbox（start 时）
        self._thread = None
        self._stop = threading.Event()
        self._subs: dict[str, Any] = {}       # to -> EventSubscription（盯梢订阅）
        self._errflags: dict[tuple, bool] = {}  # (to, turn_id) -> 出现过 error/interrupted
        self._starting: dict[str, dict] = {}  # to -> {"mid","ts","body"} 两阶段投递中
        self._cross_wait: dict[str, float] = {}  # to -> 他进程持锁等待起点

    # ── 生命周期（惰性启动 / 线程自愈）────────────────────────────

    def start(self) -> bool:
        """惰性启动；XKAGENT_MAIL=off 返回 False；已在运行返回 True。

        经 AgentManager.start_agent → _ensure_mail_postman 首次触发（v2.3.1 §4）。
        """
        env = os.environ.get("XKAGENT_MAIL", "").strip().lower()
        if env in ("off", "0", "false", "none"):
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            from codes.mailbox import Mailbox
            self.mailbox = Mailbox()
        except Exception:
            logger.exception("Mailbox 初始化失败")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mail-postman", daemon=True)
        self._thread.start()
        logger.info(f"MailPostman 启动: path={self.mailbox.path}")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    # ── 主循环（自愈：全捕获 + 轮内预算，防慢轮被误判 lease 残留）────

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._round()
            except Exception:
                logger.exception("MailPostman round 异常")
            dt = time.monotonic() - t0
            self._stop.wait(max(0.05, MAIL_SCAN_INTERVAL - dt))

    def _round(self) -> None:
        if self.mailbox is None:
            return
        if not self.mailbox.acquire_lease():
            return
        try:
            self.mailbox.scan_new()        # 1) tail 增量
            self._starting_step()          # 2) 两阶段投递推进
            self._watch_deliveries()       # 3) 盯梢三分支
            self._claim_pending()          # 4) 认领新信
            self._maybe_compact()          # 5) 行数阈值压实归档
            # 6) 空闲会话回收由独立线程负责（_ensure_idle_cleanup_thread）
        finally:
            self.mailbox.release_lease()

    # ── 两阶段投递推进（starting → ready → 直读确认 → delivered）──────

    def _starting_step(self) -> None:
        for to in list(self._starting.keys()):
            info = self._starting[to]
            proc = self.manager._agents.get(to)
            now = time.time()
            if proc is None:
                # start_agent 未建成（被拒/失败）：放弃本轮（下轮 claim 重新尝试）
                del self._starting[to]
                continue
            ar = proc.agent_ref
            if not proc.ready_event.is_set() or ar is None:
                if now - info["ts"] > MAIL_STARTING_TIMEOUT:
                    self.manager.stop_agent(to)
                    self._retry_or_dead(info["mid"], to=to)
                    del self._starting[to]
                continue
            # ready 后直读确认（v2.3.1：agent_ref 直读，不用 get_info 命令通道）
            if ar._lock_held and not ar._observing:
                tid = self.manager.send_input(info["body"], session=to,
                                      provider=info.get("provider"),
                                      model=info.get("model"),
                                      effort=info.get("effort"),
                                      mail_meta=info.get("mail_meta"))
                if tid:
                    self._deliver_ok(info["mid"], tid, to)
                else:
                    self._retry_or_dead(info["mid"], to=to)
                del self._starting[to]
            elif ar._observing:
                # 他进程刚接管：停止新 agent，转失败重投（等待路径）
                self.manager.stop_agent(to)
                self._retry_or_dead(info["mid"], to=to)
                del self._starting[to]
            else:
                # 双 false（锁定态未定）：续等
                if now - info["ts"] > MAIL_STARTING_TIMEOUT:
                    self.manager.stop_agent(to)
                    self._retry_or_dead(info["mid"], to=to)
                    del self._starting[to]

    # ── 盯梢三分支（v2.3.1 §5）────────────────────────────────────

    def _watch_deliveries(self) -> None:
        for st in self.mailbox.delivered_items():
            mid = st["send"]["id"]
            to = st["to"]
            last = st["last"]
            turn_id = last.get("turn_id")
            proc = self.manager._agents.get(to)
            ended = False
            if proc is not None and turn_id:
                with proc.bus_lock:
                    ev = proc.turn_events.get(turn_id)
                # a) turn Event：None（已 set 被清）或 is_set() → 视为已结束
                if ev is None or ev.is_set():
                    ended = True
                    if self._drain_errors(proc, turn_id, to):
                        self._retry_or_dead(mid, "turn_error", to=to)   # 事件流含 error/interrupted
                    else:
                        self._finish(mid, "done", to=to)
                    self._drop_sub(to)
                    continue
            # b) 投递者死亡（心跳 stale/无锁）→ 孤儿兜底
            if self._deliverer_dead(to):
                self._retry_or_dead(mid, "deliverer_dead", to=to)
                continue
            # c) 二级保险：turn 累计无果超时（心跳新鲜但挂起）
            if time.time() - last.get("at", 0) > MAIL_TURN_TIMEOUT:
                self._retry_or_dead(mid, "turn_timeout", to=to)

    def _drain_errors(self, proc, turn_id: str, to: str) -> bool:
        """从盯梢订阅中聚合 error/interrupted（先 drain 再判定，防溢出丢事件）。"""
        sub = self._subs.get(to)
        if sub is None:
            return False
        flagged = self._errflags.get((to, turn_id), False)
        while True:
            ev = self.manager.read_subscription(sub, timeout=0.0)
            if ev is None:
                break
            if ev.get("type") in ("error", "interrupted"):
                self._errflags[(to, ev.get("turn_id"))] = True
                if ev.get("turn_id") == turn_id:
                    flagged = True
        return flagged

    def _deliverer_dead(self, to: str) -> bool:
        """投递者进程存活检查：会话锁心跳 stale/无锁（lock.py is_locked 现成）。"""
        try:
            from codes.lock import is_locked
            locked, _meta = is_locked(to)
            return not locked    # stale/无锁 = Available = 可视为投递者已死
        except Exception:
            return False

    # ── 认领新信（send/failed 候选）────────────────────────────────

    def _claim_pending(self) -> None:
        """认领候选（send + 未耗尽 failed 重投）：均要求该 to 无在途、本进程无在途用户 turn。"""
        now = time.time()
        for send_row in self.mailbox.claimable(now, MAIL_MAX_RETRY, MAIL_RETRY_COOLDOWN):
            mid = send_row["id"]
            to = send_row.get("_to") or send_row["to"]   # 广播信逐收件人投递
            if self.mailbox.in_flight(to):
                continue
            proc = self.manager._agents.get(to)
            if proc is not None and proc.status == "running" and proc.agent_ref is not None:
                ar = proc.agent_ref
                if ar._turn_active:
                    continue                      # 用户在途 turn → 下轮
                if ar._lock_held and not ar._observing:
                    self._try_direct(send_row)
                # observing/双 false → 等待（三态识别：不 start_agent）
                continue
            # proc 不存在/线程死 → 三态识别（读锁判定）
            self._try_fresh(send_row)

    @staticmethod
    def _mail_meta_for(send_row: dict) -> dict:
        """构造 mail_meta 信封元信息（收信方可见广播收件人名单）。

        recipients：仅广播信（send.to 为 list）携带，供 _wrap_mail_instruction
        展示"本邮件为广播，同时发送给 ..."，让收信方感知全体收件人。
        """
        meta = {"id": send_row["id"],
                "from": send_row.get("from"),
                "reply_to": send_row.get("reply_to"),
                "need_reply": send_row.get("need_reply") or False}
        tos = send_row.get("to")
        if isinstance(tos, list):
            meta["recipients"] = tos
        return meta

    def _try_direct(self, send_row: dict) -> None:
        """活体直投（本进程持锁 agent，send_row["_to"] 为本次投递收件人）。"""
        to = send_row.get("_to") or send_row["to"]
        _p, _m = split_mail_provider(send_row.get("provider") or "")
        _m, _eff = _split_mail_effort(_m)
        tid = self.manager.send_input(send_row["body"], session=to,
                                      provider=_p, model=_m, effort=_eff,
                                      mail_meta=self._mail_meta_for(send_row))
        if tid:
            self._deliver_ok(send_row["id"], tid, to)
        else:
            self._retry_or_dead(send_row["id"], to=to)

    def _try_fresh(self, send_row: dict) -> None:
        """无 proc/线程死 → 判定锁后启动两阶段投递（send_row["_to"] 为收件人）。"""
        to = send_row.get("_to") or send_row["to"]
        try:
            from codes.lock import is_locked, is_same_process
            locked, meta = is_locked(to)
        except Exception:
            locked, meta = False, None
        if locked and isinstance(meta, dict):
            if is_same_process(meta):
                self._try_direct(send_row)   # 本进程持锁但 proc 不可见（少见）：直投
                return
            # 他进程持锁（agent 活）→ 等待（CROSS_WAIT 兜底）
            w0 = self._cross_wait.setdefault(to, time.time())
            if time.time() - w0 > MAIL_CROSS_WAIT:
                self._retry_or_dead(send_row["id"], to=to)
            return
        # 无锁/死 → start_agent 两阶段（wait_ready=False，postman 自己盯 ready）
        if self.manager.start_agent(to, wait_ready=False):
            _p, _m = split_mail_provider(send_row.get("provider") or "")
            _m, _eff = _split_mail_effort(_m)
            self._starting[to] = {"mid": send_row["id"], "ts": time.time(),
                                  "body": send_row["body"],
                                  "provider": _p, "model": _m, "effort": _eff,
                                  "mail_meta": self._mail_meta_for(send_row)}
        else:
            self._retry_or_dead(send_row["id"], to=to)

    # ── 状态写入/清理 ─────────────────────────────────────────────

    def _deliver_ok(self, mid: str, turn_id: str, to: str) -> None:
        """投递成功：写 delivered 行（含 turn_id+host），建立盯梢订阅（先清旧订阅防泄漏）。"""
        import socket
        self.mailbox.add_status(mid, "delivered", to=to,
                                turn_id=turn_id, host=socket.gethostname()[:16])
        self._drop_sub(to)     # 防旧订阅泄漏（重投循环中订阅累积）
        sub = self.manager.subscribe(to, maxsize=MAIL_SUBSCRIBE_MAX)
        if sub is not None:
            self._subs[to] = sub

    def _finish(self, mid: str, type_: str, to: str | None = None) -> None:
        kw = {"to": to} if to else {}
        self.mailbox.add_status(mid, type_, **kw)

    def _retry_or_dead(self, mid: str, reason: str = "", to: str | None = None) -> None:
        """failed(retry+1)；耗尽 → dead（终态，终止重投循环）。

        retry 从 max_retry 累计（mailbox 聚合维护——重投后 last=delivered 无 retry 字段，
        必须用历史 failed 行的累计值，否则 retry 恒=1 永不 dead → 无限重投）。
        广播信按收件人独立计数（to 指定时只影响该收件人）。
        """
        st = self.mailbox.get(mid)
        if st is None:
            return
        per_to = st.get("per_to") or {}
        if to is not None and to in per_to:
            retry = int(per_to[to].get("max_retry") or 0) + 1
        else:
            retry = int(st.get("max_retry") or 0) + 1
        if retry < MAIL_MAX_RETRY:
            kw = {"to": to} if to else {}
            self.mailbox.add_status(mid, "failed", retry=retry, **kw)
            logger.warning("MailPostman 邮件失败: id=%s to=%s retry=%d/%d reason=%s",
                           mid, to, retry, MAIL_MAX_RETRY, reason)
        else:
            kw = {"to": to} if to else {}
            self.mailbox.add_status(mid, "dead", **kw)
            logger.warning("MailPostman 邮件 dead（重试耗尽）: id=%s to=%s reason=%s",
                           mid, to, reason)

    def _drop_sub(self, to: str) -> None:
        sub = self._subs.pop(to, None)
        if sub is not None:
            self.manager.unsubscribe(sub)

    def _maybe_compact(self) -> None:
        """mail.jsonl 只增 → 行数超阈值压实归档（终态剔除；持锁轮内执行）。"""
        try:
            if len(self.mailbox.status) >= MAIL_COMPACT_THRESHOLD:
                n = self.mailbox.compact(MAIL_MAX_RETRY)
                logger.info(f"MailPostman compact: 保留 {n} 行")
        except Exception:
            logger.exception("compact 异常（忽略，下轮重试）")

# ── Thread target (imports Agent inside the thread) ──

def _run_agent_thread(session: str, proc: _AgentProcess,
                      embedding_model: Any = None):
    """Target function for each agent thread.

    Imports Agent lazily inside the thread to avoid heavy import at manager load time.
    """
    agent = None  # T1: 提前声明，finally 中安全引用
    session_token = None
    try:
        from codes import config
        context, session_token = config.activate_session(session, ensure=True)
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
        # ── DB 损坏检测与提示（2026-08-22 修复）──
        # 崩溃时检查 db 完整性：若损坏给出重建指引，避免"点击→崩溃→再点击"死循环。
        try:
            from codes.history import db_health_check
            _ok, _detail = db_health_check(session)
            if not _ok:
                _msg = (f"⚠️ session={session} 数据库已损坏（{_detail}）。"
                        f"建议备份后删除 {session}.msgz 文件并重启，会话将重建为空库")
                logger.error(_msg)
                proc.error = f"{e} | DB_CORRUPTED: {_msg}"
        except Exception:
            pass
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
        if session_token is not None:
            try:
                from codes import config
                config.reset_session(session_token)
            except Exception:
                pass
        proc.output_queue.put(_DEMUX_STOP)