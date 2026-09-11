# deps: stdlib only
"""refs_search.py - paper_discuss 引用追踪阶段：发现同领域未收集论文

基于 ar5iv_fetch 提取的参考文献，跨论文去重聚合，识别同领域相关论文。

核心逻辑:
  1. 对每篇核心论文抓 ar5iv 提取参考文献
  2. 跨论文去重（按标题前缀）
  3. 被多篇引用的 = 高置信度同领域
  4. 关键词过滤识别同领域（agent/tool/orchestrat/planning/workflow...）

Usage (python):
    from skills.paper_discuss.refs_search import trace_references
    result = trace_references(["2607.05465", "2605.30248"])

Usage (CLI):
    python skills/paper_discuss/refs_search.py 2607.05465 2605.30248
"""
import re
import sys
import json

from skills.paper_discuss.ar5iv_fetch import extract_references

# 同领域关键词（识别 agent 图像生成/编辑相关引用）
RELEVANT_KW = [
    "agent", "tool", "orchestrat", "planning", "workflow", "instruction",
    "multimodal", "visual program", "image edit", "image generation",
    "text-to-image", "diffusion", "creative", "synthesis", "retrieval",
    "reinforcement", "reward", "code", "svg", "canvas", "context",
]


def _is_relevant(title):
    """判断引用是否属于同领域"""
    t = title.lower()
    return any(kw in t for kw in RELEVANT_KW)


def trace_references(core_ids, timeout=30):
    """追踪核心论文的引用，返回聚合结果 JSON 字符串

    Args:
        core_ids: 核心论文 arXiv ID 列表
    """
    all_refs = {}
    errors = []
    for aid in core_ids:
        try:
            raw = extract_references(aid, timeout)
            data = json.loads(raw)
            if data.get("status") != "ok":
                errors.append({aid: f"HTTP {data.get('http')}"})
                continue
            for r in data.get("refs", []):
                key = r["title"][:50].lower()
                if key not in all_refs:
                    all_refs[key] = {
                        "title": r["title"], "authors": r["authors"],
                        "year": r["year"], "cited_by": [aid]}
                else:
                    if aid not in all_refs[key]["cited_by"]:
                        all_refs[key]["cited_by"].append(aid)
        except Exception as e:
            errors.append({aid: str(e)})

    # 分类: 同领域 vs 基础
    relevant = {k: v for k, v in all_refs.items() if _is_relevant(v["title"])}
    base = {k: v for k, v in all_refs.items() if not _is_relevant(v["title"])}

    # 排序: 被引用次数 > 年份
    def sort_key(item):
        v = item[1]
        year = int(v["year"]) if v["year"] else 0
        return (-len(v["cited_by"]), -year)

    relevant_sorted = sorted(relevant.items(), key=sort_key)
    by_cites = {}
    for k, v in relevant_sorted:
        n = len(v["cited_by"])
        by_cites.setdefault(n, []).append(v)

    return json.dumps({
        "status": "ok" if not errors else "partial",
        "core_papers": core_ids,
        "total_refs": len(all_refs),
        "relevant": len(relevant),
        "base": len(base),
        "errors": errors,
        "top_cited": [v for n in sorted(by_cites.keys(), reverse=True)
                      for v in by_cites[n]][:15],
        "new_papers": [v for k, v in relevant_sorted
                       if len(v["cited_by"]) >= 2][:20],
    }, ensure_ascii=False, indent=2)


if __name__ == "__sandbox__":
    # pythonrt 文件执行模式（默认演示）
    print(trace_references(["2607.05465"]))

if __name__ == "__main__":
    # CLI: python refs_search.py id1 id2 ...
    ids = sys.argv[1:] or ["2607.05465"]
    print(trace_references(ids))
