"""XKAgent — msgz 消息持久化（2026-08-24 全量切换，替代 sqlite）

存储：``<workdir>/.xkagent/historys/<session>.msgz``（zlib 压缩 JSON 单文件）。
设计动机：
  sqlite 的 WAL 模式依赖 -wal/-shm/-lock 多文件交互，在 CubeFS 满卷/扩容
  窗口期出现间歇性 disk I/O error（EIO），故障期写操作同步失败。
  msgz 改为内存主数据 + 定时原子落盘：写操作在内存完成（故障期零报错
  零丢失），落盘为普通 IO（tmp + os.replace 原子替换）。

旧 ``*.db`` 文件保留（只读历史），迁移脚本：``codes/msgz_migrate.py``。
所有公共函数签名与 sqlite 版保持一致（conn 实为 MsgzStore）。
"""

from __future__ import annotations

import json, os, re, shutil, threading, time
from pathlib import Path
from codes._log import logger
from codes import config as _config
from codes import session_registry as _registry
from codes.history_msgz import MsgzStore, MsgzManager, MSGZ_SUFFIX

# ── 不再使用 schema/连接管理（sqlite 已移除）──


def _db_dir(session: str) -> str:
    """Resolve the storage directory for a session（2026-08-24: 本地优先）。

    优先级：
      1. 当前 workdir 的 historys 已有该 session 的 .msgz → 本地
         （副本独立实例/共享 registry 指向他处时，数据以本地文件为准）
      2. registry 解析路径存在该 session 的 .msgz → registry 路径
      3. 都不存在 → 本地（新 session 归当前 workdir，避免写错目录）
    """
    err = _validate_session_name(session)
    if err:
        raise ValueError(err)
    # 注意：不用 config.get_historys_dir()——它受 session context 影响
    # （共享 registry 下 context.workdir 可能指向其他项目），必须用启动 workdir。
    local_dir = Path(_config.get_default_workdir()) / ".xkagent" / "historys"
    if (local_dir / f"{session}{MSGZ_SUFFIX}").exists():
        return str(local_dir)
    # 2026-08-25 修复：已注册 session 尊重注册表 workdir（含文件未建的新建场景），
    # 避免"注册表指向 A、_db_dir 归本地 B"的分裂（用户指定 workdir 的 session
    # 文件落错目录 → /session remove 删不到）。改用 get 而非 resolve，
    # 消除 resolve 的 legacy-lazy 副作用（未注册 session 不再被静默注册）。
    ctx = _registry.get(session)
    if ctx is not None:
        try:
            ctx_dir = Path(ctx.ensure().history_dir)
            if (ctx_dir / f"{session}{MSGZ_SUFFIX}").exists():
                return str(ctx_dir)
            # 已注册但文件未建（新 session 创建中）→ 归注册表目录
            return str(ctx_dir)
        except Exception:
            pass
    # 未注册 → 本地（新 session 归当前 workdir）
    return str(local_dir)


def _db_path(session: str) -> str:
    """返回 session 的 msgz 文件路径（兼容旧函数名；.msgz 单文件）。"""
    history_dir = Path(_db_dir(session)).resolve()
    p = (history_dir / f"{session}{MSGZ_SUFFIX}").resolve()
    try:
        p.relative_to(history_dir)
    except ValueError as e:
        raise ValueError(f"Session path escaped history dir: {session}") from e
    return str(p)


_manager: MsgzManager | None = None


def _get_manager() -> MsgzManager:
    global _manager
    if _manager is None:
        # 2026-08-24: 固定用启动 workdir 的 historys——get_historys_dir() 受
        # session context 影响（共享 registry 下 context.workdir 可能指向其他
        # 项目），会导致 store 从错误目录加载（空数据）。
        _manager = MsgzManager(str(Path(_config.get_default_workdir()) / ".xkagent" / "historys"))
    return _manager


# ── Session name validation ──


def _validate_session_name(name: str) -> str | None:
    """Validate session name. Returns error string or None if valid."""
    return _registry.validate_session_name(name)


# ── Core API ──

_current_session: str | None = None


def _set_session_name(name: str):
    """记录当前活跃 session（agent 启动/切换时调用，供诊断）。"""
    global _current_session
    _current_session = name


def _retry(fn, *args, tries: int = 5, base_delay: float = 0.5, **kwargs):
    """msgz 版兼容实现（agent.py import 为 _db_retry）：内存写无 IO 重试需求。"""
    return fn(*args, **kwargs)


def _mark_ioerr_cooldown(db_path: str) -> None:
    """msgz 版兼容 no-op（agent.py _sync_from_db 引用；msgz 无 IOERR 冷却概念）。"""
    return


def get_conn(session: str) -> MsgzStore:
    """返回 session 的 MsgzStore（内存主数据 + 30s 定时原子落盘）。

    msgz 版无连接概念：store 常驻内存，写操作永不因存储故障失败。
    2026-08-25 修复：按 _db_path(session) 解析（本地优先 + registry 回退），
    而不是固定启动 workdir——web 从非数据目录启动时也能读到正确 .msgz
    （原实现 list_sessions 用 registry、get_conn 用启动目录，两者不一致
    导致切换 session 时消息串台/丢失）。
    """
    return _get_manager().get(_db_path(session))


# 兼容别名
get_connection = get_conn


def _drop_store(session: str) -> None:
    """从 MsgzManager 移除 session 的内存 store（按路径后缀匹配，跨目录安全）。

    2026-08-25：store 缓存键已改为完整文件路径，fork/rename/delete 需按
    session 名匹配任意目录下的 store。
    """
    try:
        suffix = f"{session}{MSGZ_SUFFIX}"
        mgr = _get_manager()
        with mgr.lock:
            for k in [k for k in mgr.stores if k.endswith(suffix)]:
                mgr.stores.pop(k, None)
    except Exception:
        pass


def flush_all() -> int:
    """全量落盘（退出/切换前调用）。返回成功数。"""
    return _get_manager().sync_all()


# ── 命令历史持久化（/xxx 与 !xxx 及回应落库）──
COMMAND_RESULT_MAX_LEN = 4000   # 命令回应文本截断长度
SANITIZE_COMMANDS = True        # 落库前对命令/回应做敏感信息脱敏

_SENSITIVE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|authorization|bearer|token)\b"
    r"\s*[:=]\s*(?:(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+))"
)


def sanitize_cmd(text):
    """对命令/回应做基础脱敏：token/key/secret/password/bearer 等值打码为 ***。"""
    if not text:
        return ""
    text = str(text)
    if not SANITIZE_COMMANDS:
        return text
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}=***", text)


def add_command(conn, cmd_text, result="", *, kind="slash", exit_code=None, ok=None, meta=None):
    """持久化一条 /xxx 或 !xxx 命令及其回应（role='command'）。"""
    cmd_text = sanitize_cmd(cmd_text)
    result = sanitize_cmd(result)[:COMMAND_RESULT_MAX_LEN]
    extras = {"kind": kind, "result": result}
    if exit_code is not None:
        extras["exit_code"] = int(exit_code)
    if ok is not None:
        extras["ok"] = bool(ok)
    if meta:
        extras.update(meta)
    try:
        conn.add_message("command", cmd_text, extras, 0)
    except Exception:
        logger.warning(f"记录 command 历史失败: {cmd_text!r}", exc_info=True)


def get_command_history(conn, limit=50):
    """读取最近 limit 条命令记录（供 /cmds 查看），按 id 升序返回 dict 列表。"""
    if limit is None or not isinstance(limit, int) or limit <= 0:
        limit = 50
    rows = conn.get_command_history(limit)
    out = []
    for r in rows:
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
            "result": extras.get("result", ""),
            "created_at": r["created_at"] or "",
        })
    return out


def add_message(conn, role, content, extras=None, turn=0):
    """写入消息（msgz：内存 append，O(1)，永不因存储故障失败）。"""
    logger.debug(f"写入消息: role={role}, content={str(content)[:80]!r}")
    return conn.add_message(role, content, extras, turn)


def get_messages_since(conn, after_id=None, with_id=False, limit=None,
                       fields=None, with_time=False, ignore_cutoff=False):
    """增量获取消息（对齐 sqlite 版语义：after_id 游标 / limit / compact marker）。"""
    return conn.get_messages_since(after_id=after_id, with_id=with_id, limit=limit,
                                   fields=fields, with_time=with_time,
                                   ignore_cutoff=ignore_cutoff)


def get_messages_before(conn, before_id, limit=None, fields=None,
                        with_id=False, with_time=False, ignore_cutoff=False):
    """before_id 之前的最新 limit 条（倒序分页，升序返回；web 历史滚动）。"""
    return conn.get_messages_before(before_id, limit=limit, with_id=with_id,
                                    with_time=with_time, ignore_cutoff=ignore_cutoff)


def get_messages(conn, fields=None):
    """全量获取消息（向后兼容封装）。"""
    return conn.get_messages(fields=fields)


# ── User message system prefix parser (shared by repl/web) ──
_USER_MSG_PATTERN = re.compile(
    r"""^时间:\s*(.+)\n
系统模式:\s*(.+)\n
(?:路径访问权限:\s*(.*)\n)?
(?:建议技能:[^\S\n]*([^\n]*\n(?:[ ]{2}[^\n]*\n)*))?
(?:推荐信息:\n((?:[ ]{2}[^\n]*\n)*))?
(?:要求:\s*([^\n]*)\n)?
(?:⚠️\s*系统告警:\s*([^\n]*)\n)?
(?:⚠️\s*工具约束:\s*([^\n]*)\n)?
(?:正文:\n)?
([\s\S]*)""",
    re.VERBOSE
)

# 旧格式兼容（2026-07-25 前的消息），2026-08-25 修复：
#   1) "──── 用户消息系统信息 ────\n时间: ...\n模式: plan\n建议技能: ...\n────...\n\n正文"
#      （含可选前置 "  ⚙️ mode: X → Y" 修饰行）
#   2) "[skill context: plan]"（旧技能上下文标记）
# 背景：web 历史渲染曾因严格正则不匹配旧格式 → 系统前缀未剥离直接展示。
_USER_MSG_PATTERN_LEGACY = re.compile(
    r"""^(?:\s*\n)*(?:(?:[^\n]*?⚙️[^\n]*)\n\n)?
─+[^\n]*?─+(?:\n时间:\s*(.+))?
(?:\n模式:\s*(.+))?
(?:\n建议技能:\s*([^\n]*))?
(?:\n─+[^\n]*?─+)?(?:\n\n)?
([\s\S]*)
|^\[skill\ context:\s*([^\]]+)\]\s*$""",
    re.VERBOSE
)


# ── v2 通用字段头解析（2026-09-03）──
# 头部元信息区允许任意顶格 "字段名: 值" / "字段名:" 字段头（可带 "⚠️ " 告警前缀），
# 新增字段无需改解析器：已知字段映射固定 key，未知字段收进 extra_fields。
# 字段头判定：顶格 + 半角冒号 + 冒号后空白或行尾（排除 URL/时刻等冒号误判）；
# 未知字段须同行带值（"xxx: xxx"）才切分，空值顶格小标题（如片段 "Usage:"）视为普通文本。
_USER_FIELD_KNOWN = {
    "时间": "timestamp",
    "系统模式": "mode",
    "当前会话": "agent_name",
    "路径访问权限": "permission",
    "建议技能": "suggested_skills",
    "推荐信息": "recommended_info",
    "要求": "requirement",
    "系统告警": "system_alarm",
    "工具约束": "tool_constraint",
    "状态信息": "status_info",
}
# 块字段：字段体可持续多行（续行归入字段值）；其余为单行字段（其后的非字段头行即正文开始）
_USER_BLOCK_FIELDS = {"建议技能", "推荐信息", "状态信息"}
_USER_FIELD_HEAD_RE = re.compile(r"^(⚠️\s*)?([^\s:：]{1,24}):(\s|$)")


def _split_user_fields(content: str):
    """行扫描状态机：按顶格字段头切分头部元信息区与正文区。

    返回 (fields: dict[字段名->值], body: str)；首行非 "时间:" 字段头时返回 (None, None)。
    """
    lines = content.split("\n")
    if not lines[0].startswith("时间:"):
        return None, None
    fields = {"时间": []}
    rest0 = lines[0][len("时间:"):]
    if rest0.strip():
        fields["时间"].append(rest0)
    cur = "时间"
    body_start = None
    for idx in range(1, len(lines)):
        ln = lines[idx]
        if cur == "正文":
            body_start = idx
            break
        mh = _USER_FIELD_HEAD_RE.match(ln)
        if mh:
            name = mh.group(2)
            rest = ln[mh.end():]
            if (name not in _USER_FIELD_KNOWN and name != "正文"
                    and not rest.strip()):
                # 未知字段的空值头（如片段中的 "Usage:"/"策略（三层降级）:"）→ 按普通
                # 文本行处理：真实新字段规范为 "xxx: xxx" 同行带值；块式新字段
                # （冒号后换行）需在 _USER_FIELD_KNOWN 注册后才被识别
                pass
            else:
                cur = name
                fields[name] = [rest] if rest.strip() else []
                continue
        if cur is None:
            # 防御：首行已预检为 时间:，正常不会到达
            body_start = idx
            break
        if cur in _USER_BLOCK_FIELDS:
            fields[cur].append(ln)   # 块字段：续行归入字段值
        else:
            body_start = idx         # 单行字段后的非字段头行 → 正文开始
            break
    body = "\n".join(lines[body_start:]).strip() if body_start is not None else ""
    joined = {k: "\n".join(v).strip() for k, v in fields.items()}
    return joined, body


def parse_user_prefix(content: str) -> dict | None:
    """解析用户消息的系统前缀，返回 dict；无前缀返回 None。

    v2（2026-09-03）：行扫描状态机 + 通用字段头（替代原单一正则 _USER_MSG_PATTERN）。
    修复：建议技能/推荐信息的多行片段（含顶格 Markdown 表格行）使原正则连锁失配，
    导致推荐信息/工具约束/技能摘要混入正文的问题。
    - 任意顶格 "字段名: 值"/"字段名:" 识别为字段头（新字段无需改解析器）
    - 已知字段映射固定 key；未知字段收进 extra_fields（web 渲染端通用显示）
    - "正文:" 之后全部原样作为 body
    - 兼容：LEGACY 装饰头格式走原分支；无前缀消息返回 None
    """
    if not content or not isinstance(content, str):
        return None
    raw, body = _split_user_fields(content)
    if raw is None:
        # 旧格式（2026-07-25 前）与旧技能上下文标记：保持原 LEGACY 分支
        match = _USER_MSG_PATTERN_LEGACY.match(content)
        if not match:
            return None
        if match.group(5) is not None:
            return {
                "timestamp": "", "mode": "", "permission": "",
                "suggested_skills": "", "recommended_info": "", "requirement": "",
                "body": "", "skill_context": match.group(5).strip(),
            }
        return {
            "timestamp": (match.group(1) or "").strip(),
            "mode": (match.group(2) or "").strip(),
            "permission": "",
            "suggested_skills": (match.group(3) or "").strip(),
            "recommended_info": "",
            "requirement": "",
            "body": (match.group(4) or "").strip(),
        }
    out = {}
    for zh, en in _USER_FIELD_KNOWN.items():
        out[en] = raw.get(zh, "")
    out["extra_fields"] = {
        k: v for k, v in raw.items() if k not in _USER_FIELD_KNOWN and k != "正文"
    }
    out["body"] = body or ""
    return out
def clear_messages(conn):
    conn.delete_all_messages()


# ── Aliases for backward compat ──


def add_chat(conn, role, content, extras=None):
    add_message(conn, role, content, extras)


def get_chat_messages(conn) -> list[dict]:
    return conn.get_chat_messages()


def clear_chats(conn):
    clear_messages(conn)


# ── Session management ──


def session_exists(name: str) -> bool:
    """Check if a valid-named session msgz file exists on disk（本地优先）。"""
    if _validate_session_name(name) is not None:
        return False
    # 1) 当前 workdir 本地文件（get_default_workdir：不受 session context 影响）
    local = Path(_config.get_default_workdir()) / ".xkagent" / "historys" / f"{name}{MSGZ_SUFFIX}"
    if local.exists():
        return True
    # 2) registry 解析路径
    context = _registry.get(name)
    if context is not None:
        if (context.history_dir / f"{name}{MSGZ_SUFFIX}").exists():
            return True
        return False
    # 3) 无注册记录：legacy 惰性检查（本地已查过）→ 不存在
    return False


def list_sessions() -> list[str]:
    sessions = []
    for context in _registry.list_contexts():
        path = context.history_dir / f"{context.name}{MSGZ_SUFFIX}"
        if path.exists():
            sessions.append((path.stat().st_mtime, context.name))
    # 2026-08-24: 目录发现兜底——未被 registry 记录的 .msgz 也列出
    # （副本独立实例/手工放置的 msgz 文件无需注册即可出现）
    hist_dir = Path(_config.get_default_workdir()) / ".xkagent" / "historys"
    if hist_dir.is_dir():
        known = set()
        for _, n in sessions:
            known.add(n)
        for p in sorted(hist_dir.glob("*" + MSGZ_SUFFIX)):
            name = p.name[: -len(MSGZ_SUFFIX)]
            if name not in known:
                sessions.append((p.stat().st_mtime, name))
    sessions.sort(reverse=True)
    return [name for _, name in sessions]


def list_sessions_for_workdir(workdir=None) -> list[str]:
    """Return session names registered to the given workdir (default: startup workdir)."""
    wd = Path(workdir).expanduser().resolve() if workdir is not None else _config.get_default_workdir().resolve()
    sessions = []
    for context in _registry.list_contexts():
        if Path(context.workdir).resolve() != wd:
            continue
        path = context.history_dir / f"{context.name}{MSGZ_SUFFIX}"
        if path.exists():
            sessions.append((path.stat().st_mtime, context.name))
    # 目录发现兜底（workdir 下 .xkagent/historys/*.msgz）
    hist_dir = wd / ".xkagent" / "historys"
    if hist_dir.is_dir():
        known = set()
        for _, n in sessions:
            known.add(n)
        for p in sorted(hist_dir.glob("*" + MSGZ_SUFFIX)):
            name = p.name[: -len(MSGZ_SUFFIX)]
            if name not in known:
                sessions.append((p.stat().st_mtime, name))
    sessions.sort(reverse=True)
    return [name for _, name in sessions]


def sync_session(name: str) -> tuple[bool, str]:
    """强制落盘 session 的 msgz（等价旧 WAL checkpoint 语义）。

    Returns (success, message).
    """
    if not session_exists(name):
        return False, f"Session '{name}' does not exist"
    try:
        store = get_conn(name)
        if store.sync():
            return True, f"Synced '{name}': msgz flushed"
        return False, f"Sync failed for '{name}': storage I/O error (data kept in memory)"
    except Exception as e:
        return False, f"Sync failed for '{name}': {e}"


def add_session(name: str, workdir=None, title: str | None = None) -> tuple[bool, str]:
    """Create a new empty session (msgz store + agent_state).

    title（可选）：展示名（支持中文），仅写入 registry，不参与磁盘路径拼接。
    未设置时展示回退 name（session_registry.SessionContext.display_name）。
    """
    err = _validate_session_name(name)
    if err:
        return False, err
    err = _registry.validate_session_title(title)
    if err:
        return False, err
    existing_ctx = _registry.get(name)
    if existing_ctx is not None:
        return False, (f"Session '{name}' already exists "
                       f"(workdir: {existing_ctx.workdir})")
    try:
        _registry.register(name, workdir or _config.get_default_workdir(), "created",
                           title=title)
        store = get_conn(name)  # 初始化 store
        ensure_agent_state(name)
        store.sync()  # 首次落盘：创建 .msgz 文件（失败不阻塞，内存兜底）
    except Exception as e:
        if not session_exists(name):
            _registry.unregister(name)
        return False, f"Session create failed: {e}"
    return True, f"Session '{name}' created"



def _rollback_fork_target(target: str, dst_path: Path) -> None:
    """回滚 fork 失败产生的 registry、内存 store 和目标文件。"""
    _drop_store(target)
    try:
        _registry.unregister(target)
    except Exception:
        pass
    try:
        dst = str(dst_path)
        if os.path.exists(dst):
            # 遵守项目删除约定：失败产物移入 .trash，不直接删除。
            trash_dir = os.path.join(os.path.dirname(dst), ".trash")
            os.makedirs(trash_dir, exist_ok=True)
            trash = os.path.join(
                trash_dir,
                os.path.basename(dst) + ".failed_%d" % time.time_ns(),
            )
            os.replace(dst, trash)
    except OSError as e:
        logger.warning("fork 失败回滚目标文件失败: %s -> %s: %s", dst_path, target, e)

def fork_session(source: str, target: str, cutoff_id: int | None = None) -> tuple[bool, str]:
    """Fork a session by copying its msgz file.

    cutoff_id 非空时按消息截断：仅保留 id<=cutoff_id 的消息（该消息及其上方
    历史/LLM 回复），新会话 next_id=cutoff_id+1 保持 id 连续。None 时完整复制。
    """
    err = _validate_session_name(target)
    if err:
        return False, err
    if _registry.get(target) is not None:
        return False, f"Session '{target}' already exists"
    if cutoff_id is not None:
        if (isinstance(cutoff_id, bool) or not isinstance(cutoff_id, int)
                or cutoff_id <= 0):
            return False, "cutoff_id must be a positive integer"
    try:
        source_context = _registry.resolve(source, _config.get_default_workdir())
    except Exception as e:
        return False, f"Source session '{source}' not found: {e}"
    # 源先落盘（保证最新数据）
    try:
        get_conn(source).sync()
    except Exception:
        pass
    src_path = source_context.history_dir / f"{source}{MSGZ_SUFFIX}"
    dst_base = source_context.history_dir / target
    dst_path = Path(str(dst_base) + MSGZ_SUFFIX)
    if dst_path.exists():
        return False, f"Session '{target}' already exists"
    if not src_path.exists():
        return False, f"Corrupted source session '{source}': missing msgz file"
    try:
        # 2026-09-10: title 不继承——fork 出的会话应各自命名，避免展示名指向同一语义；
        # 未设置时展示自然回退到 target（name）。
        _registry.register(target, source_context.workdir, f"fork:{source}")
        try:
            shutil.copy2(str(src_path), str(dst_base) + MSGZ_SUFFIX)
        except Exception:
            _rollback_fork_target(target, dst_path)
            return False, f"Fork copy failed for '{source}' -> '{target}'"
        if cutoff_id is not None:
            # 按消息截断：复用 MsgzStore 加载刚复制的文件 → 内存过滤 → sync 落盘
            try:
                store = get_conn(target)
                cutoff_found = False
                with store.lock:
                    all_ids = {m["id"] for m in store.messages}
                    cutoff_found = cutoff_id in all_ids
                    if cutoff_found:
                        store.messages = [m for m in store.messages if m["id"] <= cutoff_id]
                        store._next_id = cutoff_id + 1
                        store.dirty = True
                if not cutoff_found:
                    _rollback_fork_target(target, dst_path)
                    return False, f"Message id {cutoff_id} not found in '{source}'"
                if not store.sync():
                    _rollback_fork_target(target, dst_path)
                    return False, f"Fork truncate persist failed for '{source}' -> '{target}'"
            except Exception:
                _rollback_fork_target(target, dst_path)
                return False, f"Fork truncate failed for '{source}' -> '{target}'"
        # 注册后清内存缓存（新 store 重新加载；按路径后缀删除，跨目录安全）
        _drop_store(target)
    except Exception as e:
        _rollback_fork_target(target, dst_path)
        return False, f"Fork registry update failed: {e}"
    return True, f"Session '{target}' forked from '{source}'"



def prepare_rerun(session: str, message_id: int) -> tuple[bool, str, dict | None]:
    """截断指定用户消息及其后的历史，并返回可重新提交的用户正文。

    选中的 user 消息本身也会被截断，随后由 Agent.run_stream 重新写入并生成
    新回复；消息 id 不回收，保持单调递增，避免前端增量游标与旧消息冲突。
    """
    if (isinstance(message_id, bool) or not isinstance(message_id, int)
            or message_id <= 0):
        return False, "message_id must be a positive integer", None
    if not session_exists(session):
        return False, f"Session '{session}' does not exist", None

    store = get_conn(session)
    selected = None
    old_messages = None
    old_next_id = None
    old_dirty = False
    with store.lock:
        for item in store.messages:
            if item.get("id") == message_id:
                selected = dict(item)
                break
        if selected is None:
            return False, f"Message id {message_id} not found in '{session}'", None
        if selected.get("role") != "user":
            return False, f"Message id {message_id} is not a user message", None
        raw_content = selected.get("content")
        if not isinstance(raw_content, str):
            return False, f"Message id {message_id} has unsupported content type", None
        parsed = parse_user_prefix(raw_content)
        text = parsed.get("body") if parsed else raw_content
        text = (text or "").strip()
        if not text:
            return False, f"Message id {message_id} has empty user content", None

        old_messages = list(store.messages)
        old_next_id = store._next_id
        old_dirty = store.dirty
        kept = []
        for item in store.messages:
            if item.get("id", 0) < message_id:
                kept.append(item)
        store.messages = kept
        store.dirty = True
        last_id = kept[-1].get("id", 0) if kept else 0
        removed = len(old_messages) - len(kept)

    if not store.sync():
        with store.lock:
            store.messages = old_messages
            store._next_id = old_next_id
            store.dirty = old_dirty
        return False, f"Rerun truncate persist failed for '{session}'", None

    return True, f"Session '{session}' truncated from message {message_id}", {
        "text": text,
        "message_id": message_id,
        "last_id": last_id,
        "removed": removed,
    }

def rename_session(old_name: str, new_name: str) -> tuple[bool, str]:
    """Rename session by renaming its msgz file."""
    err = _validate_session_name(new_name)
    if err:
        return False, err
    if _registry.get(new_name) is not None:
        return False, f"Session '{new_name}' already exists"
    try:
        context = _registry.resolve(old_name, _config.get_default_workdir())
    except Exception as e:
        return False, f"Session '{old_name}' not found: {e}"
    try:
        get_conn(old_name).sync()
    except Exception:
        pass
    old_base = context.history_dir / old_name
    new_base = context.history_dir / new_name
    old_path = str(old_base) + MSGZ_SUFFIX
    new_path = str(new_base) + MSGZ_SUFFIX
    if not os.path.exists(old_path):
        return False, f"Corrupted session '{old_name}': missing msgz file"
    try:
        os.rename(old_path, new_path)
    except Exception as e:
        return False, f"Rename file failed: {e}"
    try:
        _registry.rename(old_name, new_name)
    except Exception as e:
        os.rename(new_path, old_path)
        return False, f"Rename registry update failed: {e}"
    # 清内存缓存（按路径后缀删除，跨目录安全）
    _drop_store(old_name)
    _drop_store(new_name)
    return True, f"Session '{old_name}' renamed to '{new_name}'"


def delete_session(name: str) -> tuple[bool, str]:
    """Delete session（删 .msgz + .lockdir；旧 .db 保留为只读历史）。

    2026-08-25 修复：删除范围覆盖"注册表指向目录 + 本地默认目录"两处——
    _db_dir 本地优先策略可能把 session 文件落在启动 workdir（与注册表指向
    分裂），单删注册表目录会残留本地 msgz 导致"删不掉"。lockdir 同样双位置清理。
    """
    context = _registry.get(name)
    # 内存 store 先移除（按路径后缀匹配，跨目录安全）
    _drop_store(name)
    failures = []
    removed_any = False
    # 候选删除位置：注册表指向目录 + 本地默认目录
    candidates = []
    if context is not None:
        candidates.append(context.history_dir / f"{name}{MSGZ_SUFFIX}")
        candidates.append(context.history_dir / f"{name}.lockdir")
    local_h = Path(_config.get_default_workdir()) / ".xkagent" / "historys"
    candidates.append(local_h / f"{name}{MSGZ_SUFFIX}")
    candidates.append(local_h / f"{name}.lockdir")
    for p in candidates:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed_any = True
            elif os.path.isdir(p):
                import shutil as _sh
                _sh.rmtree(p)
                removed_any = True
        except OSError as e:
            failures.append(f"{os.path.basename(p)}: {e}")
    # 未注册场景：无文件可删 → 视为不存在；有文件 → 补注册以便 unregister
    if context is None:
        if not removed_any:
            return False, f"Session '{name}' does not exist"
        try:
            _registry.register(name, _config.get_default_workdir(), "legacy-lazy")
        except Exception:
            pass
    if failures:
        return False, f"Session '{name}' partially deleted: {'; '.join(failures)}"
    try:
        _registry.unregister(name)
    except Exception:
        pass
    return True, f"Session '{name}' deleted (legacy .db kept as read-only history)"


# ── State API（msgz states dict 存取）──


def get_agent_state(session: str) -> tuple[str, str]:
    """返回 (provider, model)。"""
    st = get_conn(session).get_state("agent_state") or {}
    return st.get("provider", ""), st.get("model", "")


def set_agent_state(session: str, provider: str | None = None,
                    model: str | None = None, force: bool = False) -> None:
    """设置 provider/model（None=保持现值；force=True 时覆盖默认继承逻辑）。"""
    store = get_conn(session)
    st = store.get_state("agent_state") or {}
    if provider is not None:
        st["provider"] = provider
    if model is not None:
        st["model"] = model
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    store.set_state("agent_state", st)


def ensure_agent_state(session: str) -> tuple[str, str]:
    """确保 agent_state 存在，返回 (provider, model)。

    空记录时用 provider.config [default] 初始化并写回（对齐 sqlite 版语义）：
      - 配置了 [default].provider → 用之（model 用 [default].model）
      - 未配置 [default] → 继承"修改时间最新"session 的 provider/model
      - 无继承源 → provider/model 均为 ""（由 Agent 启动时引导）
    幂等：已有非空记录直接返回，不覆盖用户已选状态。
    """
    store = get_conn(session)
    st = store.get_state("agent_state")
    provider = (st or {}).get("provider", "")
    model = (st or {}).get("model", "")
    if provider or model:
        return provider, model
    from codes import provider_config
    init_provider = provider_config.get_default_provider()
    init_model = provider_config.get_default_model()
    # 继承兜底：未配置 [default] 时，继承"修改时间最新"session 的 provider/model
    if not init_provider:
        for cand in list_sessions():
            if cand == session:
                continue  # 排除自身
            p, m = get_agent_state(cand)
            if p or m:
                init_provider, init_model = p, m
                break
    st = {"provider": init_provider, "model": init_model,
          "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    store.set_state("agent_state", st)
    return init_provider, init_model


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_token_state(session: str) -> dict:
    """返回 token 累计状态 dict（全字段，缺省 0）。"""
    st = get_conn(session).get_state("token_state") or {}
    return {
        "prompt_tokens": _as_int(st.get("prompt_tokens")),
        "completion_tokens": _as_int(st.get("completion_tokens")),
        "turn_count": _as_int(st.get("turn_count")),
        "model": st.get("model", ""),
        "last_prompt_tokens": _as_int(st.get("last_prompt_tokens")),
        "last_completion_tokens": _as_int(st.get("last_completion_tokens")),
        "reasoning_tokens": _as_int(st.get("reasoning_tokens")),
        "updated_at": st.get("updated_at", ""),
    }


def set_token_state(session: str, *, prompt_tokens=None, completion_tokens=None,
                    reasoning_tokens=None, turn_count=None, model=None,
                    last_prompt_tokens=None, last_completion_tokens=None) -> None:
    """增量更新 token 状态（None=保持现值）。"""
    store = get_conn(session)
    st = store.get_state("token_state") or {}
    fields = {
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens, "turn_count": turn_count,
        "model": model, "last_prompt_tokens": last_prompt_tokens,
        "last_completion_tokens": last_completion_tokens,
    }
    for k, v in fields.items():
        if v is not None:
            st[k] = v
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    store.set_state("token_state", st)


def get_mount_state(session: str) -> list[dict]:
    """返回挂载列表 [{path, writable, mount_type}]。

    兼容旧格式（2026-08-22 前单条 dict 存储 {id, path, writable, mount_type}）：
    归一化为 list[dict]，避免调用方 for m in <dict> 遍历到字符串 key
    触发 "string indices must be integers"（_load_dyn_mounts / path_guard / web files roots）。
    """
    raw = get_conn(session).get_state("mount_state") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [m for m in raw if isinstance(m, dict)]


def set_mount_state(session: str, mounts: list[dict]) -> None:
    """全量覆写 session 的动态挂载。"""
    get_conn(session).set_state("mount_state", list(mounts))


def get_search_state(session: str) -> list[dict]:
    """返回搜索范围配置 [{path, action, scope}]。"""
    return get_conn(session).get_state("search_state") or []


def set_search_state(session: str, items: list[dict]) -> None:
    """全量覆写 session 的搜索范围配置。"""
    get_conn(session).set_state("search_state", list(items))


def get_images(session: str) -> list[str]:
    """返回图片路径列表。"""
    st = get_conn(session).get_state("image_state") or {}
    return st.get("paths", [])


def add_image(session: str, path: str) -> bool:
    """登记一张图片（幂等）。"""
    store = get_conn(session)
    st = store.get_state("image_state") or {}
    paths = st.get("paths", [])
    if path not in paths:
        paths.append(path)
        st["paths"] = paths
        store.set_state("image_state", st)
    return True


def clear_images(session: str) -> int:
    """清空图片列表，返回清除条数。"""
    store = get_conn(session)
    st = store.get_state("image_state") or {}
    n = len(st.get("paths", []))
    store.set_state("image_state", {"paths": []})
    return n


def db_health_check(session: str) -> tuple[bool, str]:
    """msgz 健康检查：文件存在 + 可加载 + 可同步。"""
    try:
        store = get_conn(session)
        n = len(store.messages)
        ok = store.sync()
        if not ok:
            return False, f"msgz sync failed (storage I/O error), {n} messages in memory"
        return True, f"msgz ok: {n} messages"
    except Exception as e:
        return False, f"msgz health check failed: {e}"
