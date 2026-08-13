# pythonrt_utils.hacking — hacking 引入（需求2）

> 树节点：`pythonrt_utils` → `hacking`（通过 register_extension 接入指定库）
> 前置：`overview`（扩展通道定位）+ `audit`（先判定后引入）

## 3.1 五种接入点（完整接入一个库需要的全部改动）

以 sqlite3 / dulwich 为参照，接入一个库需在 `codes/sandbox.py` + `codes/tools.py` 完成：

```
① 依赖处理
   - 库在黑名单（DANGEROUS_MODULES）→ 用 register_extension 放行（注册表优先于黑名单）
   - 库是第三方（不在 stdlib）→ 默认被"第三方拒绝"，注册为扩展后自动放行
   - 库的可选依赖在黑名单 → 需同步处理（见 3.4 依赖链）

② 模块级默认注册（setup 遍历前完成！）
   register_extension("<lib>", setup=_xxx_setup)
   ⚠️ 必须在 _EXTENSIONS 遍历之前（模块加载时），否则 __init__ 遍历期间
      增删注册表 → RuntimeError: dictionary changed size during iteration

③ ImportGate 放行（无需改代码，已有分支）
   find_spec: root in _EXTENSIONS → 返回 None（放行）
   ⚠️ 第三方拒绝的异常类型是 ModuleNotFoundError（兼容 try/except 回退）

④ 构造参数开关（可选，便捷门控）
   __init__ 签名: allow_xxx: bool = True
   存储:          self._allow_xxx = allow_xxx
   门控:
       if self._allow_xxx:
           if "<lib>" not in _EXTENSIONS:
               register_extension("<lib>", setup=_xxx_setup)
       else:
           unregister_extension("<lib>")

⑤ setup 函数（自定义处理）
   def _xxx_setup(sandbox):
       """说明设计意图 + 依赖放行论证"""
       try:
           import <lib>
       except ImportError:
           return
       # patch 入口复用 sandbox._resolve 做路径校验 / stub 危险函数
       ...

⑥ worker 透传（codes/tools.py + sandbox.py main）
   tools.py:  params["allow_xxx"] = True
   sandbox.py main:  allow_xxx=params.get("allow_xxx", True),
```

## 3.2 register_extension 机制

```python
_EXTENSIONS: dict[str, SandboxExtension] = {}   # 进程内共享

def register_extension(name, setup=None) -> SandboxExtension:
    # 幂等：重复注册更新 setup
    # name 必须合法模块名（isidentifier 校验）

def unregister_extension(name) -> None:   # 幂等，不存在静默忽略
def get_extension(name) -> SandboxExtension | None
def load_extensions(module_names) -> None  # worker 启动时 import 扩展模块完成注册
```

## 3.3 setup 函数模式

```
模式A: 入口路径 patch（路径级库）
   def _xxx_setup(sandbox):
       _resolve = sandbox._resolve
       _real_fn = lib.open_fn
       def gate(path, *a, **k):
           _resolve(path, require_writable=True)   # 可写校验
           return _real_fn(path, *a, **k)
       lib.open_fn = gate

模式B: subprocess stub（导入链裸 import subprocess 时）
   register_extension("subprocess", setup=_subprocess_stub_setup)
   # setup 内: 保留 Popen 类（泛型注解依赖），替换 __init__ + call/run/...
   # 智能错误: 文件不存在→FileNotFoundError（dulwich hook 静默跳过）
   #          文件存在→PermissionError（禁止执行）

模式C: 模块级放行（fd级/纯stdlib 无逃逸面）
   register_extension("mmap", setup=None)         # fd 级
   register_extension("socketserver", setup=None) # 纯 stdlib
```

## 3.4 依赖链打通（实验实证的坑）

```
坑1: 模块级注册 vs setup 内注册
     setup 在 _EXTENSIONS 遍历期间执行 → 动态增删 → RuntimeError
     解决: 所有 register_extension 放模块级（遍历前完成）

坑2: ImportError vs ModuleNotFoundError
     库的可选依赖用 `except ModuleNotFoundError` 回退（dulwich→cdifflib）
     但 ImportGate 抛普通 ImportError → 捕获不到 → 可选依赖回退链断裂
     解决: 第三方拒绝改抛 ModuleNotFoundError（危险黑名单仍抛 ImportError）

坑3: subprocess 加载需要 os.waitpid
     subprocess.py 模块级 `_del_safe: waitpid = os.waitpid`（类定义时求值）
     而 os 手术删了 waitpid → 模块部分加载（Popen=None）
     解决: os 手术前捕获到 _raw_os，加载期临时恢复 → import → 删除

坑4: dulwich GitFile 需要 os.open+os.fdopen
     原子写 refs 用 os.open 打开锁文件（file.py:226）
     解决: os.open 受控包装（_gate_os_open: 路径过 _resolve、flags 解析读写性）

坑5: bytes 路径被 _resolve 拒
     dulwich 内部以 bytes 传路径
     解决: _resolve 支持 bytes（fsdecode，surrogateescape 不抛错）

坑6: os.fdopen 内部走 io.open(fd) 被 _gate_open 拦
     解决: _gate_open 支持 int fd 直通（fd 只能来自受控来源）

坑7: tempfile 需要 os.getpid / os.write
     dulwich commit-msg hook 的 prepare_msg 必经 mkstemp
     解决: getpid 移出删除列表（整数无逃逸）；read/write 恢复（fd 受控）

坑8: clone 写 reflog 需要 os.getuid
     解决: getuid 移出删除列表（仅泄露 uid 整数，setuid 已删）

坑9: 库读 ~/.gitconfig / /etc/gitconfig（越界）
     dulwich StackedConfig.default() 读用户/系统 git 配置
     解决: 环境变量隔离 GIT_CONFIG_GLOBAL / XDG_CONFIG_HOME / GIT_CONFIG_NOSYSTEM
```

## 3.5 辅助脚本 gen_extension.py

```bash
# 生成接入骨架（五种接入点模板 + setup 函数模板）
pythonrt(workdir=".", code_or_filepath="skills/pythonrt_utils/gen_extension.py",
         args: <库名> <是否需入口patch: y/n>)
```

输出：可直接粘贴到 `codes/sandbox.py` 的代码块（含注释）。

## 3.6 接入后验证清单

```
□ import <lib> 成功（受限沙箱内）
□ setup 无报错（stderr 无 "[sandbox] 扩展 xxx setup 失败"）
□ 入口 gate 已安装（porcelain.init.__qualname__ 含 "gate"）
□ 完整功能流程通过（如 git: init→add→commit→log→branch→status）
□ 越界路径被拒（init('/home') → PermissionError）
□ 危险能力仍禁（subprocess.Popen → PermissionError）
□ 既有功能未回归（sqlite3 import / 网络 / 越界读写仍被拦）
```
