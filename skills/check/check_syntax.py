#!/usr/bin/env python3
"""Syntax checker: uses ast.parse() to validate .py files.

Pure stdlib, zero dependencies. Compatible with WASM Python (no -m flag needed).

Usage:
    python check_syntax.py file1.py file2.py ...
    python check_syntax.py /path/to/dir          # recursive scan
    python check_syntax.py /path/to/dir --summary # only show summary
"""

import ast
import sys
import os
import argparse


def check_file(filepath: str) -> bool:
    """Check a single .py file for syntax errors. Returns True if OK."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            ast.parse(f.read())
        return True
    except SyntaxError as e:
        print(f"  ❌ {filepath}")
        print(f"     Line {e.lineno}, col {e.offset}: {e.msg}")
        if e.text:
            print(f"     Text: {e.text.rstrip()}")
        return False
    except Exception as e:
        print(f"  ⚠️  {filepath}: {e}")
        return False


def collect_files(paths: list[str]) -> list[str]:
    """Collect all .py files from given file/directory paths."""
    files: list[str] = []
    for p in paths:
        if not os.path.exists(p):
            print(f"  ⚠️  Path not found, skipping: {p}")
            continue
        if os.path.isfile(p):
            if p.endswith(".py"):
                files.append(p)
        elif os.path.isdir(p):
            for root, dirs, fnames in os.walk(p):
                # Skip common noise directories
                dirs[:] = [d for d in dirs
                           if d not in ("__pycache__", ".venv", "venv",
                                        ".git", ".hg", ".svn", "node_modules",
                                        ".tox", ".mypy_cache", ".pytest_cache",
                                        "egg-info", "dist", "build")]
                for fn in sorted(fnames):
                    if fn.endswith(".py"):
                        files.append(os.path.join(root, fn))
    return sorted(set(files))


def main():
    parser = argparse.ArgumentParser(
        description="Check Python files for syntax errors (zero-dependency)"
    )
    parser.add_argument(
        "paths", nargs="+",
        help="One or more .py files or directories to scan"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Only print summary, skip per-file OK messages"
    )
    args = parser.parse_args()

    files = collect_files(args.paths)
    if not files:
        print("  ⚠️  No .py files found in given paths")
        sys.exit(0)

    ok, fail = 0, 0
    for f in files:
        if check_file(f):
            if not args.summary:
                print(f"  ✅ {f}")
            ok += 1
        else:
            fail += 1

    total = ok + fail
    if fail:
        print(f"\n📊  Syntax check: {ok}/{total} passed, {fail} failed ❌")
    else:
        print(f"\n📊  Syntax check: {ok}/{total} passed, all clean ✅")

    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
