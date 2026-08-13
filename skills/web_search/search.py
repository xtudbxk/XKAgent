#!/usr/bin/env python3
"""
统一搜索脚本 — 多源搜索（使用 stdlib，兼容 WASM 沙箱 + Host Callbacks）

用法:
    # 方式1: pythonrt 内（build-unsafe 无限制）
    from skills.web_search import search
    results = search.search_baidu("搜索词")

    # 方式2: 用户 !xxx 或宿主直接执行
    python3 skills/web_search/search.py -s baidu "搜索词"

依赖:
    pip install requests  # 可选，仅宿主机调用时需要（HTML 解析用 stdlib html.parser，无需 bs4）
"""


import argparse
import asyncio
import html.parser
import json
import re
import sys
import time
import urllib.parse
# urllib.request 延迟导入（build 沙箱因 tempfile→shutil→posix 链被禁，仅在 build-unsafe 兜底用）
import xml.etree.ElementTree as ET


# ── 常量 ──────────────────────────────────────────────

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
DEFAULT_TIMEOUT = 20
DEFAULT_DELAY = 1.0

# ── 代理配置 ──────────────────────────────────────────
# 部分搜索源（Google/DuckDuckGo/GitHub API/arXiv/Semantic Scholar）需要代理才能访问。
# 优先级: set_proxy() 显式设置 > 环境变量 HTTP_PROXY/HTTPS_PROXY > 无代理直连。
# 国内直连源（百度/搜狗微信/必应 cn）不受影响，即使配置了代理也保持直连。
PROXY_CONFIG = {}  # {"http": url, "https": url}，None/空 = 直连

def set_proxy(http: str | None = None, https: str | None = None):
    """显式设置代理。

    用法:
        search.set_proxy("http://127.0.0.1:7890")          # http/https 同代理
        search.set_proxy("http://127.0.0.1:7890", "http://127.0.0.1:7890")
        search.set_proxy(None)                               # 清除代理（恢复直连）
    """
    global PROXY_CONFIG
    if http is None and https is None:
        PROXY_CONFIG = {}
    else:
        https = https or http
        PROXY_CONFIG = {"http": http, "https": https}

def _load_proxy_from_env():
    """从环境变量读取代理（HTTP_PROXY / HTTPS_PROXY），未显式设置时生效。"""
    global PROXY_CONFIG
    if PROXY_CONFIG:
        return
    import os as _os
    h = _os.environ.get("HTTP_PROXY") or _os.environ.get("http_proxy")
    hs = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy")
    if h or hs:
        PROXY_CONFIG = {"http": h or hs, "https": hs or h}

# 国内直连源（不需要代理，配置代理时也强制直连）
_DIRECT_SOURCES = {"baidu", "weixin_sogou", "bing"}


# ── HTTP 请求封装（urllib，兼容 WASM） ──

_HTTP_REQUEST_CALLBACK = None

def set_http_request_callback(cb):
    """设置 http_request 回调（供 WASM 沙箱内用户手动调用）
    
    注意：回调是 async 函数，需在 async 上下文中使用 await 调用。
    此函数仅存储引用，不自动执行。
    用法：
        search.set_http_request_callback(http_request)
        # 在 async 函数中:  r = await http_request(GET, url)
    """
    global _HTTP_REQUEST_CALLBACK
    _HTTP_REQUEST_CALLBACK = cb

def _fetch(url, params=None, headers=None, method="GET", data=None, timeout=DEFAULT_TIMEOUT,
          source: str = None):
    """统一 HTTP 请求封装。

    三层策略（build 沙箱兼容）:
      1. http_request 回调（WASM 沙箱加速，若已设置）
      2. http.client（build 可用，纯 socket，不依赖 posix）
      3. urllib.request（build-unsafe 兜底，支持 ProxyHandler 代理）

    source 参数: 搜索源名，用于判断是否需要走代理。
      国内直连源（baidu/weixin_sogou/bing）即使配置代理也直连。
    """
    _load_proxy_from_env()
    h = {**DEFAULT_HEADERS, **(headers or {})}
    full_url = url
    if params:
        full_url += '?' + urllib.parse.urlencode(params, doseq=True)
    if data and isinstance(data, str):
        data = data.encode('utf-8')

    # 策略 1: http_request 回调（若已设置）
    if _HTTP_REQUEST_CALLBACK is not None:
        try:
            r = _HTTP_REQUEST_CALLBACK(method=method, url=full_url, headers=h, body=data)
            if r.get('status') in (200, 201):
                return r.get('body', '')
        except Exception:
            pass

    # 策略 2: http.client（build 沙箱可用，不依赖 posix）
    try:
        body = _fetch_http_client(full_url, headers=h, method=method, data=data,
                                  timeout=timeout, source=source)
        if body is not None:
            return body
    except Exception:
        pass

    # 策略 3: urllib.request 兜底（build-unsafe 下可用，支持代理）
    try:
        return _fetch_urllib(full_url, headers=h, method=method, data=data,
                             timeout=timeout, source=source)
    except Exception:
        return None


def _fetch_http_client(url, headers=None, method="GET", data=None, timeout=DEFAULT_TIMEOUT,
                       source: str = None, _redirects: int = 0):
    """http.client 实现（build 沙箱可用，纯 socket；支持 HTTP/HTTPS + 代理隧道 + 重定向跟随）。

    重定向：301/302/303/307/308 最多跟随 5 跳（http.client 不自动处理，需手动）。
    """
    import http.client
    import ssl
    from urllib.parse import urlparse, urljoin

    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    # 代理判断（国内直连源不走代理）
    use_proxy = False
    proxy_host = proxy_port = None
    if PROXY_CONFIG and source not in _DIRECT_SOURCES:
        proxy_url = PROXY_CONFIG.get("https" if scheme == "https" else "http")
        if proxy_url:
            pp = urlparse(proxy_url)
            proxy_host = pp.hostname
            proxy_port = pp.port or (443 if pp.scheme == "https" else 80)
            use_proxy = True

    ctx = ssl.create_default_context()
    if use_proxy:
        if scheme == "https":
            conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=timeout, context=ctx)
            conn.set_tunnel(host, port)
        else:
            conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
    else:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    status = resp.status
    location = resp.getheader("Location")

    # 重定向跟随（GET/HEAD 才安全跟随）
    if status in (301, 302, 303, 307, 308) and location and _redirects < 5             and method in ("GET", "HEAD"):
        new_url = urljoin(url, location)
        conn.close()
        return _fetch_http_client(new_url, headers=headers, method=method, data=data,
                                  timeout=timeout, source=source, _redirects=_redirects + 1)

    body = resp.read()
    conn.close()
    if status in (200, 201):
        return body.decode("utf-8", errors="replace")
    return None


def _fetch_urllib(url, headers=None, method="GET", data=None, timeout=DEFAULT_TIMEOUT,
                  source: str = None):
    """urllib.request 兜底实现（build-unsafe 下可用，支持 ProxyHandler 代理）"""
    import urllib.request  # 延迟导入，避免 build 沙箱 posix 链问题

    proxies = None
    if PROXY_CONFIG and source not in _DIRECT_SOURCES:
        proxies = {"http": PROXY_CONFIG.get("http"), "https": PROXY_CONFIG.get("https")}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    if data:
        req.data = data
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── 工具函数 ──────────────────────────────────────────

def _parse_chinese_ratio(text: str) -> float:
    chinese_chars = len(re.findall(r'[u4e00-\u9fff]', text))
    total = len(text.strip())
    return chinese_chars / total if total > 0 else 0

def _is_chinese(text: str) -> bool:
    return _parse_chinese_ratio(text) > 0.3


# ── 轻量 HTML 解析器（替代 BeautifulSoup） ────────────

class _SimpleFinder(html.parser.HTMLParser):
    """按 (tag, class_pattern) 查找 HTML 元素。

    每个匹配元素返回: {tag, attrs, text(纯文本), raw(原始HTML片段), depth}
    raw 字段供 _find_a_in 二次提取标题/链接（text 已剥离标签无法再解析）。
    """

    def __init__(self):
        super().__init__()
        self._tag_stack = []
        self._raw_stack = []          # 每层元素的起始 offset（与 _tag_stack 同步）
        self._current = None
        self._results = []
        self._target_tag = ""
        self._target_class = None
        self._rawdata_all = ""

    def find(self, html_text, tag, class_re=None):
        self._results = []
        self._target_tag = tag
        self._target_class = re.compile(class_re) if isinstance(class_re, str) else class_re
        self._tag_stack = []
        self._raw_stack = []
        self._current = None
        self._rawdata_all = html_text
        self.feed(html_text)
        return self._results

    def _get_offset(self) -> int:
        """计算当前解析位置在 rawdata 中的字符 offset。"""
        lineno, offset = self.getpos()
        if lineno <= 1:
            return offset
        lines = self._rawdata_all.split('\n')
        return sum(len(l) + 1 for l in lines[:lineno - 1]) + offset

    def _match(self, tag, attrs):
        if tag != self._target_tag:
            return False
        if self._target_class is None:
            return True
        cls = dict(attrs).get('class', '') or ''
        if isinstance(cls, list):
            cls = ' '.join(cls)
        return bool(self._target_class.search(str(cls)))

    def handle_starttag(self, tag, attrs):
        off = self._get_offset()
        self._tag_stack.append(tag)
        self._raw_stack.append(off)
        if self._match(tag, attrs):
            self._current = {'tag': tag, 'attrs': dict(attrs), 'text': '',
                             'raw': '', 'depth': len(self._tag_stack),
                             '_start': off}

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签（如 <meta/>）不参与块匹配，但需同步栈
        self._tag_stack.append(tag)
        self._raw_stack.append(self._get_offset())
        if self._tag_stack and tag == self._tag_stack[-1]:
            self._tag_stack.pop()
            self._raw_stack.pop()

    def handle_data(self, data):
        if self._current is not None:
            self._current['text'] += data

    def handle_endtag(self, tag):
        off = self._get_offset()
        if self._current is not None and tag == self._current['tag'] and len(self._tag_stack) == self._current['depth']:
            self._current['text'] = self._current['text'].strip()
            self._current['raw'] = self._rawdata_all[self._current['_start']:off].strip()
            self._current.pop('_start', None)
            self._results.append(self._current)
            self._current = None
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
            self._raw_stack.pop()


def _find(html_text, tag, class_re=None):
    """查找 HTML 中匹配 (tag, class) 的所有元素"""
    parser = _SimpleFinder()
    return parser.find(html_text, tag, class_re)


def _find_a_in(text, tag='a'):
    """在文本中找第一个 a 标签的文本和 href"""
    els = _find(text, tag)
    for el in els:
        inner = _find(el['text'], 'a')
        if inner:
            return inner[0]['text'], inner[0]['attrs'].get('href', '')
        # 检查自身是否就是 a
        if el['tag'] == 'a':
            return el['text'], el['attrs'].get('href', '')
    return '', ''


def _safe_text(text, maxlen=200):
    return text.strip()[:maxlen] if text else ''

# ── 各源搜索函数 ──────────────────────────────────────

def search_baidu(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """百度搜索"""
    params = {"wd": query, "rn": min(max_results, 50), "ie": "utf-8"}
    html = _fetch("http://www.baidu.com/s", params=params, timeout=timeout, source="baidu")
    if not html:
        return []
    results = []
    for cls in [r'(result|c-container|result-op)', r'(c-content|result_)']:
        divs = _find(html, 'div', cls)
        for dv in divs:
            text = dv.get('text', '')
            title, link = _find_a_in(dv.get('raw') or text, 'h3')
            if not title:
                title, link = _find_a_in(dv.get('raw') or text, 'a')
            if not title:
                continue
            if link and link.startswith('/'):
                link = 'https://www.baidu.com' + link
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results


def search_bing(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """必应搜索"""
    params = {"q": query, "count": min(max_results, 50)}
    html = _fetch("https://www.bing.com/search", params=params, timeout=timeout, source="bing")
    if not html:
        return []
    results = []
    for cls in ['b_algo', 'b_ans']:
        items = _find(html, 'li', cls) or _find(html, 'div', cls)
        for item in items:
            text = item.get('text', '')
            title, link = _find_a_in(item.get('raw') or text, 'h2')
            if not title:
                title, link = _find_a_in(item.get('raw') or text, 'a')
            if not title:
                continue
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results


def search_google(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Google 搜索（失败自动 fallback 到 bing）"""
    params = {"q": query, "num": min(max_results, 50)}
    html = _fetch("https://www.google.com/search", params=params, timeout=timeout, source="google")
    if not html:
        return search_bing(query, max_results, timeout)
    results = []
    for cls in ['g', 'rc']:
        divs = _find(html, 'div', cls)
        for dv in divs:
            text = dv.get('text', '')
            title, link = _find_a_in(dv.get('raw') or text, 'h3')
            if not title:
                continue
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results or search_bing(query, max_results, timeout)


def search_duckduckgo(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """DuckDuckGo 搜索（失败自动 fallback 到 bing）"""
    html = _fetch("https://html.duckduckgo.com/html/", params={"q": query}, timeout=timeout, source="duckduckgo")
    if not html:
        return search_bing(query, max_results, timeout)
    results = []
    for cls in ['result', 'web-result']:
        divs = _find(html, 'div', cls)
        for dv in divs:
            text = dv.get('text', '')
            title, link = _find_a_in(dv.get('raw') or text, 'h2')
            if not title:
                title, link = _find_a_in(dv.get('raw') or text, 'a')
            if not title:
                continue
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results


def search_github(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """GitHub 仓库搜索（REST API）"""
    params = {"q": query, "per_page": min(max_results, 50), "sort": "stars"}
    h = {**DEFAULT_HEADERS, "Accept": "application/vnd.github.v3+json"}
    html = _fetch("https://api.github.com/search/repositories", params=params, headers=h, timeout=timeout, source="github")
    if not html:
        return []
    try:
        data = json.loads(html)
    except json.JSONDecodeError:
        return []
    results = []
    for item in data.get("items", [])[:max_results]:
        name = item.get("full_name", "")
        desc = item.get("description") or ""
        stars = item.get("stargazers_count", 0)
        url = item.get("html_url", "")
        lang = item.get("language") or ""
        summary = f"[{stars}★] [{lang}] {desc}" if lang else f"[{stars}★] {desc}"
        results.append({"title": name, "summary": summary, "url": url})
    return results


def search_arxiv(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """arXiv 学术论文搜索（Atom XML API）"""
    params = {"search_query": f"all:{query}", "max_results": min(max_results, 50),
              "sortBy": "relevance", "sortOrder": "descending"}
    xml_text = _fetch("https://export.arxiv.org/api/query", params=params, timeout=timeout, source="arxiv")
    if not xml_text:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    results = []
    for entry in root.findall("atom:entry", ns)[:max_results]:
        title = (entry.find("atom:title", ns).text or "").strip().replace("n", " ")
        summary_t = (entry.find("atom:summary", ns).text or "").strip().replace("n", " ")
        link = (entry.find("atom:id", ns).text or "").strip()
        published = (entry.find("atom:published", ns).text or "")[:4]
        if len(summary_t) > 300:
            summary_t = summary_t[:300] + "..."
        summary = f"[{published}] {summary_t}" if published else summary_t
        results.append({"title": title, "summary": summary, "url": link})
    return results


def search_semantic_scholar(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Semantic Scholar 学术搜索（REST API）"""
    params = {"query": query, "limit": min(max_results, 50),
              "fields": "title,url,publicationDate,abstract,citationCount"}
    h = {**DEFAULT_HEADERS, "Accept": "application/json"}
    html = _fetch("https://api.semanticscholar.org/graph/v1/paper/search",
                  params=params, headers=h, timeout=timeout, source="semantic_scholar")
    if not html:
        return []
    try:
        data = json.loads(html)
    except json.JSONDecodeError:
        return []
    results = []
    for item in data.get("data", [])[:max_results]:
        title = item.get("title", "")
        abstract = item.get("abstract") or ""
        url = item.get("url", "")
        date = item.get("publicationDate", "")
        citations = item.get("citationCount", 0)
        if len(abstract) > 200:
            abstract = abstract[:200] + "..."
        summary = f"[引用{citations}] [{date}] {abstract}" if date else f"[引用{citations}] {abstract}"
        results.append({"title": title, "summary": summary, "url": url})
    return results


def search_wikipedia(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Wikipedia 百科搜索（REST API）"""
    is_zh = _is_chinese(query)
    base = "https://zh.wikipedia.org/w/api.php" if is_zh else "https://en.wikipedia.org/w/api.php"
    params = {"action": "query", "list": "search", "srsearch": query,
              "srlimit": min(max_results, 50), "format": "json"}
    html = _fetch(base, params=params, timeout=timeout, source="wikipedia")
    if not html:
        return []
    try:
        data = json.loads(html)
    except json.JSONDecodeError:
        return []
    results = []
    for item in data.get("query", {}).get("search", [])[:max_results]:
        title = item.get("title", "")
        snippet = re.sub(r'<[^>]+>', '', item.get("snippet", ""))
        lang = "zh" if is_zh else "en"
        link = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        results.append({"title": title, "summary": snippet, "url": link})
    return results


def search_weixin_sogou(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """搜狗微信搜索"""
    params = {"type": 2, "query": query, "ie": "utf8", "s_from": "input", "_sug_": "n"}
    html = _fetch("https://weixin.sogou.com/weixin", params=params, timeout=timeout, source="weixin_sogou")
    if not html:
        return []
    results = []
    for cls in ['wx-rb', 'wx-rb-item', 'news-list2']:
        divs = _find(html, 'div', cls) or _find(html, 'li', cls)
        for dv in divs:
            text = dv.get('text', '')
            title, link = _find_a_in(dv.get('raw') or text, 'h3') or (None, None)
            if not title:
                title, link = _find_a_in(dv.get('raw') or text, 'h4')
            if not title:
                title, link = _find_a_in(dv.get('raw') or text, 'a')
            if not title:
                continue
            if link and link.startswith('/'):
                link = 'https://weixin.sogou.com' + link
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results



# ── 新增学术 API 源（2026-08-07）──────────────────────

def search_openalex(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """OpenAlex 开放学术图谱搜索（免费 REST API，无需 key）"""
    params = {"search": query, "per-page": min(max_results, 50)}
    h = {**DEFAULT_HEADERS, "Accept": "application/json"}
    text = _fetch("https://api.openalex.org/works", params=params, headers=h,
                  timeout=timeout, source="openalex")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = []
    for w in data.get("results", [])[:max_results]:
        title = w.get("title") or ""
        year = w.get("publication_year")
        cited = w.get("cited_by_count", 0)
        doi = w.get("doi") or ""
        authors = ", ".join(a.get("author", {}).get("display_name", "")
                            for a in (w.get("authorships") or [])[:3])
        abstract_inv = w.get("abstract_inverted_index")
        summary = ""
        if abstract_inv:
            # 逆序索引还原摘要
            pos_map = {}
            for word, poss in abstract_inv.items():
                for p in poss:
                    pos_map[p] = word
            summary = " ".join(pos_map[i] for i in sorted(pos_map))[:200]
        parts = []
        if cited:
            parts.append(f"[引用{cited}]")
        if year:
            parts.append(f"[{year}]")
        if authors:
            parts.append(authors)
        if summary:
            parts.append(summary)
        results.append({"title": title, "summary": " ".join(parts), "url": doi})
    return results


def search_crossref(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Crossref 学术文献元数据搜索（免费 API，DOI 查询）"""
    params = {"query": query, "rows": min(max_results, 50)}
    h = {**DEFAULT_HEADERS, "Accept": "application/json"}
    text = _fetch("https://api.crossref.org/works", params=params, headers=h,
                  timeout=timeout, source="crossref")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = []
    for it in data.get("message", {}).get("items", [])[:max_results]:
        title = (it.get("title") or [""])[0]
        doi = it.get("DOI", "")
        year = it.get("issued", {}).get("date-parts", [[None]])[0][0]
        authors = ", ".join(
            f"{a.get('given','')} {a.get('family','')}".strip()
            for a in (it.get("author") or [])[:3]
        )
        container = (it.get("container-title") or [""])[0]
        parts = []
        if year:
            parts.append(f"[{year}]")
        if container:
            parts.append(container)
        if authors:
            parts.append(authors)
        url = f"https://doi.org/{doi}" if doi else ""
        results.append({"title": title, "summary": " ".join(parts), "url": url})
    return results


def search_dblp(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """DBLP 计算机科学论文搜索（免费 JSON API）"""
    params = {"q": query, "format": "json", "h": min(max_results, 50)}
    h = {**DEFAULT_HEADERS, "Accept": "application/json"}
    text = _fetch("https://dblp.org/search/publ/api", params=params, headers=h,
                  timeout=timeout, source="dblp")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = []
    hits = data.get("result", {}).get("hits", {}).get("hit", [])[:max_results]
    for h_item in hits:
        info = h_item.get("info", {})
        title = info.get("title", "")
        year = info.get("year", "")
        authors = info.get("authors", {}).get("author", [])
        if isinstance(authors, dict):
            authors = [authors]
        author_names = ", ".join(
            a.get("text", "") if isinstance(a, dict) else str(a)
            for a in authors[:3]
        )
        venue = info.get("venue", "")
        url = info.get("ee") or info.get("url", "")
        parts = []
        if year:
            parts.append(f"[{year}]")
        if venue:
            parts.append(venue)
        if author_names:
            parts.append(author_names)
        results.append({"title": title, "summary": " ".join(parts), "url": url})
    return results




# ── 国内 + 代码源（2026-08-07 第二批集成）────────────────

def search_sogou(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """搜狗搜索（国内直连；反爬不稳定，可能返回 0）"""
    params = {"query": query, "ie": "utf8"}
    html = _fetch("https://www.sogou.com/web", params=params, timeout=timeout, source="sogou")
    if not html:
        return []
    results = []
    seen = set()
    # 结果块: <h3 ...><a href=...>标题</a></h3>
    for m in re.finditer(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h3>', html, re.DOTALL):
        link = m.group(1)
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        if link.startswith('/'):
            link = 'https://www.sogou.com' + link
        elif link.startswith('?'):
            link = 'https://www.sogou.com/web' + link
        results.append({"title": title, "summary": "", "url": link})
        if len(results) >= max_results:
            break
    return results



def search_so360(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """360 搜索（国内直连）"""
    params = {"q": query}
    html = _fetch("https://www.so.com/s", params=params, timeout=timeout, source="so360")
    if not html:
        return []
    results = []
    for container in ['res-list', 'result']:
        items = _find(html, 'li', container) or _find(html, 'div', container)
        for item in items:
            raw = item.get('raw') or ''
            text = item.get('text', '')
            title, link = _find_a_in(raw, 'h3') or (None, None)
            if not title:
                title, link = _find_a_in(raw, 'a')
            if not title:
                continue
            if link and link.startswith('/'):
                link = 'https://www.so.com' + link
            summary = text.replace(title, '', 1).strip()[:200]
            results.append({"title": title, "summary": _safe_text(summary), "url": link})
            if len(results) >= max_results:
                break
        if results:
            break
    return results


def search_bilibili(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """B站 视频/专栏搜索（国内直连；JS 渲染不稳定）"""
    params = {"keyword": query}
    html = _fetch("https://search.bilibili.com/all", params=params, timeout=timeout, source="bilibili")
    if not html:
        return []
    results = []
    seen = set()
    # 匹配 title 属性 + 视频链接（两者可能在任意顺序）
    for m in re.finditer(r'title="([^"]{5,120})"[^>]*href="(//www\.bilibili\.com/video/[^"]+)"', html):
        title, url = m.group(1), 'https:' + m.group(2)
        if title in seen:
            continue
        seen.add(title)
        results.append({"title": title, "summary": "[B站视频]", "url": url})
        if len(results) >= max_results:
            break
    if not results:
        for m in re.finditer(r'href="(//www\.bilibili\.com/video/[^"]+)"[^>]*title="([^"]{5,120})"', html):
            url, title = 'https:' + m.group(1), m.group(2)
            if title in seen:
                continue
            seen.add(title)
            results.append({"title": title, "summary": "[B站视频]", "url": url})
            if len(results) >= max_results:
                break
    return results



def search_csdn(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """CSDN 技术博客搜索（JSON API，国内直连）"""
    params = {"q": query, "t": "all", "p": 1, "s": 0, "tm": 0, "lv": -1, "ft": 0, "l": query}
    h = {**DEFAULT_HEADERS, "Accept": "application/json"}
    text = _fetch("https://so.csdn.net/api/v3/search", params=params, headers=h,
                  timeout=timeout, source="csdn")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = []
    for r in (data.get("result_vos") or [])[:max_results]:
        title = re.sub(r"<[^>]+>", "", r.get("title", "")).strip()
        desc = re.sub(r"<[^>]+>", "", r.get("description", "") or "").strip()
        url = r.get("url", "")
        author = r.get("nickname", "")
        parts = []
        if author:
            parts.append('[' + author + ']')
        if desc:
            parts.append(desc[:180])
        results.append({"title": title, "summary": " ".join(parts), "url": url})
    return results


def search_sourcegraph(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Sourcegraph 代码搜索（GraphQL API，需代理）"""
    q_safe = query.replace(chr(34), chr(92) + chr(34))
    gql = "query { search(query: \"" + q_safe + "\", version: V3) { results { matchCount results { __typename ... on FileMatch { file { path url repository { name } } } ... on Repository { name url } } } } }"
    body = json.dumps({"query": gql})
    h = {**DEFAULT_HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    text = _fetch("https://sourcegraph.com/.api/graphql", method="POST", data=body,
                  headers=h, timeout=timeout, source="sourcegraph")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    results = []
    res = data.get("data", {}).get("search", {}).get("results", {})
    match_count = res.get("matchCount", 0)
    for item in (res.get("results") or [])[:max_results]:
        typename = item.get("__typename", "")
        if typename == "FileMatch":
            f = item.get("file", {})
            title = f.get("path", "")
            repo = f.get("repository", {}).get("name", "")
            url = f.get("url", "")
            results.append({"title": repo + ":" + title, "summary": "[代码匹配] " + str(match_count), "url": url})
        elif typename == "Repository":
            results.append({"title": item.get("name", ""), "summary": "[仓库]", "url": item.get("url", "")})
    return results

# ── 源路由 ──────────────────────────────────────────────

SOURCE_NAMES = [
    "baidu", "google", "bing", "duckduckgo",
    "github", "arxiv", "semantic_scholar", "wikipedia", "weixin_sogou",
    "openalex", "crossref", "dblp", "sogou", "so360", "bilibili",
    "csdn", "sourcegraph",
]

SOURCE_FUNCS = {
    "baidu": search_baidu, "bing": search_bing, "google": search_google,
    "duckduckgo": search_duckduckgo, "github": search_github,
    "arxiv": search_arxiv, "semantic_scholar": search_semantic_scholar,
    "wikipedia": search_wikipedia, "weixin_sogou": search_weixin_sogou,
    "openalex": search_openalex, "crossref": search_crossref, "dblp": search_dblp,
    "sogou": search_sogou, "so360": search_so360, "bilibili": search_bilibili,
    "csdn": search_csdn, "sourcegraph": search_sourcegraph,
}

SOURCE_DESCRIPTIONS = {
    "baidu": "中文通用搜索", "bing": "微软必应（国际备选）",
    "google": "Google 搜索（失败→bing）",
    "duckduckgo": "DuckDuckGo（隐私搜索，失败→bing）",
    "github": "GitHub 代码/仓库搜索", "arxiv": "arXiv 学术论文预印本",
    "semantic_scholar": "Semantic Scholar 学术文献",
    "wikipedia": "Wikipedia 百科知识",
    "weixin_sogou": "搜狗微信文章搜索",
    "openalex": "OpenAlex 开放学术图谱", "crossref": "Crossref 文献元数据",
    "dblp": "DBLP 计算机论文",
    "sogou": "搜狗（中文通用）", "so360": "360搜索（中文）",
    "bilibili": "B站视频/专栏", "csdn": "CSDN技术博客",
    "sourcegraph": "Sourcegraph代码搜索",
}


def _try_search(source_name, func, query, max_results, timeout):
    try:
        return func(query, max_results=max_results, timeout=timeout)
    except Exception:
        return []


def search_auto(query: str, max_results: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """智能路由：按内容类型选择最优搜索源"""
    is_zh = _is_chinese(query)
    has_code = bool(re.search(r'github|repo|pip|npm|install|api|sdk|module|library|package',
                               query, re.IGNORECASE))
    has_academic = bool(re.search(r'paper|survey|arxiv|thesis|research|publication|algorithm',
                                   query, re.IGNORECASE))
    if is_zh:
        if has_code:
            sources = ["baidu", "so360", "sogou", "bing", "csdn"]
        elif has_academic:
            sources = ["baidu", "bing", "arxiv", "openalex"]
        else:
            sources = ["baidu", "sogou", "so360", "weixin_sogou", "bing"]
    else:
        if has_code:
            sources = ["github", "sourcegraph", "bing", "csdn"]
        elif has_academic:
            sources = ["arxiv", "semantic_scholar", "openalex", "crossref", "dblp"]
        else:
            sources = ["bing", "google", "sogou", "wikipedia"]
    all_results, seen = [], set()
    for src in sources:
        func = SOURCE_FUNCS.get(src)
        if not func:
            continue
        for r in _try_search(src, func, query, max_results * 2, timeout):
            t = r.get("title", "")
            if t and t not in seen:
                seen.add(t)
                all_results.append(r)
        if len(all_results) >= max_results:
            break
    return all_results[:max_results]
    return all_results[:max_results]


# ── 格式化输出 ─────────────────────────────────────────

def format_output(source: str, query: str, results: list[dict]) -> str:
    lines = [f"━ [{source}] 查询: {query} ━"]
    if not results:
        lines.append("  未找到相关结果")
        return "n".join(lines)
    for i, r in enumerate(results, 1):
        lines.append(f"─── 结果 {i} ───")
        lines.append(f"  标题: {r.get('title', '')}")
        if r.get('summary'):
            lines.append(f"  摘要: {r['summary']}")
        if r.get('url'):
            lines.append(f"  链接: {r['url']}")
        lines.append("")
    return "n".join(lines)


# ── CLI 入口 ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="web_search — 多源搜索工具")
    parser.add_argument("-s", "--source", choices=SOURCE_NAMES + ["auto"],
                        default="auto", help="搜索源 (默认: auto)")
    parser.add_argument("query", nargs="?", help="搜索查询词")
    parser.add_argument("-n", "--max-results", type=int, default=10, help="结果数")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="超时秒数")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="请求间隔秒数")
    parser.add_argument("--list-sources", action="store_true", help="列出可用搜索源")
    args = parser.parse_args()
    if args.list_sources:
        print("可用搜索源:")
        for name in SOURCE_NAMES:
            print(f"  {name:20s} {SOURCE_DESCRIPTIONS.get(name, '')}")
        return
    if not args.query:
        parser.print_usage()
        print("  错误: 需要提供搜索查询词")
        sys.exit(1)
    func = SOURCE_FUNCS.get(args.source) or search_auto
    if args.delay > 0:
        time.sleep(args.delay)
    results = func(args.query, max_results=args.max_results, timeout=args.timeout)
    print(format_output(args.source, args.query, results))


if __name__ == "__main__":
    main()
