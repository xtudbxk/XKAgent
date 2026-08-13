"""history_parser — WAL 安全 SQLite 连接读模块

解决 WASM 沙箱只读环境下 WAL 模式 SQLite 数据库无法直接连接的问题。

策略（三层降级）:
  1. 直接 sqlite3.connect() — 正常环境直接使用
  2. 只读 .db + 无 WAL → 改版本标记 + deserialize 到内存
  3. 只读 .db + 有 WAL → 合并 WAL 帧 + 改版本标记 + deserialize

Usage:
    from skills.history_parser.wal_reader import safe_get_conn
    conn = safe_get_conn("test8")
"""
import json
import os
import struct
import sqlite3

def _resolve_historys_dir() -> str:
    """按 .xkagent、根目录顺序探测 historys/。"""
    for cand in (
        os.path.join(os.getcwd(), ".xkagent", "historys"),
        os.path.join(os.getcwd(), "historys"),
    ):
        if os.path.isdir(cand):
            return cand
    # 都不存在时返回首选路径（错误信息更准确）
    return os.path.join(os.getcwd(), ".xkagent", "historys")


HISTORYS_DIR = _resolve_historys_dir()

def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """读取 SQLite varint，返回 (值, 消耗字节数)。"""
    value = 0
    for i in range(9):
        b = data[offset + i]
        value = (value << 7) | (b & 0x7f)
        if not (b & 0x80):
            return value, i + 1
    return value, 9

def _parse_record(data: bytes, offset: int, columns: int) -> tuple[list, int]:
    """解析 SQLite 记录格式，返回 (values列表, 消耗总字节数)。"""
    header_size, n = _read_varint(data, offset)
    total_consumed = n

    serial_types = []
    pos = offset + n
    remaining = header_size - n
    while len(serial_types) < columns and remaining > 0:
        st, sn = _read_varint(data, pos)
        serial_types.append(st)
        pos += sn
        remaining -= sn
        total_consumed += sn

    total_consumed += header_size - (pos - offset)

    values = []
    pos = offset + header_size
    for st in serial_types:
        if st == 0:
            values.append(None)
        elif st == 1:
            val = int.from_bytes(data[pos:pos+1], 'big', signed=True)
            values.append(val)
            pos += 1
        elif st == 2:
            val = int.from_bytes(data[pos:pos+2], 'big', signed=True)
            values.append(val)
            pos += 2
        elif st == 3:
            val = int.from_bytes(data[pos:pos+3], 'big', signed=True)
            if val >= 0x800000:
                val -= 0x1000000
            values.append(val)
            pos += 3
        elif st == 4:
            val = int.from_bytes(data[pos:pos+4], 'big', signed=True)
            values.append(val)
            pos += 4
        elif st == 5:
            val = int.from_bytes(data[pos:pos+6], 'big', signed=True)
            values.append(val)
            pos += 6
        elif st == 6:
            val = int.from_bytes(data[pos:pos+8], 'big', signed=True)
            values.append(val)
            pos += 8
        elif st == 7:
            val = struct.unpack('>d', data[pos:pos+8])[0]
            values.append(val)
            pos += 8
        elif st == 8:
            values.append(0)
        elif st == 9:
            values.append(1)
        elif st >= 12 and st % 2 == 0:
            length = (st - 12) // 2
            values.append(data[pos:pos+length])
            pos += length
        elif st >= 13 and st % 2 == 1:
            length = (st - 13) // 2
            values.append(data[pos:pos+length].decode('utf-8', errors='replace'))
            pos += length
        else:
            values.append(None)

    return values, pos - offset

def _extract_cells_from_page(page_data: bytes, page_size: int) -> list[dict]:
    """从 leaf-table B-tree 页中提取所有 cell (行数据)。

    返回: [{"rowid": int, "values": [col1, col2, ...]}, ...]
    """
    page_type = page_data[0]
    if page_type != 0x0d:  # leaf table page
        return []

    cell_count = struct.unpack_from('>H', page_data, 3)[0]

    if cell_count == 0 or cell_count > 500:
        return []

    cells = []
    for i in range(cell_count):
        cell_off = struct.unpack_from('>H', page_data, 8 + i * 2)[0]
        if cell_off >= page_size or cell_off < 0:
            continue

        payload_len, n1 = _read_varint(page_data, cell_off)
        rowid, n2 = _read_varint(page_data, cell_off + n1)

        payload = page_data[cell_off + n1 + n2:cell_off + n1 + n2 + payload_len]

        try:
            values, _ = _parse_record(payload, 0, 6)
            cells.append({"rowid": rowid, "values": values})
        except Exception:
            pass

    return cells

def _apply_wal(db_bytes: bytearray, wal_bytes: bytes, page_size: int) -> bytearray:
    """将 WAL 帧数据合并到 .db 字节数组中。"""
    wal_header_size = 32
    frame_header_size = 24
    frame_size = frame_header_size + page_size

    num_frames = (len(wal_bytes) - wal_header_size) // frame_size
    if num_frames == 0:
        return db_bytes

    latest_frames = {}
    last_db_size = len(db_bytes) // page_size

    for i in range(num_frames):
        off = wal_header_size + i * frame_size
        pgno = struct.unpack_from('>I', wal_bytes, off)[0]
        db_sz = struct.unpack_from('>I', wal_bytes, off + 4)[0]
        page_data = wal_bytes[off + frame_header_size:off + frame_size]
        latest_frames[pgno] = (page_data, db_sz)
        if db_sz > 0:
            last_db_size = db_sz

    needed_pages = max(max(latest_frames.keys()), last_db_size)
    needed_size = needed_pages * page_size
    if len(db_bytes) < needed_size:
        db_bytes.extend(b'\x00' * (needed_size - len(db_bytes)))

    for pgno, (page_data, db_sz) in latest_frames.items():
        if pgno == 1:
            struct.pack_into('>I', db_bytes, 28, last_db_size)
        else:
            off = (pgno - 1) * page_size
            db_bytes[off:off + page_size] = page_data

    change_counter = struct.unpack_from('>I', db_bytes, 24)[0]
    struct.pack_into('>I', db_bytes, 24, change_counter + 1)

    return db_bytes

# ── 公开 API ──

def safe_get_conn(session: str) -> sqlite3.Connection:
    """获取 session 数据库的安全连接（自动处理 WAL + 只读环境）。

    三层降级策略：
      1. 直接 sqlite3.connect() 尝试
      2. 只读 + 无 WAL → 改版本标记 + deserialize
      3. 只读 + 有 WAL → 合并 WAL 帧 + 改版本标记 + deserialize

    返回的 connection 已设置 row_factory = sqlite3.Row

    参数:
        session: session 名称（不含 .db 后缀）
    返回:
        sqlite3.Connection（带 row_factory）
    抛出:
        FileNotFoundError: session 不存在
    """
    db_path = os.path.join(HISTORYS_DIR, f"{session}.db")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Session '{session}' 不存在 (路径: {db_path})")

    # 策略 0: 只读 URI 连接（WASM/受限沙箱首选，无需写权限，实测可用）
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        pass

    # 策略 1: 直接连接（正常环境，可写）
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("SELECT 1").fetchone()
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return conn
    except Exception:
        pass

    # 策略 2/3: 只读降级方案（deserialize 内存加载）
    with open(db_path, "rb") as f:
        db_bytes = bytearray(f.read())

    page_size = struct.unpack_from('>H', db_bytes, 16)[0]

    wal_path = db_path + "-wal"
    if os.path.isfile(wal_path):
        with open(wal_path, "rb") as f:
            wal_bytes = f.read()
        db_bytes = _apply_wal(db_bytes, wal_bytes, page_size)

    db_bytes[18] = 1
    db_bytes[19] = 1

    conn = sqlite3.connect(":memory:")
    conn.deserialize(bytes(db_bytes))
    conn.row_factory = sqlite3.Row
    return conn

# ── 兼容旧接口 ──

def list_sessions() -> str:
    """列出所有 sessions 及其概览信息（兼容 session_scanner.py 的 list_sessions）。"""
    if not os.path.isdir(HISTORYS_DIR):
        return json.dumps({"error": f"目录不存在: {HISTORYS_DIR}"}, ensure_ascii=False)

    db_files = sorted(
        f for f in os.listdir(HISTORYS_DIR)
        if f.endswith(".db") and os.path.isfile(os.path.join(HISTORYS_DIR, f))
    )

    sessions = []
    for fname in db_files:
        session_name = fname[:-3]
        try:
            conn = safe_get_conn(session_name)
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM messages")
            total = cursor.fetchone()["cnt"]

            cursor = conn.execute(
                "SELECT MIN(created_at) as t_from, MAX(created_at) as t_to FROM messages"
            )
            row = cursor.fetchone()

            cursor = conn.execute(
                "SELECT role, COUNT(*) as cnt FROM messages GROUP BY role"
            )
            roles_dist = {r["role"]: r["cnt"] for r in cursor.fetchall()}

            conn.close()

            sessions.append({
                "name": session_name,
                "total_messages": total,
                "time_from": row["t_from"],
                "time_to": row["t_to"],
                "roles_distribution": roles_dist,
            })
        except Exception as e:
            sessions.append({
                "name": session_name,
                "error": str(e),
            })

    return json.dumps(sessions, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    import sys
    session = sys.argv[1] if len(sys.argv) > 1 else "test8"
    try:
        conn = safe_get_conn(session)
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM messages")
        total = cursor.fetchone()["cnt"]
        print(f"Session '{session}': {total} messages loaded via safe_get_conn")

        cursor = conn.execute(
            "SELECT id, role, turn, created_at FROM messages ORDER BY id DESC LIMIT 3"
        )
        for r in cursor.fetchall():
            print(f"  [{r['id']}] turn={r['turn']} role={r['role']} @ {r['created_at']}")

        conn.close()
    except Exception as e:
        print(f"Error: {e}")
