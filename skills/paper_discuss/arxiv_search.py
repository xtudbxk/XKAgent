# deps: stdlib only
"""arxiv_search.py - paper_discuss 定位阶段：候选论文搜索（三源合并）

Sources:
  1. arxiv.org/search  (网页搜索, order= 空值=Relevance)
  2. papers.cool       (Cool Papers, div.panel.paper)
  3. cn.bing.com       (必应中国, b_algo)

纯 stdlib（http.client），build/plan 沙箱兼容。
Usage (python):
    from skills.paper_discuss.arxiv_search import search_candidates
    results = search_candidates("diffusion model", max_results=10)

Usage (CLI):
    python skills/paper_discuss/arxiv_search.py "query" [max_results]
"""
import re
import json
import html as html_mod
import http.client
import sys

# 注意: arxiv.org/search 在带 UA 时强制时间序排序、无 UA 时 relevance 生效
# 因此 arxiv 源不传 UA；papers.cool / bing 用浏览器 UA（反爬）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
UA_NONE = None  # 触发 arxiv relevance 排序

ARXIV_ID_RE = r"(\d{4}\.\d{4,5})(v\d+)?"


def _http_get(url, timeout=25, ua=UA):
    """http.client GET, 跟随重定向 (301/302/303/307/308)
    ua=None 时不传 User-Agent（arxiv.org/search 无 UA 时 relevance 排序生效）"""
    cur = url
    for _ in range(5):
        proto, rest = cur.split("://", 1)
        host, _, path = rest.partition("/")
        path = "/" + path if path else "/"
        headers = {"Accept": "*/*"}
        if ua:
            headers["User-Agent"] = ua
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            if not loc:
                return resp.status, cur, body
            cur = loc if loc.startswith("http") else f"{proto}://{host}{loc}"
            continue
        return resp.status, cur, body
    raise RuntimeError(f"too many redirects: {url}")


def _clean(s):
    """去 HTML 标签 + 反转义 + 压缩空白"""
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_arxiv_id(url_or_text):
    m = re.search(ARXIV_ID_RE, url_or_text)
    return m.group(1) if m else ""


def search_arxiv_web(query, max_results=10):
    """arxiv.org/search 网页搜索（order= 空值 → Relevance 排序）"""
    q = query.strip().replace(" ", "+")
    url = (f"https://arxiv.org/search/?query={q}&searchtype=all"
           f"&order=&size=50")
    status, _, body = _http_get(url, ua=UA_NONE)  # 无 UA → relevance 排序
    if status != 200:
        return []
    text = body.decode("utf-8", errors="replace")
    results = []
    for m in re.finditer(r'<li class="arxiv-result">(.*?)</li>', text, re.S):
        seg = m.group(1)
        idm = re.search(r"arxiv\.org/abs/" + ARXIV_ID_RE, seg)
        aid = idm.group(1) if idm else ""
        tm = re.search(r'<p class="title is-5 mathjax">(.*?)</p>', seg, re.S)
        title = _clean(tm.group(1)) if tm else ""
        am = re.search(r'<span class="abstract-short[^"]*"[^>]*>(.*?)</span>',
                       seg, re.S)
        summary = _clean(am.group(1)) if am else ""
        if not summary:
            am = re.search(r'<p class="abstract mathjax">(.*?)</p>', seg, re.S)
            summary = _clean(am.group(1)) if am else ""
        if title:
            results.append({
                "source": "arxiv.org/search", "arxiv_id": aid,
                "title": title, "summary": summary[:300],
                "url": f"https://arxiv.org/abs/{aid}" if aid else ""})
        if len(results) >= max_results:
            break
    return results


def search_papers_cool(query, max_results=10):
    """papers.cool 搜索（div.panel.paper, id=arXiv ID）"""
    url = ("https://papers.cool/arxiv/search?highlight=1&query="
           + query.strip().replace(" ", "+"))
    status, _, body = _http_get(url)
    if status != 200:
        return []
    text = body.decode("utf-8", errors="replace")
    results = []
    parts = re.split(r'<div id="(\d{4}\.\d{4,5})" class="panel paper"', text)
    for i in range(1, len(parts) - 1, 2):
        aid = parts[i]
        seg = parts[i + 1]
        end = seg.find('<div id="')
        if end > 0:
            seg = seg[:end]
        tm = re.search(r'class="title-link[^"]*"[^>]*>(.*?)</a>', seg, re.S)
        title = _clean(tm.group(1)) if tm else ""
        sm = re.search(r'class="summary[^"]*"[^>]*>(.*?)</p>', seg, re.S)
        summary = _clean(sm.group(1)) if sm else ""
        if title:
            results.append({
                "source": "papers.cool", "arxiv_id": aid,
                "title": title, "summary": summary[:300],
                "url": f"https://arxiv.org/abs/{aid}"})
        if len(results) >= max_results:
            break
    return results


def search_bing_cn(query, max_results=10):
    """cn.bing.com 搜索（b_algo + h2 + b_caption）"""
    url = "https://cn.bing.com/search?q=" + query.strip().replace(" ", "+")
    status, _, body = _http_get(url)
    if status != 200:
        return []
    text = body.decode("utf-8", errors="replace")
    results = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', text, re.S):
        seg = m.group(0)
        tm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                       seg, re.S)
        if not tm:
            tm = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if not tm:
            continue
        link, title = tm.group(1), _clean(tm.group(2))
        sm = re.search(r'class="b_caption"[^>]*>(.*?)</p>', seg, re.S)
        summary = _clean(sm.group(1)) if sm else ""
        if not summary:
            sm = re.search(r'<p[^>]*>(.*?)</p>', seg, re.S)
            summary = _clean(sm.group(1)) if sm else ""
        if title:
            results.append({
                "source": "bing", "arxiv_id": _extract_arxiv_id(link + title),
                "title": title, "summary": summary[:300], "url": link})
        if len(results) >= max_results:
            break
    return results


def search_candidates(query, max_results=10, sources=None):
    """三源合并去重（按 arxiv_id / title 前缀）"""
    if sources is None:
        sources = ["arxiv", "papers_cool", "bing"]
    merged = {}
    for src in sources:
        try:
            if src == "arxiv":
                res = search_arxiv_web(query, max_results)
            elif src == "papers_cool":
                res = search_papers_cool(query, max_results)
            else:
                res = search_bing_cn(query, max_results)
        except Exception:
            res = []
        for r in res:
            key = r.get("arxiv_id") or r.get("title", "")[:40]
            if key and key not in merged:
                merged[key] = r
    return list(merged.values())[:max_results]


# ── pythonrt 文件执行模式入口 ──────────────────────────────
# pythonrt 以文件路径执行时 __name__='__sandbox__'（非 __main__），
# 且无 argv/环境变量传参 → 此处判断 __sandbox__ 用默认 query 执行主逻辑。
# 真实使用建议 import 后调用 search_candidates(query, n) 传参。
if __name__ == "__sandbox__":
    q = "image generator"   # 默认 query（文件执行模式无法传参）
    n = 10
    res = search_candidates(q, n)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n# 共 {len(res)} 条候选（query={q!r}）", file=sys.stderr)
