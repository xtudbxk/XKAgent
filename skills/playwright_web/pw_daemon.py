# deps: playwright (第三方; 需 build-unsafe 环境) + 同技能 web_runner(act 动作复用) + stdlib(subprocess/urllib/signal/socket)
"""Playwright CDP 常驻守护（v2）：跨 pythonrt 调用持久化同一浏览器实例。

总览:
    通过 Popen(start_new_session=True) 以脱离进程树方式启动 headless Chromium
    （--remote-debugging-port 开 CDP），pythonrt 每次调用是全新进程，但浏览器
    常驻存活；后续调用 connect_over_cdp 连回同一实例，页面/内存态/登录态全保留。
    状态目录 /tmp/pw_daemon/：daemon.json（pid/port）+ profile/（user_data_dir，
    重启保登录态）+ chrome.log + out/（动作产物）。
    与 v1 web_runner 的关系：v1 每任务新启浏览器（一次性）；v2 常驻跨 turn 复用，
    act() 复用 v1 的动作语义（goto/click/fill/select/press/wait/screenshot/
    extract/eval/save_state），行为一致。
Deps:
    playwright（第三方，需 build-unsafe）+ stdlib（json/os/signal/subprocess/
    time/urllib.request/socket）
Usage:
    # build-unsafe 下直接 import（技能仅 build-unsafe 可用）
    from skills.playwright_web import pw_daemon as d
    d.start()                       # 启动常驻 Chromium（幂等）
    d.open_page("https://example.com")
    # ---- 新的 pythonrt 调用（新进程）----
    d.list_pages()                  # 仍能看到上一次开的页面
    d.act("example.com", '[{"action":"screenshot","name":"shot"}]')
    d.stop()                        # 收尾关闭
"""
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request

__all__ = ["STATE_DIR", "DAEMON_JSON", "PROFILE_DIR", "LOG_FILE",
           "start", "status", "connect", "list_pages", "open_page",
           "act", "stop", "restart"]

STATE_DIR = "/tmp/pw_daemon"
DAEMON_JSON = os.path.join(STATE_DIR, "daemon.json")
PROFILE_DIR = os.path.join(STATE_DIR, "profile")
LOG_FILE = os.path.join(STATE_DIR, "chrome.log")
DEFAULT_PORT = 9222
READY_TIMEOUT = 20  # 秒，CDP 就绪等待


# ---------- 内部工具 ----------

def _chromium_exe():
    """取 playwright 管理的 chromium 可执行文件路径（内部）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def _http_ok(url, timeout=2):
    """HTTP GET 是否 200（内部）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _cdp_alive(port):
    """CDP /json/version 是否可达（内部）。"""
    return _http_ok("http://127.0.0.1:%d/json/version" % port, timeout=1.5)


def _pid_alive(pid):
    """进程是否存活（内部）。"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _free_port(begin):
    """从 begin 起找一个可用 TCP 端口（内部）。"""
    port = begin
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("找不到可用端口(从 %d 起)" % begin)


def _load_state():
    """读 daemon.json，损坏则视为无状态（内部）。"""
    try:
        with open(DAEMON_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    """原子写 daemon.json（内部）。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = DAEMON_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DAEMON_JSON)


def _clear_state():
    """清 daemon.json（保留 profile 以便重启复用登录态）（内部）。"""
    try:
        os.remove(DAEMON_JSON)
    except OSError:
        pass


# ---------- 公开 API ----------

def status():
    """健康检查：进程存活 + CDP 可达。

    Args:
        (无参数)
    Returns:
        dict: {"running": bool, "pid": int, "port": int, "pid_alive": bool,
               "cdp_ok": bool, "started": float}
    Example:
        status()  # -> {"running": True, "pid": 123, "port": 9222, ...}
    """
    st = _load_state()
    if not st:
        return {"running": False, "pid": None, "port": None,
                "pid_alive": False, "cdp_ok": False}
    pid, port = st.get("pid"), st.get("port")
    pid_ok = bool(pid and _pid_alive(pid))
    cdp_ok = bool(port and _cdp_alive(port))
    return {"running": pid_ok and cdp_ok, "pid": pid, "port": port,
            "pid_alive": pid_ok, "cdp_ok": cdp_ok, "started": st.get("started")}


def start(headless=True, port=DEFAULT_PORT, profile_dir=None, extra_args=None):
    """启动常驻 Chromium（幂等：已在运行则直接返回现状）。

    Args:
        headless: 无头模式（默认 True；False 需要显示器环境）
        port: CDP 端口（被占时自动向后找空闲端口）
        profile_dir: user_data_dir（默认 /tmp/pw_daemon/profile，重启保登录态）
        extra_args: 附加 chromium 启动参数 list[str]
    Returns:
        dict: {"running": True, "pid", "port", "started", "already": bool}
    Raises:
        RuntimeError: chromium 启动即退出或 CDP 就绪超时（附日志路径）
    Example:
        start()                       # -> {"running": True, "pid": ..., "port": 9222}
        start()                       # 幂等，already=True
    """
    cur = status()
    if cur.get("running"):
        return {**cur, "already": True}
    if _cdp_alive(port):  # 端口被其他 chromium 占用（孤儿实例）→ 换端口
        port = _free_port(port + 1)
    exe = _chromium_exe()
    prof = profile_dir or PROFILE_DIR
    os.makedirs(prof, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    args = [exe,
            "--remote-debugging-port=%d" % port,
            "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=%s" % prof,
            "--no-sandbox",              # root 环境必须
            "--disable-dev-shm-usage",   # 容器 /dev/shm 受限时必须
            "--disable-gpu",
            "--no-first-run",
            "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    if extra_args:
        args.extend(extra_args)
    with open(LOG_FILE, "ab") as logf:
        proc = subprocess.Popen(args, stdout=logf, stderr=logf,
                                start_new_session=True)  # 脱离进程树，活过本调用
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if _cdp_alive(port):
            break
        if proc.poll() is not None:
            raise RuntimeError("chromium 启动即退出 code=%s，日志: %s"
                               % (proc.returncode, LOG_FILE))
        time.sleep(0.3)
    else:
        try:
            proc.kill()
        except OSError:
            pass
        raise RuntimeError("CDP 就绪超时(%ss)，日志: %s" % (READY_TIMEOUT, LOG_FILE))
    st = {"pid": proc.pid, "port": port, "started": time.time(),
          "profile": prof, "headless": headless}
    _save_state(st)
    return {**st, "running": True, "already": False}


def connect():
    """连接常驻实例，返回 (playwright, browser)；调用方 finally 中 browser.close()+p.stop()。

    注意：connect_over_cdp 场景下 browser.close() 只断开本连接，不杀浏览器进程。

    Args:
        (无参数)
    Returns:
        tuple: (playwright, Browser)——turn 内使用，勿跨调用持有
    Raises:
        RuntimeError: daemon 未运行
    Example:
        p, browser = connect()
        try:
            page = browser.contexts[0].pages[0]
            print(page.title())
        finally:
            browser.close(); p.stop()
    """
    st = status()
    if not st.get("running"):
        raise RuntimeError("daemon 未运行: %s（先 start()）" % st)
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % st["port"])
        return p, browser
    except Exception:
        p.stop()
        raise


def list_pages():
    """列出常驻实例全部页面（跨 turn 找回的入口）。

    Args:
        (无参数)
    Returns:
        list[dict]: [{"context": i, "index": j, "url": str, "title": str}]
    Example:
        list_pages()  # -> [{"context": 0, "index": 0, "url": "https://example.com", ...}]
    """
    p, browser = connect()
    try:
        pages = []
        for ci, ctx in enumerate(browser.contexts):
            for pi, pg in enumerate(ctx.pages):
                try:
                    title = pg.title()
                except Exception:
                    title = ""
                pages.append({"context": ci, "index": pi,
                              "url": pg.url, "title": title})
        return pages
    finally:
        browser.close()
        p.stop()


def open_page(url, wait="domcontentloaded", timeout_ms=30000):
    """在常驻实例中新开页面并导航（页面跨 turn 存活）。

    Args:
        url: 目标地址
        wait: goto 等待策略（domcontentloaded/load/networkidle）
        timeout_ms: 导航超时毫秒
    Returns:
        dict: {"url", "title", "total_pages"}
    Example:
        open_page("https://example.com")  # -> {"url": "...", "title": "...", "total_pages": 2}
    """
    p, browser = connect()
    try:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        pg = ctx.new_page()
        pg.goto(url, wait_until=wait, timeout=timeout_ms)
        try:
            title = pg.title()
        except Exception:
            title = ""
        return {"url": pg.url, "title": title, "total_pages": len(ctx.pages)}
    finally:
        browser.close()
        p.stop()


def _find_page(browser, page):
    """按 page 参数定位页面：None=最后活动页 / int=全局序号 / str=url 子串（内部）。"""
    all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
    if not all_pages:
        raise RuntimeError("常驻实例无页面（先 open_page）")
    if page is None:
        return all_pages[-1]
    if isinstance(page, int):
        return all_pages[page]
    if isinstance(page, str):
        for pg in all_pages:
            if page in pg.url:
                return pg
        raise RuntimeError("未找到 url 含 %r 的页面，现有: %s"
                           % (page, [pg.url for pg in all_pages]))
    raise ValueError("page 参数须为 None/int/str(url子串)，收到 %r" % (page,))


def act(page=None, actions_json="[]", output_dir=None, timeout_ms=30000):
    """对常驻实例的指定页面执行 v1 动作序列（复用 web_runner._execute_actions）。

    Args:
        page: 页面定位——None=最后活动页 / int=全局序号 / str=url 子串
        actions_json: 动作序列 JSON 字符串或 list（语义同 v1 web_runner）
        output_dir: 产物目录（默认 /tmp/pw_daemon/out/<ts>）
        timeout_ms: 单动作默认超时
    Returns:
        dict: {"out_dir", "url", "log": [每步结果]}
    Example:
        act("example.com", '[{"action":"screenshot","name":"shot"}]')
    """
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import web_runner
    p, browser = connect()
    try:
        target = _find_page(browser, page)
        actions = (json.loads(actions_json) if isinstance(actions_json, str)
                   else actions_json)
        out = output_dir or os.path.join(STATE_DIR, "out",
                                         time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(out, exist_ok=True)
        log = web_runner._execute_actions(target, browser.contexts[0] if browser.contexts else None,
                                          actions, out)
        return {"out_dir": out, "url": target.url, "log": log}
    finally:
        browser.close()
        p.stop()


def stop(force=False, timeout=10):
    """停止常驻实例（SIGTERM 优雅退出，超时 SIGKILL；profile 保留）。

    Args:
        force: True 直接 SIGKILL
        timeout: 优雅退出等待秒数
    Returns:
        dict: {"running": False, "stopped": bool, "reason": str}
    Example:
        stop()  # -> {"running": False, "stopped": True, "reason": ""}
    """
    st = _load_state()
    if not st or not st.get("pid"):
        return {"running": False, "stopped": False, "reason": "无状态记录"}
    pid = st["pid"]
    if not _pid_alive(pid):
        _clear_state()
        return {"running": False, "stopped": False, "reason": "进程已退出"}
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.3)
    else:
        if not force:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(1)
    _clear_state()
    return {"running": False, "stopped": not _pid_alive(pid), "reason": ""}


def restart(headless=True, **kw):
    """重启常驻实例（profile 保留 → 登录态不丢）。

    Args:
        headless: 同 start；kw: 透传 start 其余参数
    Returns:
        dict: start() 结果
    Example:
        restart()  # -> {"running": True, ...}
    """
    stop()
    return start(headless=headless, **kw)
