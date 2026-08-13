"""audit_lib.py — pythonrt 库安全审计（判定库是否影响硬盘 IO 防控）。

功能：扫描目标库源码目录，输出危险特征报告 + 判定建议（JSON）。
纯标准库实现，可在受限沙箱（plan/build）与 build-unsafe 下运行。

用法：
    python audit_lib.py <库目录路径>          # 扫整个目录
    python audit_lib.py <库目录路径> --json   # JSON 输出（默认）
    python audit_lib.py <单文件.py>           # 只扫单文件

设计意图（对应 pythonrt_utils.audit.md 判定标准）：
    Q1 路径级 vs fd级  → io_entry_type 字段
    Q2 纯py vs C扩展   → c_extensions 字段
    Q3 危险依赖        → danger_imports 字段
    最终 verdict 供 LLM 参考，安全论证由 LLM 结合沙箱实现给出。
"""

import json
import os
import re
import sys

# 危险导入特征：模块名 → 判定说明
DANGER_IMPORTS = {
    "subprocess": "命令执行逃逸（fork 子进程绕沙箱）→ 需 stub",
    "ctypes": "C 调用逃逸（绕过一切 patch）→ 拒绝",
    "cffi": "C 调用逃逸 → 拒绝",
    "mmap": "fd 级库（无路径参数，安全）→ 模块级放行即可",
    "socket": "网络（net_policy 门控）→ 需确认 net_policy",
    "pickle": "反序列化 gadget → 拒绝",
    "marshal": "反序列化 gadget → 拒绝",
    "shelve": "反序列化 gadget → 拒绝",
    "dbm": "反序列化 gadget → 拒绝",
    "posix": "底层系统接口 → 拒绝",
    "nt": "底层系统接口 → 拒绝",
}

# 裸 fd / 命令执行 os 调用
DANGER_OS_CALLS = (
    "os.open(", "os.system(", "os.popen(", "os.fork(",
    "os.execv(", "os.spawn", "os.posix_spawn",
)

# C 扩展二进制后缀
C_EXT_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")


def _scan_file(path: str) -> dict:
    """扫描单个 .py 文件的危险特征。"""
    result = {"danger_imports": {}, "os_calls": [], "hardcoded_abs_paths": []}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 危险 import（含 from X import ... / import X.Y）
        for mod, note in DANGER_IMPORTS.items():
            if re.search(rf"\b(?:import|from)\s+{re.escape(mod)}\b", stripped):
                result["danger_imports"].setdefault(mod, []).append(f"{path}:{idx}")
                break
        # 裸 os 调用
        for call in DANGER_OS_CALLS:
            if call in stripped:
                result["os_calls"].append(f"{path}:{idx}:{stripped[:80]}")
        # 硬编码绝对路径 open
        m = re.search(r"\bopen\(\s*['\"](/[^'\"]+)['\"]", stripped)
        if m:
            result["hardcoded_abs_paths"].append(f"{path}:{idx}:{m.group(1)}")
    return result


def audit(target: str) -> dict:
    """审计目标路径（目录或单文件），返回 JSON 报告。

    Args:
        target: 库源码目录或 .py 文件路径。

    Returns:
        dict: 危险特征、C 扩展、IO 入口类型、判定建议等。

    Raises:
        ValueError: target 不存在或类型非法。
    """
    if not target:
        raise ValueError("audit(): target 路径不能为空")
    if not os.path.exists(target):
        raise ValueError(f"audit(): 路径不存在: {target}")

    report = {
        "target": os.path.abspath(target),
        "danger_imports": {},
        "os_calls": [],
        "hardcoded_abs_paths": [],
        "c_extensions": [],
        "io_entry_type": "unknown",
        "verdict": "unknown",
        "reason": "",
    }

    py_files = []
    if os.path.isdir(target):
        for root, _dirs, files in os.walk(target):
            for fn in files:
                full = os.path.join(root, fn)
                if fn.endswith(".py"):
                    py_files.append(full)
                elif fn.endswith(C_EXT_SUFFIXES):
                    report["c_extensions"].append(full)
    elif target.endswith(".py"):
        py_files = [target]

    if not py_files and not report["c_extensions"]:
        report["reason"] = "无 .py 或 C 扩展文件"
        return report

    for pf in py_files:
        scan = _scan_file(pf)
        for mod, locs in scan["danger_imports"].items():
            report["danger_imports"].setdefault(mod, []).extend(locs)
        report["os_calls"].extend(scan["os_calls"])
        report["hardcoded_abs_paths"].extend(scan["hardcoded_abs_paths"])

    # 判定 IO 入口类型（启发式：常见路径级函数名）
    _path_entry_hints = ("connect(", "init(", "open(", "create(", "write(",
                         "save(", "load(", "from_path", "Repo(")
    entry_hits = []
    for pf in py_files:
        try:
            with open(pf, "r", encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        except OSError:
            continue
        for hint in _path_entry_hints:
            if hint in txt:
                entry_hits.append(hint)
                break
    report["io_entry_type"] = "path-based" if entry_hits else "fd-based(推测)"

    # ── 判定建议 ──
    danger_roots = set(report["danger_imports"].keys())
    if danger_roots & {"ctypes", "cffi", "pickle", "marshal", "posix", "nt"}:
        report["verdict"] = "reject"
        report["reason"] = "含不可 stub 的危险依赖: " + ", ".join(danger_roots)
    elif "subprocess" in danger_roots:
        report["verdict"] = "needs-stub"
        report["reason"] = "subprocess 需 stub（放行 import，替换执行入口）"
    elif report["c_extensions"]:
        report["verdict"] = "needs-entry-patch"
        report["reason"] = f"C 扩展直调系统 open，需入口 patch: {report['c_extensions'][:3]}"
    elif "mmap" in danger_roots or "socket" in danger_roots:
        report["verdict"] = "safe-allow-with-review"
        report["reason"] = "fd级/受控网络依赖，模块级放行 + 确认无路径参数"
    else:
        report["verdict"] = "safe-allow"
        report["reason"] = "无危险特征；确认纯 Python 写文件走 builtins.open 闸门"
    return report


def main() -> None:
    """CLI 入口：python audit_lib.py <target> [--json]"""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(json.dumps({"error": "用法: audit_lib.py <库目录|文件>"}, ensure_ascii=False))
        sys.exit(1)
    try:
        report = audit(args[0])
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
