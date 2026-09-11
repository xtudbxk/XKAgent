> **实现状态（2026-09-03 更新）**：本方案 §3B/§6 ① 已实现（callagent 增加 deliver_at 参数，commit 5aefa82）；
> priority 排序、compact 压实（阈值 5000 条）、chain 回复链查询亦已实现；fanout 现阶段用多次 callagent
>（字段级留二期）；跨容器 = XKAGENT_MAIL 指向共享盘 + hostname 已含于消息 id（env 路径机制已验证）。
> 端到端闭环演练（ping→pong→done-ack 3/3 done）通过；单测 36 项全 PASS；深度熔断（XKAGENT_MAIL_MAX_DEPTH）尚未实现。

# 基于 mail 原语的长程任务与定时任务方案（2026-09-03，v2.3.1 之后拟稿）

## 0. 背景与目标
mail v2.3.1 已交付：全局总线 + 轮次邮差 + callagent（全异步、延迟投递、自调度）。
本方案回答：如何在其上实现 **长程任务**（多步骤/长时间/可恢复）与 **定时任务**（周期/定点触发）。
原则：不改 mail 内核（v2.3.1 冻结），只在**信封协议层 + 任务会话层**新增约定与少量工具参数。

## 1. 已有能力盘点（不做也能用的部分）
| 能力 | 载体 | 说明 |
|------|------|------|
| 定点触发 | send 行 `deliver_at`（绝对秒）+ postman 到期投递 | 1s 精度；❌ callagent 现仅暴露 delay_seconds（相对）——需补 deliver_at 参数（一行）或经由调度 agent 换算 |
| 周期唤醒 | callagent(to=自己, delay_seconds=N) 自调度 | 每轮一排信；轮间隔 ≥1s；会话消息历史即"任务日志" |
| 长 turn | TURN_TIMEOUT 二级保险语义（心跳新鲜→信任等待） | 单 turn 长跑不误杀（需用户在途信号配合） |
| 崩溃恢复 | postman 重启续扫 + 孤儿兜底重投 + 会话重启上下文恢复 | at-least-once：任务侧必须幂等 |
| 跨会话协作 | callagent 双向 + reply_to 链（对话线） | 主从/平行任务的基础 |

## 2. 长程任务架构（推荐：任务会话自持状态 + 自调度链）
```
[触发] 用户/定时 → 任务信封（邮件 body=约定 JSON）→ 任务会话 T（专用，如 t_<taskid>）
T 每步：读任务状态（会话历史/任务文件）→ 执行一步 → 落检查点 → callagent(to=T, delay=0, reply_to=上一步)
链止条件：目标达成 → callagent 不回（链自然断）；深度熔断（XKAGENT_MAIL_MAX_DEPTH）尚未实现，规划中
```
- **任务信封协议**（body JSON，一行 <3500B；无法承载大状态→状态放任务文件/msgz，信只带指针+任务 id）：
  `{"task_id":"t_<ts>_<rand>","kind":"step|done|abort|query","step":N,"ctx_key":"sess_path","payload":{...}}`
- **状态持久化**（三选一，推荐②）：
  ① 任务会话 msgz 历史（run_stream 每轮落库，天然检查点；大状态查历史慢）
  ② 任务状态文件 `<workdir>/task_<id>.json`（每步原子写，快；推荐）
  ③ 专用 orchestrator 会话内存态（崩了丢——不推荐单独用）
- **幂等**：step_id 写入任务文件；重投（at-least-once）时查 step 已执行→直接回复"已执行"（重复知情不重跑）。工具副作用（写文件/调 API）须在同一 step 内可重入。
- **并行分片**（fanout 二期钩子前的手工版）：orchestrator 会话 O + worker 会话 `_subagent_<task>_<i>`（validate_session_name 白名单兼容下划线）；
  O 逐 worker callagent（信封带 sub task）→ 每 worker 完成后 callagent(to=O, reply_to=任务信) 汇报 → O 收齐（按 reply_to 聚合）→ 下一步。
  限制：无 fanout 字段（二期），O 串行下发；worker 数多时用 delay_seconds 错峰。
- **恢复路径**：T 崩溃 → postman 重投（失败重试链）→ 会话重启（session db 恢复上下文）→ 读任务文件检查点 → 从 step N 续跑；T 长期不再应答 → 超时/熔断 → 任务标记 failed（信封 abort 回执 or dead 终态）。

## 3. 定时任务（两种范式）
- **A 轮询型（现成，零改动）**：`callagent(to=任务会话, delay_seconds=周期)` 自调度——简单定时（周期 ≥2s 稳）；漂移累积（每轮 1s 轮询误差），分钟级 ok，秒级不保证。
- **B 精确型（补一个参数）**：callagent 增加 `deliver_at`（绝对时间戳）参数（信封字段已有，exec 加一行透传）——定点精确到 1s；周期任务由最后一轮自调度计算下一次 deliver_at（无漂移）。
- **C cron 表（远期，二期钩子 + 宿主）**：调度表放任务会话状态文件（cron 列表），会话醒来后计算下一批 deliver_at 排队信——纯 mail 实现，无需宿主 scheduler；与"宿主级 scheduler"（改动 web/repl，违背零改动精神）对比后**否决**。
- 知情：deliver_at 跨容器时钟偏移分钟级（v2.3.1 知情取舍）；跨容器定时需 XKAGENT_MAIL 共享盘 + NTP。

## 4. 推荐路由
1. **立即可用**：长程链（§2）+ 轮询定时（§3A）——零内核改动，仅任务会话约定
2. **小改（~10 行）**：callagent 增加 `deliver_at` 参数（§3B）+ 任务信封示例放 docs
3. **二期**：fanout/priority 字段 → 并行分片原生支持；corr 恢复；跨容器调度
4. **防乒乓（部分规划中）**：XKAGENT_MAIL_MAX_DEPTH 深度熔断尚未实现；当前请在任务侧自控链深（如任务文件记录 step 数，超阈值即中止）

## 5. 与 v2.3.1 的咬合（均已实现，无冲突）
- 自调度 to=自己：postman 认领无在途+无用户 turn 即投递 → 会话被唤醒 round 推进 ✅
- 长 turn 信任等待：心跳新鲜→不超时误杀 ✅（长任务 turn 可达 10min+）
- 重复投递知情：任务侧 step 幂等消化 ✅
- 深度钩子：❌ 未实现（XKAGENT_MAIL_MAX_DEPTH 为规划名；当前以任务侧链深自控兜底）

## 6. 实施清单（如立项）
① callagent 加 deliver_at 参数（tools.py exec + schema + system_prompt，~10 行）
② 任务信封协议示例 + 任务会话模板（docs/ 或 skills/）
③ 长程链演示：一个任务 agent 自调度 3 步（单测级模拟，无需 LLM）
④ 幂等检查点示例（任务文件原子写 + step 去重）
