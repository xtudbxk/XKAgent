# paper_discuss — 边界与错误处理

| 场景 | 处理 |
|------|------|
| 三源搜索全失败 | 报错，建议检查网络/代理，或用 web_search skill |
| 候选论文无 TeX 源码 | 降级 ar5iv HTML（extract_sections）或 --pdf |
| tex 源码下载超时 | 大文件(>5MB)直接走 ar5iv（~400KB 沙箱可下载） |
| ar5iv 无收录(307) | 降级 PDF + 摘要分析 |
| tex 检索无命中 | 换 query 表达（同义改写），或扩大 tex_root |
| plan 模式 | 搜索+检索可用，下载跳过（提示切 build） |
| 短查询 | 自动补全为 >=3 token（ngram 守卫） |
| 输出目录不存在 | tex_search 返回 status=empty |
| 排除后无论文 | tex_search 返回 status=empty + filtered 信息 |
| 重复下载 | 用讨论状态排除已下载论文（Phase 8） |
| Semantic Scholar 429 | 重试退避（1.5s * attempt），仍失败降级 arXiv API |
| arXiv API 多词查询空 | 改用 abs: 单字段 + 空格分隔（all:"短语" 才有效） |
