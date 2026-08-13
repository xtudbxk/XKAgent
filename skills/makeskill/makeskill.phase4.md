## Phase 4: 信息收集

### 4.1 元数据收集

逐项向用户确认：

| 字段 | 必填 | 说明 | 示例 |
|------|------|------|------|
| name | 是 | 技能名，与目录名一致 | code_review |
| version | 是 | 语义化版本，初始 1.0.0 | 1.0.0 |
| description | 是 | 一句话描述（30 字以内） | 对代码进行自动审查并输出质量报告 |
| category | 是 | workflow 或 tool | workflow |
| triggers | 否 | 触发词列表，用于自动匹配用户意图 | ["审查", "review", "代码质量"] |
| compatible_modes | 是 | 兼容的运行模式列表（来自 Phase 1） | ["plan", "build"] |
| requires.pip | 否 | pip 依赖列表 | ["pylint>=2.17"] |
| requires.skills | 否 | 依赖的其他技能 | [] |
| author | 否 | 作者名 | system 或用户名 |
| prompt_sink | 否 | 该技能承接的 system_prompt 段落说明（提示哪些易变细节应从 prompt 下沉到本技能） | "pythonrt 高频失败模式表" |

> `compatible_modes` 字段将写入生成的 skill.md 的 frontmatter 中，供其他技能/系统识别该技能的运行模式兼容性。

### 4.2 技能逻辑收集

引导用户用自然语言描述技能的**行为逻辑**：

```
这个技能的工作流程是什么？先做什么再做什么最后输出什么？
有什么边界条件、禁忌、特殊情况处理？
输入是什么？输出是什么？
有没有类似 check/plan 的固定模板可以参考？
```

### 4.3 工具细节收集（仅需生成工具时）

对 Phase 3 确定的每个工具脚本，逐个确认：

```
工具 1: complexity.py
  - 脚本: complexity.py（分析圈复杂度）
  - 描述: 分析 Python 代码复杂度，返回 JSON 指标
  - 输入参数:
      file_path: string（必填）— 文件路径
      max_line_length: integer（可选，默认 79）— 最大行宽
  - 输出: 包含复杂度、行数、命名问题的 JSON
  - 依赖包: 无（纯 Python 标准库）
  - 实现逻辑简述: 读文件 → 计算圈复杂度 → 检查命名 → 返回 JSON
```

### 4.4 模式适配细节收集（根据 Phase 1 的目标模式）

如果目标模式为 **多模式兼容**，逐项确认各模式下的行为差异：

| 模式 | 脚本存放 | 文件操作 | 执行方式 |
|------|---------|---------|---------|
| 🔵 **plan** | `/tmp/<技能名>/` | 只读，不写项目目录 | 仅 pythonrt（只读受限） |
| 🟢 **build** | `skills/<技能名>/` | 可读写项目目录 | pythonrt（可写受限） |
| 🔥 **build-unsafe** | `skills/<技能名>/` | 完整文件系统 | pythonrt（无限制） |

#### 适配细节表

对每个可能受限的步骤，明确不同模式下的行为：

```
S2: 复杂度分析

  plan 模式:
    - 脚本路径: /tmp/code_review/complexity.py
    - 行为: 读文件 → 分析 → 打印结果（不持久化）
    - 限制: 不能写入项目目录

  build 模式:
    - 脚本路径: skills/code_review/complexity.py
    - 行为: 读文件 → 分析 → 写入报告到项目目录
    - 限制: 脚本必须兼容 WASM 沙箱

  build-unsafe 模式:
    - 脚本路径: skills/code_review/complexity.py
    - 行为: 同上，可额外调用系统命令
    - 限制: 无
```

### 输出

```
━━━ Phase 4: 信息收集结果

技能名:      code_review
版本:        1.0.0
描述:        对代码进行自动审查并输出质量报告
分类:        workflow
目标模式:    build（单选）
元数据:      compatible_modes: ["build"]

工具:
  1. complexity.py — 复杂度 + 命名检查
     输入: file_path(string)
     输出: JSON

模式适配:
  build 模式: 脚本存 skills/code_review/，pythonrt 调用

是否确认以上信息？
```

用户确认后进入 Phase 5。

---

**规范化模式**下，读取现有 skill.md 的 body 内容和已有的脚本文件，分析现有结构后向用户展示差异。同时检查是否已有 `compatible_modes` 字段，如缺失则补充。

---
