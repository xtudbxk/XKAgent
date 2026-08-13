## Phase 1 — 循环等待

> 📂 **加载时机**：进入 Phase 1 时加载
> **用途**：LLM 编写条件判断脚本，调用 waiter 循环等待
>
> ⚠️ **上下文**：当前模式已在 Mode Pre-Check 中确定，传递给本阶段

---

### 步骤 1.1: LLM 编写条件判断脚本

根据 Phase 0 确认的条件和当前模式，LLM 自行编写 Python 条件判断脚本。

**根据当前模式确定脚本存放路径：**

```python
# 从上下文获取当前模式
current_mode = "plan"  # 或 "build" / "build-unsafe"

if current_mode == "plan":
    script_dir = "/tmp/scheduled_task"
else:
    script_dir = "skills/scheduled_task"

script_path = f"{script_dir}/_cond_<描述>.py"
```

**脚本规范：**

```python
#!/usr/bin/env python3
"""条件判断脚本 — 由 LLM 根据 Phase 0 动态生成"""

import datetime

def check_condition() -> bool:
    """返回 True 表示条件满足，False 表示不满足。"""
    now = datetime.datetime.now()
    target = datetime.datetime(2026, 7, 22, 18, 0, 0)
    return now >= target

if __name__ == "__main__":
    import json
    print(json.dumps({"met": check_condition()}))
```

**关键约束：**
- 必须定义 `check_condition() -> bool` 函数
- 仅使用 Python 标准库（无需额外安装包）
- plan 模式：脚本保存到 `/tmp/scheduled_task/`
- build/build-unsafe 模式：脚本保存到 `skills/scheduled_task/`

### 步骤 1.2: 调用等待循环

使用 Python 工具调用 `scheduled_task_waiter.py` 进行循环等待：

```python
from skills.scheduled_task.scheduled_task_waiter import wait_loop

result = wait_loop(
    script_path=script_path,  # 已按模式解析好的路径
    interval=5,               # 从 Phase 0 获取
    mode=current_mode         # 传入当前模式
)
```

**等待循环行为：**
- 每 `interval` 秒执行一次条件判断脚本的 `check_condition()`
- 返回 `True` → 退出循环，进入 Phase 2
- 返回 `False` → 继续等待
- **无超时** — 一直等待直到条件满足或被用户中断
- 用户可通过发送新消息中断等待

### 步骤 1.3: 处理等待结果

```python
import json
parsed = json.loads(result)
if parsed["status"] == "met":
    print(f"✅ {parsed['message']}")  # 进入 Phase 2
elif parsed["status"] == "error":
    print(f"❌ 等待出错: {parsed['message']}")
```
