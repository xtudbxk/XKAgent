# 新建模式详解

> 🔀 **加载条件**：本文件仅在 Phase 1 用户选择「新建」模式后加载。
> **用途**：为 LLM 提供新建模式的完整示例参考，帮助理解 workflow 与 tool 的区别。

### 新建模式完整流程示例

```
用户: "我想写一个帮我审查 Python 代码质量的技能"

Phase 1 → 技能名 = code_review, 描述 = Python 代码审查, 目标模式 = build
Phase 2 → 主类型 = workflow（核心是审查流程）
Phase 3 → 拆解步骤：
            S1 读文件       → 已有 bash cat，无需工具
            S2 复杂度分析   → 可脚本化 → complexity.py
            S3 反模式检查   → LLM 处理
            S4 命名检查     → 可脚本化（合并到 complexity.py）
            S5 出报告       → LLM 处理
          工具清单: complexity.py（复杂度 + 命名检查）
Phase 4 → 收集元数据 + 工具参数细节 + 模式适配细节
Phase 5 → 生成 3 个文件（按目标模式注入对应约束）
Phase 6 → 验证 + 输出报告
```

### workflow 与 tool 的核心区别

| 维度 | workflow | tool |
|------|----------|------|
| 本质 | 教 LLM **怎么做** | 给 LLM **什么能力** |
| 核心产物 | 流程、步骤、思维框架 | Python 脚本、外部能力 |
| 典型 | check, plan, makeskill | web_search, history_parser |
| skill.md 主体 | 分阶段执行指令 | 工具函数文档 + 使用示例 |
| 脚本角色 | 辅助流程中的某个步骤 | 核心能力载体 |

### 目标运行模式对分类的影响

选择的「目标运行模式」会影响 workflow / tool 的倾向性：

| 目标模式 | workflow 倾向 | tool 倾向 |
|---------|-------------|----------|
| 🔵 **仅 plan** | ✅ 适合（纯流程，脚本存 `/tmp/`） | ⚠️ 受限（不能写 `skills/` 目录） |
| 🟢 **build** | ✅ 标准 workflow | ✅ 标准 tool |
| 🔥 **build-unsafe** | ✅ 可包含系统命令步骤 | ✅ 可调用系统工具 |
| 🔀 **多模式** | ✅ 最灵活（Mode Pre-Check 分支） | ⚠️ 需提供降级策略 |

### 目标运行模式对脚本存储位置的影响

| 目标模式 | 脚本存放位置 | 说明 |
|---------|------------|------|
| 🔵 **仅 plan** | `/tmp/<技能名>_<脚本名>.py` | 项目目录只读，利用 /tmp 临时存储 |
| 🟢 **build** | `skills/<技能名>/<脚本名>.py` | 标准位置 |
| 🔥 **build-unsafe** | `skills/<技能名>/<脚本名>.py` | 标准位置 |
| 🔀 **多模式** | 优先 `skills/`，plan 下 fallback 到 `/tmp/` | 需在 skill.md 中写 fallback 逻辑 |
