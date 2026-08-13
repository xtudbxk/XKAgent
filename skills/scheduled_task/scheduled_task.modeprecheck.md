## 🧭 Mode Pre-Check — 模式适配入口

> 📂 **加载时机**：进入 Phase 0 前加载
> **用途**：检测当前运行模式，选择对应执行策略

---

进入正式流程前，先检测当前运行模式。所有后续阶段（Phase 0→1→2）都受此检测结果影响。

### 如何检测模式

最可靠的方式是查看**系统消息前缀**中的「模式: xxx」字段：

| 检测方式 | 说明 |
|---------|------|
| ✅ **查系统消息前缀** | 见用户消息开头的「模式: plan / build / build-unsafe」 |
| ⚠️ 尝试写 /tmp/ | plan 和 build 都可写，无法区分二者 |
| ⚠️ 检查执行工具 | build-unsafe 下 pythonrt 无限制；其他模式受限 |

### 模式策略

| 检测结果 | 行为策略 |
|---------|---------|
| 🔵 **plan** | 条件判断脚本存 `/tmp/scheduled_task/`；**注意**：pythonrt 受限模式禁 importlib，waiter 需在 build-unsafe 运行或用户 `!xxx`；Phase 2 仅限 pythonrt（只读受限） |
| 🟢 **build** | 条件判断脚本存 `skills/scheduled_task/`；**注意**：pythonrt 受限模式禁 importlib，waiter 需在 build-unsafe 运行或用户 `!xxx`；Phase 2 用 pythonrt（可写受限） |
| 🔥 **build-unsafe** | 条件判断脚本存 `skills/scheduled_task/`；Phase 2 用 pythonrt（无限制）或提示用户 `!xxx` |

### 脚本路径解析规则

在 Phase 1 中编写条件判断脚本时，根据当前模式确定存放路径：

```python
# 根据模式确定脚本存放路径
mode = "当前检测到的模式"  # plan / build / build-unsafe

if mode == "plan":
    script_dir = "/tmp/scheduled_task"
else:  # build / build-unsafe
    script_dir = "skills/scheduled_task"

# 条件脚本保存到对应目录
script_path = f"{script_dir}/_cond_<描述>.py"
```

### 调用 waiter 时的模式传递

```python
from skills.scheduled_task.scheduled_task_waiter import wait_loop

result = wait_loop(
    script_path=script_path_var,  # 已解析好的路径
    interval=5,
    mode="plan"  # 或 "build" / "build-unsafe"
)
```

> ⚠️ 执行完此检查后，将结果（当前模式）作为上下文传递给后续所有 Phase。
