---
name: scheduled_task
version: 1.2.0
description: 定时/条件触发任务调度，满足条件后执行用户命令
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 定时
  - 等待
  - 调度
  - 循环
  - 定时任务
  - schedule
  - timer
  - wait
  - cron
requires: {}
author: xtudbxk
---

## 概述

`scheduled_task` 是一个**三阶段条件调度工作流**，让 LLM 从被动响应变为主动等待执行。
支持 plan / build / build-unsafe 三种运行模式，自适应执行策略。

```
Mode Pre-Check ──→ Phase 0 ──→ Phase 1 ──→ Phase 2
      │               │            │            │
      │ 检测当前      │ 与用户     │ LLM生成    │ 条件满足
      │ 运行模式      │ 交互确认   │ 判断脚本   │ LLM理解并
      │ 选择策略      │ 条件+命令  │ +循环等待  │ 执行命令
```

---

## 🧭 执行导航

按加载时机拆分，严格按导航表执行。

### 📖 文件加载指令

| 时机 | 指令 | 说明 |
|------|------|------|
| **加载技能后** | 无需加载 | 常驻内容在 skill.md 中 |
| **进入工作流前** | `pythonrt 读取 skills/scheduled_task/scheduled_task.modeprecheck.md` | 模式检测 + 策略选择 |
| **进入 Phase 0 时** | `pythonrt 读取 skills/scheduled_task/scheduled_task.phase0.md` | 交互确认步骤 |
| **进入 Phase 1 时** | `pythonrt 读取 skills/scheduled_task/scheduled_task.phase1.md` | 循环等待步骤 |
| **进入 Phase 2 时** | `pythonrt 读取 skills/scheduled_task/scheduled_task.phase2.md` | 命令执行步骤 |
| **遇到边缘/错误时** | `pythonrt 读取 skills/scheduled_task/scheduled_task.faq.md` | 边界条件与错误处理 |

---

## 📋 硬规则（常驻）

1. **条件脚本必须** — `check_condition() -> bool`，仅标准库，无副作用
2. **不设超时** — 会一直等待直到条件满足或被用户中断
3. **Python 兼容** — 所有脚本通过 `pythonrt` 工具（统一运行时）执行
4. **轮询间隔 ≥ 2 秒** — 避免 CPU 空转
5. **模式感知** — 进入工作流前必须先执行 Mode Pre-Check，根据结果选择策略

---

## 📦 文件清单

| 文件 | 说明 | 加载时机 |
|------|------|---------|
| `skill.md` | ✅ 常驻 — 本文件 | 加载技能后自动读取 |
| `scheduled_task.modeprecheck.md` | 🧭 入口 | 进入工作流前 |
| `scheduled_task.phase0.md` | 📂 按阶段 | 进入 Phase 0 时 |
| `scheduled_task.phase1.md` | 📂 按阶段 | 进入 Phase 1 时 |
| `scheduled_task.phase2.md` | 📂 按阶段 | 进入 Phase 2 时 |
| `scheduled_task.faq.md` | 📎 懒加载 | 遇到边缘/错误时 |
| `scheduled_task_waiter.py` | 🐍 工具脚本 | Phase 1 中 python 调用 |
