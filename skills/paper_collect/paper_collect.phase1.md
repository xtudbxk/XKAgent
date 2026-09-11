# Phase 1：多 agent 收集（callagent 长程托管）

## 目标
按主题拆分子方向，派发多个子 agent 真并行收集论文。

## 步骤
1. **拆子方向**：按技术维度（如 GAN/diffusion/自回归/3D/视频/热点）拆分，每个子方向一个子 agent
2. **命名**：`<会话名>_subagent_<角色>_<时间戳>`（时间戳如 20260907_192254）
3. **任务书自包含**（≤3500B）：含主题范围、时间窗（如 2026-01-01~09-07）、数据源优先级、
   输出格式（JSON 数组：arxiv_id/title/summary/heat）、回信方式（分片≤3500B）
4. **投递**：callagent(to=子agent名, message=任务书, need_reply=true)
   - 真并行：投不同会话名（in_flight 串行闸门只挡同会话）
5. **回信收集**：子 agent 回信可能分片，主管静默忽略冗余分片，及时确认「停止重发」
6. **超时接管**：>40 分钟无响应 → 创建重试子 agent（同角色新时间戳）接管，原 agent 恢复后合并去重

## 数据源优先级（经验）
- arXiv API（最可靠，带 ID）
- Semantic Scholar（高引，cites 字段）
- GitHub（高星仓库反查论文）
- 顶会反查（CVPR/ICCV/NeurIPS 等 26 个）
- 综述引用挖掘
- 备选：OpenAlex / Crossref / dblp（可能 429/不可达）

## 输出
各子 agent 明细 JSON → 落盘 subagent_reports/
