# -*- coding: utf-8 -*-
"""tests/test_cleanup.py — 空闲会话回收策略单测（v0902.1：容量 LRU + 活跃豁免）。

独立运行：python tests/test_cleanup.py
覆盖：数量内不杀 / 超容量杀最老 / 活跃豁免（turn/tool/llm）/ focus 豁免 /
观察者豁免 / 近期交互豁免 / 回收数上界 / env 开关与独立线程。
"""
import os
import sys
import time
import json
import threading
import queue
from types import SimpleNamespace

_TEST_DIR = "/tmp/cleanup_unittest"

try:
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    _BASE = os.environ.get("XKAGENT_BASE") or os.getcwd()
sys.path.insert(0, _BASE)

from codes.manager import (AgentManager, AGENT_MAX_ALIVE, IDLE_RECLAIM_SECONDS,
                           IDLE_CLEANUP_INTERVAL)

RESULTS = []


def case(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


class FakeAR:
    """模拟 agent_ref 的活跃/观察状态标记。"""
    def __init__(self, turn_active=False, in_tool=False, phase="idle",
                 observing=False, lock_held=True):
        self._turn_active = turn_active
        self.in_tool = in_tool
        self.phase = phase
        self._observing = observing
        self._lock_held = lock_held


class FakeProc:
    def __init__(self, session, last_active, ar=None, status="running"):
        self.session = session
        self.status = status
        self.last_active = last_active
        self.agent_ref = ar if ar is not None else FakeAR()
        self.thread = None
        self.input_queue = queue.Queue()
        self.bus_lock = threading.RLock()
        self.turn_events = {}
        self.subscriptions = set()


def make_mgr(procs, focus=None):
    """构造 AgentManager，注入 fake procs，并 monkeypatch stop_agent 记录回收。"""
    mgr = AgentManager()
    mgr._agents = dict(procs)
    mgr._focus = focus
    mgr._stopped = []
    mgr.stop_agent = lambda session: (mgr._stopped.append(session), True)[1]
    return mgr


NOW = time.time()


def test_within_limit_no_kill():
    """数量 ≤ 上限：即使全部超时不活跃也不回收。"""
    procs = {("s%02d" % i): FakeProc("s%02d" % i, NOW - 9999)
             for i in range(AGENT_MAX_ALIVE)}
    mgr = make_mgr(procs)
    n = mgr.cleanup_idle()
    case("数量内不杀", n == 0 and mgr._stopped == [], str(n))


def test_over_limit_kill_oldest():
    """超容量：回收最老的 surplus 个（LRU），不动较新的。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 5):
        sid = "s%02d" % i
        procs[sid] = FakeProc(sid, NOW - 1000 + i)   # i 越大越新
    mgr = make_mgr(procs)
    n = mgr.cleanup_idle()
    expect = ["s%02d" % i for i in range(5)]          # 最老 5 个
    case("超容量杀最老", n == 5 and mgr._stopped == expect,
         "n=%s stopped=%s" % (n, mgr._stopped))


def test_active_exempt():
    """活跃豁免：turn/tool/llm 三元组任一为真 → 即使最老也不杀。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 3):
        sid = "s%02d" % i
        ar = FakeAR()
        if sid == "s00":            # 最老的正在执行长程 tool（in_tool=True）
            ar = FakeAR(in_tool=True)
        elif sid == "s01":          # 次老正在 turn 进行中
            ar = FakeAR(turn_active=True)
        elif sid == "s02":          # 第三老在 LLM 流式回复
            ar = FakeAR(phase="llm")
        procs[sid] = FakeProc(sid, NOW - 1000 + i, ar=ar)
    mgr = make_mgr(procs)
    n = mgr.cleanup_idle()
    # 需杀 3 个：活跃的 s00/s01/s02 被豁免 → 杀 s03/s04/s05
    expect = ["s%02d" % i for i in range(3, 6)]
    case("turn/tool/llm 豁免", n == 3 and mgr._stopped == expect,
         "n=%s stopped=%s" % (n, mgr._stopped))


def test_focus_exempt():
    """focus 会话永不回收（即使最老最不活跃）。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 2):
        sid = "s%02d" % i
        procs[sid] = FakeProc(sid, NOW - 1000 + i)
    mgr = make_mgr(procs, focus="s00")               # focus = 最老
    n = mgr.cleanup_idle()
    case("focus 豁免", n == 2 and mgr._stopped == ["s01", "s02"],
         "stopped=%s" % mgr._stopped)


def test_observer_exempt():
    """观察者（他进程持锁）豁免。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 2):
        sid = "s%02d" % i
        ar = FakeAR(observing=(sid == "s00"))
        procs[sid] = FakeProc(sid, NOW - 1000 + i, ar=ar)
    mgr = make_mgr(procs)
    n = mgr.cleanup_idle()
    case("观察者豁免", n == 2 and mgr._stopped == ["s01", "s02"],
         "stopped=%s" % mgr._stopped)


def test_recent_activity_exempt():
    """近期有交互（last_active 新鲜）→ 不是候选；候选不足时少杀/不杀。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 2):
        sid = "s%02d" % i
        if i < 2:
            last = NOW - 10            # 最近 10s 有交互（半分钟后仍算新鲜）
        else:
            last = NOW - 1000 + i
        procs[sid] = FakeProc(sid, last)
    mgr = make_mgr(procs)
    # grace 用大值模拟：仅 s00/s01 豁免 → 超量 2 个但候选只有 2 个？s02 是第三老…
    n = mgr.cleanup_idle(timeout=60.0)
    # 候选 = last > 60s 前：s02..s31（30个）；超量 2 → 杀 s02/s03
    case("近期交互豁免", n == 2 and mgr._stopped == ["s02", "s03"],
         "stopped=%s" % mgr._stopped)


def test_insufficient_candidates():
    """候选不足（几乎全部活跃/新鲜）→ 不强制回收（宁超上限）。"""
    procs = {}
    for i in range(AGENT_MAX_ALIVE + 3):
        sid = "s%02d" % i
        if i == 0:
            procs[sid] = FakeProc(sid, NOW - 9999)          # 唯一真不活跃候选
        else:
            procs[sid] = FakeProc(sid, NOW - 5)             # 其余全部近期交互
    mgr = make_mgr(procs)
    n = mgr.cleanup_idle(timeout=60.0)
    # 超量 3 但候选仅 1 → 只杀 1（宁超上限，不强制杀新鲜会话）
    case("候选不足少杀", n == 1 and mgr._stopped == ["s00"],
         "n=%s stopped=%s" % (n, mgr._stopped))


def test_thread_and_env():
    """独立线程：默认启动（daemon alive）；env=off 不启动；stop 可退出。"""
    os.environ.pop("XKAGENT_IDLE_CLEANUP", None)
    mgr = AgentManager()
    mgr._ensure_idle_cleanup_thread()
    alive1 = mgr._idle_cleanup_thread is not None and mgr._idle_cleanup_thread.is_alive()
    case("清理线程默认启动", alive1)
    mgr._idle_cleanup_stop.set()
    if mgr._idle_cleanup_thread is not None:
        mgr._idle_cleanup_thread.join(timeout=2.0)
    case("清理线程可停止", not mgr._idle_cleanup_thread.is_alive())

    os.environ["XKAGENT_IDLE_CLEANUP"] = "off"
    mgr2 = AgentManager()
    mgr2._ensure_idle_cleanup_thread()
    case("env=off 不启动", mgr2._idle_cleanup_thread is None)
    os.environ.pop("XKAGENT_IDLE_CLEANUP", None)

    # max_alive env 覆盖：XKAGENT_MAX_AGENTS=5 → 6 个会话回收 1 个
    os.environ["XKAGENT_MAX_AGENTS"] = "5"
    procs = {("s%02d" % i): FakeProc("s%02d" % i, NOW - 9999 + i) for i in range(6)}
    mgr3 = make_mgr(procs)
    n = mgr3.cleanup_idle()
    case("env 上限 5 生效", n == 1, "n=%s" % n)
    os.environ.pop("XKAGENT_MAX_AGENTS", None)


if __name__ == "__main__":
    test_within_limit_no_kill()
    test_over_limit_kill_oldest()
    test_active_exempt()
    test_focus_exempt()
    test_observer_exempt()
    test_recent_activity_exempt()
    test_insufficient_candidates()
    test_thread_and_env()
    ok_all = all(ok for _, ok, _ in RESULTS)
    print("测试用例: %d 项" % len(RESULTS))
    for name, ok, detail in RESULTS:
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))
    print(json.dumps({"ok": ok_all, "total": len(RESULTS),
                      "failed": [n for n, o, _ in RESULTS if not o]}, ensure_ascii=False))
    sys.exit(0 if ok_all else 1)
