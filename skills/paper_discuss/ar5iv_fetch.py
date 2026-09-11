# deps: stdlib only
"""ar5iv_fetch.py - paper_discuss 深入阶段：ar5iv HTML 抓取（tex 下载失败时的降级数据源）

ar5iv (https://ar5iv.labs.arxiv.org) 提供 arXiv 论文的 HTML 版，
体积远小于 tex 源码（~400KB vs 20MB），沙箱可下载，且含完整参考文献。

用途:
  1. tex 源码下载失败（HTTP 429 / 超时 / 无源码）时，抓 ar5iv 提取正文方法文本
  2. 提取参考文献列表（引用追踪的基础数据）
  3. 提取章节结构（按 section 分段，供深入检索）

Usage (python):
    from skills.paper_discuss.ar5iv_fetch import fetch_paper, extract_references, extract_sections

Usage (CLI):
    python skills/paper_discuss/ar5iv_fetch.py 2607.05465 refs
    python skills/paper_discuss/ar5iv_fetch.py 2607.05465 sections
"""
import re
import sys
import os
import json
import http.client

AR5IV_BASE = "ar5iv.labs.arxiv.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def _http_get(path, timeout=30, max_redirects=5):
    """ar5iv GET，跟随重定向"""
    cur = f"https://{AR5IV_BASE}{path}"
    for _ in range(max_redirects + 1):
        proto, rest = cur.split("://", 1)
        host, _, p = rest.partition("/")
        p = "/" + p if p else "/"
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        try:
            conn.request("GET", p, headers={"Accept": "*/*", "User-Agent": UA})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            if not loc:
                return resp.status, b""
            cur = loc if loc.startswith("http") else f"{proto}://{host}{loc}"
            continue
        return resp.status, body
    return 500, b""


def fetch_paper(arxiv_id, timeout=30):
    """抓取 ar5iv HTML 页面，返回 (status, html_text)"""
    status, body = _http_get(f"/html/{arxiv_id}", timeout=timeout)
    if status != 200:
        return status, ""
    return status, body.decode("utf-8", errors="replace")


def save_html(arxiv_id, library_root="docs/arxiv", timeout=30):
    """抓取 ar5iv HTML 并落盘到论文库 docs/arxiv/{id}/html/{id}.html

    返回 (status, html_path)
    """
    status, text = fetch_paper(arxiv_id, timeout)
    if status != 200:
        return status, ""
    # 落盘到论文库
    html_dir = os.path.join(library_root, arxiv_id, "html")
    os.makedirs(html_dir, exist_ok=True)
    html_path = os.path.join(html_dir, f"{arxiv_id}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(text)
    return status, html_path


def extract_references(arxiv_id, timeout=30):
    """提取参考文献列表（title/authors/year），返回 JSON 字符串

    解析 <li id="bib.xxx" class="ltx_bibitem"> 结构。
    """
    status, text = fetch_paper(arxiv_id, timeout)
    if status != 200:
        return json.dumps({"status": "error", "http": status, "refs": []},
                          ensure_ascii=False)
    refs = []
    for m in re.finditer(r'<li id="bib\.\w+" class="ltx_bibitem[^"]*">(.*?)</li>',
                         text, re.S):
        seg = m.group(1)
        tm = re.search(r'ltx_bib_title">(.*?)</span>', seg, re.S)
        title = re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else ""
        am = re.search(r'ltx_bib_author">(.*?)</span>', seg, re.S)
        authors = re.sub(r"<[^>]+>", "", am.group(1)).strip() if am else ""
        ym = re.search(r'ltx_bib_year">\s*\((\d{4})\)', seg)
        year = ym.group(1) if ym else ""
        if title:
            refs.append({"title": title, "authors": authors[:60], "year": year})
    return json.dumps({"status": "ok", "arxiv_id": arxiv_id,
                       "refs": refs}, ensure_ascii=False, indent=2)


def extract_sections(arxiv_id, timeout=30):
    """提取正文章节结构（section 标题 + 文本），供深入检索

    解析 <section class="ltx_section"> 结构，剥 HTML 标签得纯文本。
    """
    status, text = fetch_paper(arxiv_id, timeout)
    if status != 200:
        return json.dumps({"status": "error", "http": status, "sections": []},
                          ensure_ascii=False)
    sections = []
    # 按 section 切分
    for m in re.finditer(r'<section[^>]*class="ltx_section[^"]*"[^>]*>(.*?)</section>',
                         text, re.S):
        seg = m.group(1)
        # 提取标题
        hm = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', seg, re.S)
        title = re.sub(r"<[^>]+>", "", hm.group(1)).strip() if hm else ""
        # 提取正文（剥所有标签）
        body = re.sub(r"<[^>]+>", " ", seg)
        body = re.sub(r"\s+", " ", body).strip()
        if title and len(body) > 50:
            sections.append({"title": title[:80], "text": body[:2000]})
    return json.dumps({"status": "ok", "arxiv_id": arxiv_id,
                       "sections": sections}, ensure_ascii=False, indent=2)


if __name__ == "__sandbox__":
    # pythonrt 文件执行模式（无参数，默认演示）
    aid = "2607.05465"
    print(extract_references(aid))

if __name__ == "__main__":
    # CLI 模式
    aid = sys.argv[1] if len(sys.argv) > 1 else "2607.05465"
    mode = sys.argv[2] if len(sys.argv) > 2 else "refs"
    if mode == "refs":
        print(extract_references(aid))
    elif mode == "sections":
        print(extract_sections(aid))
    else:
        print(json.dumps({"error": f"unknown mode {mode}, use refs|sections"}))
