## Phase 2: 按维度逐项检查

### 核心问题

> **代码中哪些地方有语法/逻辑/安全问题？**

### 执行顺序

```
第 0 步: 语法检查 ← 必须最先执行
         ↓ 语法错误 → 停止，只报语法错误
         ↓ 通过 → 并行执行以下所有检查
第 1 步: 导入检查
第 2 步: 属性检查
第 3 步: 逻辑检查
第 4 步: 安全审计
         ↓
第 5 步: 合并结果
```

---

### 第 0 步：语法检查

| 检查项 | 方法 | 结果 |
|--------|------|------|
| 代码能否被 `ast.parse` 解析 | `ast.parse(content)` | ❌ SyntaxError → 含行号/列号/错误信息 |
| 常见语法错误 | 缩进/括号/引号/关键字 | ✅ 无语法错误 → 继续 |

**阻断规则**：语法错误 ❌ → 立即停止，不进行后续检查。
原因：代码都解析不了，后面的检查全无意义。

---

### 第 1 步：导入检查

#### 1a. 缺失导入

| 步骤 | 方法 |
|------|------|
| ① 收集所有 import 语句 | 遍历 AST 收集 Import / ImportFrom 节点 |
| ② 构建导入符号表 | `{符号名: (模块, 行号)}` |
| ③ 收集所有 Name 引用 | 遍历 AST 中所有 `Name(Load)` 和 `Attribute(Load)` 的 value |
| ④ 求差集 | 使用了的符号 − 已导入的符号 − 内置符号 − 单字母变量 → 缺失导入 |

**判定示例**：
| 代码 | 导入 | 发现 |
|------|------|------|
| `np.array([1,2])` | 只有 `import onnxruntime as ort` | ❌ **缺失 `import numpy as np`** |
| `faiss.normalize_L2(x)` | 无 `import faiss` | ❌ **缺失 `import faiss`** |
| `os.path.join(a,b)` | 有 `import os` | ✅ 正确 |

#### 1b. 未使用导入

对每个导入的符号，检查在代码中是否有对应的 Name/Attribute 引用（定义语句本身不计数）。

| 示例 | 判定 |
|------|------|
| `import queue` 但代码中无 `queue.xxx` | ❌ 未使用 |
| `import os` 且代码中有 `os.path.join()` | ✅ 已使用 |
| `from typing import Generator` 但无类型注解 | ❌ 可能未使用 |

#### 1c. 重复导入

同一模块被 import 多次，且导入相同的符号名。

#### 1d. 通配符导入

`from X import *` — 标记为 P1，建议改为显式导入。

#### 1e. 外部库符号存在性

| 策略 | 说明 |
|------|------|
| 标准库（白名单） | 直接信任，不检查 |
| 已知第三方库（灰名单） | 比对常用子模块列表（如 numpy 的 array/zeros/dot 等） |
| 未知库（黑名单） | 标记为"⚠️ 需人工确认" |

---

### 第 2 步：属性检查

#### 2a. 类成员属性拼写检测

在同一个类中，如果一个属性名只出现少数几次，且与其他属性名只差 1 个字符，标记为可能拼写错误。

**示例**：
```python
class User:
    def __init__(self):
        self.name = "Alice"
        self.nmae = "Bob"   # ❌ 拼写错误？name vs nmae
```

#### 2b. 属性访问合法性（简单版）

对 `self.xxx` 访问，检查 `xxx` 是否在类定义的方法名或 `__init__` 中赋值的属性名中。

> ⚠️ 由于 Python 的动态特性，此检查无法完全准确，仅标记明显问题。

---

### 第 3 步：逻辑检查

#### 3a. 未定义引用

核心算法：**作用域栈追踪**

```
作用域栈结构:
  [{全局作用域}, {函数作用域1}, {嵌套作用域2}, ...]

进入函数 → 压栈（含参数名）
进入 for  → 将循环变量加入当前作用域
进入 except → 将异常变量加入当前作用域
遇到 Name(Load) → 从栈顶往下查，全查不到 → 报错
```

**过滤规则**（减少误报）：
| 类别 | 处理 |
|------|------|
| 单字母变量 (c, i, j, k, v) | 不报（通常是循环变量） |
| 下划线开头变量 (_xxx) | 不报 |
| 内置函数 (chr, repr, callable, frozenset) | 不报（完整内置表维护） |
| self / cls | 不报 |

#### 3b. 可变默认参数

```python
def foo(x=[]):   # ❌ 列表在多次调用间共享
    x.append(1)
    return x

def bar(x=None): # ✅ 正确做法
    if x is None:
        x = []
    ...
```

检测：`FunctionDef` 中 `defaults`/`kw_defaults` 是否有 `List/Set/Dict`。

#### 3c. 裸 except

```python
try:
    ...
except:        # ❌ 会捕获 KeyboardInterrupt，导致无法 Ctrl+C
    pass

try:
    ...
except Exception:  # ✅ 指定异常类型
    pass
```

检测：`ExceptHandler` 中 `type is None`。

#### 3d. == None

```python
if x == None:   # ❌ 应使用 is None
if x is None:   # ✅
```

检测：`Compare` 中 `op is Eq` + 右侧 `Constant(None)`。

#### 3e. 死代码

`Return` / `Raise` 语句之后的代码永远不会被执行。

```python
def foo():
    return 42
    print("dead")  # ❌ 死代码
```

检测：函数主体中，`Return`/`Raise` 之后的语句。

---

### 第 4 步：安全审计

#### 4a. eval/exec 调用

```python
eval("os.system('ls')")   # ❌ 任意代码执行
exec(user_input)           # ❌ 同上
```

检测：Call 节点中 `func.id in ('eval', 'exec')`。

#### 4b. 生产代码中的 assert

```python
assert x is not None  # ❌ python -O 模式下被跳过
```

检测：非 `test_` 文件中的 `Assert` 节点。

#### 4c. 硬编码凭据

扫描赋值语句，左侧含 `password/secret/api_key/token/auth` 等关键词，
右侧为字符串字面量或长字符串 → 标记。

```python
API_KEY = "sk-xxxxxxxxxxx"  # ❌ 硬编码
PASSWORD = "123456"          # ❌ 硬编码
```

---

### 检查结果整合

全部检查完成后，按严重级别排序：

```
P0 🔴 (最前) → P1 🟡 → P2 🔵 (最后)
同一级别内按行号排序
```
