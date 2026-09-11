---
name: makeskill
version: 3.2.0
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
| **Phase 5 → 需代码模板时** | `pythonrt 读取 skills/makeskill/makeskill.templates.md` | 代码模板、pythonrt 示例 |
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
11. **pythonrt+agent 一体化整理** — 所有步骤与相关脚本尽量通过 pythonrt+agent 一次性整理（合并读取/批处理/内部循环），禁止分步小步调用
12. **脚本必须可被 pythonrt 执行（沙箱双路径）** — 生成的所有脚本必须能通过 pythonrt 执行：
    - 🔵🟢 plan/build：`importlib` 动态加载（实证：沙箱对 `import` 双层封锁——`_ImportGate` 按模块名拒第三方 + 文件搜索层不可见 skills 目录；文件执行时 `__name__`≠`__main__`，`__main__` 块不触发）
    - 🔥 build-unsafe：直接 `from skills.<name>.<script> import <func>` 或文件执行
    脚本不依赖 bash/系统命令；`if __name__ == "__main__"` 仅作宿主侧 CLI 入口，沙箱内不依赖
13. **脚本依赖声明** — 每个生成的脚本头部（docstring/注释）必须声明依赖：`# deps: stdlib only`（纯标准库）或明确第三方库清单；含第三方库时须注明「需 build-unsafe 环境，执行前提示用户切换模式」；Phase 6 核对声明与实际 import 一致
14. **脚本 = 可直接导入执行的 API** — 模块级 docstring 含总览/Deps/Usage；每个公开函数/类 docstring 必含 `Args/Returns/Example` 三段；`__all__` 白名单明确对外 API 面；脚本必须**自包含**（不 import 项目内其他模块，否则沙箱加载时内部 import 仍被拦）
15. **skill.md 嵌入使用示例** — 每个脚本在 skill.md 中必须有对应调用示例（importlib 加载 + 调用 + 预期返回），示例须真实可执行，外部 LLM 照抄即可
16. **Phase 6 用 verify_skill_api.py 校验** — 五项检查：语法 / deps 声明核对 / docstring 完整性 / skill.md 示例一致性 / 冒烟测试，WARN 修复、FAIL 禁止发布
17. **路径约定（相对基准）** — 技能内所有文档/脚本路径一律用「相对技能库根」基准写法 `skills/<name>/<file>`；禁止硬编码环境相关绝对路径（换环境即失效）；LLM 实际加载时按环境映射为 workdir 可解析路径；Phase 6 用 `check_paths` 诊断

18. **技能落盘位置（生成前必问）** — 生成新技能前**必须询问用户落盘位置**：
    - **用户级** `.xkagent/skills/<name>/`：仅当前项目可见，`list_skills` 优先加载；`.xkagent` 全模式只读，需宿主 `!cmd` 复制写入
    - **系统级** `xkagent_v0902/skills/<name>/`：全局所有会话可见，挂载 ro/rw 后可直接写入
    **禁止**默认落盘到工作目录 `skills/`（不在 `_skill_dirs`，不被 `list_skills`/`selectskill`/`searchskill` 索引，等于不可发现）；按用户选择执行落盘，落盘后用 `searchskill` 验证新技能名可被检索
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
| `verify_skill_api.py` | 📎 懒加载 | Phase 6 脚本 API 约定校验（规则 17） |

## 📦 API 速查（verify_skill_api.py）

**加载与调用**（沙箱双路径；build-unsafe 可 `from skills.makeskill.verify_skill_api import verify_skill`）：

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("verify_skill_api", "skills/makeskill/verify_skill_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["verify_skill_api"] = m
spec.loader.exec_module(m)
SRC = open("skills/<name>/<script>.py", encoding="utf-8").read()
MD_SRC = open("skills/<name>/skill.md", encoding="utf-8").read()

# 全量检查（7 项：语法/deps/docstring/md 示例/冒烟/模式适配 + paths 路径诊断），返回 JSON 报告
print(m.verify_skill("skills/<name>", ["<script>.py"], "skills/<name>/skill.md"))

# 路径诊断（规则 18）：检测硬编码绝对路径 + 输出相对路径建议
print(m.check_paths("skills/<name>", ["<script>.py"], "skills/<name>/skill.md"))

# 单项检查（可选）
print(m.check_syntax(SRC, "<script>.py"))                            # 语法
print(m.check_deps(SRC))                                             # 依赖声明核对
print(m.check_docstrings(SRC))                                       # docstring 三段式
print(m.check_md_examples(MD_SRC, ["<func>"], "<script>.py"))        # skill.md 示例一致性
print(m.smoke_test("skills/<name>/<script>.py", ["<func>"]))         # 冒烟测试
print(m.check_mode_fit(SRC))                                         # 模式适配分析
```

**公开 API**：`verify_skill`（主入口）/ `check_syntax` / `check_deps` / `check_docstrings` / `check_md_examples` / `smoke_test` / `check_mode_fit`（模式适配分析：输出 fit/reasons/affected_libs）

> 🧭 **模式感知**：自动读取 skill.md frontmatter 的 `compatible_modes`（或显式 `modes=` 覆盖）。
> 第三方依赖在含 `build-unsafe` 时判定 PASS（需 build-unsafe 运行）；冒烟在第三方不可 import 时返回 SKIP。

