## Phase 5: 文件生成

### 5.1 通用规则

- **不要覆盖已有文件**：生成前检查 `skills/<name>/` 下是否存在
  - 规范化模式：备份原文件（`skill.md.bak`）再改写
  - 新建模式且文件已存在：询问用户是否覆盖
- YAML frontmatter 缩进必须正确：list 用 `  - value`（两个空格）
- `---` 单独一行，前后不能有多余空格
- body 与 frontmatter 之间空一行
- 使用 bash heredoc 写入文件，EOF 前加单引号防止变量展开
- **⚠️ 运行时模式与目标模式的区别**：
  - 当前 makeskill 运行的 mode（plan/build/build-unsafe）与你正在生成的技能的目标模式无关
  - 即使当前在 plan 模式，也可以为 build 模式的技能生成内容
  - 但当前模式会影响**写入能力**：plan 模式下只能输出预览

### 5.2 写入 skill.md

```bash
mkdir -p skills/<name>
```

YAML frontmatter 字段规则：

| 字段 | 格式要求 |
|------|---------|
| `name` | 与目录名一致 |
| `version` | MAJOR.MINOR.PATCH，初始 1.0.0 |
| `description` | 30 字以内 |
| `category` | workflow 或 tool |
| `triggers` | YAML list，每行 `  - value` |
| `compatible_modes` | YAML list，如 `  - plan`、`  - build`（**必填**，来自 Phase 1） |
| `requires.pip` | 只出现在有 pip 依赖时 |
| `requires.skills` | 只出现在有技能依赖时 |

### 5.3 写入工具脚本（按需生成）

Phase 3 确定了需要脚本化 → 生成独立的 Python 脚本文件。

**脚本存放位置取决于目标运行模式：**

| 目标模式 | 脚本存放路径 | 说明 |
|---------|------------|------|
| 🔵 **仅 plan** | `/tmp/<技能名>/` | 项目目录只读，利用 /tmp 临时存储 |
| 🟢 **build / build-unsafe** | `skills/<技能名>/` | 标准位置 |
| 🔀 **多模式** | 先 `skills/`，skill.md 中写 fallback 逻辑 | 见下方模板 |

**关键约束：**
- LLM 通过 `pythonrt` 调用：`from skills.<name> import <func>`
  - 注意：plan 模式下脚本在 `/tmp/`，无法通过 `from skills.` 导入，需使用 `importlib` 动态加载
- 网络请求优先使用 Python 标准库（`urllib.request`）
- 工具函数应返回 **字符串**（LLM 收到的是文本），复杂结构返回 JSON
- 每个函数只做一件事

### 5.4 模式自适应生成

根据 Phase 1 中用户选择的「目标运行模式」，决定生成的 skill.md 结构：

#### 情况 A：单模式（如仅 build）

生成的 skill.md **简洁直接**，无模式分支：

```
---
name: <name>
version: 1.0.0
description: ...
category: ...
compatible_modes:
  - build
triggers: [...]
---

## 概述
...

## 执行流程
Phase 1 → Phase 2 → ...
（一个流程到底，无模式分支）
```

#### 情况 B：多模式兼容（如 plan + build + build-unsafe）

生成的 skill.md **需内置「🧭 Mode Pre-Check」阶段**，放在所有 Phase 之前：

```
---
name: <name>
version: 1.0.0
...
compatible_modes:
  - plan
  - build
  - build-unsafe
---

## 🧭 Mode Pre-Check

进入正式流程前，先检测当前运行模式。

### 如何检测模式

从系统信息前缀「模式: xxx」中获取，或通过以下方式判断：

| 检测方式 | 说明 |
|---------|------|
| 检查系统消息前缀 | 最可靠，见用户消息中的「模式: xxx」 |
| 尝试写 /tmp/ | plan 和 build 都可写，无法区分 |
| 检查执行工具可用性 | build-unsafe 下 pythonrt 无限制 |

### 模式策略

| 检测结果 | 行为策略 |
|---------|---------|
| 🔵 **plan** | 脚本存 `/tmp/`，通过 `importlib` 动态加载，不写项目目录，只读操作 |
| 🟢 **build** | 脚本存 `skills/<name>/`，通过 `from skills.<name>` 导入，正常执行 |
| 🔥 **build-unsafe** | 同上，且 Phase 2 可用 pythonrt 无限制执行（或提示用户 !xxx） |

### 脚本加载方式

```python
# plan 模式：从 /tmp/ 动态加载
import importlib.util, sys
spec = importlib.util.spec_from_file_location("module", "/tmp/<name>/<script>.py")
module = importlib.util.module_from_spec(spec)
sys.modules["module"] = module
spec.loader.exec_module(module)
module.func()

# build/build-unsafe 模式：标准导入
from skills.<name>.<script> import func
func()
```
```

### 5.5 需要代码模板？

如果写作时需要生成 bash heredoc 模板或 Python 脚本骨架，请加载：

```
pythonrt 读取 skills/makeskill/makeskill.templates.md
```

### 5.6 文件生成清单

生成完成后，向用户展示清单：

```
━━━ Phase 5: 文件生成清单

技能: <name>
分类: <workflow|tool>
目标模式: <plan | build | build-unsafe | 多模式>
工具: <是（N 个工具脚本） | 否>

待生成文件:
  ✅ skills/<name>/skill.md       — 主定义文件（含模式适配）
  ✅ /tmp/<name>/<script>.py     — 工具脚本（plan 模式路径）
  ✅ skills/<name>/<script>.py   — 工具脚本（build/unsafe 模式路径）
  ✅ ...

是否需要用户确认后写入？ [y/N]
```
