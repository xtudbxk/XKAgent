## Phase 1: 加载检查器脚本

### 核心问题

> **检查脚本是否就绪？当前是什么运行模式？**

### 步骤

#### 1. 确认 checker.py 存在

| 模式 | 脚本位置 | 验证方式 |
|------|---------|---------|
| plan | `/tmp/python_checker/checker.py` | `test -f /tmp/python_checker/checker.py` |
| build | `skills/python_checker/checker.py` | `test -f skills/python_checker/checker.py` |

如果不存在，先从 skill 定义中提取脚本内容并写入对应位置。

#### 2. 加载脚本

```python
# plan 模式（通过 importlib 动态加载）
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "checker", "/tmp/python_checker/checker.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["checker"] = mod
spec.loader.exec_module(mod)

# build/build-unsafe 模式（标准导入）
from skills.python_checker.checker import run_checks, check_project
```

#### 3. 确认目标

向用户确认或从上下文推断：
- 检查单个文件还是整个项目？
- 输出格式偏好（text / JSON）？
