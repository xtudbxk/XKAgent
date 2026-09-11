# deps: playwright (第三方; 需 build-unsafe 环境，执行前提示用户切换模式)
"""Playwright 网页任务执行器：抓取 / 交互 / 截图 / 登录态 / 重试 / 落盘

用法（pythonrt, build-unsafe）:
    from skills.playwright_web.web_runner import run_fetch, run_actions, save_storage_state
    print(run_fetch(urls=["https://example.com"], output_dir="_playwright_out"))
    print(run_actions(url="https://example.com", actions_json='[{"action":"screenshot"}]'))
    print(save_storage_state("_playwright_out"))
    或文件执行: pythonrt code_or_filepath='skills/playwright_web/web_runner.py' --urls ...
"""
import argparse
import json
import os
import re
import time
from datetime import datetime

DEFAULT_OUTPUT = "_playwright_out"
DEFAULT_TIMEOUT_MS = 30000      # 每页超时 30s
DEFAULT_RETRY = 2               # 失败重试 2 次
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
VALID_ACTIONS = {"goto", "click", "fill", "select", "press", "wait",
                 "screenshot", "extract", "eval", "save_state"}


def _sanitize(name: str, maxlen: int = 60) -> str:
    """URL/名称 → 安全文件名"""
    s = re.sub(r"[^\w\-.]+", "_", name).strip("_")
    return (s[:maxlen] or "page")


def _new_session(storage_state=None, headless=True):
    """启动 headless Chromium，返回 (playwright, browser, context)；失败时 open 可读错误"""
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=storage_state,
            viewport=dict(DEFAULT_VIEWPORT),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        return p, browser, context
    except Exception:
        p.stop()
        raise


def _save_page_artifacts(page, out_dir: str, name: str) -> dict:
    """保存 html / text / summary 片段；截图由调用方按需追加"""
    arts = {}
    try:
        html = page.content()
        html_path = os.path.join(out_dir, f"{name}.html")
        with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(html)
        arts["html"] = html_path
    except Exception as e:
        arts["html_error"] = str(e)
    try:
        text = page.inner_text("body")
        txt_path = os.path.join(out_dir, f"{name}.txt")
        with open(txt_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
        arts["text"] = txt_path
    except Exception as e:
        arts["text_error"] = str(e)
    return arts


def _run_with_retry(fn, retries: int):
    """重试包装：最后一次异常向上抛"""
    last = None
    for i in range(retries + 1):
        try:
            return fn(), None
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    return None, last


def _execute_actions(page, context, actions, out_dir: str) -> list:
    """执行动作序列，返回每步结果日志（含产物记录）"""
    log = []
    for act in actions:
        a = act.get("action")
        if a not in VALID_ACTIONS:
            log.append({"ok": False, "action": a, "error": f"未知动作, 可选: {sorted(VALID_ACTIONS)}"})
            continue
        try:
            if a == "goto":
                page.goto(act["url"], wait_until="domcontentloaded")
            elif a == "click":
                page.click(act["selector"], timeout=act.get("timeout", DEFAULT_TIMEOUT_MS))
            elif a == "fill":
                page.fill(act["selector"], act["value"])
            elif a == "select":
                page.select_option(act["selector"], act.get("value") or act.get("label"))
            elif a == "press":
                page.press(act["selector"], act["key"])
            elif a == "wait":
                page.wait_for_timeout(act.get("ms", 1000))
            elif a == "screenshot":
                name = _sanitize(act.get("name", f"shot_{len(log)}"))
                path = os.path.join(out_dir, f"{name}.png")
                page.screenshot(path=path, full_page=act.get("full_page", False))
                log.append({"ok": True, "action": a, "file": path})
                continue
            elif a == "extract":
                sel = act.get("selector", "body")
                txt = page.inner_text(sel) if sel != "body" else page.inner_text("body")
                log.append({"ok": True, "action": a, "text": txt[:5000]})
                continue
            elif a == "eval":
                result = page.evaluate(act["js"] if "js" in act else act.get("code", ""))
                log.append({"ok": True, "action": a, "result": str(result)[:2000]})
                continue
            elif a == "save_state":
                sp = os.path.join(out_dir, act.get("path", "auth.json"))
                context.storage_state(path=sp)
                log.append({"ok": True, "action": a, "file": sp})
                continue
            log.append({"ok": True, "action": a})
        except Exception as e:
            log.append({"ok": False, "action": a, "error": str(e)[:300]})
    return log


def run_fetch(urls, output_dir=DEFAULT_OUTPUT, timeout_ms=DEFAULT_TIMEOUT_MS,
              retry=DEFAULT_RETRY, storage_state=None, headless=True,
              screenshot_first=False) -> str:
    """批量抓取多个 URL（串行），落盘 html/text/截图，返回 JSON 摘要字符串

    Args:
        urls (list[str]): URL 列表
        output_dir (str): 输出目录（默认 _playwright_out）
        timeout_ms (int|None): 每页超时毫秒（默认 30000）
        retry (int|None): 失败重试次数（默认 2）
        storage_state (str|None): 登录态文件路径（复用 cookie）
        headless (bool|None): 无头模式（默认 True）
        screenshot_first (bool|None): 是否先截图（默认 False）
    Returns:
        str: JSON 摘要（每 URL 的 status/输出文件路径）
    Example:
        m.run_fetch(urls=["https://example.com"], output_dir="_playwright_out")
    """
    if isinstance(urls, str):
        urls = [urls]
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_dir = os.path.join(output_dir, ts)
    os.makedirs(task_dir, exist_ok=True)

    p = browser = context = None
    summary = {"task_dir": task_dir, "items": []}
    try:
        p, browser, context = _new_session(storage_state=storage_state, headless=headless)
        for url in urls:
            name = _sanitize(url)
            t0 = time.time()
            item = {"url": url, "status": "fail", "elapsed_ms": 0}
            try:
                def _go():
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.goto(url, wait_until="domcontentloaded")
                    return page
                page, err = _run_with_retry(_go, retry)
                if err is not None:
                    raise err
                arts = _save_page_artifacts(page, task_dir, name)
                if screenshot_first:
                    arts["screenshot"] = os.path.join(task_dir, f"{name}.png")
                    page.screenshot(path=arts["screenshot"])
                item.update({"status": "ok", "elapsed_ms": int((time.time() - t0) * 1000),
                             "artifacts": arts})
                page.close()
            except Exception as e:
                item["error"] = str(e)[:300]
            summary["items"].append(item)
    finally:
        if browser:
            browser.close()
        if p:
            p.stop()
    with open(os.path.join(task_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return json.dumps(summary, ensure_ascii=False, indent=2)


def run_actions(url, actions_json, output_dir=DEFAULT_OUTPUT, timeout_ms=DEFAULT_TIMEOUT_MS,
                storage_state=None, headless=True) -> str:
    """按动作序列执行交互任务（单页），返回 JSON 摘要字符串

    Args:
        url (str): 目标 URL
        actions_json (str): 动作序列 JSON（goto/click/fill/select/press/wait/screenshot/extract/eval/save_state）
        output_dir (str): 输出目录（默认 _playwright_out）
        timeout_ms (int|None): 页面超时毫秒（默认 30000）
        storage_state (str|None): 登录态文件路径
        headless (bool|None): 无头模式（默认 True）
    Returns:
        str: JSON 摘要（动作结果/截图路径）
    Example:
        m.run_actions(url="https://example.com", actions_json="[{\"action\": \"screenshot\"}]")
    """
    actions = json.loads(actions_json) if isinstance(actions_json, str) else actions_json
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_dir = os.path.join(output_dir, ts)
    os.makedirs(task_dir, exist_ok=True)

    p = browser = None
    summary = {"task_dir": task_dir, "url": url, "actions": []}
    try:
        p, browser, context = _new_session(storage_state=storage_state, headless=headless)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        # 首动作为 goto 可省略 url（保持兼容：actions 里无 goto 则先打开 url）
        if not actions or actions[0].get("action") != "goto":
            page.goto(url, wait_until="domcontentloaded")
        summary["actions"] = _execute_actions(page, context, actions, task_dir)
        summary["artifacts"] = _save_page_artifacts(page, task_dir, "page")
        summary["ok"] = all(a.get("ok") for a in summary["actions"])
    finally:
        if browser:
            browser.close()
        if p:
            p.stop()
    with open(os.path.join(task_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    return json.dumps(summary, ensure_ascii=False, indent=2)


def save_storage_state(output_dir=DEFAULT_OUTPUT) -> str:
    """人工/预登录后保存登录态；或对公开页面保存空 state（供后续复用 cookie 结构）

    Args:
        output_dir (str): 输出目录（默认 _playwright_out）
    Returns:
        str: JSON {"ok": bool, "path": str} 登录态保存结果
    Example:
        m.save_storage_state("_playwright_out")
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "auth.json")
    p = browser = None
    try:
        p, browser, context = _new_session(storage_state=None, headless=True)
        page = context.new_page()
        page.goto("about:blank")
        context.storage_state(path=path)
    finally:
        if browser:
            browser.close()
        if p:
            p.stop()
    return json.dumps({"ok": True, "file": path}, ensure_ascii=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Playwright 网页任务执行器")
    sub = ap.add_subparsers(dest="mode", required=True)

    f = sub.add_parser("fetch", help="批量抓取 URL")
    f.add_argument("--urls", nargs="+", required=True)
    f.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    f.add_argument("--timeout", type=int, default=30)
    f.add_argument("--retry", type=int, default=DEFAULT_RETRY)
    f.add_argument("--storage-state", default=None)
    f.add_argument("--headed", action="store_true", help="非无头（需 DISPLAY）")
    f.add_argument("--screenshot", action="store_true", help="首屏截图")

    a = sub.add_parser("actions", help="动作序列交互")
    a.add_argument("--url", required=True)
    a.add_argument("--actions-json", required=True)
    a.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    a.add_argument("--storage-state", default=None)

    args = ap.parse_args()
    if args.mode == "fetch":
        print(run_fetch(args.urls, args.output_dir, args.timeout * 1000, args.retry,
                        args.storage_state, not args.headed, args.screenshot))
    else:
        print(run_actions(args.url, args.actions_json, args.output_dir,
                          storage_state=args.storage_state))
