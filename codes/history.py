"""XKAgent — Single-table message persistence.

所有会话数据库写入 ``config.get_historys_dir()``（即 workdir/.xkagent/historys/）。
"""

from __future__ import annotations

import json, os, re, sqlite3, shutil
from codes._log import logger
from codes import config as _config

# ── 不再使用模块级 DB_DIR 常量，改为运行时从 config 获取 ──

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,       -- user | assistant | tool | compact | git
    content     TEXT,
    extras      TEXT,                -- JSON
    turn        INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""


STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行表：每 session 仅一行状态
    provider    TEXT NOT NULL DEFAULT '',            -- 当前 provider 名
    model       TEXT NOT NULL DEFAULT '',            -- 当前模型完整名（空=跟随 default_model）
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""

TOKEN_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS token_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),  -- 单行表：每 session 仅一行 token 累计
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,           -- 累计输入 tokens
    completion_tokens   INTEGER NOT NULL DEFAULT 0,           -- 累计输出 tokens
    turn_count          INTEGER NOT NULL DEFAULT 0,           -- 累计回合数
    model               TEXT NOT NULL DEFAULT '',             -- 最后一次使用的模型名
    last_prompt_tokens  INTEGER NOT NULL DEFAULT 0,           -- 最近一轮单轮输入 tokens（v2 新增，供 web 面板"上一轮"显示）
    last_completion_tokens INTEGER NOT NULL DEFAULT 0,        -- 最近一轮单轮输出 tokens（v2 新增）
    updated_at          TEXT DEFAULT (datetime('now','localtime'))
);
"""

MOUNT_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mount_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,                -- 挂载路径（UNIQUE 幂等，同 path 覆写）
    writable    INTEGER NOT NULL DEFAULT 0,          -- 0=ro 1=ro/rw（plan 恒只读由 mode 层保证）
    mount_type  TEXT NOT NULL DEFAULT 'dir',         -- dir | file
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""
SEARCH_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS search_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,                -- 搜索路径（add/deny 目标，UNIQUE 幂等覆写）
    action      TEXT NOT NULL DEFAULT 'add',         -- add=加入搜索范围 | deny=禁止（前缀匹配）
    scope       TEXT NOT NULL DEFAULT 'extra',       -- 范围名（add 时生效，deny 忽略）
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""


IMAGE_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,                -- 图片绝对路径（UNIQUE 幂等，同 path 覆写）
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""


def _db_dir() -> str:
    """获取数据库目录路径（首次调用时自动创建）。"""
    return str(_config.get_historys_dir())



# Schema 版本号：用于连接时快速判定是否需要建表/升级。
# 首次创建（文件不存在）→ 完整初始化（WAL + 建表 + commit + 设版本号）；
# 已存在但版本过旧 → 幂等补建表后升级（仅触发一次）；
# 版本匹配 → 纯读连接，零写操作。
# 设计意图（修复 check 报告根因2）：
#   旧实现每次连接都执行 PRAGMA journal_mode=WAL + 3 次建表 + commit，
#   与 agent 线程高频写库（INSERT+commit）竞争写锁，触发 busy_timeout=10s，
#   阻塞 asyncio 事件循环导致所有请求排队（日志实测 19.6s）。
_SCHEMA_VERSION = 5   # v5: 新增 image_state 表（/image 图片附件 per-session 配置）；v4: 新增 search_state 表（/info 搜索范围 per-session 配置）；v3: 新增 mount_state 表（/mount 动态挂载）；v2: token_state 新增 last_* 列


def get_conn(session: str) -> sqlite3.Connection:
    """获取 session 数据库连接。

    优化：仅首次创建（或 schema 版本过旧）时才执行写操作（WAL/建表/commit），
    否则返回纯读连接——消除每次连接的写锁竞争（busy_timeout 不再被触发）。
    """
    db_path = os.path.join(_db_dir(), f"{session}.db")
    is_new = not os.path.exists(db_path)
    logger.debug(f"数据库连接: session={session} is_new={is_new}")
    conn = sqlite3.connect(db_path, timeout=10.0)  # T6: busy_timeout 10s
    conn.row_factory = sqlite3.Row

    if is_new:
        # 首次创建：完整初始化（WAL 模式持久化到 DB 文件，后续连接自动继承）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA_SQL)
        conn.execute(STATE_SCHEMA_SQL)
        conn.execute(TOKEN_STATE_SCHEMA_SQL)
        conn.execute(MOUNT_STATE_SCHEMA_SQL)
        conn.execute(SEARCH_STATE_SCHEMA_SQL)
        conn.execute(IMAGE_STATE_SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
        return conn

    # 非首次：仅当版本过旧（旧库缺表/历史版本）时幂等补建表，之后纯读
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        ver = 0
    if ver < _SCHEMA_VERSION:
        conn.execute(SCHEMA_SQL)
        conn.execute(STATE_SCHEMA_SQL)
        conn.execute(TOKEN_STATE_SCHEMA_SQL)
        conn.execute(MOUNT_STATE_SCHEMA_SQL)
        conn.execute(SEARCH_STATE_SCHEMA_SQL)
        conn.execute(IMAGE_STATE_SCHEMA_SQL)
        # v2 迁移：为旧 token_state 表补 last_* 列（幂等：列已存在则跳过）
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(token_state)").fetchall()}
        for _col, _ddl in (("last_prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
                           ("last_completion_tokens", "INTEGER NOT NULL DEFAULT 0")):
            if _col not in _cols:
                conn.execute(f"ALTER TABLE token_state ADD COLUMN {_col} {_ddl}")
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    return conn

# ── Backward-compatible session tracking ──

_current_session = 'default'

def _set_session_name(name: str):
    global _current_session
    _current_session = name


# ── Session name validation ──

_SESSION_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-.]+$')

def _validate_session_name(name: str) -> str | None:
    """Validate session name. Returns error string or None if valid."""
    if not name or not name.strip():
        return "Session name cannot be empty"
    if len(name) > 100:
        return "Session name too long (max 100 characters)"
    if not _SESSION_NAME_RE.match(name):
        return (
            "Session name can only contain letters, digits, "
            "underscore (_), hyphen (-), and dot (.)"
        )
    return None


# ── Core API ──


# ── 命令历史持久化（/xxx 与 !xxx 及回应落库）──
COMMAND_RESULT_MAX_LEN = 4000   # 命令回应文本截断长度，防单条记录撑爆 DB
SANITIZE_COMMANDS = True        # 落库前对命令/回应做敏感信息脱敏（token/key/secret 等）

_SENSITIVE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|authorization|bearer|token)\b"
    r"\s*[:=]\s*(?:(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+))"
)


def sanitize_cmd(text):
    """对命令/回应做基础脱敏：token/key/secret/password/bearer 等值打码为 ***。

    设计意图：!xxx 命令可能携带凭据（如 curl -H "Authorization: Bearer xxx"），
    落库前统一打码，避免明文凭据持久化。SANITIZE_COMMANDS=False 可关闭。
    """
    if not text:
        return ""
    text = str(text)
    if not SANITIZE_COMMANDS:
        return text
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}=***", text)


def add_command(conn, cmd_text, result="", *, kind="slash", exit_code=None, ok=None, meta=None):
    """持久化一条 /xxx 或 !xxx 命令及其回应（role='command'）。

    - cmd_text : 命令原文（!ls -la 或 /model）
    - result   : 执行回应文本（截断至 COMMAND_RESULT_MAX_LEN）
    - kind     : slash | bang | mount（mount 由 agent._record_command 记录，此处仅兼容）
    - exit_code: 进程退出码（!xxx 有；/xxx 通常无）
    - ok       : 执行是否成功（None=未知）
    - meta     : 额外元数据（并入 extras JSON）

    设计意图：对齐 agent._record_command 的 role='command' 先例——get_messages_since/
    resume()/_sync_from_db 均已过滤该 role，故新记录不会污染 LLM 上下文、不会触发
    跨进程增量推送、也不会出现在前端消息流中（保持 D1-b 设计决策）。
    """
    cmd_text = sanitize_cmd(cmd_text)
    result = sanitize_cmd(result)[:COMMAND_RESULT_MAX_LEN]
    # 2026-08-05: result 持久化进 extras["result"]——此前仅截断未落库，
    # 导致 web 历史渲染 /xxx、!xxx 时无结果可展示（get_messages_since 放开 command 后依赖此字段）
    extras = {"kind": kind, "result": result}
    if exit_code is not None:
        extras["exit_code"] = int(exit_code)
    if ok is not None:
        extras["ok"] = bool(ok)
    if meta:
        extras.update(meta)
    try:
        conn.execute(
            "INSERT INTO messages (role, content, extras, turn) VALUES ('command', ?, ?, 0)",
            (cmd_text, json.dumps(extras, ensure_ascii=False)),
        )
        conn.commit()
    except Exception:
        logger.warning(f"记录 command 历史失败: {cmd_text!r}", exc_info=True)


def get_command_history(conn, limit=50):
    """读取最近 limit 条命令记录（供 /cmds 查看），按 id 升序返回 dict 列表。

    兼容 agent._record_command 写入的旧格式 extras（含 cmd/ok/result 键）。
    """
    if limit is None or not isinstance(limit, int) or limit <= 0:
        limit = 50
    rows = conn.execute(
        "SELECT id, content, extras, created_at FROM messages "
        "WHERE role='command' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in reversed(rows):
        try:
            extras = json.loads(r["extras"]) if r["extras"] else {}
        except Exception:
            extras = {}
        out.append({
            "id": r["id"],
            "cmd": r["content"] or "",
            "kind": extras.get("kind", "mount" if "cmd" in extras else ""),
            "exit_code": extras.get("exit_code"),
            "ok": extras.get("ok"),
            "created_at": r["created_at"] or "",
        })
    return out


def add_message(conn, role, content, extras=None, turn=0):
    logger.debug(f"写入消息: role={role}, content={str(content)[:80]!r}")
    conn.execute(
        "INSERT INTO messages (role, content, extras, turn) VALUES (?, ?, ?, ?)",
        (role, content, json.dumps(extras, ensure_ascii=False) if extras else None, turn),
    )
    conn.commit()



def get_messages_since(conn, after_id: int | None = None, with_id: bool = False, limit: int | None = None, fields: list[str] | None = None, with_time: bool = False) -> tuple[list[dict], int]:
    """增量获取消息，返回 (messages, last_id)。
    - after_id 为 None → 全量（等价旧 get_messages 语义）
    - after_id >= 0 → 只返回 id > after_id 的消息（增量游标）
    - limit 非 None → 只返回最新的 limit 条（用于 web 全量分页加载；增量路径禁用）
    - last_id = 本次返回的最大真实消息 id（synthetic summary 不计入）；
    无消息返回 0，前端以此作为下次增量查询的游标
    - compact/drop marker 边界：若 after_id < marker.id（即上次拉取后发生压缩），
    仍返回 synthetic summary + marker 之后的消息，保证前端切回时能看到
    压缩上下文，避免"上下文缺失"。
    设计意图（web 方案A 增量加载）：
    - 切换 session 时不再每次全量拉取历史，前端缓存 last_id 后只拉增量，
    大幅减少大 session 的查询/序列化/传输开销。
    - 与 get_messages 共享同一套 marker/过滤/序列化逻辑，行为完全一致，
    仅多出 id 游标与 last_id 水位线，向后兼容（None → 全量）。
    - with_id=True 时每条消息附加 "_id"（DB 主键），供前端增量渲染去重；
    默认 False 保持无副作用（agent 路径直接透传 LLM，不能带多余字段）。
    - limit 仅对行查询生效（synthetic summary 始终返回），SQL 层用
    子查询取最新 limit 条再升序，避免全量加载大 session 的开销。
    - fields：额外放开的 role 列表（如 ["command"]）。默认 None → 排除集
    恒为 ('git','compact','drop','command','thinking')；传入后从排除集剔除对应 role。
    agent 路径（get_chat_messages/resume）不传 → 行为与现状完全一致，
    command 等内部角色永不进入 LLM 上下文（D1-b 决策保持）；
    thinking（思考链）同样只作展示、不进入 LLM 上下文（2026-08-06 需求）。
    """
    # 防御性校验：after_id 必须为非负整数或 None（非法游标直接抛错，
    # 避免静默返回错误数据导致前端缓存水位线错乱）
    if after_id is not None and (not isinstance(after_id, int) or after_id < 0):
        raise ValueError(
            f"after_id must be a non-negative int or None, got {after_id!r}"
        )
    # ── 最新 marker（compact/drop）判定：与旧 get_messages 逻辑一致 ──
    marker = conn.execute(
        "SELECT id, content, extras, role FROM messages WHERE role IN ('compact', 'drop') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_id = 0
    result: list[dict] = []
    # limit 校验：仅接受正整数，非法值忽略（保持全量，向后兼容）
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        limit = None
    if marker:
        extras = json.loads(marker["extras"]) if marker["extras"] else {}
        cutoff_max_id = extras.get("cutoff_max_id", 0)
        role = marker["role"]
        # 压缩发生在上次拉取点之后 → 需补 synthetic summary，
        # 否则前端只看到"断档"后的消息，缺失压缩上下文
        if after_id is None or marker["id"] > after_id:
            if role == "compact":
                summary = (marker["content"] or "").strip()
                synthetic_msg = (
                    "[对话历史已压缩。以下是完整上下文，请基于此继续当前任务：]\n\n"
                    + summary + "\n"
                )
            else:  # role == "drop"
                synthetic_msg = (
                    "[对话历史已丢弃。之前的消息已标记为丢弃，后续消息从此处开始。]\n"
                )
            syn_msg = {"role": "user", "content": synthetic_msg}
            if with_id:
                # 唯一 id：synthetic 无 DB 行，用 marker 的 DB id 前缀区分，
                # 避免前端去重时多个 summary 共享 undefined id 被误过滤
                syn_msg["_id"] = f"syn-{marker['id']}"
            result.append(syn_msg)
        # 取 cutoff 与游标的较大者：既不重复已看过的，也不遗漏压缩保留段
        since = max(cutoff_max_id, after_id or 0)
    else:
        since = after_id or 0
    # 排除集动态化：fields 传入的 role 从排除集中剔除（如 web 放开 command/thinking 渲染）。
    # agent 路径不传 fields → 排除集恒为 ('git','compact','drop','command','thinking')，
    # 行为与现状一致（thinking 只展示、不进入 LLM 上下文）。
    _BASE_EXCLUDED = ('git', 'compact', 'drop', 'command', 'thinking')
    _excluded = [r for r in _BASE_EXCLUDED if not (fields and r in fields)]
    _excl_sql = ",".join("?" for _ in _excluded)
    if limit is not None:
        # 分页：子查询取最新 limit 条（DESC + LIMIT），外层升序还原展示顺序
        rows = conn.execute(
            "SELECT * FROM ("
            "SELECT id, role, content, extras, created_at FROM messages "
            f"WHERE role NOT IN ({_excl_sql}) AND id > ? "
            "ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (*_excluded, since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, role, content, extras, created_at FROM messages "
            f"WHERE role NOT IN ({_excl_sql}) AND id > ? "
            "ORDER BY id",
            (*_excluded, since),
        ).fetchall()
    for r in rows:
        last_id = max(last_id, r["id"])
        # Ensure content is never None (some API providers reject null content)
        msg = {"role": r["role"], "content": r["content"] or ""}
        if with_id:
            msg["_id"] = r["id"]  # 前端去重用（仅 web 增量路径）
        if with_time:
            msg["created_at"] = r["created_at"]  # web 展示时间戳（LLM 上下文路径不携带）
        if r["extras"]:
            extras_d = json.loads(r["extras"])
            if r["role"] == "assistant" and "tool_calls" in extras_d:
                msg["tool_calls"] = extras_d["tool_calls"]
            if r["role"] == "tool" and "tool_call_id" in extras_d:
                msg["tool_call_id"] = extras_d["tool_call_id"]
            if r["role"] == "command":
                # 仅当调用方通过 fields 放开 command 时命中（agent 路径排除，永不进入）。
                # 兼容两种 extras 格式：
                #   新格式（add_command）：content=完整命令, extras={kind,result,exit_code,ok,...}
                #   旧格式（agent._record_command）：content=命令文本, extras={cmd,ok,result}
                msg["kind"] = extras_d.get("kind", "")
                msg["result"] = extras_d.get("result", "")
                msg["exit_code"] = extras_d.get("exit_code")
                msg["ok"] = extras_d.get("ok")
                if "cmd" in extras_d:      # agent 旧格式：content 仅是展示文本，命令原文在 extras.cmd
                    msg["cmd_text"] = extras_d.get("cmd", "")
        result.append(msg)
    return result, last_id



def get_messages(conn, fields: list[str] | None = None) -> list[dict]:
    """全量获取消息（向后兼容封装，等价 get_messages_since(conn, None)[0]）。

    保留独立入口避免破坏现有调用方（agent.py / repl.py / web.py），
    内部复用增量查询的同一套 marker/过滤/序列化逻辑，行为完全一致。
    fields 透传给 get_messages_since（默认 None → 排除集不变）。
    """
    return get_messages_since(conn, None, fields=fields)[0]
# ── User message system prefix parser (shared by repl/web) ──
# repl 与 web 的展示层都需要剥离用户消息的"系统前缀"元数据
# （时间/系统模式/建议技能/正文），只展示真实正文。
# 统一放 history.py，避免 repl.py 与 web.py 各维护一份正则导致漂移。
_USER_MSG_PATTERN = re.compile(
    r"""^时间:\s*(.+)\n
系统模式:\s*(.+)\n
(?:路径访问权限:\s*(.*)\n)?
建议技能:[^\S\n]*([^\n]*\n(?:[ ]{2}[^\n]*\n)*)
(?:推荐信息:\n((?:[ ]{2}[^\n]*\n)*))?
(?:要求:\s*([^\n]*)\n)?
正文:\n
([\s\S]*)""",
    re.VERBOSE
)


def parse_user_prefix(content: str) -> dict | None:
    """解析用户消息的系统前缀，返回 {timestamp, mode, permission, suggested_skills, recommended_info, requirement, body}；无前缀返回 None。
    注：建议技能可能多行（每行 2 空格缩进），正则按块匹配。

    设计意图：
    - 用户消息存储时带 `时间:/系统模式:/建议技能:/正文:` 前缀（agent.py 构造），
      展示层不应原样输出，需剥离后仅显示正文。
    - 无前缀消息（compact 合成摘要、[skill context: xxx] 等）返回 None，
      由调用方 fallback 原样展示，保证健壮性。
    """
    if not content:
        return None
    match = _USER_MSG_PATTERN.match(content)
    if not match:
        return None
    return {
        "timestamp": match.group(1).strip(),
        "mode": match.group(2).strip(),
        "permission": (match.group(3) or "").strip(),
        "suggested_skills": match.group(4).strip(),
        "recommended_info": (match.group(5) or "").strip(),
        "requirement": (match.group(6) or "").strip(),
        "body": match.group(7).strip() or "",
    }
def clear_messages(conn):
    conn.execute("DELETE FROM messages")
    conn.commit()




# ── Aliases for backward compat ──

get_connection = get_conn

def add_chat(conn, role, content, extras=None):
    add_message(conn, role, content, extras)


def get_chat_messages(conn) -> list[dict]:
    return get_messages(conn)


def clear_chats(conn):
    clear_messages(conn)


# ── Session management ──

def session_exists(name: str) -> bool:
    """Check if a valid-named session DB file exists on disk."""
    if _validate_session_name(name) is not None:
        return False
    return os.path.exists(os.path.join(_db_dir(), name + ".db"))


def list_sessions() -> list[str]:
    dbd = _db_dir()
    files = [f for f in os.listdir(dbd)
             if f.endswith(".db") and _validate_session_name(f[:-3]) is None]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(dbd, f)), reverse=True)
    return [f[:-3] for f in files]


def sync_session(name: str) -> tuple[bool, str]:
    """WAL checkpoint: flush .db-wal into .db, truncate WAL/SHM.

    Returns (success, message).
    Should be called before any file-level session operation (fork, rename)
    to ensure data integrity.
    """
    if not session_exists(name):
        return False, f"Session '{name}' does not exist"
    try:
        conn = get_conn(name)
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
        busy, log, ckpt = row[0], row[1], row[2]
        if busy == 0:
            return True, f"Synced '{name}': {ckpt} WAL pages checkpointed"
        return True, f"Sync partial for '{name}': busy={busy}, {ckpt}/{log} pages (data in .db-wal preserved)"
    except Exception as e:
        return False, f"Sync failed for '{name}': {e}"


def add_session(name: str) -> tuple[bool, str]:
    """Create a new empty session (init DB + schema + agent_state)."""
    err = _validate_session_name(name)
    if err:
        return False, err
    if session_exists(name):
        return False, f"Session '{name}' already exists"
    conn = get_conn(name)
    conn.close()
    # 初始化 agent_state（无 [default] 时为空串，由 Agent 启动引导）
    ensure_agent_state(name)
    return True, f"Session '{name}' created"


def fork_session(source: str, target: str) -> tuple[bool, str]:
    """Fork a session by copying .db / .db-wal / -shm files.

    Syncs source first (WAL checkpoint) to ensure data integrity.
    """
    err = _validate_session_name(target)
    if err:
        return False, err
    if not session_exists(source):
        return False, f"Source session '{source}' does not exist"
    if session_exists(target):
        return False, f"Target session '{target}' already exists"

    # Step 1: sync source (WAL checkpoint)
    sync_ok, sync_msg = sync_session(source)
    if not sync_ok:
        return False, sync_msg

    # Step 2: copy files
    dbd = _db_dir()
    src_base = os.path.join(dbd, source)
    dst_base = os.path.join(dbd, target)
    exts_copied = []
    try:
        for ext in (".db", ".db-wal", ".db-shm"):
            src_path = src_base + ext
            if os.path.exists(src_path):
                shutil.copy2(src_path, dst_base + ext)
                exts_copied.append(ext)
    except OSError as e:
        # Rollback: remove any already copied files on failure
        for ext_rolled in exts_copied:
            try:
                os.remove(dst_base + ext_rolled)
            except OSError:
                pass
        return False, f"Fork failed at '{ext}': {e}. Rolled back {len(exts_copied)} file(s)."

    if ".db" not in exts_copied:
        # Clean up partial copies if source was corrupted
        for ext_rolled in exts_copied:
            try:
                os.remove(dst_base + ext_rolled)
            except OSError:
                pass
        return False, f"Corrupted source session '{source}': missing .db file"

    return True, f"Forked '{source}' -> '{target}' ({len(exts_copied)} file(s))"


def rename_session(old_name: str, new_name: str) -> tuple[bool, str]:
    """Rename session by renaming .db / .db-wal / -shm files.

    Syncs old session first, then renames with rollback on failure.
    """
    err = _validate_session_name(new_name)
    if err:
        return False, err
    if not session_exists(old_name):
        return False, f"Session '{old_name}' does not exist"
    if session_exists(new_name):
        return False, f"Session '{new_name}' already exists"

    # Step 1: sync old (WAL checkpoint)
    sync_ok, sync_msg = sync_session(old_name)
    if not sync_ok:
        return False, sync_msg

    # Step 2: pre-check all source files
    dbd = _db_dir()
    old_base = os.path.join(dbd, old_name)
    new_base = os.path.join(dbd, new_name)
    exts_to_move = []
    for ext in (".db", ".db-wal", ".db-shm", ".db.lock"):
        if os.path.exists(old_base + ext):
            exts_to_move.append(ext)
    if not exts_to_move:
        return False, f"No files found for session '{old_name}'"
    if ".db" not in exts_to_move:
        return False, f"Corrupted session '{old_name}': missing .db file"

    # Step 3: rename with rollback
    moved = []
    for ext in exts_to_move:
        try:
            os.rename(old_base + ext, new_base + ext)
            moved.append(ext)
        except OSError as e:
            # Rollback
            for ext_rolled in moved:
                try:
                    os.rename(new_base + ext_rolled, old_base + ext_rolled)
                except OSError as rollback_err:
                    print(f"  ⚠️  Rollback warning: failed to restore '{ext_rolled}': {rollback_err}")
            return False, f"Rename failed at '{ext}': {e}. All changes rolled back."

    return True, f"Renamed '{old_name}' -> '{new_name}'"


def delete_session(name: str) -> tuple[bool, str]:
    """Delete session files. Returns (success, message).

    Checks for .db existence first to detect corrupted sessions
    (missing .db but sidecar files present).
    """
    err = _validate_session_name(name)
    if err:
        return False, err
    dbd = _db_dir()
    base = os.path.join(dbd, name)
    db_exists = os.path.exists(base + ".db")
    found_any = False
    for suffix in (".db", ".db-wal", ".db-shm", ".db.lock"):
        try:
            os.remove(base + suffix)
            found_any = True
        except OSError:
            pass
    if not found_any:
        return False, f"Session '{name}' does not exist"
    if not db_exists:
        # .db missing but sidecar files existed — session was corrupted
        logger.warning(f"Deleted corrupted session '{name}': missing .db, cleaned up remaining files")
        return True, f"Session '{name}' deleted (was corrupted: missing .db, cleaned up)"
    return True, f"Session '{name}' deleted"


# ────────────────────────────────────────────────────────────────
#  Agent 状态持久化（provider / model 按 session 独立存储）
# ────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────
#  Agent 状态持久化（provider / model 按 session 独立存储）
# ────────────────────────────────────────────────────────────────

def get_agent_state(session: str) -> tuple[str, str]:
    """读取 session 的 provider/model 状态，无记录时返回 ("", "")。

    agent_state 是单行表（id=1），每个 session 独立 .db 文件，
    因此不同 session 的模型选择互不影响。
    """
    conn = get_conn(session)
    try:
        row = conn.execute(
            "SELECT provider, model FROM agent_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return "", ""
        return row["provider"] or "", row["model"] or ""
    finally:
        conn.close()


def set_agent_state(
    session: str,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """写入 session 的 provider/model 状态（原子 UPSERT 单行 id=1）。

    参数为 None 时保留原值；返回写入后的完整状态 (provider, model)。
    由 /model 命令在切换时调用，实现"重启恢复上次选择"。

    设计：单条 INSERT ... ON CONFLICT DO UPDATE 完成插入/更新，
    避免"先读后写"的并发竞态（读-改-写窗口）。
    """
    conn = get_conn(session)
    try:
        row = conn.execute(
            "SELECT provider, model FROM agent_state WHERE id = 1"
        ).fetchone()
        cur_provider = row["provider"] if row else ""
        cur_model = row["model"] if row else ""
        new_provider = provider if provider is not None else cur_provider
        new_model = model if model is not None else cur_model
        conn.execute(
            "INSERT INTO agent_state (id, provider, model) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, "
            "model = excluded.model, updated_at = datetime('now','localtime')",
            (new_provider, new_model),
        )
        conn.commit()
        return new_provider, new_model
    finally:
        conn.close()


def ensure_agent_state(session: str) -> tuple[str, str]:
    """确保 session 有 agent_state 记录，返回 (provider, model)。

    无记录时用 provider.config [default] 初始化并写回：
      - 配置了 [default].provider → 用之（model 用 [default].model）
      - 未配置 [default] → 继承"修改时间最新"session 的 provider/model
        （list_sessions 按 mtime 降序，排除自身，取第一个非空）
      - 无继承源 → provider/model 均为 ""（未选择，
        由 Agent 启动时引导用户 /model @xxx 或配置 [default]）
    幂等：已有记录直接返回，不覆盖用户已选状态。
    并发安全：INSERT OR IGNORE 避免重复插入冲突（add_session 与
    Agent.__init__ 可能同时触发）。
    """
    provider, model = get_agent_state(session)
    # 注意：空状态 ('', '') 也是合法记录（未选择 provider 的 session）
    # 不能仅凭 provider or model 判'无记录'，否则每次调用都会重复 INSERT
    if provider or model:
        return provider, model
    from codes import provider_config
    init_provider = provider_config.get_default_provider()
    init_model = provider_config.get_default_model()
    # ── 继承兜底：未配置 [default] 时，继承"修改时间最新"session 的 provider/model ──
    # 方便新 session 直接沿用最近使用的模型，避免每次手动 /model 选择。
    # 优先级不变：session db > 显式参数 > [default] > 最新 session 继承 > 引导。
    if not init_provider:
        for cand in list_sessions():
            if cand == session:
                continue  # 排除自身（add_session 先建 db 文件，自身 mtime 最新）
            p, m = get_agent_state(cand)
            if p or m:
                init_provider, init_model = p, m
                break
    conn = get_conn(session)
    try:
        # UPSERT：已存在空记录时也能覆盖（add_session 会先建空 agent_state，
        # INSERT OR IGNORE 无法更新已存在的空行 → 继承值写不进去）
        conn.execute(
            "INSERT INTO agent_state (id, provider, model) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, "
            "model = excluded.model, updated_at = datetime('now','localtime')",
            (init_provider, init_model),
        )
        conn.commit()
        # 重读：可能是本线程刚插入的，也可能是并发线程插入的（幂等）
        row = conn.execute("SELECT provider, model FROM agent_state WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return init_provider, init_model
    return row["provider"] or "", row["model"] or ""


# ────────────────────────────────────────────────────────────────
#  Token 累计状态持久化（prompt/completion tokens 按 session 独立存储）
# ────────────────────────────────────────────────────────────────

def get_token_state(session: str) -> dict:
    """读取 session 的 token 累计状态，无记录时返回全 0。

    token_state 是单行表（id=1），每个 session 独立 .db 文件，
    因此不同 session 的 token 累计互不影响（切 session 各看各的）。
    """
    conn = get_conn(session)
    try:
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens, turn_count, model, "
            "last_prompt_tokens, last_completion_tokens "
            "FROM token_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "turn_count": 0, "model": "",
                    "last_prompt_tokens": 0, "last_completion_tokens": 0}
        return {
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "turn_count": row["turn_count"] or 0,
            "model": row["model"] or "",
            "last_prompt_tokens": row["last_prompt_tokens"] or 0,
            "last_completion_tokens": row["last_completion_tokens"] or 0,
        }
    finally:
        conn.close()


def set_token_state(
    session: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    turn_count: int | None = None,
    model: str | None = None,
    last_prompt_tokens: int | None = None,
    last_completion_tokens: int | None = None,
) -> dict:
    """写入 session 的 token 累计状态（原子 UPSERT 单行 id=1）。

    参数为 None 时保留原值；返回写入后的完整状态。
    与 set_agent_state 同风格：单条 INSERT ... ON CONFLICT DO UPDATE，
    避免"先读后写"并发竞态；任何一处累加（主对话/技能选择/压缩）后调用。
    """
    conn = get_conn(session)
    try:
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens, turn_count, model, "
            "last_prompt_tokens, last_completion_tokens "
            "FROM token_state WHERE id = 1"
        ).fetchone()
        if row is None:
            cur = {"prompt_tokens": 0, "completion_tokens": 0, "turn_count": 0, "model": "",
                   "last_prompt_tokens": 0, "last_completion_tokens": 0}
        else:
            cur = {
                "prompt_tokens": row["prompt_tokens"] or 0,
                "completion_tokens": row["completion_tokens"] or 0,
                "turn_count": row["turn_count"] or 0,
                "model": row["model"] or "",
                "last_prompt_tokens": row["last_prompt_tokens"] or 0,
                "last_completion_tokens": row["last_completion_tokens"] or 0,
            }
        new_p = prompt_tokens if prompt_tokens is not None else cur["prompt_tokens"]
        new_c = completion_tokens if completion_tokens is not None else cur["completion_tokens"]
        new_t = turn_count if turn_count is not None else cur["turn_count"]
        new_m = model if model is not None else cur["model"]
        new_lp = last_prompt_tokens if last_prompt_tokens is not None else cur["last_prompt_tokens"]
        new_lc = last_completion_tokens if last_completion_tokens is not None else cur["last_completion_tokens"]
        conn.execute(
            "INSERT INTO token_state (id, prompt_tokens, completion_tokens, turn_count, model, "
            "last_prompt_tokens, last_completion_tokens) "
            "VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "prompt_tokens = excluded.prompt_tokens, "
            "completion_tokens = excluded.completion_tokens, "
            "turn_count = excluded.turn_count, "
            "model = excluded.model, "
            "last_prompt_tokens = excluded.last_prompt_tokens, "
            "last_completion_tokens = excluded.last_completion_tokens, "
            "updated_at = datetime('now','localtime')",
            (new_p, new_c, new_t, new_m, new_lp, new_lc),
        )
        conn.commit()
        return {"prompt_tokens": new_p, "completion_tokens": new_c, "turn_count": new_t,
                "model": new_m, "last_prompt_tokens": new_lp, "last_completion_tokens": new_lc}
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
#  动态挂载状态持久化（/mount 动态挂载按 session 独立存储，v3）
# ────────────────────────────────────────────────────────────────

def get_mount_state(session: str) -> list[dict]:
    """读取 session 的动态挂载列表（重启恢复用）。无记录返回 []。

    mount_state 是多行表，每个 session 独立 .db 文件，
    因此不同 session 的动态挂载互不影响（per-session 隔离）。
    返回项: {"path", "writable", "mount_type"}，由 agent 侧补齐 source/type。
    """
    conn = get_conn(session)
    try:
        rows = conn.execute(
            "SELECT path, writable, mount_type FROM mount_state ORDER BY id"
        ).fetchall()
        return [
            {"path": r["path"], "writable": bool(r["writable"]),
             "mount_type": r["mount_type"] or "dir"}
            for r in rows
        ]
    finally:
        conn.close()


def set_mount_state(session: str, mounts: list[dict]) -> None:
    """全量覆写 session 的动态挂载（事务：DELETE + 批量 INSERT）。

    挂载量小（通常 <10 条），全量覆写保证与内存 _dyn_mounts 严格一致，
    避免逐条 diff 的复杂度；单事务保证失败回滚不留半状态。
    mounts 项须含 "path" / "writable"，可选 "mount_type"（默认 dir）。
    """
    conn = get_conn(session)
    try:
        conn.execute("DELETE FROM mount_state")
        conn.executemany(
            "INSERT INTO mount_state (path, writable, mount_type) VALUES (?, ?, ?)",
            [
                (m["path"], 1 if m.get("writable") else 0,
                 m.get("mount_type", "dir"))
                for m in mounts
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
#  搜索范围配置持久化（/info 搜索范围 per-session 配置，v4）
# ────────────────────────────────────────────────────────────────

def get_search_state(session: str) -> list[dict]:
    """读取 session 的搜索范围配置（/info add|deny）。无记录返回 []。

    search_state 是多行表，每个 session 独立 .db 文件，
    因此不同 session 的搜索范围互不影响（per-session 隔离，对齐 mount_state）。
    返回项: {"path", "action", "scope"}（action: add | deny）。
    """
    conn = get_conn(session)
    try:
        rows = conn.execute(
            "SELECT path, action, scope FROM search_state ORDER BY id"
        ).fetchall()
        return [
            {"path": r["path"], "action": r["action"] or "add",
             "scope": r["scope"] or "extra"}
            for r in rows
        ]
    finally:
        conn.close()


def set_search_state(session: str, items: list[dict]) -> None:
    """全量覆写 session 的搜索范围配置（事务：DELETE + 批量 INSERT）。

    配置量小（通常 <10 条），全量覆写保证与调用方内存视图严格一致；
    单事务保证失败回滚不留半状态。
    items 项须含 "path" / "action"，可选 "scope"（默认 extra）。
    """
    conn = get_conn(session)
    try:
        conn.execute("DELETE FROM search_state")
        conn.executemany(
            "INSERT INTO search_state (path, action, scope) VALUES (?, ?, ?)",
            [
                (it["path"], it.get("action", "add"),
                 it.get("scope") or "extra")
                for it in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
#  图片附件持久化（/image add|clear|list，v5）
# ────────────────────────────────────────────────────────────────


def get_images(session: str) -> list[str]:
    """读取 session 已附加的图片路径列表（发送前注入用）。无记录返回 []。

    image_state 是多行表，每个 session 独立 .db 文件，
    因此不同 session 的图片附件互不影响（per-session 隔离）。
    """
    conn = get_conn(session)
    try:
        rows = conn.execute("SELECT path FROM image_state ORDER BY id").fetchall()
        return [r["path"] for r in rows]
    finally:
        conn.close()


def add_image(session: str, path: str) -> bool:
    """幂等追加一张图片路径；同路径重复添加返回 False（不重复计数）。"""
    conn = get_conn(session)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO image_state (path) VALUES (?)", (path,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_images(session: str) -> int:
    """清空图片附件列表，返回清除数量。"""
    conn = get_conn(session)
    try:
        cur = conn.execute("DELETE FROM image_state")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
