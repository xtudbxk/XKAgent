"""Static HTML pages for the Web UI (loaded once, cached in memory)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=8)
def load_page(name: str) -> str:
    """Load a page from web_static/ by filename (e.g. index.html)."""
    path = (_DIR / name).resolve()
    if path.parent != _DIR:
        raise ValueError(f"Invalid page name: {name!r}")
    return path.read_text(encoding="utf-8")
