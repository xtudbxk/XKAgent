"""Shared path-in-root checks (pythonrt, searchinfo, @refs, exec_agent images, files API)."""

from __future__ import annotations

import os


def root_contains(root: str, path: str) -> bool:
    """True if realpath(path) is under root (handles root == '/')."""
    root = os.path.realpath(root)
    ap = os.path.realpath(path)
    if root == os.sep:
        return ap == os.sep or ap.startswith(os.sep)
    return ap == root or ap.startswith(root + os.sep)


def path_in_roots(path: str, roots: list[str]) -> bool:
    if not path or not roots:
        return False
    try:
        candidate = os.path.realpath(path)
    except OSError:
        return False
    return any(root_contains(r, candidate) for r in roots if r)


def allowed_roots_for_agent(agent) -> list[str]:
    roots: list[str] = [str(getattr(agent, "cwd", "") or ""), "/tmp"]
    roots.extend(p for p, _ in getattr(agent, "_perm_volumes", []) or [])
    roots.extend(str(m.get("path", "")) for m in getattr(agent, "_dyn_mounts", []) or [])
    out: list[str] = []
    for r in roots:
        if not r:
            continue
        try:
            out.append(os.path.realpath(r))
        except OSError:
            continue
    return out


def allowed_roots_for_session(session: str | None) -> list[str]:
    from codes import config
    from codes.agent import _parse_permission_file
    from codes.history import get_mount_state

    wd = str(config.get_session_context(session).workdir if session else config.get_workdir())
    roots = [os.path.realpath(wd)]
    try:
        for p, _w in _parse_permission_file():
            rp = os.path.realpath(p)
            if rp not in roots:
                roots.append(rp)
    except Exception:
        pass
    if session:
        try:
            for m in get_mount_state(session):
                rp = os.path.realpath(m.get("path", ""))
                if rp and rp not in roots:
                    roots.append(rp)
        except Exception:
            pass
    return [r for r in roots if os.path.exists(r)]


def resolve_in_roots(path: str, base: str, roots: list[str]) -> str | None:
    """Resolve path against base; return realpath if inside roots else None."""
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(base, path)
    try:
        candidate = os.path.realpath(p)
    except OSError:
        return None
    return candidate if path_in_roots(candidate, roots) else None
