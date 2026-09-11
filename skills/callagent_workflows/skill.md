---
name: callagent_workflows
version: 1.0.0
description: callagent 邮件系统的三个核心流程：定时唤醒任意agent / 与现存agent信息交换 / 长程任务托管（含验收对照与偏离检测）
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - callagent
  - 定时唤醒
  - 心跳
  - 自调度
  - 信息交换
  - 长程任务
  - 子agent
  - 广播
  - fanout
  - 巡检
requires: {}
author: xtudbxk
---

## 概述

`callagent_workflows` 定义 **callagent（agent 间全异步邮件）** 的三种核心用法。
系统能力（状态机/重试/投递）见各流程文档；本文件是**入口 + 判定 + 参数速查**。

```
callagent 三流程
├── ① schedule_wake（定时唤醒任意 agent）  — 给时间上闹钟
├── ② info_exchange（与现存 agent 信息交换）— 与有记忆的 agent 说话
└── ③ longtask_manage（长程任务托管）       — 分配+巡检+纠偏的管理循环
```

---

## 🧭 执行导航

| 什么时候用 | 打开哪份 |
|---|---|
| 需要"某 agent 到点醒来做事/巡检/汇报"（自己或别人） | `schedule_wake.md` |
| 需要与一个已有会话做一问一答/数据交换/状态对齐 | `info_exchange.md` |
| 需要把一个长任务切成多份、交给多个子 agent、并持续看管 | `longtask_manage.md` |
| 遇到投递/状态/命名/恢复等具体问题 | `faq.md` |

---

## ⚠️ 适用性判定（先过这 5 问，再决定要不要用 callagent）

1. **需要另一个大脑**（独立上下文/长期记忆）吗？—— 否 → 单会话 pythonrt 直接做
2. **需要并行/异步**（不等结果、多路并发）吗？—— 否 → 单会话工具链
3. **需要时间维度**（定时唤醒/循环/心跳）吗？—— 否 → 不需要流程①
4. **需要持久可靠**（重启不丢、可重试、有证据链）吗？—— 否 → 不需要流程③的长程协议
5. **需要质量隔离**（角色分工/不同模型）吗？—— 否 → 单会话 self-check 更省

> 只用流程③但任务 ≤1 回合 → 过度设计。callagent 的价值在"组织任务"不在"执行任务"。

---

## 📮 参数速查（callagent 工具，9 参数）

| 参数 | 说明 |
|---|---|
| `to` | 必需。str=单收件人；list=广播（单 msg_id、per-to 状态、禁 need_reply） |
| `message` | 必需。≤3500 字节；**任务书必须自包含**（数据/背景/目标随信附上） |
| `reply_to` | 回信必填 = 对方来信的 msg_id（构建可回溯回复链） |
| `delay_seconds` | 延迟投递秒数（到点唤醒收件人） |
| `deliver_at` | 绝对投递时间戳（Unix 秒；定点更稳） |
| `priority` | 优先级（大者优先，默认 0） |
| `provider` | 收信方本次任务 LLM provider（如 my-provider/gpt-5.4:max；`:effort` 被切分透传） |
| `need_reply` | 默认 false=通知型；true 才注入回信指引；**广播禁 true** |

**状态机**：`send → delivered → done`；失败 `failed(retry≤2) → dead`。
广播按收件人独立状态，聚合：全 done→done；任一 dead 且其余全终态→dead；否则按进展。

---

## 🧠 关键机制事实（各流程通用）

- **自动创建**：投递给未注册但名字合法的会话 → `start_agent` 自动创建（只校验名字）；自动创建者默认 **plan 模式、无外部挂载**
- **会话名来源**：优先用户消息元信息「当前会话」字段（自指/收件人）；目标名查 `session_registry.json` 或 mail.jsonl 历史
- **串行闸门**：同会话邮件串行投递（`in_flight`）；会话处理中（`_turn_active`）跳过，空闲后续投
- **重投**：`MAIL_MAX_RETRY=2`、冷却 0.2s，只重投失败收件人
- **心跳保活**：< `IDLE_RECLAIM_SECONDS(300s)` 防空闲回收
- **审计**：mail.jsonl 状态行带时间戳/to/turn_id；`compact()` 在邮件条目 >5000 时压实——已完全终态的邮件剔除（长期审计需另存档）
