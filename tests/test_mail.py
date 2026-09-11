# -*- coding: utf-8 -*-
"""tests/test_mail.py — mail v2.3.1 单测（mailbox 文件层 + MailPostman 六场景）。

独立运行：python tests/test_mail.py（XKAGENT_MAIL 指临时文件，不触碰真实总线）。
覆盖：状态机/撕裂半行/死行/文件替换/lease 接管/终态保护；
六场景：活体直投/error→failed→重投/用户在途跳过/观察者等待/两阶段+跨场景接管/孤儿兜底。
"""
import os
import sys
import time
import threading
import json

_TEST_DIR = "/tmp/mail_v231_unittest"


def _clean_dir(p):
    """纯 os 目录清理（沙箱 shutil stub 无 ignore_errors）。"""
    for _root, _dirs, _files in os.walk(p, topdown=False):
        for _fn in _files:
            try:
                os.remove(os.path.join(_root, _fn))
            except OSError:
                pass
        for _d in _dirs:
            try:
                os.rmdir(os.path.join(_root, _d))
            except OSError:
                pass
    try:
        os.rmdir(p)
    except OSError:
        pass


_clean_dir(_TEST_DIR)                     # 目录级清理（可能残留 lockdir 目录）
os.makedirs(_TEST_DIR, exist_ok=True)
os.environ["XKAGENT_MAIL"] = os.path.join(_TEST_DIR, "mail.jsonl")
try:
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    _BASE = os.environ.get("XKAGENT_BASE") or os.getcwd()   # 沙箱直接执行时无 __file__（可用 XKAGENT_BASE 覆盖）
sys.path.insert(0, _BASE)

import codes.lock
from codes.mailbox import Mailbox, MAIL_LOCK_STALE
from codes.manager import MailPostman, MAIL_MAX_RETRY

fake_locks = {}


class FakeSub:
    def __init__(self, session):
        self.session = session
        self.q = []
        self.closed = False


class FakeAgent:
    def __init__(self, lock_held=True, observing=False, turn_active=False):
        self._lock_held = lock_held
        self._observing = observing
        self._turn_active = turn_active


class FakeProc:
    def __init__(self, ar):
        self.status = "running"
        self.agent_ref = ar
        self.ready_event = threading.Event()
        self.ready_event.set()
        self.turn_events = {}
        self.bus_lock = threading.RLock()


class FakeManager:
    def __init__(self, agents=None):
        self._agents = agents or {}
        self.subs = {}
        self.log = []
        self.cleaned = 0
        self._fake_ids = iter(["tid_%d" % i for i in range(1000)])
        self._started = []

    def send_input(self, text, session=None, provider=None, model=None, mail_meta=None, effort=None):
        self.log.append(("send_input", session, text[:16], provider, model))
        tid = next(self._fake_ids)
        proc = self._agents.get(session)
        if proc is not None:
            with proc.bus_lock:
                proc.turn_events[tid] = threading.Event()
        return tid

    def start_agent(self, session, wait_ready=True):
        self.log.append(("start_agent", session))
        self._started.append(session)
        ar = FakeAgent(lock_held=True, observing=False)
        self._agents[session] = FakeProc(ar)
        fake_locks[session] = time.time()
        return True

    def subscribe(self, session, maxsize=4096):
        sub = FakeSub(session)
        self.subs[session] = sub
        return sub

    def unsubscribe(self, sub):
        if sub:
            sub.closed = True
            self.subs.pop(sub.session, None)

    def read_subscription(self, sub, timeout=0.0):
        if sub and sub.q:
            return sub.q.pop(0)
        return None

    def cleanup_idle(self, timeout=300.0):
        self.cleaned += 1

    def stop_agent(self, session, join_timeout=3.0):
        self.log.append(("stop_agent", session))
        self._agents.pop(session, None)


def _fake_is_locked(session):
    if session in fake_locks:
        return True, {"instance_id": "fake-host", "hostname": "x", "pid": 1,
                      "locked_at": fake_locks[session]}
    return False, "Available"


codes.lock.is_locked = _fake_is_locked
_SAME_PROC = {"flag": True}   # 可切换：True=本进程持锁；False=他进程（S7 用）


def _fake_is_same_process(meta):
    return _SAME_PROC["flag"]


codes.lock.is_same_process = _fake_is_same_process


def make_pm(agents=None):
    mgr = FakeManager(agents)
    pm = MailPostman(mgr)
    pm.mailbox = Mailbox()
    return mgr, pm


def round1(pm):
    if not pm.mailbox.acquire_lease():
        raise RuntimeError("lease 失败")
    try:
        pm.mailbox.scan_new()
        pm._starting_step()
        pm._watch_deliveries()
        pm._claim_pending()
    finally:
        pm.mailbox.release_lease()


RESULTS = []


def case(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


def test_mailbox_layers():
    mb1 = Mailbox()
    ok, mid = mb1.add_send("sA", "sB", "你好")
    case("add_send", ok and mid.startswith("m_"), mid)
    mb2 = Mailbox()
    mb2.scan_new()
    case("per进程独立", mb2.get(mid)["state"] == "send")
    assert mb2.acquire_lease()
    mb2.add_status(mid, "delivered", turn_id="t1", host="h")
    mb2.add_status(mid, "failed", retry=1)
    mb2.add_status(mid, "delivered", turn_id="t2", host="h")
    mb2.add_status(mid, "done")
    mb2.release_lease()
    case("状态机 done", mb2.get(mid)["state"] == "done")
    mb2.add_status(mid, "failed", retry=9)
    case("终态保护", mb2.get(mid)["state"] == "done")
    line = json.dumps({"type": "send", "id": "m_partial", "from": "sA", "to": "sB",
                       "reply_to": None, "deliver_at": 0, "created_at": 0,
                       "body": "partial"}, ensure_ascii=False) + "\n"
    half = line[:len(line) // 2]
    with open(mb2.path, "a", encoding="utf-8") as f:
        f.write(half)
    c0 = mb2.cursor
    mb2.scan_new()
    case("半行不消费", mb2.cursor == c0)
    with open(mb2.path, "a", encoding="utf-8") as f:
        f.write(line[len(half):])
    mb2.scan_new()
    case("补全可读", mb2.get("m_partial") is not None
         and mb2.get("m_partial")["state"] == "send")
    with open(mb2.path, "a", encoding="utf-8") as f:
        f.write('{"type":"send","id":"m_slow","from')
    for _ in range(4):
        mb2.scan_new()
    case("死行跳过", mb2.cursor == os.path.getsize(mb2.path))
    os.rename(mb2.path, mb2.path + ".old")
    with open(mb2.path, "w", encoding="utf-8") as f:
        f.write('{"type":"send","id":"m_repl","from":"a","to":"b","reply_to":null,'
                '"deliver_at":0,"created_at":0,"body":"x"}\n')
    mb2.scan_new()
    case("替换重建", mb2.get("m_repl") is not None)
    stale_ts = time.time() - MAIL_LOCK_STALE - 1
    os.makedirs(mb2.lockdir, exist_ok=True)
    os.utime(mb2.lockdir, (stale_ts, stale_ts))
    case("stale 接管", mb2.acquire_lease())
    mb2.release_lease()
    os.makedirs(mb2.lockdir, exist_ok=True)
    case("新鲜锁让位", mb2.acquire_lease() is False)
    os.rmdir(mb2.lockdir)


def test_postman_s1_direct_done():
    fake_locks["sB"] = time.time()
    proc = FakeProc(FakeAgent(lock_held=True))
    mgr, pm = make_pm({"sB": proc})
    ok, mid = pm.mailbox.add_send("sA", "sB", "S1 你好")
    round1(pm)
    st = pm.mailbox.get(mid)
    case("S1 投递", st["state"] == "delivered" and st["last"]["turn_id"] == "tid_0")
    proc.turn_events["tid_0"].set()
    round1(pm)
    case("S1 done", pm.mailbox.get(mid)["state"] == "done")
    fake_locks.pop("sB", None)


def test_postman_s2_error_retry():
    fake_locks["sB"] = time.time()
    proc2 = FakeProc(FakeAgent(lock_held=True))
    mgr2, pm2 = make_pm({"sB": proc2})
    ok, mid2 = pm2.mailbox.add_send("sA", "sB", "S2 任务")
    round1(pm2)
    assert pm2.mailbox.get(mid2)["state"] == "delivered"
    sub = mgr2.subs["sB"]
    sub.q.append({"type": "error", "turn_id": "tid_0", "data": "boom"})
    proc2.turn_events["tid_0"].set()
    round1(pm2)
    st = pm2.mailbox.get(mid2)
    case("S2 error→failed", st["state"] == "failed" and st["last"]["retry"] == 1)
    time.sleep(0.25)
    round1(pm2)
    st = pm2.mailbox.get(mid2)
    case("S2 重投", st["state"] == "delivered" and st["last"]["turn_id"] == "tid_1")
    proc2.turn_events["tid_1"].set()
    round1(pm2)
    case("S2 done", pm2.mailbox.get(mid2)["state"] == "done")
    fake_locks.pop("sB", None)


def test_postman_s3_user_turn_skip():
    fake_locks["sB"] = time.time()
    proc3 = FakeProc(FakeAgent(lock_held=True, turn_active=True))
    mgr3, pm3 = make_pm({"sB": proc3})
    ok, mid3 = pm3.mailbox.add_send("sA", "sB", "S3 任务")
    round1(pm3)
    case("S3 在途跳过", pm3.mailbox.get(mid3)["state"] == "send")
    proc3.agent_ref._turn_active = False
    round1(pm3)
    case("S3 释放投递", pm3.mailbox.get(mid3)["state"] == "delivered")
    fake_locks.pop("sB", None)


def test_postman_s4_observer_wait():
    fake_locks.pop("sB", None)
    proc4 = FakeProc(FakeAgent(lock_held=False, observing=True))
    mgr4, pm4 = make_pm({"sB": proc4})
    ok, mid4 = pm4.mailbox.add_send("sA", "sB", "S4 任务")
    round1(pm4)
    case("S4 观察者等待", pm4.mailbox.get(mid4)["state"] == "send"
         and not any(l[0] == "start_agent" for l in mgr4.log))


def test_postman_s5_two_phase():
    fake_locks.clear()
    mgr5, pm5 = make_pm({})
    ok, mid5 = pm5.mailbox.add_send("sA", "sB2", "S5 任务")
    round1(pm5)
    case("S5 starting", "sB2" in mgr5._started)
    round1(pm5)
    st = pm5.mailbox.get(mid5)
    case("S5 两阶段投递", st["state"] == "delivered")
    tid5 = st["last"]["turn_id"]
    mgr5._agents["sB2"].turn_events[tid5].set()
    round1(pm5)
    case("S5 done", pm5.mailbox.get(mid5)["state"] == "done")


def test_postman_s7_other_process_hold():
    """他进程持锁（is_same_process=False）→ 等待不投递（CROSS_WAIT 未到不 retry）。"""
    _SAME_PROC["flag"] = False
    fake_locks["sB"] = time.time()
    mgr7, pm7 = make_pm({})      # 本宿主无 sB 的 proc
    ok, mid7 = pm7.mailbox.add_send("sA", "sB", "S7 任务")
    round1(pm7)
    st = pm7.mailbox.get(mid7)
    case("S7 他进程等待", st["state"] == "send" and "sB" in pm7._cross_wait)
    round1(pm7)   # 未到 CROSS_WAIT → 仍等待
    case("S7 不重投", pm7.mailbox.get(mid7)["state"] == "send")
    _SAME_PROC["flag"] = True
    fake_locks.pop("sB", None)


def test_postman_s8_retry_cumulative_dead():
    """回归（用户事故）：持续 error → retry 累计到 MAX → dead 终态。
    修复前 _retry_or_dead 从 delivered 行取 retry（无字段）→ 恒=1 → 永不 dead 无限循环。"""
    fake_locks["sB8"] = time.time()
    proc = FakeProc(FakeAgent(lock_held=True))
    mgr, pm = make_pm({"sB8": proc})
    ok, mid = pm.mailbox.add_send("sA", "sB8", "S8 任务")
    round1(pm)
    st0 = pm.mailbox.get(mid)
    assert st0["state"] == "delivered", st0
    tid0 = st0["last"]["turn_id"]   # 动态取（共享总线+前序场景已消耗 fake_ids）
    # 第一次 error → failed(retry=1)
    mgr.subs["sB8"].q.append({"type": "error", "turn_id": tid0, "data": "e1"})
    proc.turn_events[tid0] = threading.Event()
    proc.turn_events[tid0].set()
    round1(pm)
    st = pm.mailbox.get(mid)
    assert st["state"] == "failed" and st["last"].get("retry") == 1, st
    time.sleep(0.25)
    round1(pm)   # 重投 → delivered(tid_1 动态)
    st = pm.mailbox.get(mid)
    assert st["state"] == "delivered", st
    tid1 = st["last"]["turn_id"]
    # 第二次 error → retry 累计 → dead（修复前：又 failed(retry=1) 无限循环）
    mgr.subs["sB8"].q.append({"type": "error", "turn_id": tid1, "data": "e2"})
    proc.turn_events[tid1] = threading.Event()
    proc.turn_events[tid1].set()
    round1(pm)
    st = pm.mailbox.get(mid)
    case("S8 累计 retry→dead", st["state"] == "dead", str(st))
    cand = pm.mailbox.claimable()
    case("S8 dead 后不重投", not any(c["id"] == mid for c in cand))
    fake_locks.pop("sB8", None)


def test_postman_lifecycle():
    """生命周期：start 起线程 / off 禁用 / stop 停线程。"""
    mgr, pm = make_pm({})
    ok_start = pm.start()
    case("生命周期 start", ok_start and pm._thread is not None and pm._thread.is_alive())
    pm.stop()
    case("生命周期 stop", pm._thread is None)
    os.environ["XKAGENT_MAIL"] = "off"
    pm2 = MailPostman(FakeManager({}))
    case("生命周期 off 禁用", pm2.start() is False)
    os.environ["XKAGENT_MAIL"] = os.path.join(_TEST_DIR, "mail.jsonl")


def test_postman_s6_orphan_retry():
    fake_locks.clear()
    mgr6, pm6 = make_pm({})
    ok, mid6 = pm6.mailbox.add_send("sA", "sB3", "S6 任务")
    pm6.mailbox.add_status(mid6, "delivered", turn_id="tid_x", host="h_dead")
    round1(pm6)
    st = pm6.mailbox.get(mid6)
    case("S6 孤儿兜底", st["state"] == "failed" and st["last"]["retry"] == 1)
    cand0 = pm6.mailbox.claimable()
    case("S6 冷却保护", not any(c["id"] == mid6 for c in cand0))
    time.sleep(0.25)
    cand = pm6.mailbox.claimable()
    case("S6 重投候选", any(c["id"] == mid6 for c in cand))


def test_features_deliver_at_priority_compact_chain():
    """二期钩子：deliver_at 定点 / priority 排序 / compact 压实 / chain 回复链。"""
    # --- deliver_at：未来不投，到期可投 ---
    fake_locks["sB"] = time.time()
    proc = FakeProc(FakeAgent(lock_held=True))
    mgr, pm = make_pm({"sB": proc})
    fut = time.time() + 60
    ok1, mid1 = pm.mailbox.add_send("sA", "sB", "未来信", deliver_at=fut)
    case("deliver_at 未到期", pm.mailbox.claimable(now=time.time() + 10)[0]["id"] != mid1
         if pm.mailbox.claimable(now=time.time() + 10) else True)
    case("deliver_at 到期可投", any(c["id"] == mid1
                                    for c in pm.mailbox.claimable(now=fut + 1)))
    # --- priority 排序 ---
    ok2, mid_lo = pm.mailbox.add_send("sA", "sB", "低优先", priority=0)
    ok3, mid_hi = pm.mailbox.add_send("sA", "sB", "高优先", priority=5)
    cands = pm.mailbox.claimable(now=time.time() + 2)
    order = [c["id"] for c in cands if c["id"] in (mid_lo, mid_hi)]
    case("priority 排序", order == [mid_hi, mid_lo], str(order))
    # --- chain 回复链 ---
    ok4, midA = pm.mailbox.add_send("sA", "sB", "A")
    ok5, midB = pm.mailbox.add_send("sB", "sA", "B", reply_to=midA)
    ok6, midC = pm.mailbox.add_send("sA", "sB", "C", reply_to=midB)
    ch = pm.mailbox.chain(midC)
    case("chain 回复链", [c["id"] for c in ch] == [midA, midB, midC], str([c["id"] for c in ch]))
    # --- compact 压实 ---
    with pm.mailbox.acquire_lease() if False else open(os.devnull, "w"):
        pass
    # 制造终态：A done（用独立信）
    ok7, midDone = pm.mailbox.add_send("sA", "sB", "终态信")
    pm.mailbox.add_status(midDone, "delivered", turn_id="tx", host="h")
    pm.mailbox.add_status(midDone, "done")
    assert pm.mailbox.acquire_lease()
    kept = pm.mailbox.compact(MAIL_MAX_RETRY)
    pm.mailbox.release_lease()
    case("compact 保留数", kept > 0)
    # 重建扫描：终态信消失，未终态仍在（mid1/mid_lo/mid_hi 处于 send 态）
    mb_new = Mailbox()
    case("compact 后终态剔除", mb_new.get(midDone) is None)
    case("compact 后未终态保留", mb_new.get(mid1) is not None
         and mb_new.get(mid_hi) is not None)
    fake_locks.pop("sB", None)


def test_mail_provider_extensions():
    """callagent provider 扩展：split 语法 / send 行字段 / 直投透传。"""
    from codes.mailbox import split_mail_provider as _smp
    case("split 空", _smp("") == (None, None) and _smp(None) == (None, None))
    case("split 纯 provider", _smp("my-provider") == ("my-provider", None))
    case("split 复合", _smp("my-provider/gpt-5.4") == ("my-provider", "gpt-5.4"))
    case("split 多斜杠", _smp("a/b/c") == ("a", "b/c"))
    fake_locks["sPX"] = time.time()
    proc = FakeProc(FakeAgent(lock_held=True))
    mgr, pm = make_pm({"sPX": proc})
    ok, mid = pm.mailbox.add_send("sA", "sPX", "带 provider 的信",
                                  provider="my-provider/gpt-5.4")
    case("add_send provider 字段", ok and pm.mailbox.get(mid)["send"].get("provider")
          == "my-provider/gpt-5.4")
    ok2, mid2 = pm.mailbox.add_send("sA", "sPX", "无 provider 的信")
    case("add_send 缺省 None", ok2 and pm.mailbox.get(mid2)["send"].get("provider") is None)
    round1(pm)
    sent = [l for l in mgr.log if l[0] == "send_input"]
    case("直投透传 provider/model",
         any(l[2] == "带 provider 的信" and l[3] == "my-provider" and l[4] == "gpt-5.4"
             for l in sent), str(sent))
    fake_locks.pop("sPX", None)


def test_mail_provider_two_phase():
    """两阶段投递（start_agent 路径）同样透传 provider/model。"""
    fake_locks.clear()
    mgr, pm = make_pm({})
    ok, mid = pm.mailbox.add_send("sA", "sPY", "两阶段带 provider", provider="xiaomi/mimo-v2.5")
    round1(pm)
    case("T2P starting 记 provider", "sPY" in mgr._started
         and pm._starting.get("sPY", {}).get("provider") == "xiaomi")
    round1(pm)
    st = pm.mailbox.get(mid)
    case("T2P 投递", st["state"] == "delivered", str(st))
    sent = [l for l in mgr.log if l[0] == "send_input"]
    case("T2P send_input 透传", any(l[3] == "xiaomi" and l[4] == "mimo-v2.5" for l in sent),
         str(sent))


def test_mail_send_input_item():
    """manager.send_input 带 provider/model → input item 含 _provider/_model；普通消息不带。"""
    from codes.manager import AgentManager
    from types import SimpleNamespace as _SN
    import queue
    mgr = AgentManager()
    proc = _SN(status="running", thread=None,
               input_queue=queue.Queue(), bus_lock=threading.RLock(),
               turn_events={}, last_active=0.0)
    mgr._agents["sQ"] = proc
    tid = mgr.send_input("任务", session="sQ", provider="my-provider", model="gpt-5.4")
    item = proc.input_queue.get_nowait()
    case("send_input item 字段", bool(tid) and item.get("_provider") == "my-provider"
         and item.get("_model") == "gpt-5.4" and item.get("_input") == "任务"
         and item.get("_turn_id") == tid, str(item))
    tid2 = mgr.send_input("普通", session="sQ")
    item2 = proc.input_queue.get_nowait()
    case("send_input 普通无覆盖", bool(tid2) and "_provider" not in item2
         and "_model" not in item2, str(item2))


def test_mail_exec_callagent_provider():
    """exec_callagent provider 校验：合法通过 / 未知拒绝 / 空忽略（mock provider_config）。"""
    from types import SimpleNamespace as _SN
    import codes.tools as _tools
    import codes.provider_config as _pc
    _orig = _pc.get_provider
    _known = {"my-provider", "xiaomi"}

    def _fake_get(n):
        if n in _known:
            return {}
        raise ValueError(f"Unknown provider '{n}'")

    _pc.get_provider = _fake_get
    try:
        r_ok = _tools.exec_callagent(_SN(session="sA"), to="sB", message="p1",
                                     provider="my-provider/gpt-5.4")
        j = json.loads(r_ok.stdout)
        case("exec 合法 provider", j.get("status") == "sent", r_ok.stdout)
        if j.get("status") == "sent":
            st = Mailbox().get(j["msg_id"])
            case("exec 写入 provider", st is not None
                 and st["send"].get("provider") == "my-provider/gpt-5.4",
                 str(st and st["send"]))
        r_bad = _tools.exec_callagent(_SN(session="sA"), to="sB", message="p2", provider="nope/x")
        jb = json.loads(r_bad.stdout)
        case("exec 未知 provider 拒绝", jb.get("status") == "error"
             and "Unknown provider" in jb.get("error", ""), r_bad.stdout)
        r_none = _tools.exec_callagent(_SN(session="sA"), to="sB", message="p3")
        case("exec 空 provider 正常", json.loads(r_none.stdout).get("status") == "sent")
    finally:
        _pc.get_provider = _orig


def test_mail_need_reply():
    """callagent need_reply：true 写入信封 / 缺省 false / mailbox 校验类型。"""
    from types import SimpleNamespace as _SN
    import codes.tools as _tools
    # 1) need_reply=true → send 行写入 true
    r_yes = _tools.exec_callagent(_SN(session="sA"), to="sB", message="nr1", need_reply=True)
    j1 = json.loads(r_yes.stdout)
    case("exec need_reply=true sent", j1.get("status") == "sent", r_yes.stdout)
    if j1.get("status") == "sent":
        st = Mailbox().get(j1["msg_id"])
        case("send 行写入 need_reply=true", st is not None
             and st["send"].get("need_reply") is True, str(st and st["send"]))
    # 2) 缺省 → false
    r_no = _tools.exec_callagent(_SN(session="sA"), to="sB", message="nr2")
    j2 = json.loads(r_no.stdout)
    case("exec 缺省 need_reply sent", j2.get("status") == "sent", r_no.stdout)
    if j2.get("status") == "sent":
        st2 = Mailbox().get(j2["msg_id"])
        case("send 行缺省 need_reply=false", st2 is not None
             and st2["send"].get("need_reply") is False, str(st2 and st2["send"]))
    # 3) 非 bool 拒绝（直连 mailbox 层）
    mb = Mailbox()
    ok_bad, err_bad = mb.add_send("sA", "sB", "nr3", need_reply="yes")
    case("mailbox 非 bool 拒绝", ok_bad is False and "need_reply" in str(err_bad), str((ok_bad, err_bad)))


def test_wrap_mail_instruction_need_reply():
    """_wrap_mail_instruction：need_reply=true 含回信指引 / false 及旧信封仅信封头。"""
    from codes.agent import _wrap_mail_instruction as _w
    meta_t = {"id": "m_x", "from": "sA", "reply_to": None, "need_reply": True}
    s_t = _w("正文1", meta_t)
    case("wrap true 含回信指引", ("期望回复" in s_t) and ("callagent" in s_t)
         and ("reply_to = m_x" in s_t), s_t)
    meta_f = {"id": "m_y", "from": "sA", "reply_to": None, "need_reply": False}
    s_f = _w("正文2", meta_f)
    case("wrap false 无回信指引", ("callagent 工具回信" not in s_f)
         and ("未要求回复" in s_f) and ("正文2" in s_f), s_f)
    # 兼容：无 need_reply 字段（旧信）→ 视作通知型
    meta_old = {"id": "m_z", "from": "sA", "reply_to": None}
    s_o = _w("正文3", meta_old)
    case("wrap 旧信封无字段=通知型", ("未要求回复" in s_o)
         and ("callagent 工具回信" not in s_o) and ("正文3" in s_o), s_o)


def test_mail_apply_turn_override():
    """agent 层 apply_turn_override：设置覆盖并返回旧值（run_forever finally 恢复用）。"""
    from types import SimpleNamespace as _SN
    from codes.agent import apply_turn_override as _ato
    ag = _SN(provider="oldp", model="oldm")
    old = _ato(ag, "newp", None)
    case("override 存旧值", old == ("oldp", "oldm", None))
    case("override provider 且 model 跟随 default", ag.provider == "newp" and ag.model is None)
    ag2 = _SN(provider="oldp", model="oldm")
    _ato(ag2, "newp", "newm")
    case("override 复合双值", ag2.provider == "newp" and ag2.model == "newm")
    ag3 = _SN(provider="oldp", model="oldm")
    _ato(ag3, None, "m")
    case("override 仅 model", ag3.provider == "oldp" and ag3.model == "m")
    ag4 = _SN(provider=None, model=None)
    old4 = _ato(ag4, "", None)
    case("override 空参数", old4 == (None, None, None) and ag4.provider is None and ag4.model is None)


def test_mail_exec_broadcast():
    """exec_callagent to 列表=广播：单信多收件人 / 单 to 兼容 / 广播禁 need_reply / 非法整体拒绝。"""
    from types import SimpleNamespace as _SN
    import codes.tools as _tools
    # 1) list（含重复）→ 单信，send 行 to 去重后为 list
    r = _tools.exec_callagent(_SN(session="sA"), to=["sB", "sC", "sB"], message="bc1")
    j = json.loads(r.stdout)
    case("广播 sent 单 msg_id", j.get("status") == "sent" and j.get("msg_id"), r.stdout)
    if j.get("status") == "sent":
        st = Mailbox().get(j["msg_id"])
        case("广播 send 行 to=list", st is not None and st["send"].get("to") == ["sB", "sC"],
             str(st and st["send"].get("to")))
    # 2) 单 to 兼容（to 保持 str）
    r2 = _tools.exec_callagent(_SN(session="sA"), to="sB", message="bc2")
    j2 = json.loads(r2.stdout)
    case("单 to 兼容 msg_id", j2.get("status") == "sent" and j2.get("msg_id"), r2.stdout)
    if j2.get("status") == "sent":
        st2 = Mailbox().get(j2["msg_id"])
        case("单 to 行保持 str", st2 is not None and st2["send"].get("to") == "sB",
             str(st2 and st2["send"].get("to")))
    # 3) 广播 + need_reply=true → 拒绝（广播=通知型）
    r3 = _tools.exec_callagent(_SN(session="sA"), to=["sB", "sC"], message="bc3", need_reply=True)
    j3 = json.loads(r3.stdout)
    case("广播禁 need_reply", j3.get("status") == "error" and "need_reply" in j3.get("error", ""), r3.stdout)
    # 4) 任意非法名 → 整体拒绝（原子性）
    r4 = _tools.exec_callagent(_SN(session="sA"), to=["sB", "bad name!"], message="bc4")
    j4 = json.loads(r4.stdout)
    case("广播非法名整体拒绝", j4.get("status") == "error" and "bad name!" in j4.get("error", ""), r4.stdout)
    # 5) 空列表拒绝
    r5 = _tools.exec_callagent(_SN(session="sA"), to=[], message="bc5")
    j5 = json.loads(r5.stdout)
    case("广播空列表拒绝", j5.get("status") == "error", r5.stdout)


def test_mail_broadcast_lifecycle():
    """广播 per-to 状态机：逐收件人投递/done / 部分失败聚合 dead / per-to 重投 / in_flight per-to。"""
    mb = Mailbox()
    ok, mid = mb.add_send("sA", ["sB", "sC"], "msg-bc")
    case("广播 add_send ok", ok and bool(mid), str((ok, mid)))
    st = mb.get(mid)
    case("广播初始 send", st is not None and st["state"] == "send"
         and st["send"]["to"] == ["sB", "sC"], str(st and st.get("state")))
    cand = sorted(c["_to"] for c in mb.claimable() if c["id"] == mid)
    case("广播 claimable 双候选", cand == ["sB", "sC"], str(cand))
    # 部分投递：sB delivered → 聚合 delivered；sC 未投
    mb.add_status(mid, "delivered", to="sB", turn_id="t1", host="h")
    st = mb.get(mid)
    case("广播部分投递=delivered", st["state"] == "delivered", str(st.get("state")))
    case("broadcast in_flight(sB)", mb.in_flight("sB") is True)
    case("broadcast in_flight(sC) 未投", mb.in_flight("sC") is False)
    # 全 done 聚合
    mb.add_status(mid, "done", to="sB")
    mb.add_status(mid, "delivered", to="sC", turn_id="t2", host="h")
    mb.add_status(mid, "done", to="sC")
    st = mb.get(mid)
    case("广播全 done 聚合", st["state"] == "done", str(st.get("state")))
    case("广播 done 后无候选", all(c["id"] != mid for c in mb.claimable()), "claimable 仍有广播候选")
    # 部分失败：sB done + sC 失败耗尽 → dead 聚合
    ok2, mid2 = mb.add_send("sA", ["sB", "sC"], "msg-bc2")
    mb.add_status(mid2, "delivered", to="sB", turn_id="t1", host="h")
    mb.add_status(mid2, "done", to="sB")
    mb.add_status(mid2, "delivered", to="sC", turn_id="t2", host="h")
    mb.add_status(mid2, "failed", to="sC", retry=1)
    mb.add_status(mid2, "dead", to="sC", retry=2)
    st2 = mb.get(mid2)
    case("广播部分失败聚合 dead", st2["state"] == "dead", str(st2.get("state")))
    # per-to 重投：sB done + sC failed(1) → claimable 仅含 sC
    ok3, mid3 = mb.add_send("sA", ["sB", "sC"], "msg-bc3")
    mb.add_status(mid3, "delivered", to="sB", turn_id="t1", host="h")
    mb.add_status(mid3, "done", to="sB")
    mb.add_status(mid3, "delivered", to="sC", turn_id="t2", host="h")
    mb.add_status(mid3, "failed", to="sC", retry=1)
    cand3 = [c["_to"] for c in mb.claimable(now=time.time() + 1) if c["id"] == mid3]  # now 快进过 cooldown
    case("广播重投仅失败收件人", cand3 == ["sC"], str(cand3))


def test_wrap_mail_instruction_broadcast():
    """_wrap_mail_instruction 广播信封：recipients>1 → 展示全体收件人。"""
    from codes.agent import _wrap_mail_instruction as _w
    meta = {"id": "m_bc", "from": "sA", "reply_to": None, "need_reply": False,
            "recipients": ["sB", "sC", "sD"]}
    s = _w("正文bc", meta)
    case("wrap 广播展示全体", ("📡" in s) and ("sB" in s) and ("sC" in s) and ("sD" in s), s)
    meta2 = {"id": "m_x", "from": "sA", "reply_to": None, "need_reply": False}
    s2 = _w("正文x", meta2)
    case("wrap 单收件人无广播段", "📡" not in s2, s2)


def test_mail_effort_split_and_send():
    """manager _split_mail_effort 拆分 + send_input effort 透传 _effort。"""
    from codes.manager import _split_mail_effort as _sme
    case("effort 拆出", _sme("gpt-5.4:max") == ("gpt-5.4", "max"), repr(_sme("gpt-5.4:max")))
    case("effort 无后缀", _sme("gpt-5.4") == ("gpt-5.4", None), repr(_sme("gpt-5.4")))
    case("effort 空值", _sme(None) == (None, None), repr(_sme(None)))
    case("effort 冒号后缀", _sme("m:low") == ("m", "low"), repr(_sme("m:low")))
    # send_input effort 透传
    from codes.manager import AgentManager
    from types import SimpleNamespace as _SN
    import queue
    mgr = AgentManager()
    proc = _SN(status="running", thread=None,
               input_queue=queue.Queue(), bus_lock=threading.RLock(),
               turn_events={}, last_active=0.0)
    mgr._agents["sQ"] = proc
    tid = mgr.send_input("任务", session="sQ", provider="my-provider",
                         model="gpt-5.4", effort="max")
    item = proc.input_queue.get_nowait()
    case("send_input effort 透传", bool(tid) and item.get("_effort") == "max"
         and item.get("_model") == "gpt-5.4", str(item))
    tid2 = mgr.send_input("普通", session="sQ")
    item2 = proc.input_queue.get_nowait()
    case("send_input 无 effort 不带", bool(tid2) and "_effort" not in item2, str(item2))


def test_mail_apply_turn_override_effort():
    """apply_turn_override effort 扩展：三元组返回 + _cmd_effort 临时设置/恢复。"""
    from types import SimpleNamespace as _SN
    from codes.agent import apply_turn_override as _ato
    ag = _SN(provider="oldp", model="oldm", _cmd_effort=None)
    old = _ato(ag, "newp", "gpt-5.4", "max")
    case("override effort 设置", ag._cmd_effort == "max" and old == ("oldp", "oldm", None),
         str((ag._cmd_effort, old)))
    ag.provider, ag.model, ag._cmd_effort = old   # 模拟 run_forever finally 恢复
    case("effort 恢复三元组", ag.provider == "oldp" and ag.model == "oldm" and ag._cmd_effort is None,
         str((ag.provider, ag.model, ag._cmd_effort)))
    ag2 = _SN(provider="p", model="m", _cmd_effort="high")
    old2 = _ato(ag2, None, "gpt-5.4")       # 无 effort → 不清除已有 _cmd_effort
    case("无 effort 保留原 eff", ag2._cmd_effort == "high" and old2[2] == "high",
         str((ag2._cmd_effort, old2)))


def test_postman_retry_to_passthrough():
    """回归（真实总线广播实测暴露）：投递失败路径 _retry_or_dead 必须透传收件人 to。

    修复前 _try_direct/_try_fresh/_starting_step 共 7 处调用漏传 to →
    failed 行无 to 字段、per-to 重试计数走整信级（广播多收件人时串扰/误判）。
    场景 A 覆盖 _try_direct 直投失败；场景 B 覆盖 _try_fresh 启动失败。
    """
    # 场景 A：_try_direct send_input 失败（活体会话直投失败）
    fake_locks["sB9A"] = time.time()
    procA = FakeProc(FakeAgent(lock_held=True, turn_active=False))
    mgrA, pmA = make_pm({"sB9A": procA})
    mgrA.send_input = lambda *a, **k: None          # 注入投递失败
    okA, midA = pmA.mailbox.add_send("sA", "sB9A", "S9A 任务")
    round1(pmA)
    stA = pmA.mailbox.get(midA)
    case("S9A 直投失败 failed", stA["state"] == "failed", str(stA.get("state")))
    case("S9A failed 行带 to", stA["last"].get("to") == "sB9A", str(stA.get("last")))
    case("S9A retry=1", stA["last"].get("retry") == 1, str(stA.get("last")))
    fake_locks.pop("sB9A", None)

    # 场景 B：_try_fresh start_agent 失败（无锁无 proc → 启动失败）
    fake_locks.pop("sB9B", None)
    mgrB, pmB = make_pm({})
    mgrB.start_agent = lambda *a, **k: False        # 注入启动失败
    okB, midB = pmB.mailbox.add_send("sA", "sB9B", "S9B 任务")
    round1(pmB)
    stB = pmB.mailbox.get(midB)
    case("S9B 启动失败 failed", stB["state"] == "failed", str(stB.get("state")))
    case("S9B failed 行带 to", stB["last"].get("to") == "sB9B", str(stB.get("last")))
    fake_locks.pop("sB9B", None)


def test_mail_cmd_cancel_perto():
    """/mail cancel 回归：per_to 场景必须写带 to 的 rejected + 读回确认。

    修复前单收件人取消不带 to → 整体迁移漏改 per_to → claimable 仍重投（取消无效）。
    另覆盖：广播 A 在途 B 未投（混合取消）；XKAGENT_MAIL=off 禁用提示。
    """
    from codes.commands import cmd_mail, CommandContext
    ctx = CommandContext(get_session=lambda: "t_cmdsender")
    mb = Mailbox()

    # 场景 A：单收件人 failed（per_to 已建立）→ cancel → per_to rejected + 无重投候选
    ok1, midA = mb.add_send("t_s", "t_cmdp1", "失败重投信")
    mb.add_status(midA, "failed", retry=1, to="t_cmdp1")
    out = cmd_mail(None, "cancel " + midA, ctx)
    case("perto cancel 返回成功", "已取消" in out, out)
    mbA = Mailbox()
    stA = mbA.get(midA)
    ptA = ((stA or {}).get("per_to") or {}).get("t_cmdp1") or {}
    case("perto cancel per_to rejected", ptA.get("state") == "rejected", str(ptA))
    cands = mbA.claimable(time.time(), 2, 0)
    case("perto cancel 后无重投候选", not any(c["id"] == midA for c in cands), str(cands))
    case("perto cancel 聚合终态", (stA or {}).get("state") in ("rejected", "dead"),
         str((stA or {}).get("state")))

    # 场景 B：广播 A=delivered（在途）B=send → cancel 仅 B；A 保持 delivered
    ok2, midB = mb.add_send("t_s", ["t_cmdp2", "t_cmdp3"], "广播混合取消")
    mb.add_status(midB, "delivered", to="t_cmdp2", turn_id="tx", host="h")
    out = cmd_mail(None, "cancel " + midB, ctx)
    case("broadcast 混合 cancel 提示在途", "在途不可取消" in out, out)
    mbB = Mailbox()
    stB = mbB.get(midB)
    perB = (stB or {}).get("per_to") or {}
    case("broadcast A 保持 delivered", (perB.get("t_cmdp2") or {}).get("state") == "delivered",
         str(perB.get("t_cmdp2")))
    case("broadcast B rejected", (perB.get("t_cmdp3") or {}).get("state") == "rejected",
         str(perB.get("t_cmdp3")))
    candsB = mbB.claimable(time.time(), 2, 0)
    case("broadcast 混合 cancel B 无候选", not any(c["id"] == midB for c in candsB),
         str(candsB))

    # 场景 C：XKAGENT_MAIL=off → send 禁用提示（校验在 Mailbox 构造前，不触真实总线）
    old_env = os.environ.get("XKAGENT_MAIL", "")
    os.environ["XKAGENT_MAIL"] = "off"
    try:
        out = cmd_mail(None, "send t_cmd_x 禁用测试", ctx)
        case("cmd send off 禁用提示", "已禁用" in out, out)
    finally:
        os.environ["XKAGENT_MAIL"] = old_env



def test_mail_cmd_layers():
    """/mail 命令层：list/get/pending/cancel/send（XKAGENT_MAIL 已指向临时文件，不触碰真实总线）。

    覆盖：send 正常/校验拒绝（非法名、空消息、广播禁 need-reply、--delay 定时）、
    list 过滤、get 详情、pending、cancel（send 态成功/终态拒绝/在途拒绝/广播 per-to）。
    """
    import re as _re
    from codes.commands import cmd_mail, CommandContext
    ctx = CommandContext(get_session=lambda: "t_cmdsender")

    # ── send 正常路径 ──
    out = cmd_mail(None, "send t_cmd_a 你好 测试消息", ctx)
    case("cmd send 返回已发送", "已发送" in out, out)
    m = _re.search(r"(m_[A-Za-z0-9_-]+)", out)
    mid = m.group(1) if m else ""
    case("cmd send 提取 msg_id", bool(mid), mid)
    mb = Mailbox()
    st = mb.get(mid) if mid else None
    case("cmd send 落库 send 态", bool(st) and st["state"] == "send",
         str((st or {}).get("state")))
    case("cmd send from=当前会话", bool(st) and st["send"].get("from") == "t_cmdsender",
         str((st or {}).get("send", {}).get("from")))
    case("cmd send to 正确", bool(st) and st["send"].get("to") == "t_cmd_a",
         str((st or {}).get("send", {}).get("to")))
    case("cmd send body 正确", bool(st) and st["send"].get("body") == "你好 测试消息",
         str((st or {}).get("send", {}).get("body")))

    # ── send 校验拒绝 ──
    out = cmd_mail(None, "send bad!!name 消息", ctx)
    case("cmd send 非法名拒绝", "非法" in out, out)
    mb2 = Mailbox()
    bad_found = any(v["send"].get("to") == "bad!!name" for v in mb2.status.values())
    case("cmd send 非法名未落库", not bad_found, str(bad_found))
    out = cmd_mail(None, "send t_cmd_a", ctx)
    case("cmd send 空消息拒绝", "message 为空" in out, out)
    out = cmd_mail(None, "send t_cmd_c,t_cmd_d 广播消息 --need-reply", ctx)
    case("cmd send 广播禁 need-reply", "不支持 --need-reply" in out, out)

    # ── send --delay 定时 ──
    out = cmd_mail(None, "send t_cmd_a 定时消息 --delay 60", ctx)
    m2 = _re.search(r"(m_[A-Za-z0-9_-]+)", out)
    mid_d = m2.group(1) if m2 else ""
    mb3 = Mailbox()
    st_d = mb3.get(mid_d) if mid_d else None
    da = (st_d or {}).get("send", {}).get("deliver_at") if st_d else None
    case("cmd send --delay 60 定时", bool(da) and abs(da - (time.time() + 60)) < 3,
         f"deliver_at={da} now={time.time():.1f}")

    # ── list / get / pending ──
    out = cmd_mail(None, "list", ctx)
    case("cmd list 含最近邮件", mid[-12:] in out, out[:160])
    out = cmd_mail(None, "list --state send", ctx)
    case("cmd list --state send 命中", mid[-12:] in out, out[:160])
    out = cmd_mail(None, "list --state done", ctx)
    case("cmd list --state done 不命中", mid[-12:] not in out, out[:160])
    out = cmd_mail(None, "get " + mid, ctx)
    case("cmd get 详情含 id", mid in out, out[:160])
    case("cmd get 详情含 body", "测试消息" in out, out[:200])
    out = cmd_mail(None, "pending", ctx)
    case("cmd pending 含定时信", mid_d[-12:] in out, out[:200])

    # ── cancel：send 态成功 → 终态拒绝 ──
    out = cmd_mail(None, "cancel " + mid, ctx)
    case("cmd cancel send 态成功", "已取消" in out, out)
    mb4 = Mailbox()
    st4 = mb4.get(mid)
    pt4 = ((st4 or {}).get("per_to") or {}).get("t_cmd_a") or {}
    case("cmd cancel 后 per_to rejected", pt4.get("state") == "rejected",
         str(pt4))
    case("cmd cancel 后聚合为终态", (st4 or {}).get("state") in ("rejected", "dead"),
         str((st4 or {}).get("state")))
    out = cmd_mail(None, "cancel " + mid, ctx)
    case("cmd cancel 终态拒绝", "终态" in out, out)

    # ── cancel：delivered 在途不可取消 ──
    ok2, mid_in = mb4.add_send("t_s", "t_cmd_b", "在途信")
    mb4.add_status(mid_in, "delivered", turn_id="tx", host="h")
    out = cmd_mail(None, "cancel " + mid_in, ctx)
    case("cmd cancel 在途拒绝", "已投递在途" in out, out)

    # ── cancel：done 终态拒绝 ──
    ok3, mid_dn = mb4.add_send("t_s", "t_cmd_b", "终态信")
    mb4.add_status(mid_dn, "delivered", turn_id="tx2", host="h")
    mb4.add_status(mid_dn, "done")
    out = cmd_mail(None, "cancel " + mid_dn, ctx)
    case("cmd cancel done 终态拒绝", "终态" in out, out)

    # ── 广播：send 正常 + 逐收件人 cancel ──
    out = cmd_mail(None, "send t_cmd_c,t_cmd_d 广播消息", ctx)
    m3 = _re.search(r"(m_[A-Za-z0-9_-]+)", out)
    mid_b = m3.group(1) if m3 else ""
    mb5 = Mailbox()
    st_b = mb5.get(mid_b) if mid_b else None
    case("cmd send 广播 to=list", bool(st_b) and isinstance(st_b["send"].get("to"), list),
         str((st_b or {}).get("send", {}).get("to")))
    out = cmd_mail(None, "cancel " + mid_b, ctx)
    mb6 = Mailbox()
    st_b2 = mb6.get(mid_b)
    per = (st_b2 or {}).get("per_to") or {}
    case("cmd cancel 广播 per-to 均 rejected",
         all((per.get(r) or {}).get("state") == "rejected"
             for r in ("t_cmd_c", "t_cmd_d")), str(per))

    # ── 未知子命令 ──
    out = cmd_mail(None, "frobnicate x", ctx)
    case("cmd mail 未知子命令提示", "未知子命令" in out, out)



if __name__ == "__main__":
    test_mailbox_layers()
    test_postman_s1_direct_done()
    test_postman_s2_error_retry()
    test_postman_s3_user_turn_skip()
    test_postman_s4_observer_wait()
    test_postman_s5_two_phase()
    test_postman_s6_orphan_retry()
    test_postman_s7_other_process_hold()
    test_postman_s8_retry_cumulative_dead()
    test_postman_retry_to_passthrough()
    test_mail_cmd_layers()
    test_mail_cmd_cancel_perto()
    test_postman_lifecycle()
    test_features_deliver_at_priority_compact_chain()
    test_mail_provider_extensions()
    test_mail_provider_two_phase()
    test_mail_send_input_item()
    test_mail_exec_callagent_provider()
    test_mail_apply_turn_override()
    test_mail_need_reply()
    test_wrap_mail_instruction_need_reply()
    test_mail_exec_broadcast()
    test_mail_broadcast_lifecycle()
    test_wrap_mail_instruction_broadcast()
    test_mail_effort_split_and_send()
    test_mail_apply_turn_override_effort()
    ok_all = all(ok for _, ok, _ in RESULTS)
    print("测试用例: %d 项" % len(RESULTS))
    for name, ok, detail in RESULTS:
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))
    print(json.dumps({"ok": ok_all, "total": len(RESULTS),
                      "failed": [n for n, o, _ in RESULTS if not o]}, ensure_ascii=False))
    sys.exit(0 if ok_all else 1)
