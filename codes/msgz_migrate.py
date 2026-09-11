"""msgz_migrate.py — sqlite → msgz 迁移脚本（纯标准库）

用法: python msgz_migrate.py <sqlite_dir> <msgz_dir> [--force] [session...]
  - 从 sqlite_dir/*.db 读取消息与状态（WAL 安全：immutable 只读连接）
  - 写入 msgz_dir/*.msgz（zlib 压缩）
  - 目标 .msgz 已存在时默认跳过（幂等，防重复迁移）；--force 时先备份再重建
  - 不指定 session 则迁移全部 *.db

退出码: 0 全部成功（含跳过）/ 2 存在失败
"""
import os, sys, json, sqlite3, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from history_msgz import MsgzStore, MSGZ_SUFFIX

# 状态表映射：表名 → state key
STATE_TABLES = ["token_state", "agent_state", "mount_state", "search_state", "image_state"]


def migrate_one(src_db: str, dst_msgz: str, force: bool = False) -> dict:
    """迁移单个 db → msgz，返回统计。

    幂等：目标已存在且非 force → 跳过（skipped=True）。
    force：将现有目标备份为 <dst>.bak.<ts> 后再全量重建。
    落盘失败（sync False）时 stat["ok"]=False（供上层计为失败，防静默误报）。
    """
    stat = {"messages": 0, "states": 0, "ok": True, "skipped": False}
    if os.path.exists(dst_msgz):
        if not force:
            stat["skipped"] = True
            return stat
        backup = dst_msgz + ".bak." + time.strftime("%Y%m%d_%H%M%S")
        os.replace(dst_msgz, backup)
    # WAL 安全读：immutable 跳过 -wal/-shm
    con = sqlite3.connect(f"file:{src_db}?mode=ro&immutable=1", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    store = MsgzStore(dst_msgz, auto_sync=False)
    try:
        rows = con.execute(
            "SELECT id, role, content, extras, turn, created_at FROM messages ORDER BY id"
        ).fetchall()
        for r in rows:
            store.add_message(
                r["role"], r["content"],
                json.loads(r["extras"]) if r["extras"] else None,
                r["turn"], r["created_at"])
        stat["messages"] = len(rows)
        for tbl in STATE_TABLES:
            try:
                cols = [c["name"] for c in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
                if not cols:
                    continue
                row = con.execute(f"SELECT * FROM {tbl}").fetchone()
                if row is not None:
                    store.set_state(tbl, {c: row[c] for c in cols})
                    stat["states"] += 1
            except Exception:
                pass
        if not store.sync():
            stat["ok"] = False   # 落盘失败必须上报（历史：静默误报成功）
    finally:
        con.close()
    return stat


def main(argv):
    force = "--force" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 1
    src_dir, dst_dir = args[0], args[1]
    sessions = args[2:] or [f[:-3] for f in os.listdir(src_dir) if f.endswith(".db")]
    os.makedirs(dst_dir, exist_ok=True)
    total = {"messages": 0, "states": 0, "ok": 0, "fail": 0, "skip": 0}
    for sess in sessions:
        src = os.path.join(src_dir, sess + ".db")
        if not os.path.exists(src):
            print(f"[skip] {sess}: db 不存在")
            continue
        dst = os.path.join(dst_dir, sess + MSGZ_SUFFIX)
        try:
            st = migrate_one(src, dst, force=force)
            if st.get("skipped"):
                total["skip"] += 1
                print(f"[skip] {sess}: 目标已存在（{os.path.basename(dst)}；如需重迁用 --force）")
                continue
            if not st.get("ok"):
                total["fail"] += 1
                print(f"[fail] {sess}: 落盘失败（msgz sync 返回 False）")
                continue
            total["messages"] += st["messages"]
            total["states"] += st["states"]
            total["ok"] += 1
            print(f"[ok] {sess}: {st['messages']} 消息, {st['states']} 状态 -> {os.path.basename(dst)}")
        except Exception as e:
            total["fail"] += 1
            print(f"[fail] {sess}: {e}")
    print(f"\n完成: {total['ok']} 成功 / {total['skip']} 跳过 / {total['fail']} 失败, "
          f"消息 {total['messages']}, 状态 {total['states']}")
    return 0 if total["fail"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
