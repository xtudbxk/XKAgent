# paper_discuss — 定位阶段（Phase 1-2）

## Phase 1: 定位 — 生成检索关键词

从用户问题提取核心主题，生成 2~3 个检索 query（英文优先，arXiv 覆盖好）。

## Phase 2: 定位 — 多源搜索候选论文

```python
from skills.paper_discuss.arxiv_search import search_candidates
results = search_candidates("diffusion model self-conditioning", max_results=10)
```

**多源策略**（按需组合）：
1. **三源网页搜索**：arxiv.org/search（Relevance 排序）+ papers.cool + cn.bing.com
   - `search_candidates(query, max_results)` 合并去重
2. **arXiv API**（语法已固化，避免踩坑）：
   - 查询格式：`abs:{query}` 空格分隔（`all:"短语"` 有效但多词 AND 无效）
   - 年份过滤：`submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]`
   - 适用：精确主题搜索 + 年份限定
3. **Semantic Scholar**（按引用排序）：
   - 适用：找高影响力论文（`sort=citationCount:desc`）
   - ⚠️ 429 限流频繁 → 需重试退避

- 合并去重（按 arXiv ID），输出 JSON: [{source, arxiv_id, title, summary, url}]
- 选 2~3 篇候选（优先 arXiv 直接命中的）
