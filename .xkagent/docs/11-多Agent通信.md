# 11 · 多 Agent 通信（mail 与 callagent）

简体中文 | [English](11-multi-agent-communication.md)

多个会话之间如何互相发信、任务如何“到点唤醒”、一个长任务怎样托管给多个子 Agent？本篇说明 mail 原语与 `callagent` 工具的设计和用法；三条标准流程的操作细节见 `skills/callagent_workflows/`。

## 三个使用场景

| 场景 | 形态 | 入口 |
|---|---|---|
| 定时唤醒 | 给某个会话（含自己）设“时间闹钟”，到点自动唤醒执行信内任务 | `callagent(delay_seconds=…)` / `callagent(deliver_at=…)` |
| 信息交换 | 与一个已有会话一问一答或做数据/状态对齐，结论直接落进对方上下文 | `callagent(need_reply=True)` |
| 长程托管 | 把长任务拆给多个子 Agent，持续巡检、对照验收、发现偏离即纠偏 | 见 `skills/callagent_workflows/longtask_manage.md` |

更完整的适用性判定（五问）与流程模板见 `skills/callagent_workflows/skill.md`。

## mail：全局邮件总线

所有跨会话邮件落在同一个 append-only 文件：默认 `<项目根>/mail.jsonl`（与 `session_registry.json` 同目录），可用 `XKAGENT_MAIL` 环境变量覆盖路径；`XKAGENT_MAIL=off` 时整体禁用、不写信。

- **一行一事件**：`send` 行开启一封邮件，后续 `delivered / done / failed / dead / rejected` 状态行描述其进展；
- **状态机**：`send → delivered → done`；失败记 `failed`（重试上限 2 次）→ 终态 `dead`；主动取消为 `rejected`；
- **字节游标 + 撕裂半行处理**：读取方按字节游标增量消费；写入中断产生的半行会被等待补全，确认无法补全时仅跳过该半行、不牵连之前的完整邮件，也不会卡死；
- **原子锁**：写入经 `mail.jsonl.lockdir` 毫秒级临界区（mkdir 原子互斥，残留锁自动接管）；
- **at-least-once**：进程崩溃/重启后由邮差续扫、孤儿兜底重投，可能发生重复投递——**任务书应按幂等设计**；
- **compact 压实**：总线邮件条目达到 5000 时自动压实归档——已完全终态的邮件整条剔除；仍有在途/待投递收件人的邮件保留（含各收件人最新状态行），避免重放误判与重复投递（长期审计请另行存档）。

## MailPostman：轮次邮差

`AgentManager` 首次启动 Agent 时惰性启动一个 MailPostman 线程，负责把总线上的邮件真正递进目标会话：

- 两阶段投递与 `turn_id` 盯梢，精确判断收信方是否已处理完这一轮；
- 正在跑回合的会话先跳过，空闲后继续投递；同一会话的邮件串行投递（`in_flight` 闸门）；
- 投递失败会冷却重试（`MAIL_MAX_RETRY=2`），重试耗尽转为 `dead` 终态，防止无限重投；
- 投递给“名字合法但未注册”的会话会自动创建（新会话默认 `plan` 模式、无外部挂载）。

## callagent 工具

模型通过 `callagent` 发送邮件（全异步，立即返回）：

| 参数 | 说明 |
|---|---|
| `to` | 收件会话名；传列表 = 广播（单封信多收件人、逐人独立投递/重试；广播=通知型，禁止 `need_reply`）；可为自己（自调度） |
| `message` | 正文，≤3500 字节；**任务书必须自包含**（背景、数据、目标随信附上） |
| `reply_to` | 回信必填 = 对方来信的 `msg_id`（构建可回溯的回复链） |
| `delay_seconds` | 延迟投递秒数；`to=自己` 即自调度循环（定时唤醒） |
| `deliver_at` | 绝对投递时间戳（Unix 秒，精度 1s；与 `delay_seconds` 同时给出时优先） |
| `priority` | 投递优先级（大者先投，默认 0） |
| `provider` | 收信方本次任务的 LLM provider（如 `myprovider` 或 `provider/model:effort`；仅本 turn 生效，不改变会话自身配置） |
| `need_reply` | `true` = 期望回复：指令中注入回信指引（收信方会主动回信）；缺省 `false` = 通知型，不诱导回复 |

快速示例：

```python
# 派发任务并索取回信
callagent(to="worker_a", message="请汇总 X 数据并输出 JSON；完成后回信。", need_reply=True)

# 收到任务后回信（reply_to 填来信的 msg_id）
callagent(to="<来信 from>", message="完成：…", reply_to="<来信 msg_id>")

# 30 分钟后唤醒自己巡检
callagent(to="<当前会话>", delay_seconds=1800, message="巡检任务进度并记录偏差")
```

## 长程与定时任务模式

- **自调度链**：任务会话每完成一步，就 `callagent(to=自己, reply_to=上一步)` 推进下一步；不再回信时链自然终止；
- **检查点幂等**：每步把状态写入任务文件/会话历史；重复投递时先查 `step_id`，已执行则直接回报，不重复执行；
- **并行分片**：主控会话逐个子 Agent 下发任务（扇出），子 Agent 完成后回信主控（以 `reply_to` 聚合）；
- **巡检与纠偏**：托管方定期唤醒自己，对照验收要点检查进度，发现偏离即纠正（见 `longtask_manage.md`）。

应用示例：`skills/paper_collect` 以“多 Agent 并行收集 → 去重汇总 → 翻译 → 生成 HTML 清单”演示了完整的扇出与汇总流程。

## 运维

- `/mail list|get|pending|cancel|send`：查看最近邮件、单封详情、待投递候选、取消未投递邮件、手动发信；
- 常见问题（投递、状态、命名、恢复）见 `skills/callagent_workflows/faq.md`。

## 限制与边界

- 投递依赖邮差轮询（约 1s 粒度）；`delay_seconds` 为相对时间，长时间定时建议用 `deliver_at`（绝对时间）减少漂移；
- `deliver_at` 跨容器使用时存在分钟级时钟偏移风险（需要 NTP 对齐）；
- 邮件语义为 at-least-once：极端场景可能重复或延迟，重要任务请使用幂等 + 检查点协议；
- 单封信正文 ≤3500 字节，大状态请放任务文件，信中只带指针。

## 相关文档

- [06 · Agent 引擎](06-Agent引擎.md)：会话、线程与回合
- [09 · 命令系统](09-命令系统.md)：`/mail` 等命令
- `skills/callagent_workflows/`：三流程完整模板与 FAQ
- `docs/mail_longtask_plan.md`：长程/定时任务设计稿（实现状态见文首）
