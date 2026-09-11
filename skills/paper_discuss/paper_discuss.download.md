# paper_discuss — 下载落盘阶段（Phase 3）

## Phase 3: 下载候选论文（三级降级链）

```bash
python skills/download_arxiv/download_arxiv.py <arxiv_id> --output-dir docs/arxiv
```

**三级降级链**（tex 失败不再卡死）：

```
① 下载 tex 源码      → 成功: 用 tex_search.py 深入检索
    ↓ 失败 (429/超时/无源码)
② 抓 ar5iv HTML      → 成功: 用 ar5iv_fetch.py 提取方法文本+参考文献
    ↓ 失败 (307/未收录)
③ PDF + 摘要         → 摘要级分析（arxiv API 摘要）
```

- **build**: 正常下载到 docs/arxiv/{id}/tex/unpacked/
- **plan**: 跳过下载，提示用户切 build 后继续

**落盘到论文库**（持久化，供后续讨论复用）：
```python
# tex 源码落盘
python skills/download_arxiv/download_arxiv.py <id> --output-dir docs/arxiv

# ar5iv HTML 落盘（tex 失败 / 大文件时用，~400KB 沙箱可下载）
from skills.paper_discuss.ar5iv_fetch import save_html
status, path = save_html("2607.05465", library_root="docs/arxiv")

# 引用追踪结果落盘
from skills.paper_discuss.refs_search import trace_references
# (trace_references 结果保存到 docs/arxiv/{id}/refs.json)
```

- 大文件 tex（>5MB）易超时 → 直接用 ar5iv（~400KB，沙箱可下载）
- 落盘后论文库被 paper_search 索引，后续讨论直接检索复用
