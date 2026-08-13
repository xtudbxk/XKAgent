"""history_parser — 核心消息提取引擎（7 种提取模式）

提供 7 种提取模式函数，支持 JSON / Markdown / Text 三种输出格式。
所有函数返回 JSON 字符串，兼容 pythonrt（统一运行时）调用。

Usage:
    from skills.history_parser.message_extractor import dump_messages, filter_by_keyword, ...
    result = json.loads(dump_messages("default", output_format="json"))
"""
import json
import os
import re
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
    # 都不存在时返回首选路径（错误信息更准确）
    return os.path.join(os.getcwd(), ".xkagent", "historys")


HISTORYS_DIR = _resolve_historys_dir()

# ── 内部工具 ──

# 使用 wal_reader 安全连接（自动处理 WAL + 只读环境）
from skills.history_parser.wal_reader import safe_get_conn as _get_conn

def _rows_to_list(rows) -> list[dict]:
    """将 sqlite3.Row 列表转为普通 dict 列表。"""
    result = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if d.get("extras"):
            try:
                d["extras"] = json.loads(d["extras"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result

def _format_output(data: list | dict, fmt: str, title: str = "") -> str:
    """格式化输出: json / markdown / text。"""
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)

    if fmt == "markdown":
        lines = []
        if title:
            lines.append(f"# {title}\n")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    role = item.get("role", "?")
                    created = item.get("created_at", "")
                    content = (item.get("content", "") or "")[:500]
                    lines.append(f"## [{created}] — {role}")
                    lines.append("")
                    lines.append(content)
                    lines.append("")
                else:
                    lines.append(str(item))
        elif isinstance(data, dict):
            for k, v in data.items():
                lines.append(f"- **{k}**: {v}")
        return "\n".join(lines)

    # text
    lines = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                role = item.get("role", "?")
                created = item.get("created_at", "")
                content = (item.get("content", "") or "")[:500]
                lines.append(f"[{created}] {role}: {content}")
            else:
                lines.append(str(item))
    elif isinstance(data, dict):
        for k, v in data.items():
            lines.append(f"{k}: {v}")
    return "\n".join(lines)

def _get_messages_ordered(conn) -> list:
    """获取按 id 排序的所有消息（排除 git 角色）。"""
    rows = conn.execute(
        "SELECT * FROM messages WHERE role != 'git' ORDER BY id"
    ).fetchall()
    return _rows_to_list(rows)

# ── 模式 A: raw_dump ──

def dump_messages(session: str, *, output_format: str = "json",
                  limit: int = None, offset: int = 0) -> str:
    """原始消息转储，按时间顺序输出。

    参数:
        session: session 名称
        output_format: "json" | "markdown" | "text"
        limit: 最多返回条数（默认不限制）
        offset: 偏移量（默认 0）
    """
    try:
        conn = _get_conn(session)
        query = "SELECT * FROM messages WHERE role != 'git' ORDER BY id"
        params = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        if offset:
            query += " OFFSET ?"
            params.append(offset)
        rows = conn.execute(query, params).fetchall()
        conn.close()
        data = _rows_to_list(rows)
        return _format_output(data, output_format, f"Session: {session}")
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 B: by_keyword ──

def filter_by_keyword(session: str, keyword: str, *,
                      regex: bool = False, case_sensitive: bool = False,
                      role: str = None,
                      output_format: str = "json",
                      limit: int = None) -> str:
    """按关键词或正则表达式筛选消息。

    参数:
        session: session 名称
        keyword: 关键词或正则表达式
        regex: 是否作为正则表达式处理
        case_sensitive: 是否大小写敏感
        role: 限制角色 (user/assistant/tool)
        output_format: "json" | "markdown" | "text"
        limit: 最多返回条数
    """
    try:
        conn = _get_conn(session)
        messages = _get_messages_ordered(conn)
        conn.close()

        # 过滤
        flags = 0 if case_sensitive else re.IGNORECASE
        if regex:
            pattern = re.compile(keyword, flags)
            match_fn = lambda c: bool(pattern.search(c or ""))
        else:
            def match_fn(c):
                c = c or ""
                if not case_sensitive:
                    return keyword.lower() in c.lower()
                return keyword in c

        results = []
        for msg in messages:
            if role and msg["role"] != role:
                continue
            if match_fn(msg.get("content", "")):
                results.append(msg)
            if limit and len(results) >= limit:
                break

        return _format_output(
            results, output_format,
            f"Session: {session} | 关键词: {keyword}"
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 C: by_time ──

def filter_by_time(session: str, *,
                   start: str = None, end: str = None,
                   role: str = None,
                   output_format: str = "json") -> str:
    """按时间范围筛选消息。

    参数:
        session: session 名称
        start: 起始时间 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"
        end: 结束时间 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"
        role: 限制角色
        output_format: "json" | "markdown" | "text"
    """
    try:
        conn = _get_conn(session)
        conditions = ["role != 'git'"]
        params = []

        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at <= ?")
            params.append(end)
        if role:
            conditions.append("role = ?")
            params.append(role)

        query = f"SELECT * FROM messages WHERE {' AND '.join(conditions)} ORDER BY id"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        data = _rows_to_list(rows)
        return _format_output(data, output_format, f"Session: {session} | 时间范围")
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 D: by_role ──

def filter_by_role(session: str, role: str, *,
                   output_format: str = "json",
                   limit: int = None) -> str:
    """按角色筛选消息。

    参数:
        session: session 名称
        role: 角色 (user/assistant/tool/compact)
        output_format: "json" | "markdown" | "text"
        limit: 最多返回条数
    """
    valid_roles = {"user", "assistant", "tool", "compact", "git"}
    if role not in valid_roles:
        return json.dumps({"error": f"无效角色 '{role}'，有效值: {valid_roles}"},
                          ensure_ascii=False)
    try:
        conn = _get_conn(session)
        if limit:
            rows = conn.execute(
                "SELECT * FROM messages WHERE role = ? ORDER BY id LIMIT ?",
                (role, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE role = ? ORDER BY id",
                (role,)
            ).fetchall()
        conn.close()
        data = _rows_to_list(rows)
        return _format_output(data, output_format, f"Session: {session} | 角色: {role}")
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 E: extract_code ──

_CODE_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n(.*?)```", re.DOTALL
)

def extract_code_blocks(session: str, *,
                        language: str = None,
                        output_format: str = "json",
                        limit: int = None) -> str:
    """提取消息内容中的所有代码块。

    参数:
        session: session 名称
        language: 指定语言过滤（如 "python", "json"），不指定则提取全部
        output_format: "json" | "markdown" | "text"
    返回:
        每项包含 id, role, language, code, context(代码所在消息的前缀)
    """
    try:
        conn = _get_conn(session)
        messages = _get_messages_ordered(conn)
        conn.close()

        results = []
        for msg in messages:
            content = msg.get("content", "") or ""
            for match in _CODE_BLOCK_RE.finditer(content):
                lang = match.group(1).strip() or "text"
                code = match.group(2).strip()
                if language and lang.lower() != language.lower():
                    continue
                # context: 代码块前面的文本（最多 200 字）
                before_code = content[:match.start()].strip()
                context = before_code[-200:] if len(before_code) > 200 else before_code
                results.append({
                    "id": msg["id"],
                    "role": msg["role"],
                    "created_at": msg.get("created_at"),
                    "language": lang,
                    "code": code,
                    "context": context,
                })
                if limit is not None and len(results) >= limit:
                    break

        return _format_output(
            results, output_format,
            f"Session: {session} | 代码块提取"
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 F: extract_json ──

_JSON_OBJECT_RE = re.compile(
    r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL
)

def extract_json_objects(session: str, *,
                         output_format: str = "json",
                         limit: int = None) -> str:
    """提取消息内容中的 JSON 对象。

    通过正则匹配 {} 包裹的 JSON 结构，并尝试解析。

    参数:
        session: session 名称
        output_format: "json" | "markdown" | "text"
    """
    try:
        conn = _get_conn(session)
        messages = _get_messages_ordered(conn)
        conn.close()

        results = []
        for msg in messages:
            content = msg.get("content", "") or ""
            for match in _JSON_OBJECT_RE.finditer(content):
                raw = match.group(0)
                try:
                    parsed = json.loads(raw)
                    results.append({
                        "id": msg["id"],
                        "role": msg["role"],
                        "created_at": msg.get("created_at"),
                        "json_data": parsed,
                    })
                    if limit is not None and len(results) >= limit:
                        break
                except (json.JSONDecodeError, ValueError):
                    pass

        return _format_output(
            results, output_format,
            f"Session: {session} | JSON 提取"
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ── 模式 G: custom ──

def query_custom(session: str, where_clause: str, *,
                 output_format: str = "json",
                 limit: int = None) -> str:
    """自定义 WHERE 子句查询。

    参数:
        session: session 名称
        where_clause: SQL WHERE 子句（如 "role='user' AND turn > 5"）
        output_format: "json" | "markdown" | "text"
    注意:
        - session 名称已校验，防止路径注入
        - WHERE 子句直接拼接到 SQL 中，谨慎使用
    """
    # 基本安全检查：不允许 DROP/DELETE/INSERT/UPDATE/ALTER/ATTACH
    forbidden = {"DROP ", "DELETE ", "INSERT ", "UPDATE ", "ALTER ", "ATTACH ",
                 "PRAGMA ", "CREATE ", "VACUUM", "REINDEX"}
    upper_where = where_clause.upper().strip()
    for keyword in forbidden:
        if keyword in upper_where:
            return json.dumps(
                {"error": f"禁止在 WHERE 子句中使用 '{keyword.strip()}'"},
                ensure_ascii=False
            )

    try:
        conn = _get_conn(session)
        query = f"SELECT * FROM messages WHERE {where_clause} ORDER BY id"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        conn.close()
        data = _rows_to_list(rows)
        return _format_output(data, output_format, f"Session: {session} | 自定义查询")
    except Exception as e:
        return json.dumps({"error": f"查询失败: {e}"}, ensure_ascii=False)

if __name__ == "__main__":
    """命令行调用:
    
    # 原始转储
    python3 message_extractor.py dump <session> [--format json|markdown|text] [--limit N]
    
    # 关键词搜索
    python3 message_extractor.py keyword <session> <keyword> [--role user] [--format json]
    
    # 按角色
    python3 message_extractor.py role <session> <user|assistant|tool>
    
    # 时间范围
    python3 message_extractor.py time <session> --start "2026-07-21" --end "2026-07-22"
    
    # 代码块提取
    python3 message_extractor.py code <session> [--language python]
    
    # JSON 提取
    python3 message_extractor.py json <session>
    
    # 自定义
    python3 message_extractor.py custom <session> "role='user'"
    """
    import argparse

    parser = argparse.ArgumentParser(description="history_parser 消息提取工具")
    parser.add_argument("mode", choices=["dump", "keyword", "time", "role",
                                          "code", "json", "custom"])
    parser.add_argument("session", help="session 名称")
    parser.add_argument("--keyword", "-k", help="关键词（keyword 模式）")
    parser.add_argument("--regex", action="store_true", help="使用正则")
    parser.add_argument("--case-sensitive", action="store_true", help="大小写敏感")
    parser.add_argument("--role", help="角色过滤")
    parser.add_argument("--start", help="起始时间")
    parser.add_argument("--end", help="结束时间")
    parser.add_argument("--language", "-l", help="代码语言过滤")
    parser.add_argument("--format", "-f", default="json",
                        choices=["json", "markdown", "text"])
    parser.add_argument("--limit", "-n", type=int, help="限制条数")
    parser.add_argument("--offset", "-o", type=int, default=0, help="偏移量")
    parser.add_argument("where", nargs="?", help="自定义 WHERE 子句（custom 模式）")

    args = parser.parse_args()

    mode_map = {
        "dump": lambda: dump_messages(args.session, output_format=args.format,
                                       limit=args.limit, offset=args.offset),
        "keyword": lambda: filter_by_keyword(
            args.session, args.keyword or "", regex=args.regex,
            case_sensitive=args.case_sensitive, role=args.role,
            output_format=args.format, limit=args.limit),
        "time": lambda: filter_by_time(
            args.session, start=args.start, end=args.end,
            role=args.role, output_format=args.format),
        "role": lambda: filter_by_role(
            args.session, args.role or "user",
            output_format=args.format, limit=args.limit),
        "code": lambda: extract_code_blocks(
            args.session, language=args.language,
            output_format=args.format),
        "json": lambda: extract_json_objects(
            args.session, output_format=args.format),
        "custom": lambda: query_custom(
            args.session, args.where or "",
            output_format=args.format),
    }

    result = mode_map[args.mode]()
    print(result)
