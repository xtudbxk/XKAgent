# deps: stdlib only
"""paper_search.py - paper_discuss 独立检索库（精简版）

从 codes/search.py 提取 ngram+grep 核心，剥离 codes 依赖，完全独立。
扩展支持 .tex / .html 后缀，索引论文库 docs/arxiv/，跨会话复用。

核心能力:
  1. ngram（近似语义）+ grep（精确）双通道检索
  2. 论文库收集: 遍历 docs/arxiv/ 下 .tex/.html 构造 blocks
  3. 短查询自动补全（>=3 token 守卫）
  4. 支持 exclude/include 过滤

Usage (python):
    from skills.paper_discuss.paper_search import search_library
    result = search_library("self-conditioning diffusion", tex_root="docs/arxiv")

Usage (CLI):
    python skills/paper_discuss/paper_search.py "query" [tex_root]
"""
import os
import re
import sys
import json
import math
import collections
from dataclasses import dataclass, field

# ── 常量（对齐 codes/search.py 精简） ──────────────────────────
C = {
    "NGRAM_MIN_SCORE": 1.0,       # ngram 加权分下限
    "SHORT_QUERY_TOKENS": 2,      # 有效 token <2 → 空查询
    "GREP_CONTEXT": 100,          # grep 上下文总字符数（±50）
    "MAX_FILE_SIZE": 10_000_000,  # >10MB 文件跳过（tex 大文件）
}


@dataclass
class SearchHit:
    """统一搜索结果结构"""
    key: str
    scope: str
    channel: str
    method: str
    score: float
    snippet: str
    meta: dict = field(default_factory=dict)


# ── 工具函数（纯函数，无 codes 依赖） ──────────────────────────

def _tokenize(text: str) -> list:
    tokens = []
    for m in re.finditer(r"[a-zA-Z][a-zA-Z0-9_-]*", text):
        tokens.append(m.group().lower())
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        for ch in seq:
            tokens.append(ch)
        for i in range(len(seq) - 1):
            tokens.append(seq[i:i + 2])
        for i in range(len(seq) - 2):
            tokens.append(seq[i:i + 3])
    return tokens


def _char_ngrams(text: str, n_range: tuple = (2, 4)) -> collections.Counter:
    clean = re.sub(r"\s+", "", text)
    return collections.Counter(
        clean[i:i + n] for n in range(n_range[0], n_range[1] + 1)
        for i in range(len(clean) - n + 1))


def _cosine_counter(a: collections.Counter, b: collections.Counter) -> float:
    keys = set(a) | set(b)
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _grep_match(query: str, text: str):
    idx = text.find(query)
    if idx >= 0:
        return ("exact", idx)
    words = [w for w in re.split(r"[\s,，。;；:：]+", query) if len(w) >= 2]
    for w in words:
        idx = text.find(w)
        if idx >= 0:
            return ("word", idx, w)
    return None


# ── 检索方案（ngram + grep） ──────────────────────────────────

def search_ngram(query: str, blocks: list, top_k: int = 5) -> list:
    q_tokens = collections.Counter(_tokenize(query))
    q_ngrams = _char_ngrams(query)
    if sum(q_tokens.values()) < C["SHORT_QUERY_TOKENS"]:
        return []
    scored = []
    for b in blocks:
        text = b.meta.get("text", "")
        sim = _cosine_counter(q_ngrams, _char_ngrams(text))
        t_tokens = set(_tokenize(text))
        kw = sum(q_tokens.get(tok, 0) for tok in t_tokens if tok in q_tokens)
        score = sim * 20.0 + kw * 2.0
        if score >= C["NGRAM_MIN_SCORE"]:
            scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return [SearchHit(key=b.key, scope=b.scope, channel=b.channel, method="ngram",
                      score=s, snippet=b.meta.get("text", "")[:200], meta=b.meta)
            for s, b in scored[:top_k]]


def search_grep(query: str, blocks: list, context: int | None = None,
                top_k: int = 10) -> list:
    if not query or not query.strip():
        return []
    ctx = C["GREP_CONTEXT"] if context is None else context
    hits = []
    for b in blocks:
        text = b.meta.get("text", "")
        m = _grep_match(query, text)
        if not m:
            continue
        idx = m[1]
        start = max(0, idx - ctx // 2)
        end = min(len(text), idx + len(query) + ctx // 2)
        hits.append(SearchHit(key=b.key, scope=b.scope, channel=b.channel,
                              method="grep", score=1.0, snippet=text[start:end],
                              meta={"match": m[0], "pos": idx}))
    return hits[:top_k]


# ── 论文库提取器（.tex / .html） ──────────────────────────────

# LaTeX 噪声剥离
_TEX_CMDS = re.compile(
    r"\\(?:begin|end)\{[^}]*\}|"
    r"\\[a-zA-Z]+\*?|"
    r"\\[^\s]|"
    r"[{}]|"
    r"\$[^$]*\$|"
    r"\\\[.*?\\\]|"
    r"%.*$",
    re.M | re.S)
_MULTISPACE = re.compile(r"\s+")
_SECTION_RE = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\s*\{(.*?)\}",
    re.S)


def _tex_to_text(tex):
    t = _TEX_CMDS.sub(" ", tex)
    return _MULTISPACE.sub(" ", t).strip()


def _split_sections(tex):
    sections = []
    parts = _SECTION_RE.split(tex)
    if parts and parts[0].strip():
        sections.append(("preamble", _tex_to_text(parts[0])))
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((title, _tex_to_text(body)))
    return sections


def _html_to_text(html):
    """html → 纯文本（剥标签 + 去脚本/样式）"""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    return _MULTISPACE.sub(" ", text).strip()


def _extract_arxiv_id(path):
    parts = os.path.normpath(path).split(os.sep)
    for p in parts:
        if re.fullmatch(r"\d{4}\.\d{4,5}", p):
            return p
    return ""


def collect_library(tex_root="docs/arxiv", max_file_bytes=C["MAX_FILE_SIZE"]):
    """遍历论文库，收集 .tex/.html 构造 SearchHit blocks"""
    blocks = []
    if not os.path.isdir(tex_root):
        return blocks
    for dirpath, dirnames, filenames in os.walk(tex_root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                if os.path.getsize(fp) > max_file_bytes:
                    continue
                with open(fp, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            aid = _extract_arxiv_id(fp)
            if ext == ".tex":
                for title, text in _split_sections(content):
                    if len(text) < 20:
                        continue
                    blocks.append(SearchHit(
                        key=f"{aid or fp}#{title}", scope="papers",
                        channel="semantic", method="", score=0.0,
                        snippet=text[:200],
                        meta={"text": text, "arxiv_id": aid,
                              "section": title, "file": fp, "type": "tex"}))
            elif ext == ".html":
                text = _html_to_text(content)
                if len(text) > 50:
                    blocks.append(SearchHit(
                        key=f"{aid or fp}#html", scope="papers",
                        channel="semantic", method="", score=0.0,
                        snippet=text[:200],
                        meta={"text": text, "arxiv_id": aid,
                              "section": "html", "file": fp, "type": "html"}))
    return blocks


def _expand_query(query):
    tokens = [t for t in re.split(r"[\s,，。;；:：]+", query.strip()) if t]
    if len(tokens) >= 3:
        return query
    return " ".join(tokens * 3)


def search_library(query, tex_root="docs/arxiv", top_k=10,
                   exclude=None, include=None):
    """检索论文库（.tex/.html），按论文分组返回 JSON 字符串"""
    blocks = collect_library(tex_root)
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

    # 合并去重
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
            "type": h.meta.get("type", ""),
            "method": h.method, "score": round(float(h.score), 3),
            "snippet": h.snippet[:300],
            "file": h.meta.get("file", "")})
    return json.dumps({"status": "ok", "tex_root": tex_root,
                       "blocks": len(blocks), "query": query,
                       "exclude": list(exclude or []),
                       "include": list(include or []),
                       "papers": by_paper}, ensure_ascii=False, indent=2)


if __name__ == "__sandbox__":
    # pythonrt 文件执行模式（默认参数）
    print(search_library("diffusion model"))

if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "diffusion model"
    root = sys.argv[2] if len(sys.argv) > 2 else "docs/arxiv"
    print(search_library(q, tex_root=root))
