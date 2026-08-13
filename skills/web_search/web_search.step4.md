## 🔄 步骤④ — 循环分析（直到主动叫停）

对每个搜索结果：

1. **总结**当前返回的网页内容，判断是否有价值
2. 如果有重要链接，使用 `wget` 或 `curl` 获取详细网页信息：
   ```bash
   wget -q -O - "<完整URL>" 2>/dev/null
   curl -s "<URL>"
   ```
3. 分析获取到的详细内容，提取关键信息
4. **决定下一步**：
   - ❓ 信息不够 → 继续搜索下一个 query 或切换搜索源
   - 🔗 需要看更多详情 → wget/curl 抓取更多链接
   - 🔄 当前源效果不佳 → 切换源（如 baidu → google，或 semantic_scholar → arxiv）
   - ✅ **信息足够 → 输出【搜索完成】后给出综合报告**

### 源切换建议

| 当前源效果不好 | 可切换至 |
|:-------------:|:--------:|
| baidu 不理想 | google 或 bing |
| google 连不上 | ✅ 自动 fallback 到 bing，无需手工切换 |
| semantic_scholar | arxiv 或 paperswithcode |
| arxiv | semantic_scholar（信息更丰富，含引用数） |
| google 搜不到学术 | semantic_scholar 或 arxiv |
| 搜狗微信不可用 | google site:mp.weixin.qq.com |


### ⏹️ 循环终止条件

当满足以下任一条件时，**必须主动终止循环**：

| 条件 | 说明 |
|:---:|------|
| ✅ 信息充分 | 已收集足够信息回答用户问题 |
| 🔁 覆盖全面 | 所有 query 已搜索完毕且无更多价值 |
| ❌ 连续失败 | 同一 query 重试 2 次仍失败 |

终止时输出：

```
━━━ 【搜索完成】 ━━━
<综合报告>
━━━━━━━━━━━━━━━━━━━
```

不要在搜索完成后再额外搜索或擅自继续循环。LLM 必须主动判断何时停止。
