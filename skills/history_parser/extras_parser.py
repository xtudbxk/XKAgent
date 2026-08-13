"""history_parser — extras 字段解析工具

解析 messages 表中 extras (JSON) 字段的结构化信息，包括:
  - tool_calls: 助手消息中的工具调用信息
  - tool_call_id: 工具消息中的工具调用 ID 映射
  - compact: 对话压缩标记信息
  - 其他自定义 extras

Usage:
    from skills.history_parser.extras_parser import parse_extras
    result = json.loads(parse_extras("default"))
"""
import json
import os
import sys
import sqlite3

def _resolve_historys_dir() -> str:
    """按 .xkagent、根目录顺序探测 historys/。"""
    for cand in (
        os.path.join(os.getcwd(), ".xkagent", "historys"),
        os.path.join(os.getcwd(), "historys"),
    ):
        if os.path.isdir(cand):
            return cand
    return os.path.join(os.getcwd(), ".xkagent", "historys")


HISTORYS_DIR = _resolve_historys_dir()

# 使用 wal_reader 安全连接（自动处理 WAL + 只读环境）
from skills.history_parser.wal_reader import safe_get_conn as _get_conn

def parse_extras(session: str, *,
                 include_tool_calls: bool = True,
                 output_format: str = "json") -> str:
    """解析 session 中所有消息的 extras 字段。

    参数:
        session: session 名称
        include_tool_calls: 是否包含 tool_calls 详细数据
        output_format: "json" | "markdown" | "text"
    返回:
        按类型组织的解析结果:
          - tool_calls: 从 assistant 消息中提取的工具调用列表
          - tool_results: 从 tool 消息中提取的结果列表（含 tool_call_id 映射）
          - compact_markers: compact 标记信息
          - stats: 统计信息
    """
    try:
        conn = _get_conn(session)
        rows = conn.execute(
            "SELECT id, role, extras, content, created_at FROM messages "
            "WHERE extras IS NOT NULL AND extras != '' "
            "ORDER BY id"
        ).fetchall()
        conn.close()
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"连接失败: {e}"}, ensure_ascii=False)

    # ── 分类解析 ──
    tool_calls = []
    tool_results = []
    compact_markers = []
    other_extras = []

    for r in rows:
        try:
            extras = json.loads(r["extras"])
        except (json.JSONDecodeError, TypeError):
            other_extras.append({
                "id": r["id"],
                "role": r["role"],
                "created_at": r["created_at"],
                "raw_extras": r["extras"],
                "parse_error": True,
            })
            continue

        role = r["role"]

        # assistant: tool_calls
        if role == "assistant" and "tool_calls" in extras and include_tool_calls:
            for tc in extras["tool_calls"]:
                tool_calls.append({
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "tool_call_id": tc.get("id") or tc.get("tool_call_id", ""),
                    "function_name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                })

        # tool: tool_call_id
        elif role == "tool" and "tool_call_id" in extras:
            tool_results.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "tool_call_id": extras["tool_call_id"],
                "content_preview": (r["content"] or "")[:200],
                "name": extras.get("name", ""),
            })

        # compact
        elif role == "compact":
            cutoff = extras.get("cutoff_max_id", 0)
            compact_markers.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "cutoff_max_id": cutoff,
                "summary_preview": (r["content"] or "")[:150],
            })

        # other
        else:
            other_extras.append({
                "id": r["id"],
                "role": role,
                "created_at": r["created_at"],
                "extras_keys": list(extras.keys()),
                "extras_preview": json.dumps(extras, ensure_ascii=False)[:200],
            })

    # ── 统计 ──
    total_with_extras = len(rows)
    stats = {
        "total_messages_with_extras": total_with_extras,
        "tool_calls_count": len(tool_calls),
        "tool_results_count": len(tool_results),
        "compact_markers_count": len(compact_markers),
        "other_extras_count": len(other_extras),
    }

    result = {
        "session": session,
        "stats": stats,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "compact_markers": compact_markers,
        "other_extras": other_extras,
    }

    # ── 格式化输出 ──
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)

    if output_format == "markdown":
        lines = [f"# extras 解析结果 — Session: {session}\n"]
        lines.append(f"## 统计")
        for k, v in stats.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

        if tool_calls:
            lines.append(f"## 工具调用 ({len(tool_calls)})")
            for tc in tool_calls:
                lines.append(f"- [{tc['created_at']}] `{tc['function_name']}` → {tc['arguments'][:100]}")
            lines.append("")

        if tool_results:
            lines.append(f"## 工具结果 ({len(tool_results)})")
            for tr in tool_results:
                lines.append(f"- [{tr['created_at']}] id={tr['tool_call_id']}: {tr['content_preview'][:100]}")
            lines.append("")

        if compact_markers:
            lines.append(f"## 压缩标记 ({len(compact_markers)})")
            for cm in compact_markers:
                lines.append(f"- id={cm['id']} cutoff={cm['cutoff_max_id']}: {cm['summary_preview'][:80]}")
            lines.append("")

        return "\n".join(lines)

    # text
    lines = [f"Session: {session}"]
    lines.append(f"带 extras 的消息数: {total_with_extras}")
    lines.append(f"Tool calls: {len(tool_calls)}, Tool results: {len(tool_results)}, "
                 f"Compact markers: {len(compact_markers)}, Other: {len(other_extras)}")
    if tool_calls:
        lines.append(f"\n工具调用:")
        for tc in tool_calls:
            lines.append(f"  [{tc['created_at']}] {tc['function_name']}")
    if tool_results:
        lines.append(f"\n工具结果:")
        for tr in tool_results:
            lines.append(f"  [{tr['created_at']}] → {tr['content_preview'][:80]}")
    if compact_markers:
        lines.append(f"\n压缩标记:")
        for cm in compact_markers:
            lines.append(f"  id={cm['id']} cutoff={cm['cutoff_max_id']}")
    return "\n".join(lines)

if __name__ == "__main__":
    """命令行调用:
    
    python3 extras_parser.py <session> [--format json|markdown|text]
    """
    import argparse
    parser = argparse.ArgumentParser(description="extras 字段解析")
    parser.add_argument("session", help="session 名称")
    parser.add_argument("--format", "-f", default="json",
                        choices=["json", "markdown", "text"])
    args = parser.parse_args()
    print(parse_extras(args.session, output_format=args.format))
