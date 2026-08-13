# pythonrt_utils.examples — 实战示例

> 树节点：`pythonrt_utils` → `examples`（两个真实接入案例，含完整代码与验证）

## 4.1 sqlite3（路径级 C 扩展 → 入口 patch）

**判定**：Q1=路径级（connect("/path")）、Q2=C 扩展（直调系统 open）→ 必须入口 patch。

```python
# codes/sandbox.py
def _sqlite_setup(sandbox):
    """sqlite3 内建扩展 setup：三处入口 patch（connect / ATTACH / backup）。"""
    import sqlite3
    sandbox._sqlite = sqlite3
    _real_connect = sqlite3.connect
    _resolve = sandbox._resolve

    class _GatedConnection(sqlite3.Connection):
        """包装连接：override execute(拦 ATTACH) 与 backup(拦目标路径)。"""
        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str):
                import re as _re
                for m in _re.finditer(
                        r"ATTACH\s+(?:DATABASE\s+)?['\"]([^'\"]+)['\"]", sql, _re.I):
                    _resolve(m.group(1), require_writable=True)
            return super().execute(sql, *args, **kwargs)

        def backup(self, target, *args, **kwargs):
            if hasattr(target, "name"):
                _resolve(target.name, require_writable=True)
            return super().backup(target, *args, **kwargs)

    def gate_connect(database, *args, **kwargs):
        if database != ":memory:":
            _resolve(database, require_writable=True)
        kwargs.setdefault("factory", _GatedConnection)
        return _real_connect(database, *args, **kwargs)
    sqlite3.connect = gate_connect

register_extension("sqlite3", setup=_sqlite_setup)
```

**要点**：C 类型 `sqlite3.Connection` 不可直接赋值方法（immutable），必须用 factory 子类化方案。

## 4.2 dulwich（纯 Python + subprocess 依赖 → 放行 + stub）

**判定**：Q1=路径级（Repo.init(path)）、Q2=纯 Python（写 .git 走 builtins.open）、
Q3=导入链裸 import subprocess → 放行 import + stub 命令执行 + 入口路径校验。

### 4.2.1 依赖放行（模块级注册，遍历前完成）

```python
register_extension("subprocess", setup=_subprocess_stub_setup)  # 放行但 stub
register_extension("socketserver", setup=None)                   # 纯 stdlib 放行
register_extension("mmap", setup=None)                           # fd 级放行
register_extension("dulwich", setup=_dulwich_setup)
```

### 4.2.2 subprocess stub（可 import 不可执行）

```python
def _block_subprocess(*args, **kwargs):
    """命令执行被拒。智能错误：文件不存在→FileNotFoundError（dulwich hook
    静默跳过）；存在/越界/纯命令名→PermissionError（禁止执行）。"""
    import os as _os
    _cmd = args[0] if args else kwargs.get("args")
    if isinstance(_cmd, (list, tuple)) and _cmd:
        _exe = _cmd[0]
        if isinstance(_exe, (str, bytes, os.PathLike)):
            _s = _os.fspath(_exe)
            if "/" not in _s:  # 纯命令名（PATH 查找）→ 保守禁止
                raise PermissionError("[sandbox] subprocess 被禁用：请用 build-unsafe")
            try:
                _exists = _os.path.isfile(_s)
            except PermissionError:
                _exists = True
            if not _exists:
                raise FileNotFoundError(2, "No such file or directory", _s)
    raise PermissionError("[sandbox] subprocess 被禁用：请用 build-unsafe")
```

### 4.2.3 入口路径校验 + 配置隔离

```python
def _dulwich_setup(sandbox):
    import os as _os
    # git 配置隔离（防读 ~/.gitconfig 越界）
    _os.environ["GIT_CONFIG_GLOBAL"] = _os.path.join(sandbox.cwd, ".gitconfig.sandbox-nonexist")
    _os.environ["XDG_CONFIG_HOME"] = _os.path.join(sandbox.cwd, ".config.sandbox-nonexist")
    _os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    from dulwich import porcelain, repo
    _resolve = sandbox._resolve
    _real_init = repo.Repo.init
    _real_discover = repo.Repo.discover

    def gate_init(path, *a, **k):
        _resolve(path, require_writable=True)   # 新建仓库需可写
        return _real_init(path, *a, **k)

    def gate_discover(start, *a, **k):
        _resolve(start)                          # 发现仓库只读校验
        return _real_discover(start, *a, **k)

    repo.Repo.init = gate_init
    repo.Repo.discover = gate_discover
    # porcelain.init / open_repo / clone 同理包装
```

## 4.3 验证清单（dulwich 实测输出）

```
A. gate 安装      porcelain.init / Repo.init / open_repo / clone 全 gate ✅
B. 完整 git 流程  init → add → commit×2 → walker(log) → branch → status → E2E_OK ✅
C. clone          src(3 commits) → clone → 读取 3 commits → CLONE_OK ✅
D. 安全回归
   · 越界 init (/home, /etc)      → PermissionError ✅
   · subprocess.Popen/call/run    → PermissionError（stub）✅
   · /etc/passwd 读、越界写       → PermissionError ✅
   · mmap / sqlite3 import        → OK ✅
```

## 4.4 两种接入的对比（决策速查）

| 维度 | sqlite3 | dulwich |
|------|---------|---------|
| 实现 | C 扩展 | 纯 Python |
| IO 入口 | connect(path) 路径级 | Repo.init(path) 路径级 |
| 危险依赖 | 无 | subprocess（裸 import） |
| 处理 | connect/ATTACH/backup 三处 patch | 放行+stub subprocess + Repo 入口 gate |
| 文件写 | C 层直写（入口 patch 唯一途径） | builtins.open 闸门天然覆盖 + 入口前置校验 |
| 附加 | factory 子类化（C 类型不可赋值） | 配置环境变量隔离 + os.open 受控包装 |
