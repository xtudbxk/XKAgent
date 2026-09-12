---
name: python_checker
version: 1.1.0
description: Python 脚本语法与逻辑错误检查(输出报告)
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - python检查
  - 代码检查
  - 语法检查
  - pycheck
  - 静态分析
  - 代码审计
  - 错误检测
requires: {}
author: system
open_source: true
---

## 概要

对 Python 代码进行多维度语法与逻辑检查，输出结构化错误报告。
**只报告影响运行结果的问题，不做风格/规范性检查。**

核心检查覆盖 **7 大维度**：
1. 语法检查 — 代码是否可解析
2. 导入检查 — 缺失导入/未使用/重复/通配符/外部库符号
3. 属性检查 — 类成员属性访问合法性/拼写
4. 逻辑检查 — 未定义引用/可变默认参数/裸except/死代码/==None
5. 安全审计 — eval/exec/assert/硬编码凭据
6. 跨文件检查 — 循环引用/模块存在性
7. 语义检查 — 实现与注释一致性（LLM 处理）

## 核心流程

```
Phase 1: 加载检查器脚本
Phase 2: 按维度逐项检查
Phase 3: 交叉检查 + LLM 语义分析
Phase 4: 输出结构化报告
```

## 文件引用

| 文件 | 内容 | 读取时机 |
|------|------|---------|
| [phase1](python_checker.phase1.md) | 加载脚本 + 模式适配 | 进入 Phase 1 |
| [phase2](python_checker.phase2.md) | 7 大类检查详细步骤 + 判定规则 | 进入 Phase 2 |
| [phase3](python_checker.phase3.md) | 语义检查 + 跨文件分析 | 进入 Phase 3 |
| [phase4](python_checker.phase4.md) | 报告格式 + 严重级别定义 | 进入 Phase 4 |

### 外部工具

| 工具 | 路径 | 说明 |
|------|------|------|
| checker.py | `/tmp/python_checker/checker.py` (plan) / `skills/python_checker/checker.py` (build) | 核心检查脚本，零依赖 |

---

## 严重级别定义

| 级别 | 含义 | 举例 |
|------|------|------|
| **P0 🔴** | 运行时必然崩溃 | 语法错误、缺失导入、未定义引用 |
| **P1 🟡** | 可能导致错误或安全风险 | 裸except、可变默认参数、eval、硬编码凭据 |
| **P2 🔵** | 建议优化，不影响结果 | ==None、未使用导入、生产环境assert |

## 执行流程

### Phase 1: 加载检查器

根据当前运行模式加载 checker.py：

```python
# plan 模式：从 /tmp/ 动态加载
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "checker", "/tmp/python_checker/checker.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["checker"] = mod
spec.loader.exec_module(mod)

# build 模式：标准导入
from skills.python_checker.checker import run_checks, check_project
```

### Phase 2: 逐项检查

对每个目标文件，调用 checker.py 中的检查函数：

1. **语法检查** — `check_syntax(filepath, content)` → 确保 `ast.parse` 通过
   - ❌ 有语法错误 → 立即停止该文件，只报语法错误
   - ✅ 无语法错误 → 继续

2. **导入检查** — `check_imports(filepath, content, tree)`
   - 收集所有 import 语句，构建符号表
   - 扫描 Name 引用，比对发现缺失导入
   - 检查未使用导入 / 重复导入 / 通配符导入
   - 外部库符号与灰名单比对

3. **属性检查** — `check_attributes(filepath, content, tree)`
   - 收集类定义中的方法和属性
   - 扫描 self.xxx 访问
   - 检测拼写相似的属性名

4. **逻辑检查** — `check_logic(filepath, content, tree)`
   - 使用作用域栈追踪未定义引用
   - 检查可变默认参数、裸except、==None、死代码

5. **安全审计** — `check_security(filepath, content, tree)`
   - 检查 eval/exec 调用
   - 检查生产代码中的 assert
   - 检查硬编码密码/密钥

### Phase 3: 交叉检查 + 语义分析

**跨文件检查**（项目级）：
- 收集项目的导入依赖图，检测循环引用
- 检查 `from codes.xxx import yyy` 中 yyy 在目标文件中是否存在

**LLM 语义检查**（需要理解代码含义）：
- 实现与注释一致性：读注释 → 看代码 → 判断是否匹配
- 逻辑方向判断：条件写反了、边界遗漏
- API 误用：用错了函数的行为（如 sort 的返回值、remove 的用法）
- 资源泄漏：文件/网络连接未关闭

### Phase 4: 输出报告

按以下格式输出问题报告：

```
📄 <文件名>
   总问题数: N

❌ P0 L<行号>: <问题描述>
   细节: <补充说明>

⚠️ P1 L<行号>: <问题描述>
   细节: <补充说明>

ℹ️ P2 L<行号>: <问题描述>
   细节: <补充说明>
```

---

## 快速使用

### 检查单个文件
```python
from python_checker.checker import run_checks
result = run_checks("path/to/file.py")
print(result)
```

### 检查整个项目
```python
from python_checker.checker import check_project
result = check_project("path/to/project/")
print(result)
```

### Bash 直接调用
```bash
python /tmp/python_checker/checker.py path/to/file.py
python /tmp/python_checker/checker.py path/to/project/ --summary
python /tmp/python_checker/checker.py path/to/file.py --json
```
