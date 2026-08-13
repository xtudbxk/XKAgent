## Phase 3: 交叉检查 + LLM 语义分析

### 核心问题

> **跨文件的引用是否正确？代码的行为是否和注释描述的一致？**

---

### 第 5 步：跨文件检查

#### 5a. 循环引用检测

构建导入依赖图 `{文件A: [文件B, 文件C]}`，用 DFS 找环。

```python
import_graph = {
    "agent.py":    ["history.py", "lock.py"],
    "history.py":  ["lock.py"],
    "lock.py":     ["agent.py"],  # ❌ 循环: agent → lock → agent
}
```

**报错示例**：
```
❌ P0: 检测到循环引用: agent.py → lock.py → agent.py
   可能导致 ImportError: cannot import name 'xxx' from partially initialized module
```

#### 5b. 导入符号存在性

检查 `from codes.xxx import yyy` 中的 `yyy` 是否在 `xxx.py` 的顶层导出中存在。

```python
# agent.py 中有:
from codes.history import add_chat, get_connection

# 检查 history.py 顶层是否有 add_chat 和 get_connection 的定义
```

#### 5c. 跨文件函数签名一致性（进阶）

如果同一个项目中，调用函数的参数数量和定义函数的参数数量明显不匹配，标记。

---

### 第 6 步：LLM 语义检查

以下检查无法脚本化，需要 LLM 结合上下文理解来执行：

#### 6a. 实现与注释一致性

**方法**：
1. 读取注释/文档字符串的描述
2. 看代码实际做了什么
3. 比对两者是否匹配

**典型问题**：
```python
# 返回两数之和
def multiply(a, b):   # ❌ 注释说"和"，但实现是乘法
    return a * b
```

```python
def parse_data(raw):
    """解析 JSON 数据并返回第一个元素"""
    data = json.loads(raw)
    return data          # ❌ 注释说返回第一个元素，但返回了整个数据
```

#### 6b. 边界遗漏

```python
def first_element(lst):
    return lst[0]   # ❌ lst 为空时会 IndexError

def divide(a, b):
    return a / b    # ❌ b 为 0 时会 ZeroDivisionError
```

#### 6c. API 误用

```python
nums = [3, 1, 2]
result = nums.sort()   # ❌ sort() 原地排序，返回 None，不返回新列表
print(result)          # → None

# 正确做法：
nums.sort()
print(nums)            # [1, 2, 3]
```

```python
f = open("data.txt", "r")
content = f.read()
# ❌ 没有 f.close()，资源泄漏

# 正确做法：
with open("data.txt", "r") as f:
    content = f.read()
```

#### 6d. 逻辑方向判断

```python
if score < 60:
    grade = "pass"     # ❌ 应该是 score >= 60 才 pass
else:
    grade = "fail"     # ❌ 方向反了
```

---

### 汇总

脚本化检查结果 + LLM 语义检查结果合并，按 P0/P1/P2 分级排序，进入 Phase 4 输出。
