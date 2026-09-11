## Phase 3: 脚本化分析（识别可脚本化的子任务）

这一步是 makeskill 最关键的步骤。**无论 workflow 还是 tool 类型，都要做**。

### 3.1 把技能分解成原子步骤

先让用户描述技能的完整流程，LLM 将其拆解为**原子步骤**。

示例——用户说"我想写一个代码审查技能"：

```
步骤拆解：
  S1: 读取用户指定的代码文件
  S2: 分析代码复杂度（圈复杂度、行数）
  S3: 检查常见反模式（过长函数、魔法数字等）
  S4: 检查命名规范
  S5: 生成审查报告
```

### 3.2 逐步骤判断：能否脚本化？

对每个步骤，用以下标准判断。**同时必须考虑 Phase 1 确定的「目标运行模式」**：

| 判断维度 | 适合脚本化 | 不适合脚本化（留 LLM 做） |
|----------|-----------|------------------------|
| 操作性质 | 重复性操作、规则明确 | 需要理解语义、上下文判断 |
| 数据来源 | 需要读文件、调 API、查网络 | 纯推理、归纳、总结 |
| 执行方式 | 有确定算法/公式 | 需要创造性、审美判断 |
| 输出格式 | 结构化数据、格式化文本 | 开放式分析、建议 |
| 🔵 **plan 模式限制** | 不可写 `skills/` 目录 → 脚本存 `/tmp/`，仅做计算/读取 | 需要写持久化文件 → 需降级为预览 |
| 🟢 **build 模式限制** | 脚本必须兼容 python WASM 沙箱 | 需系统调用 → 不可用 |
| 🔥 **build-unsafe 限制** | 无限制，pythonrt 全能力 | — |

用表格形式输出给用户确认：

```
步骤分解与脚本化分析：

  S1: 读取用户指定的代码文件
      判断: 需要读文件系统 → 适合脚本化
      方案: 用 pythonrt 读取文件内容（open+print），无需额外脚本

  S2: 分析代码复杂度
      判断: 规则明确（计算圈复杂度） → 适合脚本化
      方案: 编写 python 脚本 complexity.py，通过 pythonrt 调用

  S3: 检查常见反模式
      判断: 需要语义理解 → 留 LLM 处理
      方案: 不脚本化

  S4: 检查命名规范
      判断: 正则规则明确 → 适合脚本化
      方案: 合并到 complexity.py，或单独命名检查脚本

  S5: 生成审查报告
      判断: 需要综合判断 → 留 LLM 处理
      方案: 不脚本化

工具清单：
  1. complexity.py — 分析代码复杂度 + 命名规范检查
     输入: 文件路径
     输出: JSON 格式的复杂度指标和命名问题列表

模式适配:
  🔵 plan:     脚本存 /tmp/，仅读文件分析，不写项目目录
  🟢 build:    脚本存 skills/code_review/，正常执行
  🔥 unsafe:   同上，可额外执行系统命令
```

### 3.3 两类产物的判定

```
┌─────────────────────────────────────────────────────────┐
│  经过脚本化分析后，一个技能可能包含：                      │
│                                                         │
│  A) skill.md（必有）— 给 LLM 的流程指令                   │
│  B) 工具脚本（可能需要）— Python 实现文件                    │
│  C) 工具脚本（可能需要）— Python 实现文件                  │
└─────────────────────────────────────────────────────────┘

组合情况：
  ┌─ 只有 A                  → 纯 workflow（如 check、plan）
  ├─ A + B + C（有脚本）     → workflow 带工具（如带复杂度分析的 code_review）
  └─ A + B + C（有脚本）     → tool 类型（如 web_search）
```

### 3.4 输出

向用户展示：

```
━━━ Phase 3: 脚本化分析结果

步骤分解: 5 步
可脚本化: 2 步 → 1 个脚本 (complexity.py)
LLM 处理: 3 步（反模式检查、报告生成）

模式适配:
  plan 模式: S2 脚本化 → 脚本存 /tmp/
  build 模式: S2 脚本化 → 脚本存 skills/<name>/

是否需要生成脚本文件？ → 是

是否确认以上分析？
```

用户确认后进入 Phase 4。

### 3.5 pythonrt 一体化约束（硬性要求）

结合 makeskill 硬规则 11/12，Phase 3 的脚本化方案必须同时满足：

**约束 1：生成的脚本必须能直接在 pythonrt 中运行（沙箱双路径）**
- 每个脚本都要能通过 pythonrt 执行：
  - 🔵🟢 plan/build：`importlib` 动态加载（`spec_from_file_location` 直接文件加载，绕过 _ImportGate；沙箱禁 import 项目内模块）
  - 🔥 build-unsafe：`from skills.<name>.<script> import func; func()`（或文件执行）
- 脚本优先使用 pythonrt 沙箱内可用能力（stdlib 白名单）；若确需 bash/系统命令或第三方库，须在脚本头部注明所需依赖/命令，并提示用户切换 build-unsafe 模式后再执行（依赖声明格式见 makeskill.rules_detail.md 规则 14）
- 函数返回字符串（复杂数据 JSON 序列化），`__main__` 入口 `print()` 输出

**约束 2：多步骤尽量合并为一个 pythonrt 统一脚本（内部可调用 agent）**
- 能脚本化的步骤合并到同一脚本（脚本内顺序执行/循环/批处理），禁止分步小步调用
- 需要语义理解/判断的中间环节 → 同一整理单元内调 agent 工具（pythonrt 产出 → agent 回传 JSON → 脚本继续）
- 整体执行单元 = 一个 pythonrt 脚本 + 其中 LLM 通过 agent 处理语义环节，不拆多轮

#### 示例 1：单脚本可直接在 pythonrt 运行

场景：code_review 技能「复杂度分析」脚本

```python
# skills/code_review/complexity.py
"""分析 Python 代码复杂度，返回 JSON 字符串

Deps:
    stdlib only
"""
import json
import ast


def analyze(file_path: str) -> str:
    """读文件 → 统计函数数/行数 → 返回 JSON 字符串"""
    with open(file_path, encoding='utf-8', errors='replace') as fh:
        src = fh.read()
    tree = ast.parse(src)
    funcs = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    result = {"file": file_path, "lines": len(src.splitlines()), "functions": funcs}
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    print(analyze(sys.argv[1] if len(sys.argv) > 1 else "target.py"))
```

pythonrt 两种调用方式（生成方案时需写明，沙箱双路径见 rules_detail 规则 13）：

```python
# 方式1: 🔵🟢 plan/build 沙箱 — importlib 动态加载（沙箱禁 import 项目内模块）
import importlib.util, sys
spec = importlib.util.spec_from_file_location("complexity", "skills/code_review/complexity.py")
m = importlib.util.module_from_spec(spec); sys.modules["complexity"] = m
spec.loader.exec_module(m)
print(m.analyze("target.py"))

# 方式2: 🔥 build-unsafe — 直接 import（或文件执行走 __main__ 入口）
from skills.code_review.complexity import analyze
print(analyze("target.py"))
```
#### 示例 2：多步骤合并为一个 pythonrt 统一脚本（内部可调用 agent）

场景：code_review 技能全流程（读文件 → 复杂度分析 → 语义审查 → 报告）

```python
# skills/code_review/pipeline.py
"""code_review 全流程统一脚本：读文件 + 复杂度分析 + 汇总
语义审查环节由 LLM 在同一整理单元内调 agent 完成

Deps:
    stdlib only
"""
import json
import ast


def analyze_and_prepare(file_path: str) -> str:
    """步骤1+2: 读文件 + 复杂度分析（脚本内顺序完成，一次调用）"""
    with open(file_path, encoding='utf-8', errors='replace') as fh:
        src = fh.read()
    tree = ast.parse(src)
    funcs = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    metrics = {"file": file_path, "lines": len(src.splitlines()), "functions": funcs}
    return json.dumps(metrics, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    print(analyze_and_prepare(sys.argv[1] if len(sys.argv) > 1 else "target.py"))
```

LLM 侧执行（**一个整理单元完成，不拆小步**）：

```python
# ① 一个 pythonrt：读文件 + 复杂度分析 + 输出待审查上下文
#    🔵🟢 沙箱：importlib 动态加载（build-unsafe 可直接 from skills.code_review.pipeline import analyze_and_prepare）
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pipeline", "skills/code_review/pipeline.py")
m = importlib.util.module_from_spec(spec); sys.modules["pipeline"] = m
spec.loader.exec_module(m)
metrics = m.analyze_and_prepare("target.py")    # 返回 JSON 字符串

# ② 同一整理单元内：反模式语义审查 → 调 agent 工具
#    agent(prompt="基于以下代码上下文分析反模式...", tools=["pythonrt"])
#    → agent 回传 JSON 审查结果

# ③ 脚本内汇总：合并 metrics + agent 审查结果 → 最终报告
#    （可复用同一脚本的汇总函数或第二个 pythonrt 收尾）
```

> ✅ 校验标准：生成的每个脚本都能单独被 pythonrt 执行（约束 1）；
> 完整流程可用「一个 pythonrt + 内部 agent」跑通（约束 2），无分步小步调用。


---
