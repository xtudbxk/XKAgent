## ⚠️ 边界条件与注意事项

> 📎 **加载时机**：遇到错误或特殊情况时加载

---

### 中断处理

用户在等待期间发送新消息时，LLM 应判断：

| 用户行为 | 处理方式 |
|---------|---------|
| 取消任务 | 终止等待，清理临时条件脚本，告知用户已取消 |
| 修改条件 | 终止当前等待，重新从 Phase 0 开始 |
| 修改命令 | 更新命令记录，继续当前等待 |
| 无关消息 | 记录消息待处理，继续等待（告知用户正在等待中） |

### 条件脚本合法性检查

LLM 生成的条件脚本必须确保：
- 仅使用 Python 标准库
- `check_condition()` 不产生副作用（如删除文件、修改系统配置）
- 不包含死循环
- 执行时间不宜过长（建议 < 1 秒）

### 资源消耗注意事项

- 轮询间隔不宜过短（建议 ≥ 2 秒），避免 CPU 空转
- 对于长时间等待（如数小时），考虑增加间隔至 30-60 秒
- 避免创建大量临时条件脚本，每次 Phase 1 结束后清理 `_cond_*.py`

### 模式相关 FAQ

#### Q: plan 模式下条件脚本存在哪里？

plan 模式下项目目录只读，条件脚本存到 `/tmp/scheduled_task/` 目录。waiter 通过 `importlib` 动态加载。Phase 2 仅限使用 `pythonrt`（只读受限）。

#### Q: build 模式和 build-unsafe 模式有什么实际区别？

| 维度 | build | build-unsafe |
|------|-------|-------------|
| 脚本位置 | `skills/scheduled_task/` | 同 build |
| Phase 2 执行 | pythonrt（受限，plan 只读 / build 可写） | + **pythonrt 无限制**（build-unsafe） |
| 适用场景 | 标准任务 | 系统管理、部署、重启服务 |

#### Q: 如果 waier 找不到条件脚本怎么办？

`resolve_script_path()` 会按以下顺序尝试：
1. 如果路径是绝对路径 → 直接使用
2. 如果路径已存在 → 直接使用
3. plan 模式 → 尝试 `/tmp/<文件名>`
4. build/unsafe → 尝试 `skills/scheduled_task/<文件名>`
5. 都找不到 → 报错 `FileNotFoundError`

#### Q: 不同模式下轮询间隔有什么建议？

| 模式 | 建议间隔 | 原因 |
|------|---------|------|
| plan | ≥ 5 秒 | python 工具调用有开销 |
| build | ≥ 2 秒 | 标准间隔 |
| build-unsafe | ≥ 2 秒 | 同 build |

### Python 沙箱兼容

所有脚本均通过 `pythonrt`（统一运行时）执行，注意：
- 无法直接访问文件系统（通过 workdir 参数控制）
- 无法执行系统命令
- 网络请求使用 urllib（标准库）

### 脚本路径说明

推荐在 Phase 1 中根据模式构造路径：

```python
import os
mode = "当前模式"  # 来自 Mode Pre-Check

if mode == "plan":
    script_dir = "/tmp/scheduled_task"
else:
    script_dir = os.path.join(os.getcwd(), "skills/scheduled_task")

script_path = os.path.join(script_dir, "_cond_xxx.py")
result = wait_loop(script_path=script_path, interval=5, mode=mode)
```
