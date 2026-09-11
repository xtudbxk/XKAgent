# Phase 2：去重汇总

## 目标
合并所有子 agent 产出，去重生成统一 index.json。

## 去重规则
1. **主键**：arxiv_id（去版本号 vN 后缀）
2. **兜底**：无 ID 的用标题归一化（小写→去标点→去空格）匹配
3. 合并时保留字段：arxiv_id / title / contribution(要点) / subdir(领域) / source / hot_level / heat_sources

## 代码（fetch/dedup 核心逻辑）

```python
import json, re

def norm_id(aid):
    """arxiv_id 去版本号"""
    return re.sub(r'v\d+$', '', aid.strip())

def norm_title(t):
    """标题归一化用于去重"""
    return re.sub(r'[^a-z0-9]', '', t.lower())

def dedup_papers(all_papers):
    """按 arxiv_id 主键 + 标题兜底去重。
    Args: all_papers: 论文 dict 列表（含 arxiv_id/title）。
    Returns: 去重后的论文列表。
    """
    seen_ids, seen_titles, out = set(), set(), []
    for p in all_papers:
        aid = norm_id(p.get('arxiv_id', ''))
        if aid and aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)
        else:
            nt = norm_title(p.get('title', ''))
            if nt in seen_titles:
                continue
            seen_titles.add(nt)
        out.append(p)
    return out
```

## 输出
index.json（含 total_raw/total_dedup/with_arxiv_id/agent_stats/papers）
