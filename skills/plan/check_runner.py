# deps: stdlib only
"""plan 技能 — 批量检查执行器

Phase 4 中用于批量运行标准检查命令（环境/文件/配置），
返回结构化结果，减少 LLM 逐条敲 bash 的重复劳动。

Usage:
    from skills.plan.check_runner import run_checks
    result = run_checks([
        {"name": "Python 版本", "cmd": "python3 --version", "expected": "3.10"},
    ])
"""
import json
import shlex
import subprocess


def run_checks(checks: list[dict]) -> str:
    """批量运行检查命令，返回 JSON 结果字符串。

    每条检查项格式：
        {"name": "<显示名称>", "cmd": "<bash 命令>", "expected": "<期望包含的字符串（可选）>"}

    Args:
        checks: 检查项列表

    Returns:
        JSON 字符串，每项包含 name, cmd, passed, stdout, stderr, exit_code
    """
    results = []
    for check in checks:
        name = check.get("name", "?")
        cmd = check.get("cmd", "")
        expected = check.get("expected", "")

        entry = {"name": name, "cmd": cmd}

        if not cmd:
            entry["passed"] = False
            entry["error"] = "empty command"
            results.append(entry)
            continue

        try:
            r = subprocess.run(
                shlex.split(cmd),
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = r.stdout + r.stderr
            if expected:
                entry["passed"] = expected in output
            else:
                entry["passed"] = r.returncode == 0
            entry["stdout"] = r.stdout.strip()
            entry["stderr"] = r.stderr.strip()
            entry["exit_code"] = r.returncode
        except subprocess.TimeoutExpired:
            entry["passed"] = False
            entry["error"] = "timeout (30s)"
        except FileNotFoundError:
            entry["passed"] = False
            entry["error"] = f"command not found: {cmd.split()[0]}"
        except Exception as e:
            entry["passed"] = False
            entry["error"] = str(e)

        results.append(entry)

    return json.dumps(results, ensure_ascii=False, indent=2)


def format_report(results_json: str) -> str:
    """将 JSON 结果转换为终端风格报告。

    Args:
        results_json: run_checks 返回的 JSON 字符串

    Returns:
        格式化后的文本报告，每行包含 [OK]/[FAIL] + 名称 + 详情
    """
    try:
        results = json.loads(results_json)
    except json.JSONDecodeError:
        return "[ERROR] Invalid JSON input"

    lines = []
    for r in results:
        if r.get("passed"):
            lines.append(f"  [OK] {r['name']}")
        else:
            lines.append(f"  [FAIL] {r['name']}")
            if r.get("cmd"):
                lines.append(f"     cmd: {r['cmd']}")
            if r.get("stdout"):
                # 只取前 200 字符避免过长
                out = r["stdout"][:200]
                lines.append(f"     out: {out}")
            if r.get("stderr"):
                err = r["stderr"][:200]
                lines.append(f"     err: {err}")
            if r.get("error"):
                lines.append(f"     err: {r['error']}")
    return "\n".join(lines)


# ── CLI 入口（方便 bash 直接调试）──

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # 从命令行传入 JSON 文件路径
        with open(sys.argv[1], "r") as f:
            checks = json.load(f)
    else:
        # 默认示例
        checks = [
            {"name": "Python 版本", "cmd": "python3 --version", "expected": "3"},
            {"name": "磁盘空间",    "cmd": "df -h /", "expected": "/"},
        ]
    raw = run_checks(checks)
    print(format_report(raw))
