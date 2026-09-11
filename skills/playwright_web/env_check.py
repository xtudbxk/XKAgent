# deps: playwright (第三方; 延迟导入检测; 需 build-unsafe 环境，执行前提示用户切换模式)
"""Playwright 环境探测：Python 版本 / playwright 安装 / Chromium 就位

用法（pythonrt）:
    from skills.playwright_web.env_check import check_env
    print(check_env())
    或文件执行: pythonrt code_or_filepath='skills/playwright_web/env_check.py'
"""
import json
import os
import platform
import sys


def check_env() -> str:
    """返回 JSON 字符串：就绪状态 + 各检查项 + 修复指引

    Args:
        (无参数)
    Returns:
        str: JSON {"ready": bool, "checks": [{"item","ok","detail"}], "fix": [...]} 环境探测结果
    Example:
        m.check_env()  # -> {"ready": false, "checks": [...], "fix": ["pip install playwright"]}
    """
    result = {"ready": False, "checks": [], "fix": []}

    # 1) Python 版本
    py_ver = sys.version_info
    result["checks"].append({
        "item": "python_version", "ok": py_ver >= (3, 10),
        "detail": f"{platform.python_version()} (需 >=3.10)"
    })

    # 2) playwright 安装
    try:
        import playwright  # noqa: F401
        pw_installed = True
        pw_detail = f"playwright {getattr(playwright, '__version__', '?')}"
    except ImportError:
        pw_installed = False
        pw_detail = "未安装"
        result["fix"].append("pip install playwright")
    result["checks"].append({"item": "playwright_pkg", "ok": pw_installed, "detail": pw_detail})

    # 3) Chromium 浏览器就位
    if pw_installed:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                ep = p.chromium.executable_path
                if hasattr(p.chromium, 'executable_path'):
                    ep = p.chromium.executable_path
                browser_ok = bool(ep)
                detail = f"chromium path: {ep}"
        except Exception as e:
            browser_ok = False
            detail = f"检测失败: {e}"
            result["fix"].append("playwright install chromium  (root 下可加 playwright install-deps chromium)")
        result["checks"].append({"item": "chromium_bin", "ok": browser_ok, "detail": detail})
    else:
        result["checks"].append({"item": "chromium_bin", "ok": False, "detail": "未检测（playwright 未安装）"})

    result["ready"] = all(c["ok"] for c in result["checks"])
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print(check_env())
