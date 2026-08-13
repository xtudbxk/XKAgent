---
name: plan
version: 2.4.0
description: 制定多步骤执行计划：意图澄清→Smoke Test→验证
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 计划
  - 规划
  - 方案
  - plan
  - 任务分解
  - 执行方案
requires: {}
author: system
---

## 概要

在接到任何非平凡任务时，先通过本流程制定详细计划。核心方法是 **意图澄清 → 逻辑检查 → Smoke Test(可选) → 任务分解 → 细节检查循环 → 复杂度评估与优化 → 输出 Todo List → Final Verification(可选)**，全程维护进度感。

本技能附带两个工具脚本：
- **`check_runner.py`** — 批量执行 bash 检查命令（环境/文件/资源等）
- **`check_syntax.py`**（位于 `skills/check/`）— 零依赖 Python 语法检查

## 核心流程

```
Phase 1: 意图澄清 + 成功标准
     ↓
Phase 2: 方案逻辑检查
     ↓ (FAIL → 否决方案，退回重提)
Phase 2.5: Smoke Test（可选）⚡
     ↓ (阻断性 FAIL → 强制停止)
Phase 3: 任务分解 + 进度锚点
     ↓
Phase 4: 细节检查 + 修复循环 ←─── max 5 轮 ────┐
     ↓                       ↑ (继续循环)       │
     ├── 全部通过 → Phase 5 ────────────────────┘
     ├── blocked → 停止，报告 blocker
     └── 方向变更 → 退回 Phase 2
Phase 5: 复杂度评估 + 性能优化
     ↓
Phase 6: 形成 Todo List
     ↓ (执行 Todo List...)
Phase 7: Final Verification（可选）✅
```

## 文件引用

本技能已拆分为多个子文件，LLM 按需通过 pythonrt 读取：

| 文件 | 内容 | 读取时机 |
|------|------|---------|
| [overview](plan.overview.md) | 工具化分析（各Phase脚本化判断）+ 工具说明 + 适用范围 | 加载技能后阅读 |
| [phase1](plan.phase1.md) | Phase 1 意图澄清详细步骤 + 输出格式 + 规则 | 进入 Phase 1 时 |
| [phase2](plan.phase2.md) | Phase 2 7项逻辑检查完整表格 + 判定规则 + 输出示例 | 进入 Phase 2 时 |
| [smoketest](plan.smoketest.md) | Phase 2.5 Smoke Test 适用判断 + 执行方式 + 检查清单 | 进入 Phase 2.5 时 |
| [phase3](plan.phase3.md) | Phase 3 任务分解步骤 + 标签模板 + 初步复杂度评估 | 进入 Phase 3 时 |
| [phase4](plan.phase4.md) | Phase 4 批量检查工具 + 手动检查模板 + 循环/停止规则 | 进入 Phase 4 时 |
| [phase5](plan.phase5.md) | Phase 5 6维度复杂度评估 + 6种优化策略 + 输出示例 | 进入 Phase 5 时 |
| [phase6](plan.phase6.md) | Phase 6 Todo List 输出格式 + 进度图表 | 进入 Phase 6 时 |
| [phase7](plan.phase7.md) | Phase 7 Final Verification 适用判断 + 执行方式 + 检查模版 | 进入 Phase 7 时 |
| [interaction](plan.interaction.md) | 8阶段交互顺序 + 重要规则 | 加载技能后参考 |
| [rules](plan.rules.md) | 11条硬规则清单 | 加载技能后参考 |

### 外部工具引用

| 工具 | 路径 | 说明 |
|------|------|------|
| check_runner.py | `skills/plan/check_runner.py` | 批量执行 bash 检查命令 |
| check_syntax.py | `skills/check/check_syntax.py` | Python 语法检查（零依赖，兼容 WASM） |

> 💡 使用 pythonrt 读取 `skills/plan/<filename>`（open+print）获取子文件内容。
