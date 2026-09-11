# paper_discuss — 脚本加载方式（pythonrt 直接执行）

```bash
# 方式1: pythonrt 文件执行（code_or_filepath 传 .py 路径，直接跑）
#   ⚠️ 文件执行模式 __name__='__sandbox__'，无 argv/env 传参 → 用默认 query
#   真实使用建议 import 传参（方式2）
python skills/paper_discuss/arxiv_search.py          # 默认 query="image generator", 10 条
python skills/paper_discuss/tex_search.py            # 默认 query="diffusion model", root=docs/arxiv

# 方式2: pythonrt 内 import（推荐，可编程组合 + 传参）
from skills.paper_discuss.arxiv_search import search_candidates
from skills.paper_discuss.tex_search import search_tex
results = search_candidates("diffusion model", max_results=10)   # 传 query + 数量
result = search_tex("self-conditioning", tex_root="docs/arxiv") # 传 query + tex 根

# 方式3: CLI（宿主/unsafe 直接 python 执行）
python skills/paper_discuss/arxiv_search.py "query" 10
python skills/paper_discuss/tex_search.py "query" docs/arxiv
```

> ⚠️ **pythonrt 文件执行模式**：`__name__='__sandbox__'`（非 `__main__`），无 argv/env 传参。
> 脚本用 `if __name__ == "__sandbox__"` 判断并执行默认参数主逻辑；真实场景用 import 方式传参。
