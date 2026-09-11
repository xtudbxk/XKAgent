# paper_discuss — 深入阶段（Phase 4-5）

## Phase 4: 深入 — 检索论文库（paper_search 索引 + tex_search 兜底）

```python
# ① 优先查独立检索库索引（跨会话复用，快）
from skills.paper_discuss.paper_search import search_library
result = search_library("self-conditioning diffusion", tex_root="docs/arxiv")

# ② 索引无命中时，tex_search 实时遍历兜底
from skills.paper_discuss.tex_search import search_tex
result = search_tex("self-conditioning", tex_root="docs/arxiv",
                    exclude=["2406.02507"])
```

- **paper_search.py（独立检索库）**：
  - 从 codes/search.py 提取 ngram+grep 核心，剥离 codes 依赖，完全独立
  - 支持 `.tex` / `.html` 双类型索引（论文库资产）
  - 自动遍历 docs/arxiv/ 下所有 .tex/.html
  - 支持 exclude / include 过滤
  - 输出 JSON: {status, blocks, query, exclude, include, papers}
- **tex_search.py（兜底）**：实时遍历，剥 LaTeX + 按 section 分段
- 短查询自动补全（>=3 token 守卫）

## Phase 5: 深入 — 读取命中段落综合

- 基于 snippet 优先回复
- 不够 → 直接读命中 tex 文件的对应 section 全文
- 用户追问 → 读 method > experiments > intro 顺序
