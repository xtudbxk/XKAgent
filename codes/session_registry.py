"""Global session registry and per-session path resolution.

Registry 位置（2026-08-27 起）：<project_root>/session_registry.json
  - project_root = 读 system_prompt.txt 的目录（codes/ 上一级）
  - 环境变量 XKAGENT_REGISTRY 可覆盖（副本隔离）
  - 旧位置 ~/.xkagent/session_registry.json 仅作读取 fallback（兼容旧部署）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")
# 2026-09-10: session title（展示名）——与 name（ASCII 路径 key）解耦，允许中文/空格/emoji。
# title 不参与任何路径拼接，因此规则比 name 宽松：NFC 归一化 + ≤200 字符 + 禁控制字符。
_TITLE_MAX_LEN = 200
_TITLE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_THREAD_LOCK = threading.RLock()
_LOCK_STALE_SECONDS = 60.0


def _registry_base_dir() -> Path:
    """Registry 默认存放目录：项目代码根（读 system_prompt.txt 的目录）。

    2026-08-27: 从 ~/.xkagent 迁到代码根，便于多环境可见/备份/排查
    （原位置在服务进程 HOME 下，agent 沙箱与用户都难以确认）。
    推导方式与 agent.py::_build_system_prompt 的 project_root 一致。
    """
    return Path(__file__).resolve().parent.parent


def _home_data_dir() -> Path:
    """legacy 路径（<HOME>/.xkagent）：仅作历史 registry 读取 fallback。"""
    return Path.home() / ".xkagent"


def registry_path() -> Path:
    # 2026-08-24: 支持环境变量隔离（副本独立实例不共享生产 registry）
    override = os.environ.get("XKAGENT_REGISTRY")
    if override:
        return Path(override).expanduser()
    # 2026-08-27: 默认落到项目代码根（system_prompt.txt 同目录）
    return _registry_base_dir() / "session_registry.json"


def validate_session_name(name: str) -> str | None:
    if not name or not name.strip():
        return "Session name cannot be empty"
    if len(name) > 100:
        return "Session name too long (max 100 characters)"
    if name in (".", "..") or name.startswith(".") and set(name) <= {".", "-"}:
        return "Session name cannot be a path component like '.' or '..'"
    if not _NAME_RE.match(name):
        return "Session name can only contain letters, digits, underscore (_), hyphen (-), and dot (.)"
    return None


def validate_session_title(title: str | None) -> str | None:
    """校验可选的 session title（展示名）。返回错误串或 None（合法）。

    title 仅用于展示（支持中文/空格/emoji），不参与磁盘路径拼接与调用寻址，
    因此规则比 session name 宽松：禁控制字符 + NFC 归一化 + ≤_TITLE_MAX_LEN 字符。
    空串/None 视为「未设置标题」——合法，展示时回退 session name。
    """
    if title is None:
        return None
    if not isinstance(title, str):
        return "Session title must be a string"
    if _TITLE_CTRL_RE.search(title):
        return "Session title cannot contain control characters or newlines"
    if len(unicodedata.normalize("NFC", title).strip()) > _TITLE_MAX_LEN:
        return f"Session title too long (max {_TITLE_MAX_LEN} characters)"
    return None


def normalize_session_title(title: str | None) -> str:
    """标题归一化（NFC + strip）。None/非字符串/空白 → ''（表示未设置）。"""
    if not isinstance(title, str):
        return ""
    return unicodedata.normalize("NFC", title).strip()


def validate_workdir(workdir: str | Path) -> Path:
    path = Path(workdir).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Workdir is not a directory: {path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ValueError(f"Workdir is not readable: {path}")
    data = path / ".xkagent"
    if data.exists():
        if not data.is_dir():
            raise ValueError(f"Data path is not a directory: {data}")
        if not os.access(data, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError(f"Data directory is not writable: {data}")
    elif not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f"Workdir cannot host .xkagent: {path}")
    return path


@dataclass(frozen=True)
class SessionContext:
    name: str
    workdir: Path
    created_at: str
    source: str
    title: str = ""       # 展示名（可中文）；空 = 未设置，展示回退 name
    pinned: bool = False  # 2026-09-10: 置顶（侧边栏「📌 置顶」分区固定显示）
    pinned_at: float = 0.0  # 置顶时间戳（排序用：最近置顶在前）

    @property
    def display_name(self) -> str:
        """展示名：title 优先，未设置时回退 name（唯一 fallback 规则）。"""
        return self.title or self.name

    @property
    def data(self) -> Path:
        return self.workdir / ".xkagent"

    @property
    def history(self) -> Path:
        return self.data / "historys"

    @property
    def log(self) -> Path:
        return self.data / "logs"

    @property
    def search_index(self) -> Path:
        return self.data / "search_index"

    @property
    def files(self) -> Path:
        return self.data / "files"

    @property
    def skills(self) -> Path:
        return self.data / "skills"

    data_dir = data
    history_dir = history
    log_dir = log
    search_index_dir = search_index
    files_dir = files
    skills_dir = skills

    def ensure(self) -> "SessionContext":
        for path in (self.data, self.history, self.log, self.search_index, self.files, self.skills):
            path.mkdir(parents=True, exist_ok=True)
        return self


class _RegistryLock:
    def __init__(self) -> None:
        self.path = registry_path().parent / "session_registry.lockdir"
        self.owner = self.path / "owner.json"
        self.token = uuid.uuid4().hex

    def __enter__(self):
        registry_path().parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 15.0
        while True:
            try:
                os.mkdir(self.path)
                self.owner.write_text(json.dumps({
                    "token": self.token, "pid": os.getpid(), "hostname": socket.gethostname(),
                    "created_at": time.time(),
                }), encoding="utf-8")
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.owner.stat().st_mtime
                except OSError:
                    try:
                        age = time.time() - self.path.stat().st_mtime
                    except OSError:
                        continue
                if age > _LOCK_STALE_SECONDS:
                    stale = self.path.with_name(f"{self.path.name}.stale.{uuid.uuid4().hex}")
                    try:
                        os.replace(self.path, stale)
                    except OSError:
                        continue
                    shutil.rmtree(stale, ignore_errors=True)
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for session registry lock")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        try:
            owner = json.loads(self.owner.read_text(encoding="utf-8"))
        except Exception:
            owner = {}
        if owner.get("token") == self.token:
            shutil.rmtree(self.path, ignore_errors=True)


def _load_unlocked() -> dict[str, dict]:
    path = registry_path()
    if not path.exists():
        # 2026-08-27: 兼容旧部署 —— 旧位置（~/.xkagent/）有数据时继续读取（只读，不写回）
        legacy = _home_data_dir() / "session_registry.json"
        if legacy.exists():
            legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
            if not isinstance(legacy_data, dict):
                raise ValueError("Session registry must contain a JSON object")
            return legacy_data
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Session registry must contain a JSON object")
    return data


def _write_unlocked(data: dict[str, dict]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _context(name: str, record: dict) -> SessionContext:
    return SessionContext(name=name, workdir=Path(record["workdir"]),
                          created_at=record["created_at"], source=record["source"],
                          title=record.get("title", "") or "",
                          pinned=bool(record.get("pinned")),
                          pinned_at=float(record.get("pinned_at") or 0.0))


def list_contexts() -> list[SessionContext]:
    with _THREAD_LOCK:
        data = _load_unlocked()
    return [_context(name, record) for name, record in sorted(data.items())]


def get(name: str) -> SessionContext | None:
    if validate_session_name(name):
        return None
    with _THREAD_LOCK:
        record = _load_unlocked().get(name)
    return _context(name, record) if record else None


def title_of(name: str) -> str:
    """返回 session 的 title（未注册/未设置 → ''）。"""
    context = get(name)
    return context.title if context else ""


def set_title(name: str, title: str | None) -> SessionContext:
    """设置/清除 session title（展示名）。返回更新后的 SessionContext。

    title 为空/None/纯空白 → 删除字段（展示回退 name）。
    复用 registry 文件锁（与 register 同模式）；session 未注册抛 KeyError。
    """
    err = validate_session_name(name)
    if err:
        raise ValueError(err)
    err = validate_session_title(title)
    if err:
        raise ValueError(err)
    norm = normalize_session_title(title)
    with _THREAD_LOCK, _RegistryLock():
        data = _load_unlocked()
        record = data.get(name)
        if record is None:
            raise KeyError(f"Session '{name}' is not registered")
        if norm:
            record["title"] = norm
        else:
            record.pop("title", None)
        _write_unlocked(data)
    return _context(name, record)


def set_pin(name: str, pinned: bool = True) -> SessionContext:
    """置顶/取消置顶 session（侧边栏「📌 置顶」分区固定显示）。

    与 set_title 同模式：复用 registry 文件锁 + 原子写；session 未注册抛 KeyError。
    pinned_at 记录置顶时间戳（排序用：最近置顶在前）。
    """
    err = validate_session_name(name)
    if err:
        raise ValueError(err)
    with _THREAD_LOCK, _RegistryLock():
        data = _load_unlocked()
        record = data.get(name)
        if record is None:
            raise KeyError(f"Session '{name}' is not registered")
        if pinned:
            record["pinned"] = True
            record["pinned_at"] = time.time()
        else:
            record.pop("pinned", None)
            record.pop("pinned_at", None)
        _write_unlocked(data)
    return _context(name, record)


def list_pins() -> list[str]:
    """置顶 session 名列表（最近置顶在前）；无置顶 → 空列表。"""
    with _THREAD_LOCK:
        data = _load_unlocked()
    items = [(float(r.get("pinned_at") or 0.0), n)
             for n, r in data.items() if r.get("pinned")]
    items.sort(key=lambda t: t[0], reverse=True)
    return [n for _, n in items]


def register(name: str, workdir: str | Path, source: str = "created",
             title: str | None = None) -> SessionContext:
    err = validate_session_name(name)
    if err:
        raise ValueError(err)
    err = validate_session_title(title)
    if err:
        raise ValueError(err)
    canonical = validate_workdir(workdir)
    with _THREAD_LOCK, _RegistryLock():
        data = _load_unlocked()
        existing = data.get(name)
        if existing:
            if Path(existing["workdir"]) != canonical:
                raise ValueError(f"Session '{name}' is already registered to {existing['workdir']}")
            return _context(name, existing)
        record = {"workdir": str(canonical),
                  "created_at": datetime.now(timezone.utc).isoformat(), "source": source}
        norm_title = normalize_session_title(title)
        if norm_title:
            record["title"] = norm_title
        data[name] = record
        _write_unlocked(data)
    return _context(name, record)


def resolve(name: str, startup_workdir: str | Path | None = None,
            lazy: bool = True) -> SessionContext:
    context = get(name)
    if context:
        return context
    if not lazy or startup_workdir is None:
        raise KeyError(f"Session '{name}' is not registered")
    return register(name, startup_workdir, "legacy-lazy")


def unregister(name: str) -> bool:
    with _THREAD_LOCK, _RegistryLock():
        data = _load_unlocked()
        if name not in data:
            return False
        del data[name]
        _write_unlocked(data)
    return True


def rename(old_name: str, new_name: str) -> SessionContext:
    err = validate_session_name(new_name)
    if err:
        raise ValueError(err)
    with _THREAD_LOCK, _RegistryLock():
        data = _load_unlocked()
        if old_name not in data:
            raise KeyError(f"Session '{old_name}' is not registered")
        if new_name in data:
            raise ValueError(f"Session '{new_name}' is already registered")
        record = data.pop(old_name)
        data[new_name] = record
        _write_unlocked(data)
    return _context(new_name, record)


def migrate_legacy(startup_workdir: str | Path) -> list[str]:
    workdir = validate_workdir(startup_workdir)
    history_dir = workdir / ".xkagent" / "historys"
    if not history_dir.is_dir():
        return []
    migrated = []
    for db in sorted(history_dir.glob("*.db")):
        name = db.stem
        if validate_session_name(name) is None and get(name) is None:
            register(name, workdir, "legacy-migration")
            migrated.append(name)
    return migrated
