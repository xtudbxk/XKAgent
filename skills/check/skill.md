---
name: check
version: 2.3.0
description: 对代码/方案/配置进行全面检查，输出健康度报告
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 检查
  - 审查
  - check
  - 诊断
  - 验证
  - 健康度
  - 审计
requires: {}
author: system
---

## 概要

对代码、方案或配置进行系统和全面的检查，输出结构化健康度报告。核心是 **语法检查 → 逻辑检查 → 细节检查 → 输出报告** 四阶段流程。

本技能附带语法检查工具 `check_syntax.py`（基于 `ast.parse`，零依赖，兼容 WASM Python）。

## 核心流程

```
Phase 1: 逻辑检查   — 9 个维度评估方案/代码的合理性
Phase 2: 细节检查   — 五层顺序检查（语法/环境/文件/配置/运行）
Phase 3: 输出报告   — 格式化健康度报告 + 修复建议
```

## 文件引用

| 文件 | 内容 | 读取时机 |
|------|------|---------|
| [overview](check.overview.md) | 适用范围 + 核心原则 + 整体结构 | 加载技能后 |
| [phase1](check.phase1.md) | Phase 1 9 维度逻辑检查 | 进入 Phase 1 |
| [phase2](check.phase2.md) | Phase 2 五层细节检查（最大块，含 L0 语法检查） | 进入 Phase 2 |
| [phase3](check.phase3.md) | Phase 3 报告格式 + 示例 | 进入 Phase 3 |
| [interaction](check.interaction.md) | 交互模式 + 硬规则 + 心态 | 加载技能后参考 |

### 外部工具

| 工具 | 路径 | 说明 |
|------|------|------|
| check_syntax.py | `skills/check/check_syntax.py` | Python 语法检查（`ast.parse`，零依赖） |

> 💡 使用 pythonrt 读取 `skills/check/<filename>`（open+print）获取子文件内容。
