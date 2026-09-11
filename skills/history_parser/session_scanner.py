# deps: stdlib only
"""history_parser — Session 发现与概览工具
提供列出所有 sessions 及获取单个 session 详细信息的函数。
所有函数返回 JSON 字符串，兼容 pythonrt（统一 Python 运行时）工具调用方式。
Usage:
    from skills.history_parser.session_scanner import list_sessions, get_session_info
    result = json.loads(list_sessions())
"""
import json
import os
import sys
import sqlite3
# ── 路径配置 ──
# 从当前项目目录出发定位 historys/
# 当通过 pythonrt 调用时，cwd 是项目根目录
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
# 由 wal_reader.safe_get_conn 替代
from skills.history_parser.wal_reader import safe_get_conn as _get_conn
def list_sessions() -> str:
    """列出 historys/ 目录下所有 sessions 及其概览信息。
    返回 JSON 数组，每项包含:
      - name: session 名称
      - total_messages: 消息总数
      - time_from: 最早消息时间
      - time_to: 最晚消息时间
      - roles_distribution: 各角色消息数 {role: count}
    """
    if not os.path.isdir(HISTORYS_DIR):
        return json.dumps({"error": f"目录不存在: {HISTORYS_DIR}"}, ensure_ascii=False)
    db_files = sorted(
        f for f in os.listdir(HISTORYS_DIR)
        if f.endswith(".db") and os.path.isfile(os.path.join(HISTORYS_DIR, f))
    )
    sessions = []
    for fname in db_files:
        session_name = fname[:-3]  # 去掉 .db 后缀
        try:
            conn = _get_conn(session_name)
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM messages")
            total = cursor.fetchone()["cnt"]
            cursor = conn.execute(
                "SELECT MIN(created_at) as t_from, MAX(created_at) as t_to FROM messages"
            )
            row = cursor.fetchone()
            t_from = row["t_from"]
            t_to = row["t_to"]
            cursor = conn.execute(
                "SELECT role, COUNT(*) as cnt FROM messages GROUP BY role"
            )
            roles_dist = {r["role"]: r["cnt"] for r in cursor.fetchall()}
            conn.close()
            sessions.append({
                "name": session_name,
                "total_messages": total,
                "time_from": t_from,
                "time_to": t_to,
                "roles_distribution": roles_dist,
            })
        except Exception as e:
            sessions.append({
                "name": session_name,
                "error": str(e),
            })
    return json.dumps(sessions, ensure_ascii=False, indent=2)
def get_session_info(session: str) -> str:
    """获取单个 session 的详细信息。
    参数:
        session: session 名称（不含 .db 后缀）
    返回 JSON 包含:
      - 基本信息: name, total_messages, time_from, time_to
      - 角色分布
      - turn 分布: turn 最大值、平均每轮消息数
      - 前 5 条消息预览
      - 包含 extras 的消息数
      - 紧凑标记信息 (compact)
    """
    try:
        conn = _get_conn(session)
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"连接失败: {e}"}, ensure_ascii=False)
    try:
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM messages")
        total = cursor.fetchone()["cnt"]
        cursor = conn.execute(
            "SELECT MIN(created_at) as t_from, MAX(created_at) as t_to FROM messages"
        )
        row = cursor.fetchone()
        t_from = row["t_from"]
        t_to = row["t_to"]
        cursor = conn.execute(
            "SELECT role, COUNT(*) as cnt FROM messages GROUP BY role"
        )
        roles_dist = {r["role"]: r["cnt"] for r in cursor.fetchall()}
        cursor = conn.execute("SELECT MAX(turn) as max_turn FROM messages")
        max_turn = cursor.fetchone()["max_turn"] or 0
        cursor = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE extras IS NOT NULL AND extras != ''"
        )
        extras_count = cursor.fetchone()["cnt"]
        # 紧凑标记
        cursor = conn.execute(
            "SELECT id, content, extras FROM messages WHERE role='compact' ORDER BY id DESC LIMIT 1"
        )
        compact_row = cursor.fetchone()
        compact_info = None
        if compact_row:
            compact_info = {
                "id": compact_row["id"],
                "summary_preview": (compact_row["content"] or "")[:100],
            }
        # 前 5 条预览
        cursor = conn.execute(
            "SELECT id, role, substr(content, 1, 100) as preview, created_at FROM messages ORDER BY id LIMIT 5"
        )
        previews = [
            {"id": r["id"], "role": r["role"], "preview": r["preview"],
             "created_at": r["created_at"]}
            for r in cursor.fetchall()
        ]
        conn.close()
        info = {
            "name": session,
            "total_messages": total,
            "time_from": t_from,
            "time_to": t_to,
            "roles_distribution": roles_dist,
            "max_turn": max_turn,
            "messages_with_extras": extras_count,
            "compact_marker": compact_info,
            "preview": previews,
        }
        return json.dumps(info, ensure_ascii=False, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": f"查询失败: {e}"}, ensure_ascii=False)
if __name__ == "__main__":
    """命令行调用: python3 session_scanner.py [session_name]
    
    无参数: 列出所有 sessions
    有参数: 获取指定 session 的详细信息
    """
    if len(sys.argv) > 1:
        print(get_session_info(sys.argv[1]))
    else:
        print(list_sessions())
