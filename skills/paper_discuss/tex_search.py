# deps: stdlib only
"""tex_search.py - paper_discuss 深入阶段：本地 arXiv tex 的 ngram+grep 检索

复用 codes.search 的 search_ngram / search_grep（仅 ngram+grep，不用 embedding）。
前置: config.set_workdir 重定向日志目录到 /tmp（否则 codes._log 写 .xkagent/logs 被只读拦截）。

.tex 不在 search.py 的 ROUTE_TABLE → 本脚本自写 tex 提取器：
  - 剥 LaTeX 命令/注释/数学环境
  - 按 section 分段构造 blocks（meta.text 为纯文本）
  - 短查询自动补全（ngram SHORT_QUERY_TOKENS=2 守卫，需 >=3 token）

Usage (python):
    from skills.paper_discuss.tex_search import search_tex
    result = search_tex("self-conditioning diffusion", tex_root="docs/arxiv")

Usage (CLI):
    python skills/paper_discuss/tex_search.py "query" [tex_root]
"""
import os
import re
import sys
import json

# 前置：把 workdir 重定向到 /tmp，使 codes._log 可写（build 沙箱 .xkagent 只读）
try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = os.environ.get("XKAGENT_BASE") or os.getcwd()
sys.path.insert(0, _REPO_ROOT)
from codes import config  # noqa: E402
config.set_workdir(os.environ.get("PAPER_DISCUSS_TMP", "/tmp/paper_discuss_work"))

from codes.search import search_ngram, search_grep, SearchHit  # noqa: E402

# LaTeX 噪声剥离规则
_TEX_CMDS = re.compile(
    r"\\(?:begin|end)\{[^}]*\}|"      # begin/end 环境
    r"\\[a-zA-Z]+\*?|"                # 命令 \section \ref ...
    r"\\[^\s]|"                      # 特殊命令 \, \%
    r"[{}]|"                            # 花括号
    r"\$[^$]*\$|"                     # 行内数学
    r"\\\[.*?\\\]|"               # 显示数学
    r"%.*$",                            # 注释
    re.M | re.S)
_MULTISPACE = re.compile(r"\s+")
_SECTION_RE = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\s*\{(.*?)\}",
    re.S)


def _tex_to_text(tex):
    """剥离 LaTeX 噪声 → 纯文本"""
    t = _TEX_CMDS.sub(" ", tex)
    t = _MULTISPACE.sub(" ", t)
    return t.strip()


def _split_sections(tex):
    """按 section 切分，返回 [(title, text)]"""
    sections = []
    parts = _SECTION_RE.split(tex)
    # parts[0] = 前言（未进入任何 section）
    if parts and parts[0].strip():
        sections.append(("preamble", _tex_to_text(parts[0])))
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((title, _tex_to_text(body)))
    return sections


def _collect_tex_blocks(tex_root, max_file_bytes=10_000_000):
    """遍历 tex_root 下所有 .tex，构造 SearchHit blocks"""
    blocks = []
    if not os.path.isdir(tex_root):
        return blocks
    for dirpath, dirnames, filenames in os.walk(tex_root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".tex"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > max_file_bytes:
                    continue
                with open(fp, encoding="utf-8", errors="replace") as f:
                    tex = f.read()
            except OSError:
                continue
            # 论文 ID 从路径提取（docs/arxiv/{id}/tex/unpacked/...）
            aid = ""
            parts = os.path.normpath(fp).split(os.sep)
            for p in parts:
                if re.fullmatch(r"\d{4}\.\d{4,5}", p):
                    aid = p
                    break
            for title, text in _split_sections(tex):
                if len(text) < 20:
                    continue
                key = f"{aid or fp}#{title}"
                blocks.append(SearchHit(
                    key=key, scope="tex", channel="semantic", method="",
                    score=0.0, snippet=text[:200],
                    meta={"text": text, "arxiv_id": aid,
                          "section": title, "file": fp}))
    return blocks


def _expand_query(query):
    """短查询补全：ngram 需 >=3 token（SHORT_QUERY_TOKENS=2 守卫）"""
    tokens = [t for t in re.split(r"[\s,，。;；:：]+", query.strip()) if t]
    if len(tokens) >= 3:
        return query
    # 不足 3 个 → 用原词重复拼接（ngram 字符重叠可命中）
    return " ".join(tokens * 3)


def search_tex(query, tex_root="docs/arxiv", top_k=10,
               exclude=None, include=None):
    """对 tex_root 下 tex 做 ngram+grep 检索，按论文分组返回 JSON 字符串

    Args:
        query: 检索关键词（自动补全 >=3 token）
        tex_root: tex 根目录（默认 docs/arxiv）
        top_k: 每论文最多命中数
        exclude: 排除的 arxiv_id 列表（已讨论/已对比的论文）
        include: 只检索这些 arxiv_id（None=全部已下载论文）
    """
    blocks = _collect_tex_blocks(tex_root)
    if not blocks:
        return json.dumps({"status": "empty", "tex_root": tex_root,
                           "blocks": 0, "hits": []}, ensure_ascii=False)

    # 过滤 exclude / include
    if exclude or include:
        ex_set = set(exclude or [])
        inc_set = set(include or [])
        blocks = [b for b in blocks
                  if b.meta.get("arxiv_id")
                  and b.meta["arxiv_id"] not in ex_set
                  and (b.meta["arxiv_id"] in inc_set if include else True)]
        if not blocks:
            return json.dumps({"status": "empty", "tex_root": tex_root,
                               "blocks": 0, "hits": [],
                               "filtered": {"exclude": list(ex_set),
                                            "include": list(inc_set)}},
                              ensure_ascii=False)

    q = _expand_query(query)
    ng = search_ngram(q, blocks, top_k=top_k * 2) or []
    gp = search_grep(query, blocks, top_k=top_k * 2) or []

    # 合并去重（key 相同取高分）
    merged = {}
    for h in ng + gp:
        k = h.key
        if k not in merged or h.score > merged[k].score:
            merged[k] = h

    # 按论文分组
    by_paper = {}
    for h in sorted(merged.values(), key=lambda x: -x.score):
        aid = h.meta.get("arxiv_id") or h.key.split("#")[0]
        by_paper.setdefault(aid, []).append({
            "section": h.meta.get("section", ""),
            "method": h.method, "score": round(float(h.score), 3),
            "snippet": h.snippet[:300],
            "file": h.meta.get("file", "")})
    return json.dumps({"status": "ok", "tex_root": tex_root,
                       "blocks": len(blocks), "query": query,
                       "exclude": list(exclude or []),
                       "include": list(include or []),
                       "papers": by_paper}, ensure_ascii=False, indent=2)


# ── pythonrt 文件执行模式入口 ──────────────────────────────
# __sandbox__ = pythonrt 文件执行模式（无 argv/env 传参）→ 默认参数执行
if __name__ == "__sandbox__":
    q = "diffusion model"   # 默认 query
    root = "docs/arxiv"     # 默认 tex 根
    print(search_tex(q, tex_root=root))
