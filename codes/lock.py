"""XKAgent — mkdir-lease based cross-container lock manager for agent sessions.

跨容器文件锁：用目录原子创建（os.mkdir）实现跨客户端互斥，
替代 flock（flock 在 NFS 上不跨容器互斥，导致多容器同时持有同一 session 锁）。

锁模型
-------
锁 = ``{session}.lockdir/`` 目录的存在与否（NFS 服务器端原子，多容器并发
mkdir 仅一个成功，其余收到 FileExistsError）。

- ``owner.json``: 持有者 metadata（instance_id/pid/thread_id/hostname/locked_at）
- 心跳: daemon 线程每 HEARTBEAT_INTERVAL 秒 touch owner.json（更新 mtime）
- 租约: owner.json 的 mtime 超过 STALE_TIMEOUT 视为 stale，可被 rename 原子接管
- 接管: rename 移走 stale lockdir → 重新 mkdir → 写 owner → 启心跳；
  rename 原子保证多容器并发接管时仅一个成功。

兼容性
-------
API 签名与旧 flock 版完全一致（acquire/release/is_locked/acquire_or_recover/
is_same_process），web.py/repl.py/manager.py/agent.py 调用点无需改动。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from codes import config as _config
from codes import session_registry as _registry


# ═══════════════════════════════════════════════════════════════════
#  Lock semantics (mkdir lease)
# ═══════════════════════════════════════════════════════════════════
#
# 为什么不用 flock？
#   flock 锁关联 open file description，在 NFS 上由客户端本地模拟，
#   不跨容器互斥 → 多容器可同时 acquire 同一 session 锁（实测双持有）。
#   mkdir 是 NFS 服务器端原子操作，多客户端并发仅一个成功（EEXIST）→
#   天然跨容器互斥。
#
# 为什么需要租约（mtime 心跳）？
#   进程崩溃时 lockdir 无法自动消失（mkdir 没有内核自动清理）。
#   持有者通过心跳线程持续 touch owner.json 刷新 mtime；
#   崩溃后心跳停止 → mtime 停止 → 超过 STALE_TIMEOUT 即 stale → 他人接管。
#   mtime 由 NFS 服务器统一维护，跨容器时钟不一致不影响判定
#   （仅需本机 time.time() 与服务器时间粗略同步，35s 超时 vs 10s 心跳（3.5 倍余量）
#   有 18 倍余量，容忍分钟级时钟偏移）。
#
# 为什么 rename 接管？
#   stale 判定后，直接 rmdir 会与"持有者恰好恢复"产生竞态。
#   rename(lockdir → stale.xxx) 原子移走 → 原路径消失 → 再 mkdir 新 lockdir。
#   并发接管时仅 rename 成功者可继续，其余让位 → 无双持。
#
# 为什么 release 防误删？
#   release 删除前校验 owner.instance_id == _INSTANCE_ID；
#   即使不校验，非空目录（含他人新 owner.json）也删不掉（ENOTEMPTY）。
#
# ═══════════════════════════════════════════════════════════════════

# 进程级唯一实例 ID（每次进程启动生成一次），用于锁身份判定
_INSTANCE_ID: str = uuid.uuid4().hex

# 心跳间隔（秒）：持有者 touch owner.json 的频率
HEARTBEAT_INTERVAL = 10

# 租约超时（秒）：owner.json mtime 超过此值视为 stale，可被接管
# （用户确认：35s；10s 心跳 × 3.5 = 35s，3.5 倍余量）
STALE_TIMEOUT = 35

# 心跳线程注册表: {session: {thread, stop, lockdir}}
_heartbeats: dict[str, dict] = {}


# ── 路径 ────────────────────────────────────────────────────────────

def _lockdir(session: str, ensure: bool = False) -> str:
    """锁目录路径：{session}.lockdir/ 存在即持锁。"""
    context = _registry.resolve(session, _config.get_default_workdir())
    if ensure:
        context.ensure()
    return str(context.history_dir / f"{session}.lockdir")


# ── owner.json 读写 ─────────────────────────────────────────────────

def _write_owner(lockdir: str, meta: dict) -> None:
    path = os.path.join(lockdir, "owner.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False))
    # 立即 touch，避免刚创建即被误判 stale
    try:
        os.utime(path)
    except OSError:
        pass


def _read_owner(lockdir: str) -> dict | None:
    path = os.path.join(lockdir, "owner.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        return None


# ── 工具 ────────────────────────────────────────────────────────────

def _thread_alive(tid: int | None) -> bool:
    """返回 tid 对应线程是否仍存活（用于判定同进程残留）。"""
    if tid is None:
        return False
    return any(t.ident == tid for t in threading.enumerate())


def _is_stale(lockdir: str) -> bool:
    """owner.json 的 mtime 是否超过租约超时（可接管）。

    若 owner.json 缺失（mkdir 成功与写 owner 之间存在初始化窗口，或
    创建者崩溃在窗口内），用 lockdir 目录自身的 mtime 兜底判定：
    目录 mtime 新鲜（< STALE_TIMEOUT）→ 视为初始化中，不可接管（busy）；
    目录 mtime 超时 → 创建者已崩溃，可接管。
    """
    owner = os.path.join(lockdir, "owner.json")
    try:
        st = os.stat(owner)
    except OSError:
        try:
            st = os.stat(lockdir)
        except OSError:
            return True
        return (time.time() - st.st_mtime) > STALE_TIMEOUT
    return (time.time() - st.st_mtime) > STALE_TIMEOUT


def _rmtree(path: str) -> None:
    """纯 os 递归删除目录（沙箱禁 shutil；best-effort）。"""
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _steal_lock(session: str) -> bool:
    """接管 stale 锁：rename 原子移走 → 重新 mkdir。

    仅 rename 成功者可继续；其余并发者让位。返回是否取得 lockdir 所有权。
    """
    lockdir = _lockdir(session)
    stale = f"{lockdir}.stale.{uuid.uuid4().hex}"
    try:
        os.rename(lockdir, stale)   # 原子移走
    except FileNotFoundError:
        return False                # 已被他人接管
    except OSError:
        return False
    try:
        os.mkdir(lockdir)           # 重新创建
        return True
    except FileExistsError:
        return False                # 被更快的竞争者抢建
    finally:
        _rmtree(stale)              # 清理移走的 stale 目录


# ── 心跳 ────────────────────────────────────────────────────────────

def _start_heartbeat(session: str, lockdir: str) -> None:
    """启动 daemon 心跳线程：每 HEARTBEAT_INTERVAL 秒 touch owner.json。"""
    stop_ev = threading.Event()

    def _hb() -> None:
        owner = os.path.join(lockdir, "owner.json")
        try:
            while not stop_ev.wait(HEARTBEAT_INTERVAL):
                try:
                    meta = _read_owner(lockdir)
                    if meta is None:
                        break                     # owner.json 被删（被接管）→ 停止
                    tid = meta.get("thread_id")
                    if tid is not None and not _thread_alive(tid):
                        break                     # 持有线程已退出 → 停止心跳（自然 stale）
                    os.utime(owner)               # touch 刷新 mtime
                except FileNotFoundError:
                    break
                except OSError:
                    continue                      # NFS 抖动，下轮重试
        finally:
            # 自停（持有线程死亡/被接管/owner 被删）时清理注册表条目，避免泄漏
            cur = _heartbeats.get(session)
            if cur is not None and cur.get("thread") is threading.current_thread():
                _heartbeats.pop(session, None)

    t = threading.Thread(target=_hb, daemon=True, name=f"lockhb-{session}")
    t.start()
    _heartbeats[session] = {"thread": t, "stop": stop_ev, "lockdir": lockdir}


def _stop_heartbeat(session: str) -> None:
    """停止 session 的心跳线程（幂等）。"""
    hb = _heartbeats.pop(session, None)
    if hb:
        hb["stop"].set()
        try:
            hb["thread"].join(timeout=1.0)
        except Exception:
            pass


# ── 身份判定（与旧版一致，跨容器安全）────────────────────────────

def is_same_process(meta: dict | None) -> bool:
    """锁 metadata 是否属于当前进程（跨容器安全）。

    优先用 instance_id（进程启动时生成的 UUID，全局唯一，
    免疫跨容器 pid namespace 撞车与 hostname 复用）。
    旧格式锁文件无 instance_id 时回退 hostname+pid 双校验。
    """
    if not isinstance(meta, dict):
        return False
    iid = meta.get("instance_id")
    if iid is not None:
        return iid == _INSTANCE_ID
    return (meta.get("hostname") == socket.gethostname()
            and meta.get("pid") == os.getpid())


# ── 公开 API ────────────────────────────────────────────────────────

def acquire(session: str, holder_pid: int, holder_name: str,
            thread_id: int | None = None) -> tuple:
    """获取 session 独占锁（跨容器互斥）。

    Returns
    -------
    tuple
        (True, lock_ctx) 成功；lock_ctx = {"session", "lockdir", "instance_id"}
        (False, error_message) 失败（他人持有）
    """
    lockdir = _lockdir(session, ensure=True)
    meta = {
        "pid": holder_pid,
        "thread_id": thread_id,
        "hostname": socket.gethostname(),
        "instance_id": _INSTANCE_ID,
        "holder": holder_name,
        "locked_at": time.time(),
    }
    for _ in range(2):
        try:
            os.mkdir(lockdir)                 # 原子创建：多容器仅一个成功
        except FileExistsError:
            # 已存在：若 stale 则尝试接管，否则返回 busy
            if _is_stale(lockdir) and _steal_lock(session):
                break
            return False, "Lock busy"
        except OSError as e:
            return False, f"Cannot create lockdir: {e}"
        break
    else:
        return False, "Lock busy"
    # 独占写 owner.json（open 'x' = O_CREAT|O_EXCL，原子）：
    # 防御"mkdir 成功后被并发者抢先接管/写入"的竞态——owner.json 已存在
    # 说明我们不是真正持有者（让位），lockdir 被移走则返回 busy。
    owner = os.path.join(lockdir, "owner.json")
    try:
        with open(owner, "x", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False))
    except (FileExistsError, FileNotFoundError):
        return False, "Lock busy"
    except OSError as e:
        # mkdir 已由本次 acquire 创建；owner 写失败时立即回滚，避免留下需等待
        # STALE_TIMEOUT 才能恢复的孤儿 lockdir。
        try:
            os.unlink(owner)
        except OSError:
            pass
        try:
            os.rmdir(lockdir)
        except OSError:
            pass
        return False, f"Cannot write lock owner: {e}"
    try:
        os.utime(owner)
    except OSError:
        pass
    _start_heartbeat(session, lockdir)
    return True, {"session": session, "lockdir": lockdir,
                  "instance_id": _INSTANCE_ID}


def release(lock_ctx) -> tuple:
    """释放锁（停心跳 + 校验 owner + 删除 lockdir）。

    Parameters
    ----------
    lock_ctx : dict
        acquire() 成功返回的上下文 {"session", "lockdir", "instance_id"}。

    Returns
    -------
    tuple
        (True, "Released") 或 (False, 原因)
    """
    if not isinstance(lock_ctx, dict):
        return False, "Invalid lock context"
    session = lock_ctx.get("session")
    if not session:
        return False, "Invalid lock context"
    _stop_heartbeat(session)
    lockdir = _lockdir(session)
    meta = _read_owner(lockdir)
    if meta is not None and meta.get("instance_id") != _INSTANCE_ID:
        return False, "Not owner (stolen)"
    _rmtree(lockdir)
    return True, "Released"


def is_locked(session: str) -> tuple:
    """检查 session 是否被持有（非阻塞）。

    Returns
    -------
    tuple
        (True, metadata_dict) 被持有
        (False, "Available") 未持有 / stale（可接管）
    """
    lockdir = _lockdir(session)
    if not os.path.exists(lockdir):
        return False, "Available"
    if _is_stale(lockdir):
        return False, "Available"     # stale 可接管，不视为占用
    meta = _read_owner(lockdir)
    if meta is None:
        return True, "Locked (metadata unavailable)"
    return True, meta


def acquire_or_recover(session: str, holder_pid: int, holder_name: str,
                       thread_id: int | None = None) -> tuple:
    """获取锁，并从同进程崩溃残留中恢复（与旧版语义一致）。

    场景：agent 线程 crashed（thread 已退出）但 lockdir 未被清理。
    此时心跳线程也已停止（检测到持有线程死亡）→ mtime 停止。
    若锁 metadata 表明持有者是本进程且原线程已死 → 清理残留并重试。
    """
    ok, info = acquire(session, holder_pid, holder_name, thread_id)
    if ok:
        return ok, info

    locked, meta = is_locked(session)
    if not (locked and isinstance(meta, dict)):
        return ok, info
    if not is_same_process(meta):
        # 其他进程（含跨容器）持有 → 正常冲突，不可接管
        return ok, info

    old_tid = meta.get("thread_id")
    if _thread_alive(old_tid):
        # 原持有线程仍存活 → 真实占用（同进程另一 agent 线程），不接管
        return ok, info

    # 同进程残留：停心跳 + 删除残留 lockdir + 重试
    _stop_heartbeat(session)
    _rmtree(_lockdir(session))
    return acquire(session, holder_pid, holder_name, thread_id)


# ── 退出清理（/exit、/session stop 兜底）────────────────────────────

def cleanup(session: str) -> tuple:
    """退出时清理 session 的 mkdir 锁（幂等，按 session 名，无需 lock_ctx）。

    与 release() 的区别：release 需要 acquire 返回的 lock_ctx（调用方持有）；
    本函数供 /exit、/session stop、stop_agent join 超时等退出路径兜底调用，
    此时可能拿不到 lock_ctx（如线程未退出、进程即将结束）。

    安全性：仅删除 owner.instance_id == _INSTANCE_ID（本进程）的锁；
    他人持有（含跨容器）或 lockdir 不存在 → 不删除，返回 (False, 原因)。
    """
    _stop_heartbeat(session)
    lockdir = _lockdir(session)
    if not os.path.isdir(lockdir):
        return False, "Not locked"
    meta = _read_owner(lockdir)
    if meta is None:
        # 2026-08-13 多进程安全修复: owner.json 缺失/损坏时保守跳过。
        # 场景: 他人进程 acquire 初始化窗口（mkdir 成功、owner.json 未写）
        # 或元数据异常 → 此时删除会误伤他人锁。宁可残留也不误删
        # （残留由 STALE_TIMEOUT 接管机制兜底，与 is_locked 保守语义一致）。
        return False, "No owner metadata (skip)"
    if meta.get("instance_id") != _INSTANCE_ID:
        return False, "Not owner (other process)"
    # 二次校验: 读→删 之间他人可能重建（TOCTOU），删除前再确认仍是本进程
    meta2 = _read_owner(lockdir)
    if meta2 is None or meta2.get("instance_id") != _INSTANCE_ID:
        return False, "Owner changed (skip)"
    _rmtree(lockdir)
    return True, "Cleaned"


def cleanup_all() -> int:
    """清理当前进程持有的所有 session lockdir（进程退出兜底）。

    Returns
    -------
    int
        实际清理的 lockdir 数量（幂等：不存在的自动跳过）。
    """
    n = 0
    for context in _registry.list_contexts():
        lockdir = context.history_dir / f"{context.name}.lockdir"
        if lockdir.is_dir():
            try:
                ok, _ = cleanup(context.name)
                if ok:
                    n += 1
            except Exception:
                pass
    return n
