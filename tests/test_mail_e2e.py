# -*- coding: utf-8 -*-
"""tests/test_mail_e2e.py — 端到端回信闭环（双宿主 postman + 真实 callagent，mock agent）。

场景：sA/sB 两"会话"分属两个宿主（各自独立 Mailbox+MailPostman 线程，共享 bus 文件）：
  sA callagent(sB,"ping") → postmanB 投递 → sB 收到后回信 sA("pong") → postmanA 投递
  → sA 收到后回信 sB("done-ack") → postmanB 投递 → 闭环完成（断言各信 done）。
"""
import os
import sys
import time
import json
import threading

_DIR = "/tmp/mail_e2e_dir"
shutil_rmtree = lambda: None  # 占位（由外层清理）
os.makedirs(_DIR, exist_ok=True)
os.environ["XKAGENT_MAIL"] = os.path.join(_DIR, "mail.jsonl")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codes.mailbox import Mailbox
from codes.manager import MailPostman
from codes.tools import exec_callagent
from types import SimpleNamespace

# 全局完成信号
EVENTS = []
EVENTS_LOCK = threading.Lock()
DONE = threading.Event()
PV_DONE = threading.Event()  # provider 场景闭环完成


class FakeAgent:
    def __init__(self):
        self._lock_held = True
        self._observing = False
        self._turn_active = False


class FakeProc:
    def __init__(self, on_receive):
        self.status = "running"
        self.agent_ref = FakeAgent()
        self.ready_event = threading.Event()
        self.ready_event.set()
        self.turn_events = {}
        self.bus_lock = threading.RLock()
        self._on_receive = on_receive
        self._consume_thread = None
        self._stop = threading.Event()

    def start_consumer(self, session, bus):
        """模拟 agent run_forever 消费 input_queue —— 简化：回调驱动。"""
        # 真实 manager.send_input 写入 proc.input_queue；此处 Fake 的发送经 turn_events 挂钩
        pass


class FakeManager:
    """模拟单宿主多会话 manager：send_input 按 session 路由（真实 AgentManager 语义）。"""

    def __init__(self, proc_map, on_receive, locks):
        self._agents = proc_map
        self._locks = locks
        self.subs = {}
        self._on_receive = on_receive
        self._seq = 0

    def send_input(self, text, session=None, provider=None, model=None, mail_meta=None, effort=None):
        tid = "e2e_%d" % self._seq
        self.provider_log = getattr(self, "provider_log", []) + [
            (session, provider, model)]
        self._seq += 1
        proc = self._agents.get(session)
        if proc is not None:
            with proc.bus_lock:
                proc.turn_events[tid] = threading.Event()
        # 模拟投递：异步触发"agent 处理"（turn 开始）
        threading.Thread(target=self._on_receive, args=(session, text, tid),
                         daemon=True).start()
        return tid

    def start_agent(self, session, wait_ready=True):
        return True

    def subscribe(self, session, maxsize=4096):
        return SimpleNamespace(session=session, q=[], closed=False)

    def unsubscribe(self, sub):
        sub.closed = True

    def read_subscription(self, sub, timeout=0.0):
        if sub and sub.q:
            return sub.q.pop(0)
        return None

    def cleanup_idle(self, timeout=300.0):
        pass

    def stop_agent(self, session, join_timeout=3.0):
        pass


# 会话对总线的重要引用（供断言）
BUS = Mailbox()
FAKE_LOCKS = {}


def fake_is_locked(session):
    from codes.mailbox import mail_path
    # e2e 简化：会话锁假象——两 host 的 agent 都视为本进程持锁
    state = "locked" if session in ("sA", "sB") else "none"
    if state == "locked":
        import time as _t
        return True, {"instance_id": "fake", "hostname": "x", "pid": 1, "locked_at": _t.time()}
    return False, "Available"


import codes.lock
codes.lock.is_locked = fake_is_locked
codes.lock.is_same_process = lambda meta: True


def handler(session, text, tid):
    """'agent' 处理消息 → set turn_events[tid]（turn 完成）→ 可能回信。"""
    try:
        with EVENTS_LOCK:
            EVENTS.append((session, text))
        print("  [handler] %s 收到 %r (tid=%s) 开始处理" % (session, text, tid), flush=True)
        proc = HOSTS[session]["proc"]
        with proc.bus_lock:
            ev = proc.turn_events.get(tid)
            if ev is not None:
                ev.set()
        print("  [handler] %s turn 完成" % session, flush=True)
        if session == "sB" and text == "ping":
            time.sleep(0.1)
            print("  [handler] sB 回信 pong", flush=True)
            r = exec_callagent(SimpleNamespace(session="sB"), to="sA", message="pong",
                               reply_to="", delay_seconds=0)
            print("  sB 回信 pong:", r.stdout, flush=True)
        elif session == "sA" and text == "pong":
            time.sleep(0.1)
            r = exec_callagent(SimpleNamespace(session="sA"), to="sB", message="done-ack",
                               reply_to="", delay_seconds=0)
            print("  sA 回信 done-ack:", r.stdout, flush=True)
        elif session == "sB" and text == "done-ack":
            print("  sB 收到 done-ack —— 闭环完成", flush=True)
            DONE.set()
        elif session == "sB" and text.startswith("pv:"):
            # provider 场景：sB 收带 provider 的信（body 以 pv: 开头）→ 回 ack
            time.sleep(0.1)
            r = exec_callagent(SimpleNamespace(session="sB"), to="sA",
                               message="pv-ack", reply_to="", delay_seconds=0)
            print("  sB 回信 pv-ack:", r.stdout, flush=True)
        elif session == "sA" and text == "pv-ack":
            time.sleep(0.1)
            r = exec_callagent(SimpleNamespace(session="sA"), to="sB",
                               message="pv-done", reply_to="", delay_seconds=0)
            print("  sA 回信 pv-done:", r.stdout, flush=True)
        elif session == "sB" and text == "pv-done":
            print("  sB 收到 pv-done —— provider 闭环完成", flush=True)
            PV_DONE.set()
    except Exception as ex:
        import traceback
        traceback.print_exc()
        print("  [handler 异常] %r" % ex, flush=True)



HOSTS = {}


def main():
    # 清空总线
    from codes.mailbox import mail_path
    mp = mail_path()
    if os.path.exists(mp):
        os.remove(mp)
    for f in os.listdir(_DIR):
        p = os.path.join(_DIR, f)
        if os.path.isfile(p):
            os.remove(p)
    # 起点：sA 发 ping
    r = exec_callagent(SimpleNamespace(session="sA"), to="sB", message="ping",
                       reply_to="", delay_seconds=0)
    print("发起 ping:", r.stdout)
    assert json.loads(r.stdout)["status"] == "sent"
    ping_id = json.loads(r.stdout)["msg_id"]

    # 单宿主多会话：sA+sB 同属一个 manager（真实 web 宿主模型），先注册后启动
    proc_a = FakeProc(handler)
    proc_b = FakeProc(handler)
    proc_a.session, proc_b.session = "sA", "sB"
    proc_map = {"sA": proc_a, "sB": proc_b}
    mgr = FakeManager(proc_map, handler, FAKE_LOCKS)
    HOSTS["sA"] = {"proc": proc_a}
    HOSTS["sB"] = {"proc": proc_b}
    pm = MailPostman(mgr)
    pm.mailbox = Mailbox()   # per-进程独立（同一文件、独立游标）
    pm.start()
    HOSTS["sA"]["pm"] = pm
    HOSTS["sB"]["pm"] = pm
    print("宿主 postman 启动（线程 %s 存活=%s）" % (pm._thread.name, pm._thread.is_alive()))

    # provider 场景：sA 发一封带 provider 的信（校验真实 provider.config）
    r_pv = exec_callagent(SimpleNamespace(session="sA"), to="sB", message="pv:provider 场景",
                          reply_to="", delay_seconds=0, provider="xiaomi")
    print("发起 pv 信:", r_pv.stdout)
    assert json.loads(r_pv.stdout)["status"] == "sent"

    # 等待闭环（≤30s）
    t0 = time.time()
    while (not DONE.is_set() or not PV_DONE.is_set()) and time.time() - t0 < 30:
        time.sleep(0.2)
    # DONE 后留 ≥2 个 watch 轮（SCAN_INTERVAL=1s），确保最后一封信判定 done
    time.sleep(2.5)
    # 停止 postman
    HOSTS["sA"]["pm"].stop()
    if not DONE.is_set() or not PV_DONE.is_set():
        print("!! 闭环超时，事件流:", EVENTS)
        print(json.dumps({"ok": False, "events": EVENTS}, ensure_ascii=False))
        sys.exit(1)
    # 断言：ping/pong/done-ack 三封信均终态 done
    time.sleep(1.0)   # 留出最后一轮收尾
    mb = Mailbox()
    summary = {"events": EVENTS}
    for st in mb.status.values():
        summary.setdefault("mails", []).append(
            {"id": st["send"]["id"], "state": st["state"], "from": st["send"]["from"],
             "to": st["send"]["to"], "body": st["send"]["body"]})
    print("邮件终态:", json.dumps(summary.get("mails", []), ensure_ascii=False))
    done_cnt = sum(1 for m in summary.get("mails", []) if m["state"] == "done")
    print("done 邮件数:", done_cnt, "/", len(summary.get("mails", [])))
    pv_log = getattr(mgr, "provider_log", []) or []
    pv_seen = any(s == "sB" and p == "xiaomi" for s, p, _m in pv_log)
    print("provider 透传记录:", pv_log)
    ok = done_cnt == 6 and pv_seen
    print(json.dumps({"ok": ok, "events": EVENTS, "mails": summary.get("mails", []),
                      "provider_log": pv_log}, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
