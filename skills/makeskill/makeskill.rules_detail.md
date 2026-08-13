## 📋 脚本化与生成细节规则

> 📎 **懒加载** — 仅在进入 Phase 5 需要脚本化约束时加载

---

### 规则 9: 大小阈值

**>5KB 时必须建议结构化** — Phase 6 验证后提示进入 Phase 7。

### 规则 11: 网络请求

网络请求优先使用 Python 标准库（`urllib.request`），或通过 `http_request` 回调机制完成。

### 规则 12: WASM 沙箱兼容

所有工具脚本必须在 **pythonrt（统一运行时）** 中可用（stdlib 白名单，第三方被拒）。
- 不能依赖系统命令（如 `curl`、`wget`）
- 不能依赖外部共享库（如 `.so` 文件）
- 只能使用 Python 标准库 + 已安装的 pip 包

### 规则 13: 双调用兼容

工具脚本需同时兼容 **python 调用** 和 **bash CLI 调用** 两种方式：
- python 调用：`from skills.<name>.<script> import func; func()`
- bash 调用：`python skills/<name>/<script>.py --arg value`
- 脚本内通过 `if __name__ == "__main__"` 分支实现
- 函数返回字符串，CLI 入口 `print()` 输出
