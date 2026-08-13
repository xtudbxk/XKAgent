---
name: makeskill
version: 3.1.0
description: 创建/规范化/结构化技能，生成 skill.md 并支持拆分
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 创建技能
  - 写 skill
  - 规范化
  - 生成 skill
  - makeskill
  - 技能生成器
  - 结构化
  - 简化
  - 拆分
  - 太大
requires: {}
author: system
---

## 概要

makeskill 是一个**元技能**——它不解决具体业务问题，而是生成或优化其他技能。
三种工作模式：

| 模式 | 场景 | 产出 |
|------|------|------|
| **新建** | 描述想法 → 完整 skill | skill.md + 脚本 |
| **规范化** | 修复格式/补充元数据 | 修复后的 skill.md |
| **结构化** | 按访问模式拆分按需加载 | 精简 skill.md + 子文件 |

---

## 🧭 执行导航

按加载时机拆分，严格按导航表执行。

### 📖 文件加载指令

| 时机 | 指令 | 说明 |
|------|------|------|
| **加载技能后** | 无需加载 | 常驻内容在 skill.md 中 |
| **进入 Phase 1 时** | `pythonrt 读取 skills/makeskill/makeskill.phase1.md` | 需求澄清 + 目标模式确认 |
| **Phase 1 → 用户选「新建」后** | `pythonrt 读取 skills/makeskill/makeskill.modes.md` | 新模式详解 + 模式对分类的影响 |
| **Phase 1 → 用户选「规范化」后** | 直接按 phase1 流程执行 | — |
| **Phase 1 → 用户选「结构化」后** | 跳转 Phase 7 | 按 Phase 7 执行 |
| **进入 Phase 2 时** | `pythonrt 读取 skills/makeskill/makeskill.phase2.md` | 分类决策（含模式考量） |
| **进入 Phase 3 时** | `pythonrt 读取 skills/makeskill/makeskill.phase3.md` | 脚本化分析（含模式约束） |
| **进入 Phase 4 时** | `pythonrt 读取 skills/makeskill/makeskill.phase4.md` | 信息收集（含模式适配） |
| **进入 Phase 5 时** | `pythonrt 读取 skills/makeskill/makeskill.phase5.md` | 文件生成（按模式分支） |
| **Phase 5 → 需代码模板时** | `pythonrt 读取 skills/makeskill/makeskill.templates.md` | 代码模板、bash 示例 |
| **Phase 5 → 需脚本细节规则时** | `pythonrt 读取 skills/makeskill/makeskill.rules_detail.md` | 大小阈值、网络、沙箱等细节 |
| **进入 Phase 6 时** | `pythonrt 读取 skills/makeskill/makeskill.phase6.md` | 验证细节 |
| **进入 Phase 7 时** | `pythonrt 读取 skills/makeskill/makeskill.phase7.md` | 结构化拆分细节 |
| **遇到边缘/错误时** | `pythonrt 读取 skills/makeskill/makeskill.faq.md` | 边界条件与错误处理 |

---

## 📋 硬规则（常驻）

1. **不要覆盖已有文件** — 生成前检查，存在则备份/询问
2. **frontmatter 缩进正确** — YAML list 用 `  - value`
3. **辅助脚本不是 category 附属品** — workflow 也可有脚本
4. **工具脚本可独立运行** — 提供 `if __name__` 入口
5. **函数返回字符串** — 复杂数据 JSON 序列化
6. **description ≤ 30 字**
7. **version 初始 1.0.0**
8. **phase 之间汇报进度**
9. **子文件命名** — `<技能名>.<模块名>.md`
10. **目标模式感知** — 根据目标模式注入对应约束

> 📎 脚本细节规则（大小阈值、网络请求、沙箱兼容等）见 `makeskill.rules_detail.md`。

---


### 🖥️ 宿主读取约定（XKAgent 适配）

- 所有子文件一律用 **pythonrt 工具**读取：`open('skills/makeskill/<file>.md', encoding='utf-8').read()` + print
- **不使用 `cat`/bash**（XKAgent 的 LLM 工具集无 bash；bash 仅用户 `!xxx` 或 build-unsafe 宿主侧可用）
- 读取多个子文件时合并到**一次 pythonrt 调用**（循环读取 + 打印），减少往返

### ⚖️ 复杂度分级（新建模式必答）

| 级别 | 判断标准 | 后续路径 |
|------|---------|---------|
| 🪶 轻量 | 单一流程、步骤 ≤ 8、无脚本、单模式 | Phase 1 → 2 → 4 → 5 → 6（跳过 Phase 3 详细脚本化、Phase 7 结构化） |
| 🏋️ 完整 | 多流程、需脚本化、多模式兼容 | 全流程 Phase 1 → 2 → 3 → 4 → 5 → 6 → 7 |
## 文件清单

| 文件 | 状态 | 加载时机 |
|------|------|---------|
| `skill.md` | ✅ 常驻 | 加载后自动读取 |
| `phase1.md` | 📂 按阶段 | Phase 1 |
| `modes.md` | 🔀 分支 | 用户选「新建」后 |
| `phase2.md` | 📂 按阶段 | Phase 2 |
| `phase3.md` | 📂 按阶段 | Phase 3 |
| `phase4.md` | 📂 按阶段 | Phase 4 |
| `phase5.md` | 📂 按阶段 | Phase 5 |
| `templates.md` | 📎 懒加载 | Phase 5 需代码模板时 |
| `rules_detail.md` | 📎 懒加载 | Phase 5 需细节规则时 |
| `phase6.md` | 📂 按阶段 | Phase 6 |
| `phase7.md` | 📂 按阶段 | Phase 7 |
| `faq.md` | 📎 懒加载 | 遇到边缘/错误时 |
