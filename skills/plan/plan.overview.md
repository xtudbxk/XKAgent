## 概要

在接到任何非平凡任务时，先通过本流程制定详细计划。核心方法是**意图澄清 → 逻辑检查 → Smoke Test(可选) → 任务分解 → 细节检查循环 → 复杂度评估与优化 → 输出 Todo List → Final Verification(可选)**，全程维护进度感。

本技能附带两个工具脚本，分别用于不同场景：
- **`check_runner.py`** — 批量执行 bash 检查命令（环境/文件/资源等）
- **`check_syntax.py`** — 零依赖 Python 语法检查（基于 `ast.parse`）

---

## 工具化分析

### 设计思路

按 makeskill 的工具化分析方法，对 plan 的 8 个阶段逐步骤判断：

| 阶段 | 工作内容 | 是否脚本化 | 理由 |
|------|---------|-----------|------|
| Phase 1 意图澄清 | 复述需求、界定范围、定义成功标准 | ❌ LLM 处理 | 语义理解、归纳推理，无法算法化 |
| Phase 2 逻辑检查 | 7 项逻辑判断（目标对齐/因果链等） | ❌ LLM 处理 | 推理链评估、假设识别，需要上下文理解 |
| Phase 2.5 Smoke Test | 快速验证外部环境/资源/硬件是否就绪 | **✅ 脚本化** | 检查命令可枚举、<60s 批量执行、返回值可判定 |
| Phase 3 任务分解 | 拆分子任务、打标签、算权重 | ⚠️ 部分辅助 | 分解本身需语义理解，但标签模板已固化到 prompt 中 |
| Phase 4 细节检查 | 逐项运行检查命令（test -f, python3 --version 等）+ 语法检查 | **✅ 脚本化** | 检查命令可枚举、返回值可判定、批量执行 |
| Phase 5 复杂度评估 | 评估任务复杂度、识别瓶颈、优化执行策略 | ❌ LLM 处理 | 需要综合分析和推理判断 |
| Phase 6 形成 Todo List | 格式化输出执行清单、进度图 | ❌ LLM 处理 | 排版输出，LLM 直接输出即可 |
| Phase 7 Final Verification | 执行完后用关键命令验证结果 + 语法检查 | **✅ 脚本化** | 验证命令可枚举、返回值可判定 |

### 工具说明

#### 工具 1：`check_runner.py`

名称: **check_runner.py**（通过 bash 调用）
路径: `skills/plan/check_runner.py`
功能: 接收一个 JSON 检查清单，逐项执行 bash 命令，返回结构化结果
适用: Phase 4 和 Phase 2.5 中所有标准检查项（文件存在、版本检测、磁盘空间等）

使用方法：

```python
# LLM 构造检查清单
checks = [
    {"name": "Python 版本", "cmd": "python3 --version", "expected": "3.10"},
    {"name": "磁盘空间",    "cmd": "df -h /data",       "expected": "/data"},
    {"name": "配置文件",    "cmd": "test -f config.yaml && echo exists", "expected": "exists"},
]
# 调用工具，返回格式化的终端报告
result = subprocess.run(["python3", "skills/plan/check_runner.py", json.dumps(checks)], capture_output=True, text=True)
```

效果：一次调用替代多次 bash 调用，输出统一，LLM 可直接展示给用户。

#### 工具 2：`check_syntax.py`

名称: **check_syntax.py**（通过 `pythonrt` 调用）
路径: `skills/check/check_syntax.py`
功能: 使用 `ast.parse()` 对 `.py` 文件做纯语法检查，**零依赖**，兼容 WASM Python 环境
适用: 所有新建/修改 Python 文件后的语法验证（Phase 4 细节检查、Phase 7 最终验证、任意写代码步骤之后）

使用方法：

```bash
# 检查单个文件
python skills/check/check_syntax.py my_script.py

# 递归检查整个目录
python skills/check/check_syntax.py /path/to/code/

# 批量模式（只输出汇总）
python skills/check/check_syntax.py /path/to/code/ --summary
```

效果：替代 `python -m py_compile`（因 WASM 环境不支持 `-m` 参数），输出统一的 ✅/❌ 报告。

---

## 适用范围

需要 **2 步以上**完成的任务，包括但不限于：
- 实验规划（训练/推理/评估）
- 代码开发、重构、debug
- 环境配置、迁移、文件操作

**不适用的场景**：
- 纯信息查询（查文档、看日志）
- 单步操作（启动一个脚本、读一个文件）

---
