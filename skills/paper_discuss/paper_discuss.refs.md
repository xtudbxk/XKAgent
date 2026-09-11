# paper_discuss — 引用追踪阶段（Phase 7）

## Phase 7: 引用追踪 — 发现同领域未收集论文

```python
from skills.paper_discuss.refs_search import trace_references
# 对已深入分析的论文做引用追踪
result = trace_references(["2607.05465", "2605.30248", "2602.09084"])
# 输出: 全部引用 → 按被引用次数排序 → 识别同领域未收集论文
```

- **数据源**：ar5iv HTML 参考文献（tex 下载失败也能用，~400KB 沙箱可下载）
- **核心逻辑**：
  - 跨论文去重（按标题前缀）
  - 被多篇引用的 = 高置信度同领域
  - 关键词过滤（agent/tool/orchestrat/planning/workflow...）识别同领域
- **产出**：`new_papers` 列表（同领域未收集论文）→ 喂回下一轮 Phase 2 定位
- **状态更新**：把追踪过的论文记入 `ref_traced`，避免重复追踪
