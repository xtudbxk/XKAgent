"""codes/sandbox.py — 受限 Python 沙箱（轻量替代 pyeryx，纯 stdlib 零依赖）。

设计意图（对应多轮方案讨论的收敛结论）：
  - 威胁模型：只防 LLM 误用（写错路径 / 越权 import / 误碰系统文件），
    不防对抗性诱导（__subclasses__ gadget 等）→ 可疑代码由调用方引导 build-unsafe。
    plan/build 沙箱不保证对抗性越狱安全；Web 暴露时须配合认证与路径白名单（见 path_guard）。
  - 与 pyeryx 的取舍：牺牲 WASM 硬边界，换取"纯 stdlib、跨平台、无二进制依赖"。
  - 实现"五层 + 一横切"防护（对齐多轮方案收敛结论）：
      层1 内置函数处理：BUILTIN_BLOCK 剔除 + builtins/io.open 兜底 patch（_gate_open）
      层2 os 模块手术：删除逃逸函数（system/open/read/write/fdopen/pipe/getpid 等）
      层3 库 hacking：register_extension 扩展通道（sqlite3 入口 patch + 网络库 hack）
      层4 import 白名单：meta_path allowlist（stdlib + 危险黑名单 + 扩展放行 + 第三方拒绝）
      层5 路径白名单：_resolve（realpath 防逃逸 + rw/ro roots 判定）—— 防硬盘IO越界核心
      横切 资源限制：RLIMIT_CPU/AS/FSIZE/NOFILE（C 层暴力写兜底）
  - 网络能力：net_policy="allow" 时经扩展注册放行 socket/_ssl/ssl/asyncio/urllib 等
    纯py网络库 + 第三方 requests/urllib3；写盘仍受层5路径白名单约束
  - "先捕获、后手术"：vfs 内部使用沙箱初始化时保存的原始 os 引用，
    用户可见的 os 模块则被删除逃逸函数——授权代码与未授权环境彻底分离。

worker 子进程协议（供 tools.py 复用 _ERYX_WORKER_CODE 的进程隔离框架）：
    stdin:  JSON {code, cwd, rw_roots, ro_roots, timeout_ms, net_policy, allow_sqlite, allow_git}
    stdout: __SANDBOX_RESULT__{json}  ← 与 _ERYX_WORKER_CODE 相同的标记协议
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sys
from codes import config
from codes.path_guard import root_contains

# ── 模块级配置（可被构造参数覆盖，避免硬编码）──

# 危险 stdlib 黑名单：能碰进程/底层C/反序列化/动态导入/系统信息/网络(默认)
DANGEROUS_MODULES: frozenset[str] = frozenset({
    # 进程逃逸
    "subprocess", "multiprocessing", "posix", "nt", "os2emxpath",
    # 底层 C 调用（绕过一切 patch）
    "ctypes", "cffi", "mmap",
    # 反序列化 gadget
    "pickle", "marshal", "shelve", "dbm",
    # 动态导入机制（可绕过 meta_path 拦截器；importlib 经扩展定向放行只读子模块）
    "pkgutil", "runpy", "zipimport", "pydoc",
    # 调试/交互（可执行任意代码）
    "pdb", "trace", "code", "codeop", "bdb", "rlcompleter",
    # 系统信息泄露（platform 已移除：只读信息库，无IO/进程/网络能力；
    # urllib3 contrib.ssa/hface 依赖它检测系统类型，放行不增越界风险）
    "sysconfig", "distutils",
    # 网络（黑名单默认禁；net_policy="allow" 时经 NET_LIB_EXTENSIONS 注册放行：
    #   socket/_socket/_ssl/ssl/asyncio/urllib/http/ftplib/smtplib/email/mimetypes，
    #   及 THIRD_PARTY_NET_EXTENSIONS：requests/urllib3/certifi/idna/charset_normalizer/
    #   importlib/h11。注：imaplib/poplib/httpx/anyio 因依赖 subprocess 保持禁用）
    "socket", "ssl", "urllib", "http", "ftplib", "imaplib",
    "smtplib", "telnetlib", "xmlrpc", "socketserver", "wsgiref", "cgi",
    # 漏网网络库补黑名单（原不在黑名单，实测可 import 触网）
    "poplib", "nntplib", "webbrowser",
    # C 层网络实现（地基，网络栈必需；单独注册受控放行）
    "_socket", "_ssl",
    # 文件系统直连（allow_sqlite=True 时移除）
    "sqlite3",
})

# os 手术：逃逸函数（进程/裸fd/路径写/系统信息）
OS_ESCAPE_FUNCS: tuple[str, ...] = (
    # 进程执行
    "system", "popen", "fork", "execv", "execve", "execl", "execle",
    "execlp", "execvp", "execvpe", "spawnv", "spawnve", "spawnlp",
    "spawnlpe", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
    "kill", "killpg", "wait", "setsid", "setpgid",
    # 裸 fd 文件 io。open/fdopen/read/write 不在删除列表：
    #   - os.open 已受控包装（_gate_os_open：路径过 _resolve、flags 解析读写性）
    #     —— dulwich GitFile 原子写依赖（file.py:226），stdlib 用法非 C 绕过；
    #   - os.read/write 只操作已有 fd，而 fd 只能来自受控 os.open 或
    #     builtins.open.fileno()（os.pipe 仍删，匿名 fd 不可构造）→ 读写绑定
    #     授权文件，无新增逃逸面；stdlib tempfile._get_default_tempdir 测试
    #     /tmp 可写性需要 os.write（dulwich commit-msg hook 的 prepare_msg 必经）。
    "pread", "pwrite", "pipe",
    # 系统信息。getpid/getuid 不在删除列表：
    #   - getpid：stdlib tempfile.mkstemp 依赖（dulwich commit-msg hook 必经）
    #   - getuid：dulwich reflog 写身份依赖（_get_default_identity → pwd.getpwuid）
    # 二者仅泄露整数（pid/uid），无操作能力（kill/wait/setuid 已删）。
# waitpid 同理由删除列表移出：subprocess 模块加载依赖（_del_safe: waitpid = os.waitpid），
# 而 fork/exec/spawn 已删 → waitpid 无 pid 可操作，不构成逃逸面。
    # 与 tempfile/pwd 等标准库只读信息用法配套，不构成逃逸面。
    "uname", "getlogin", "gethostname", "geteuid", "getgid",
    "getegid", "getppid", "chroot", "setuid", "setgid",
)

# os 路径写操作（用户可见版会被包装过闸门；vfs 内部用原始引用）
OS_PATH_WRITE_FUNCS: tuple[str, ...] = (
    "rename", "replace", "remove", "unlink", "rmdir", "makedirs", "mkdir",
    "chmod", "chown", "link", "symlink", "utime", "truncate",
)

# os 路径读操作（用户可见版被包装过闸门；vfs 内部用原始引用）
OS_PATH_READ_FUNCS: tuple[str, ...] = (
    "listdir", "scandir", "stat", "lstat", "walk",
)

# 用户代码可见的 builtins 黑名单（元编程/交互）
BUILTIN_BLOCK: frozenset[str] = frozenset({
    "exec", "eval", "compile", "input", "help", "breakpoint", "exit", "quit",
    "globals", "locals", "vars", "memoryview",
})

# 资源限制默认值（setrlimit；WASM 环境无 RLIMIT_* 时自动降级跳过）
RLIMIT_DEFAULTS: dict = {"CPU": 30, "AS_MB": 512, "FSIZE_MB": 64, "NOFILE": 256}


# ── 扩展注册表：允许新增指定库 + 自定义处理 ──
# 设计意图：
#   - 默认策略是"stdlib 全放行 + 危险黑名单 + 第三方拒绝"，但用户可能需要
#     显式信任某个库（含第三方，或黑名单中的危险库如 socket），并为它注入
#     自定义处理（如入口路径校验）。注册表即该"显式信任 + 自定义处理"通道。
#   - setup(sandbox) 在沙箱初始化时调用，可访问 sandbox._resolve / _raw_os /
#     _gate_open 等做入口拦截，复用同一套路径闸门。
#   - 注册是代码级的（模块级全局），worker 子进程通过 params["extensions"]
#     指定要加载的扩展模块（模块内调用 register_extension 完成注册）。

class SandboxExtension:
    """沙箱扩展：允许导入一个库 + 注入自定义处理。

    Attributes:
        name: 顶层模块名（如 'socket'、'yaml'）
        setup: 可选；沙箱初始化时调用 setup(sandbox)，用于自定义 patch。
               典型模式：patch 该库的入口函数，复用 sandbox._resolve 做路径校验。
    """

    def __init__(self, name: str, setup=None):
        if not name or not name.isidentifier():
            raise ValueError(f"扩展名必须是合法模块名: {name!r}")
        self.name = name
        self.setup = setup


# 模块级注册表（进程内共享；worker 协议每进程单实例，天然隔离）
_EXTENSIONS: dict[str, SandboxExtension] = {}


def register_extension(name: str, setup=None) -> SandboxExtension:
    """注册沙箱扩展：允许导入 name（含第三方/黑名单库），并注入自定义处理。

    Parameters
    ----------
    name : str
        顶层模块名（如 'yaml'、'socket'）。注册后该库在沙箱内可导入，
        即使它在 DANGEROUS_MODULES 黑名单或非 stdlib。
    setup : callable(sandbox) or None
        可选；沙箱初始化时调用，典型用法是 patch 该库的入口做路径/参数校验。

    Returns
    -------
    SandboxExtension
        注册的扩展对象（幂等：重复注册同 name 会更新 setup）。

    Examples
    --------
    >>> def setup(sb):
    ...     import yaml
    ...     _real = yaml.safe_load
    ...     def gate(stream, *a, **k):
    ...         # 校验 stream 路径...（省略）
    ...         return _real(stream, *a, **k)
    ...     yaml.safe_load = gate
    >>> register_extension('yaml', setup=setup)
    """
    ext = _EXTENSIONS.get(name)
    if ext is None:
        ext = SandboxExtension(name=name, setup=setup)
        _EXTENSIONS[name] = ext
    else:
        if setup is not None:
            ext.setup = setup
    return ext


def unregister_extension(name: str) -> None:
    """注销扩展（幂等，不存在的 name 静默忽略）。"""
    _EXTENSIONS.pop(name, None)


def get_extension(name: str) -> SandboxExtension | None:
    """查询扩展（未注册返回 None）。"""
    return _EXTENSIONS.get(name)


def load_extensions(module_names: list[str] | None) -> None:
    """加载扩展模块：import 指定模块，触发其内部的 register_extension 调用。

    设计意图：worker 子进程无法共享宿主的注册状态，因此通过 params
    传递扩展模块名列表，worker 启动时先 import 这些模块完成注册。
    """
    for mod in module_names or []:
        try:
            __import__(mod)
        except Exception as e:
            print(f"[sandbox] 扩展模块加载失败 {mod}: {e}", file=sys.stderr)


def _iter_stdlib_names() -> frozenset[str]:
    """标准库命名空间（Python 3.10+ 提供 sys.stdlib_module_names）。"""
    names = getattr(sys, "stdlib_module_names", None)
    if names is None:  # 3.9 及以下：退化为空集 → 第三方全拒、stdlib 需显式白名单
        return frozenset()
    return frozenset(names)


class _ImportGate:
    """meta_path 拦截器：stdlib 全放行 + 危险黑名单拒绝 + 第三方拒绝。

    设计意图：
      - 用「stdlib 命名空间 + 危险子集黑名单」而非手工白名单，
        因为 pathlib→ntpath 这类传递依赖若在白名单外会误杀整个库（已实验实证）。
      - 第三方模块一律拒绝（numpy/pandas 等 C 扩展会绕过 IO patch），引导 build-unsafe。
    """

    def __init__(self, dangerous: frozenset[str], stdlib: frozenset[str],
                 allow_roots: frozenset[str] = frozenset()):
        # 设计意图：黑名单固定不变，放行完全交给扩展注册表（_EXTENSIONS）。
        # 旧版按 allow_sqlite/net_policy 移除黑名单是"第二通道"，与扩展放行重复，
        # 且无法覆盖"注册 socket 但 keep ssl/urllib 禁"等组合场景 → 统一单一通道。
        # allow_roots：项目自身顶层包（如 codes/skills）→ 受限模式下放行，
        #   使 skill 脚本（from skills.xxx import）在 plan/build 可用；第三方仍拒。
        self._dangerous = set(dangerous)
        self._stdlib = stdlib
        self._allow_roots = set(allow_roots)
        # 预加载的危险模块：import 命中 sys.modules 缓存会绕过 meta_path，必须清除
        for m in list(sys.modules):
            root = m.split(".")[0]
            if root in self._dangerous:
                del sys.modules[m]
        # posix 假模块注入（关键修复）：真 posix 提供 fork/exec/open 等进程逃逸
        # 能力，必须保持禁用；但 shutil（L38 `import posix`）被 zipfile/tempfile/
        # urllib.request/requests/urllib3/certifi 等整条链顶层依赖——预清除
        # sys.modules 后任何 `import posix` 都会触发 ImportGate 拦截 → 上述库
        # 全部断链（实测 ImportError: 危险模块被禁: posix）。
        # 方案：注入 PEP 562 假模块（__getattr__ 抛 AttributeError = "无属性"）。
        # shutil 侧 `_HAS_FCOPYFILE = posix and hasattr(posix, "_fcopyfile")`
        # → hasattr=False → 安全回退普通拷贝路径；用户代码 import posix 仅得
        # 空壳（AttributeError），零进程/系统能力，不新增逃逸面。
        if "posix" in self._dangerous:
            import types as _types

            class _FakePosix(_types.ModuleType):
                """PEP 562 空壳 posix：任何属性访问抛 AttributeError（模拟无属性）。"""

                __all__ = ()  # star import 安全（迭代空列表）

                def __getattr__(self, _name):
                    raise AttributeError(_name)

            sys.modules["posix"] = _FakePosix("posix")

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in _EXTENSIONS:  # 显式注册的扩展（含第三方/黑名单库）→ 放行
            return None
        if root in self._allow_roots:  # 项目自身包（codes/skills）→ 放行
            return None
        if root in self._dangerous:
            raise ImportError(f"[sandbox] 危险模块被禁: {name}")
        if self._stdlib and root not in self._stdlib:
            # 抛 ModuleNotFoundError 而非 ImportError：第三方库常用
            # `try: import X except ModuleNotFoundError: <回退>` 处理可选依赖
            # （dulwich 对 cdifflib 即如此）。ImportError 是 ModuleNotFoundError
            # 的父类，except ModuleNotFoundError 捕获不到普通 ImportError → 误伤
            # 可选依赖回退链。危险黑名单仍抛 ImportError（不可静默绕过）。
            raise ModuleNotFoundError(f"[sandbox] 第三方模块被拒: {name}（请用 build-unsafe）")
        return None  # 放行，交给后续 finder


class _TeeWriter:
    """同时写入 StringIO 缓冲与实时输出流（stream_output 模式，进度透传）。

    替换 sys.stdout/sys.stderr 后：用户代码 print 实时透传到 worker 进程 stdout
    （tools.py 读线程逐行回调 → tool_progress 事件），同时仍完整累积进 buf
    （最终 __SANDBOX_RESULT__ payload 的 stdout/stderr 不变，LLM 上下文零影响）。
    """
    __slots__ = ("buf", "real", "name")

    def __init__(self, buf, real, name="stdout"):
        self.buf = buf
        self.real = real
        self.name = name

    def write(self, s):
        self.buf.write(s)
        try:
            self.real.write(s)
            self.real.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass

    def isatty(self):
        return False


class Sandbox:
    """受限 Python 沙箱：导入限制 + 结构化 IO + 专项放行。"""

    def __init__(
        self,
        cwd: str,
        rw_roots: list[str] | None = None,
        ro_roots: list[str] | None = None,
        net_policy: str = "allow",       # off | allow（allow 时开放受控网络）
        allow_sqlite: bool = True,      # 放行 sqlite3（入口 patch 白名单）
        allow_git: bool = True,         # 放行 dulwich（纯py git，入口路径校验）
        allow_network: bool | None = None,  # 兼容旧名，优先用 net_policy
        allow_roots: tuple = (),        # 项目自身顶层包（受限模式放行，如 codes/skills）
        stream_output: bool = False,    # 进度透传: True 时用户 stdout/stderr 实时 tee 到 worker stdout
    ):
        if not cwd or not os.path.isdir(cwd):
            raise ValueError(f"sandbox cwd 无效: {cwd!r}")
        if net_policy not in ("off", "allow"):
            raise ValueError(f"net_policy 必须是 off|allow, got {net_policy!r}")
        if allow_network is not None:
            net_policy = "allow" if allow_network else "off"

        self.cwd = os.path.realpath(cwd)
        # 白名单根（realpath 规范化；rw 优先于 ro，重叠时 rw 生效）
        self._rw = [os.path.realpath(p) for p in (rw_roots or [])]
        self._ro = [os.path.realpath(p) for p in (ro_roots or [])]
        # 数据目录在任何 mode 下只读。
        # _resolve 按最长前缀匹配，ro 子目录可覆盖 workdir 的 rw。
        _protected = os.path.realpath(os.path.join(self.cwd, config.DATA_DIR_NAME))
        if _protected not in self._ro:
            self._ro.append(_protected)
        # 标准库目录隐式只读放行：sandbox 策略是"stdlib 全放行 import"，而
        # Python 3.13 的 importlib 用 io.open_code 加载 stdlib 源码（被
        # _gate_open_code 拦截 workdir 外读取）→ 不放行 stdlib 目录会导致
        # 未预加载的 stdlib（如 pathlib）首次 import 失败。os.__file__ 即
        # stdlib 根目录（os.py 所在）；放行只读不削弱写防控（写仍被拦）。
        _os_file = getattr(os, "__file__", None)
        if _os_file:
            try:
                _stdlib_dir = os.path.realpath(os.path.dirname(os.path.abspath(_os_file)))
                if _stdlib_dir:
                    self._ro.append(_stdlib_dir)
            except Exception:
                pass
        self._net_policy = net_policy
        self._allow_sqlite = allow_sqlite
        self._allow_git = allow_git
        self._allow_roots = frozenset(allow_roots)
        self.stream_output = stream_output

        # ── 层0: 先捕获原始 os 引用（供 vfs/sqlite 内部使用，"先捕获后手术"）──
        self._raw_os = {}
        self._in_resolve = False  # 重入保护：_resolve 内部 realpath→os.lstat 递归时直通
        for n in ("remove", "unlink", "rmdir", "rename", "replace",
                  "makedirs", "mkdir", "chmod", "chown", "link", "symlink",
                  "utime", "truncate", "stat", "listdir", "realpath", "abspath",
                  "path", "getcwd", "environ", "waitpid", "open"):
            if hasattr(os, n):
                self._raw_os[n] = getattr(os, n)

        # ── 层1: 导入限制 ──
        stdlib = _iter_stdlib_names()
        self._gate = _ImportGate(DANGEROUS_MODULES, stdlib,
                                 allow_roots=self._allow_roots)
        sys.meta_path.insert(0, self._gate)

        # ── 层2: builtins / io.open 兜底 patch ──
        # 注意：底层 open 必须是 builtins.open（返回文件对象），
        # 不能是 os.open（返回裸 fd，with 上下文管理会崩）——实测踩坑。
        self._real_open = builtins.open
        builtins.open = self._gate_open
        try:
            io.open = self._gate_open  # pathlib.Path.open 内部走 io.open（已实验实证）
        except Exception:
            pass

        # _io/io.FileIO 旁路修复：底层文件对象构造器也必须过 _resolve。
        # 设计意图：此前只 patch 了 builtins.open / io.open，导致用户可直接
        # import _io; _io.FileIO('/etc/passwd') 越界读盘。这里统一把 _io.open、
        # _io.FileIO、io.FileIO、_io.open 纳入同一闸门。
        try:
            import _io as _iomod
        except Exception:
            _iomod = None
        self._real_io_fileio = getattr(io, 'FileIO', None)
        self._real__io_fileio = getattr(_iomod, 'FileIO', None) if _iomod is not None else None
        self._real__io_open = getattr(_iomod, 'open', None) if _iomod is not None else None

        def _needs_write(mode) -> bool:
            if mode is None:
                return False
            try:
                s = str(mode)
            except Exception:
                return False
            return any(c in s for c in 'wax+')

        def _gate_fileio(path, mode='r', *args, **kwargs):
            # fd 直通：整数 fd 只能来自受控 os.open / builtins.open.fileno。
            if not isinstance(path, int):
                path = self._resolve(path, require_writable=_needs_write(mode))
            # 优先使用原始 _io.FileIO；若平台缺失则退回 io.FileIO 原始引用。
            ctor = self._real__io_fileio or self._real_io_fileio
            return ctor(path, mode, *args, **kwargs)

        def _gate__io_open(path, mode='r', *args, **kwargs):
            if isinstance(path, int):
                if self._real__io_open is not None:
                    return self._real__io_open(path, mode, *args, **kwargs)
                return self._real_open(path, mode, *args, **kwargs)
            path = self._resolve(path, require_writable=_needs_write(mode))
            if self._real__io_open is not None:
                return self._real__io_open(path, mode, *args, **kwargs)
            return self._real_open(path, mode, *args, **kwargs)


        try:
            io.FileIO = _gate_fileio
        except Exception:
            pass
        if _iomod is not None:
            try:
                _iomod.FileIO = _gate_fileio
            except Exception:
                pass
            try:
                _iomod.open = _gate__io_open
            except Exception:
                pass

        # ── os 手术：删逃逸函数，包装路径写函数 ──
        for n in OS_ESCAPE_FUNCS:
            if hasattr(os, n):
                try:
                    delattr(os, n)
                except AttributeError:
                    pass
        # os.open 受控包装：dulwich GitFile（原子写 refs/config）用 os.open 打开
        # 锁文件（file.py:226）。包装后路径过 _resolve 闸门、按 flags 解析读写性，
        # 安全性等价 builtins.open 闸门（同一 _resolve）；os.fdopen 保留（fd 只能
        # 来自受控 os.open 或 builtins.open.fileno → 已授权，无新增逃逸面）。
        _raw_os_open = self._raw_os.get("open")
        if _raw_os_open is not None:
            def _gate_os_open(path, flags, mode=0o777, *a, **k):
                # dulwich _GitFile 内部以 bytes 传锁文件路径（file.py:222），
                # _resolve 仅接受 str/PathLike → 先 fsdecode 归一化
                if isinstance(path, bytes):
                    path = os.fsdecode(path)
                _accmode = flags & getattr(os, "O_ACCMODE", 0)
                _writable = (
                    _accmode != getattr(os, "O_RDONLY", 0)
                    or bool(flags & getattr(os, "O_CREAT", 0))
                )
                _rp = self._resolve(path, require_writable=_writable)
                return _raw_os_open(_rp, flags, mode, *a, **k)
            os.open = _gate_os_open
        for n in OS_PATH_WRITE_FUNCS:
            if hasattr(os, n) and n not in self._raw_os:
                self._raw_os[n] = getattr(os, n)
            if hasattr(os, n):
                setattr(os, n, self._make_os_wrapper(n, require_writable=True))
        for n in OS_PATH_READ_FUNCS:
            if hasattr(os, n) and n not in self._raw_os:
                self._raw_os[n] = getattr(os, n)
            if hasattr(os, n):
                setattr(os, n, self._make_os_wrapper(n, require_writable=False))

        # ── 结构化 IO 对象（唯一文件出入口）──
        self.vfs = self._build_vfs()

        # ── 专项放行（统一走扩展通道；构造参数转注册/跳过）──
        # 设计意图：sqlite/网络与用户自定义扩展同构，均由 register_extension 注册、
        # ImportGate 放行、setup 在初始化时执行。参数仅作便捷开关：
        #   allow_sqlite=True  → 确保 sqlite3 扩展已注册（默认已注册）
        #   net_policy="allow" → 注册 socket 扩展
        # allow_sqlite=False 语义 = 强制禁用（unregister 默认注册，参数优先）
        # allow_sqlite=True  = 确保已注册（默认已注册，幂等）
        if self._allow_sqlite:
            if "sqlite3" not in _EXTENSIONS:
                register_extension("sqlite3", setup=_sqlite_setup)
        else:
            unregister_extension("sqlite3")
        # dulwich（纯py git）：allow_git=True → 确保已注册（默认注册，幂等）
        # allow_git=False 语义 = 强制禁用。与 sqlite3 同构：注册表放行 + setup patch 入口
        if self._allow_git:
            if "dulwich" not in _EXTENSIONS:
                register_extension("dulwich", setup=_dulwich_setup)
        else:
            unregister_extension("dulwich")
        # 网络库扩展：net_policy="allow" → 批量注册（stdlib 纯py + C层地基 + 第三方）
        # net_policy="off" → 全部注销（网络能力彻底关闭）
        if self._net_policy == "allow":
            for name, setup in (*NET_LIB_EXTENSIONS, *THIRD_PARTY_NET_EXTENSIONS):
                if name not in _EXTENSIONS:
                    register_extension(name, setup=setup)
        else:
            for name, _ in (*NET_LIB_EXTENSIONS, *THIRD_PARTY_NET_EXTENSIONS):
                unregister_extension(name)

        # ── 资源限制（WASM 环境无 RLIMIT_* 时静默降级）──
        self._install_rlimits()

        # ── 扩展：运行注册库的自定义处理（setup 失败不阻断初始化，记录即可）──
        for ext in _EXTENSIONS.values():
            if ext.setup is not None:
                try:
                    ext.setup(self)
                except Exception as e:
                    print(f"[sandbox] 扩展 {ext.name} setup 失败: {e}", file=sys.stderr)

        # 标记本实例已安装全局 patch（供 close 释放 / 同进程多实例诊断）
        self._closed = False

    # ────────────── 唯一安全闸门 ──────────────
    def _resolve(self, path, require_writable: bool = False) -> str:
        """路径解析：realpath 防 ../ 与 symlink 逃逸 → 白名单前缀 → rw/ro 判定。

        相对路径以 self.cwd 为基准（与宿主 cwd 语义一致）。
        """
        if isinstance(path, (bytes, bytearray)):
            # dulwich 内部以 bytes 传路径（file.py _GitFile 锁文件/refs 读），
            # fsdecode 用 surrogateescape 不会抛错（非 UTF-8 文件名保留原字节）
            path = os.fsdecode(path)
        if not isinstance(path, (str, os.PathLike)):
            raise PermissionError(f"[vfs] 非法路径类型: {type(path).__name__}")
        p = os.fspath(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        # 重入保护：realpath 内部调 os.lstat/stat（已被包装），置标志位防无限递归
        self._in_resolve = True
        try:
            ap = os.path.realpath(os.path.abspath(p))
        finally:
            self._in_resolve = False
        # 最长前缀匹配优先：ro 子目录可覆盖 rw 父目录（如 work 可写但 work/ro 只读）。
        # 若 rw 优先于 ro，父级 rw 会掩盖子级只读（实测踩坑）。
        best = None  # (匹配根长度, 是否可写)
        for root in self._rw:
            if ap == root or ap.startswith(root + os.sep):
                if best is None or len(root) > best[0]:
                    best = (len(root), True)
        for root in self._ro:
            if ap == root or ap.startswith(root + os.sep):
                if best is None or len(root) > best[0]:
                    best = (len(root), False)
        if best is None:
            raise PermissionError(f"[vfs] outside allowed roots: {path}")
        if require_writable and not best[1]:
            raise PermissionError(f"[vfs] read-only: {path}")
        return ap

    # ────────────── 兜底 open ──────────────
    def _gate_open(self, path, mode="r", *args, **kwargs):
        """builtins.open / io.open 的兜底闸门（用 _real_open 防递归）。

        fd 直通：os.fdopen(fd) 内部经 io.open(fd) 到达本闸门（Python 3.13 的
        fdopen 用 io.open 创建文件对象）。fd 只能来自受控 os.open（过 _resolve）
        或 builtins.open(fileno)（过闸门）→ 已授权，直接透传原始 open。
        """
        if isinstance(path, int):  # fd 直通（已受控来源）
            return self._real_open(path, mode, *args, **kwargs)
        require_w = any(c in mode for c in "wax+")
        resolved = self._resolve(path, require_writable=require_w)
        if hasattr(os, "O_NOFOLLOW") and "opener" not in kwargs:
            flags = os.O_RDWR if "+" in mode else os.O_WRONLY if require_w else os.O_RDONLY
            if "w" in mode:
                flags |= os.O_CREAT | os.O_TRUNC
            elif "a" in mode:
                flags |= os.O_CREAT | os.O_APPEND
            elif "x" in mode:
                flags |= os.O_CREAT | os.O_EXCL
            fd = self._raw_os["open"](resolved, flags | os.O_NOFOLLOW, 0o666)
            try:
                return self._real_open(fd, mode, *args, **kwargs)
            except Exception:
                os.close(fd)
                raise
        return self._real_open(resolved, mode, *args, **kwargs)

    # ────────────── os 路径写包装 ──────────────
    def _make_os_wrapper(self, name, require_writable: bool = False):
        raw = self._raw_os[name]
        def wrapper(path, *args, **kwargs):
            if self._in_resolve:  # 重入保护：_resolve 内部调用直通原始函数，防递归
                return raw(path, *args, **kwargs)
            if not isinstance(path, int):  # fd 直通（shutil.rmtree 的 scandir/stat(fd)）
                path = self._resolve(path, require_writable=require_writable)
            return raw(path, *args, **kwargs)
        wrapper.__name__ = name
        wrapper.__doc__ = getattr(raw, "__doc__", None)
        return wrapper

    # ────────────── vfs 结构化 IO ──────────────
    def _build_vfs(self):
        """vfs 对象：read/write/append/list/stat/delete/rename/move/copy/mkdir/exists/glob。

        内部一律用 self._raw_os 的原始引用（不受 os 手术影响）。
        """
        raw = self._raw_os
        resolve = self._resolve
        real_open = self._real_open  # builtins.open 原始引用（文件对象）

        class VFS:
            def read(self, path, binary=False):
                with real_open(resolve(path), "rb" if binary else "r") as f:
                    return f.read()

            def write(self, path, content, binary=False):
                with real_open(resolve(path, True), "wb" if binary else "w") as f:
                    return f.write(content)

            def append(self, path, content):
                with real_open(resolve(path, True), "a") as f:
                    return f.write(content)

            def list(self, path="."):
                return sorted(raw["listdir"](resolve(path)))

            def stat(self, path):
                p = resolve(path)
                st = raw["stat"](p)
                return {"size": st.st_size, "mtime": st.st_mtime,
                        "is_dir": os.path.isdir(p), "is_file": os.path.isfile(p)}

            def exists(self, path):
                try:
                    resolve(path)
                    return True
                except PermissionError:
                    return False

            def delete(self, path):
                p = resolve(path, True)
                if os.path.isdir(p):
                    raw["rmdir"](p)
                else:
                    raw["remove"](p)

            def rename(self, src, dst):
                sp = resolve(src, True)
                dp = resolve(dst, True)
                raw["rename"](sp, dp)

            def move(self, src, dst):
                sp = resolve(src, True)
                dp = resolve(dst, True)
                raw["rename"](sp, dp)

            def copy(self, src, dst):
                sp = resolve(src)
                dp = resolve(dst, True)
                with real_open(sp, "rb") as fin, real_open(dp, "wb") as fout:
                    fout.write(fin.read())

            def mkdir(self, path):
                raw["makedirs"](resolve(path, True), exist_ok=True)

            def glob(self, pattern, path="."):
                import fnmatch
                p = resolve(path)
                return sorted(
                    os.path.join(path, n) for n in raw["listdir"](p)
                    if fnmatch.fnmatch(n, pattern)
                )

        return VFS()

    # ────────────── 专项: sqlite3 入口 patch ──────────────
# ────────────── 内建扩展：sqlite3 / 网络（setup 函数，经 register_extension 注册）──────────────
# 设计意图：sqlite3/网络从 __init__ 硬编码方法重构为扩展接口（统一"放行库+自定义处理"通道）。
#   - _sqlite_setup / _network_setup 是模块级 setup 函数，签名 setup(sandbox)
#   - 与用户自定义扩展完全同构：ImportGate 放行 + setup 内 patch 入口复用 sandbox._resolve
#   - 构造参数 allow_sqlite/net_policy 保留为便捷开关（见 __init__），内部转注册/跳过

    def _install_rlimits(self):
        """setrlimit：CPU/内存/文件大小/打开文件数（替代 WASM ResourceLimits 角色）。

        WASM 环境无 resource 模块或 RLIMIT_* 常量时静默降级（测试友好）。
        """
        try:
            import resource
        except ImportError:
            return
        try:
            r = resource
            if hasattr(r, "RLIMIT_CPU"):
                r.setrlimit(r.RLIMIT_CPU, (RLIMIT_DEFAULTS["CPU"], RLIMIT_DEFAULTS["CPU"]))
            if hasattr(r, "RLIMIT_AS"):
                mb = RLIMIT_DEFAULTS["AS_MB"] * 1024 * 1024
                r.setrlimit(r.RLIMIT_AS, (mb, mb))
            if hasattr(r, "RLIMIT_FSIZE"):
                mb = RLIMIT_DEFAULTS["FSIZE_MB"] * 1024 * 1024
                r.setrlimit(r.RLIMIT_FSIZE, (mb, mb))
            if hasattr(r, "RLIMIT_NOFILE"):
                r.setrlimit(r.RLIMIT_NOFILE, (RLIMIT_DEFAULTS["NOFILE"], RLIMIT_DEFAULTS["NOFILE"]))
        except (ValueError, OSError):
            pass  # 平台不支持 → 降级

    # ────────────── 释放（同进程多实例场景）──────────────
    def close(self):
        """从 sys.meta_path 移除本实例的 gate（worker 协议下每进程单实例，通常无需调用）。

        注意：builtins.open / io.open / os 手术是进程级全局修改，worker 协议下
        随进程消亡天然隔离；close 仅处理 meta_path，供同进程多实例测试场景使用。
        """
        if self._closed:
            return
        try:
            if self._gate in sys.meta_path:
                sys.meta_path.remove(self._gate)
        except ValueError:
            pass
        self._closed = True

# ────────────── 执行 ──────────────
    def run(self, code: str, timeout_ms: int = 30_000) -> dict:
        """在受限环境中执行用户代码，返回 {ok, stdout, stderr, exit_code}。

        - globals 只注入：vfs（唯一 IO 出入口）+ os（手术后的只读版本）
        - builtins 用安全子集（剔 exec/eval/compile 等元编程内建）
        - 输出重定向到 StringIO，与 worker 协议隔离
        """
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "stdout": "", "stderr": "[sandbox] code 为空", "exit_code": 1}

        # 安全 builtins：复制 builtins 并剔除危险项，open 指向闸门
        safe_builtins = {k: v for k, v in vars(builtins).items() if k not in BUILTIN_BLOCK}
        safe_builtins["open"] = self._gate_open
        safe_builtins["__import__"] = builtins.__import__  # 保留（meta_path 已拦截）

        g = {
            "__builtins__": safe_builtins,
            "__name__": "__sandbox__",
            "vfs": self.vfs,
            "os": os,          # 已手术的只读 os
        }

        old_out, old_err = sys.stdout, sys.stderr
        buf_out, buf_err = io.StringIO(), io.StringIO()
        if self.stream_output:
            sys.stdout, sys.stderr = _TeeWriter(buf_out, old_out), _TeeWriter(buf_err, old_err)
        else:
            sys.stdout, sys.stderr = buf_out, buf_err
        timeout_signal = None
        previous_handler = None

        class _SandboxTimeout(Exception):
            """执行超过 timeout_ms 时的内部控制异常。"""

        try:
            # worker 进程中 run 位于主线程，Unix 可用 SIGALRM 提供真正的
            # wall-clock 硬截止；父进程 kill 仍作为跨平台/信号失效兜底。
            try:
                import signal
                if timeout_ms <= 0:
                    raise ValueError("timeout_ms 必须是正整数")
                timeout_signal = getattr(signal, "SIGALRM", None)
                if timeout_signal is not None:
                    previous_handler = signal.getsignal(timeout_signal)
                    signal.signal(timeout_signal, lambda *_: (_ for _ in ()).throw(_SandboxTimeout()))
                    signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000.0)
            except (ImportError, AttributeError, OSError, ValueError):
                timeout_signal = None

            try:
                os.chdir(self.cwd)
            except OSError:
                pass
            try:
                exec(compile(code, "<sandbox>", "exec"), g)
                return {"ok": True, "stdout": buf_out.getvalue(),
                        "stderr": buf_err.getvalue(), "exit_code": 0}
            except SystemExit as e:
                code_ = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                return {"ok": code_ == 0, "stdout": buf_out.getvalue(),
                        "stderr": buf_err.getvalue() or f"[sandbox] SystemExit({e.code})",
                        "exit_code": code_}
            except _SandboxTimeout:
                return {"ok": False, "stdout": buf_out.getvalue(),
                        "stderr": f"[sandbox] execution timed out after {timeout_ms}ms",
                        "exit_code": 124}
            except BaseException as e:  # 用户代码异常 → 记入 stderr，不崩溃 worker
                import traceback
                tb = traceback.format_exc()
                return {"ok": False, "stdout": buf_out.getvalue(),
                        "stderr": tb, "exit_code": 1}
        finally:
            if timeout_signal is not None:
                try:
                    import signal
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(timeout_signal, previous_handler)
                except (ImportError, OSError, ValueError):
                    pass
            sys.stdout, sys.stderr = old_out, old_err


# ────────────── worker 子进程入口（供 tools.py 复用进程隔离框架）──────────────

def _sqlite_setup(sandbox):
    """sqlite3 内建扩展 setup：三处入口 patch（connect / ATTACH / backup）。

    背景（实验实证）：sqlite3 是 C 扩展，直接调系统 open() 绕过 builtins.open
    patch，所以必须在 sqlite3 自己的入口层拦截；这也说明文件层 patch 对 C 扩展
    是盲区，入口 patch 是唯一正确位置。
    """
    try:
        import sqlite3
    except ImportError:
        return
    sandbox._sqlite = sqlite3
    _real_connect = sqlite3.connect
    _resolve = sandbox._resolve

    # C 类型 sqlite3.Connection 不可直接赋值方法（immutable type）：
    # 必须用官方支持的 factory 子类化方案，在子类中 override execute/backup。
    class _GatedConnection(sqlite3.Connection):
        """包装连接：override execute(拦 ATTACH) 与 backup(拦目标路径)。"""

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str):
                import re as _re
                # ATTACH DATABASE 'x' AS y / ATTACH 'x' AS y（次要逃逸入口）
                for m in _re.finditer(
                    r"ATTACH\s+(?:DATABASE\s+)?['\"]([^'\"]+)['\"]", sql, _re.I):
                    _resolve(m.group(1), require_writable=True)
            return super().execute(sql, *args, **kwargs)

        def backup(self, target, *args, **kwargs):
            if hasattr(target, "name"):  # 文件对象 → 校验其底层路径
                _resolve(target.name, require_writable=True)
            return super().backup(target, *args, **kwargs)

    def _connect_requires_write(database) -> bool:
        """判定 sqlite3.connect 的 database 是否要求可写。

        只读 URI（file:...?mode=ro / immutable=1）不应触发 require_writable=True，
        否则只读查询也会被沙箱误拦（实测：agent 用 mode=ro 读历史库被
        [vfs] read-only 拒绝，被迫复制到 /tmp 才能读——日志 14:23 即此根因）。
        """
        if isinstance(database, (bytes, bytearray)):
            database = os.fsdecode(database)
        d = str(database)
        if d.startswith("file:"):
            q = d.split("?", 1)[1] if "?" in d else ""
            params = {}
            for pair in q.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v
            if params.get("mode", "rw") == "ro" or params.get("immutable") == "1":
                return False
        return True

    def gate_connect(database, *args, **kwargs):
        if database != ":memory:":
            _resolve(database, require_writable=_connect_requires_write(database))
        # factory 注入包装连接子类（拦截 ATTACH/backup 逃逸）
        kwargs.setdefault("factory", _GatedConnection)
        return _real_connect(database, *args, **kwargs)
    sqlite3.connect = gate_connect


def _network_setup(sandbox):
    """网络内建扩展 setup：仅放行 socket，不再附加任何主机/IP/AF_UNIX 限制。

    设计意图：当 net_policy="allow" 时，提供完全放开的 socket 能力；
    连接目标、地址类型与 socket family 不再由 sandbox 额外拦截。
    """
    import socket
    sandbox._socket = socket

# ── 默认注册：sqlite3 内建扩展（保持 allow_sqlite=True 默认可用）──
# 设计意图：sqlite3 走入口 hack（C 扩展写库文件）。网络库扩展（纯py + _socket/_ssl）
# 不在此无条件注册，统一由 __init__ 中 net_policy 门控（net_policy="off" 时全部注销）。
register_extension("sqlite3", setup=_sqlite_setup)

def _block_subprocess(*args, **kwargs):
    """subprocess 入口 stub：命令执行被拒（PermissionError）。

    智能 FileNotFoundError（实验实证）：dulwich 的 ShellHook.execute 用
    `except FileNotFoundError` 静默跳过"不存在的 hook 文件"（hooks.py:121）。
    若 stub 一律抛 PermissionError，dulwich commit 会因默认 hook 检查而崩溃。
    故：目标文件不存在 → 抛 FileNotFoundError（与真实 subprocess 一致，dulwich
    静默跳过）；目标文件存在 → 抛 PermissionError（沙箱禁止命令执行）。
    越界路径 stat 被闸门拦 → 视为"存在"→ 抛 PermissionError（同样禁止）。
    """
    import os as _os
    _cmd = args[0] if args else kwargs.get("args")
    if isinstance(_cmd, (list, tuple)) and _cmd:
        _exe = _cmd[0]
        if isinstance(_exe, (str, bytes, os.PathLike)):
            _s = _os.fspath(_exe)
            if "/" not in _s:
                # 纯命令名（靠 PATH 查找）：无法预判命中，保守一律禁止
                raise PermissionError(
                    "[sandbox] subprocess 被禁用：命令执行不属于受限沙箱能力，请用 build-unsafe")
            try:
                _exists = _os.path.isfile(_s)
            except PermissionError:
                _exists = True  # 越界无法 stat → 禁止执行
            if not _exists:
                raise FileNotFoundError(
                    2, "No such file or directory", _s)
    raise PermissionError(
        "[sandbox] subprocess 被禁用：命令执行不属于受限沙箱能力，请用 build-unsafe")


def _subprocess_stub_setup(sandbox):
    """subprocess 内建扩展 setup：放行 import 但 stub 所有命令执行入口。

    设计意图（实验实证）：
      - dulwich 的 client/hooks/merge_drivers 在模块级裸 import subprocess
        （无 try/except 可绕过）→ 必须放行 import。
      - 但放行 = 命令执行逃逸。折中：注册扩展放行 import，setup 内把命令
        执行入口替换为抛 PermissionError 的 stub。用户代码 import subprocess
        同样只拿到 stub 版 → 命令执行面彻底关闭。
      - Popen 不能替换成普通函数：client.py 类定义处用 subprocess.Popen[bytes]
        泛型注解，函数不可下标 → 保留类、只替换 __init__。

    加载依赖：subprocess.py 模块级 L109 `class _del_safe: waitpid = os.waitpid`
    在类定义时引用 os.waitpid，而 os 手术已删除它 → 模块部分加载（Popen=None）。
    解决：从 posix（os 依赖已缓存于 sys.modules）临时恢复 waitpid 完成加载，
    随后立即删除，保持 os 手术完整性。
    """
    import os as _os
    _restored = []
    if not hasattr(_os, "waitpid") and "waitpid" in sandbox._raw_os:
        # 仅加载期临时恢复（手术前已捕获到 _raw_os），import 完成后立即删除，
        # 保持 os 手术对用户代码的完整性。posix 模块已被 ImportGate 清出
        # sys.modules（黑名单），无法经它恢复，故依赖 _raw_os 预捕获。
        _os.waitpid = sandbox._raw_os["waitpid"]
        _restored.append("waitpid")
    try:
        import subprocess as _sp
    finally:
        for _n in _restored:
            try:
                delattr(_os, _n)
            except AttributeError:
                pass

    if getattr(_sp.Popen.__init__, "__name__", "") != "_blocked_popen_init":
        def _blocked_popen_init(self, *args, **kwargs):
            raise PermissionError(
                "[sandbox] subprocess.Popen 被禁用：命令执行不属于受限沙箱能力，请用 build-unsafe")
        _sp.Popen.__init__ = _blocked_popen_init  # type: ignore[misc]
        for _f in ("call", "run", "check_call", "check_output",
                   "getoutput", "getstatusoutput"):
            if hasattr(_sp, _f):
                setattr(_sp, _f, _block_subprocess)
    sandbox._subprocess_stub = _sp


def _dulwich_setup(sandbox):
    """dulwich 内建扩展 setup：patch 仓库路径入口，复用 sandbox._resolve 闸门。

    背景（与 sqlite3 的差异）：dulwich 是纯 Python git 实现，读写 .git 文件自然
    经过 builtins.open 统一闸门（_gate_open 已拦截），无需逐文件 hack；此处额外
    patch 的是"仓库路径"入口（Repo.init/discover 及 porcelain 便捷函数），
    使目标路径在打开前即按 rw/ro roots 判定——提前报错、语义清晰。
    注：dulwich 1.2.12 无 Repo.open（open_repo 走 Repo.__init__ 构造器），
    其内部文件读取已被 _gate_open 兜底，无需额外 patch 构造器。

    依赖放行说明（实验实证，逐个核对导入链）：
      - mmap:      porcelain 必经 midx.py/pack.py 顶层 import。fd 级库（mmap.mmap
                   (fileno,...) 无路径参数，自身不能打开文件），沙箱内越界 fd 不可
                   构造（os.open 已删、open 全走闸门）→ 放行不引入路径逃逸面。
      - cdifflib:  pack.py 可选加速（except ModuleNotFoundError 回退 difflib）。
                   沙箱未安装 → ImportGate 抛 ModuleNotFoundError 即可兼容回退。
      - subprocess: client/hooks/merge_drivers 模块级裸 import。放行 import 但
                    stub 危险入口（见 _block_subprocess）→ 命令执行被拒。
      - socketserver: server.py 裸 import（纯 stdlib 无危险依赖）→ 直接放行。
    """
    # subprocess 已由 _subprocess_stub_setup（独立扩展 setup，先于本 setup 执行）
    # stub；此处直接依赖其结果。setup 顺序由 _EXTENSIONS 插入顺序保证：
    # subprocess 注册在 dulwich 之前（见模块级注册段）。
    import os as _os
    # git 配置隔离（实验实证）：dulwich 的 StackedConfig.default() 会读
    # ~/.gitconfig、~/.config/git/config、/etc/gitconfig —— 均位于沙箱 roots
    # 之外 → _gate_open 抛 PermissionError 使 Repo.init 崩溃。用环境变量将
    # 配置搜索指向沙箱内不存在的路径（from_path 抛 FileNotFoundError → 跳过）：
    #   GIT_CONFIG_GLOBAL   覆盖 ~/.gitconfig 与 XDG 用户配置
    #   XDG_CONFIG_HOME     覆盖 ~/.config/ 基址
    #   GIT_CONFIG_NOSYSTEM 跳过 /etc/gitconfig
    _os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    _os.environ["GIT_CONFIG_GLOBAL"] = _os.path.join(
        sandbox.cwd, ".gitconfig.sandbox-nonexist")
    _os.environ["XDG_CONFIG_HOME"] = _os.path.join(
        sandbox.cwd, ".config.sandbox-nonexist")

    try:
        from dulwich import porcelain, repo
    except ImportError:
        return
    sandbox._dulwich = porcelain
    _resolve = sandbox._resolve

    _real_init = repo.Repo.init
    _real_discover = repo.Repo.discover

    def gate_init(path, *args, **kwargs):
        """新建仓库：目标路径必须可写（rw_roots 内）。"""
        _resolve(path, require_writable=True)
        return _real_init(path, *args, **kwargs)

    def gate_discover(start, *args, **kwargs):
        """向上发现仓库：只读校验（在任一根内即可）。"""
        _resolve(start)
        return _real_discover(start, *args, **kwargs)

    repo.Repo.init = gate_init
    repo.Repo.discover = gate_discover

    # porcelain 便捷入口：init 需可写；open_repo 只读校验
    def _make_porcelain_gate(real_fn, require_writable):
        def gate(path, *args, **kwargs):
            _resolve(path, require_writable=require_writable)
            return real_fn(path, *args, **kwargs)
        return gate

    for _name, _mode in (("init", True), ("open_repo", False)):
        _fn = getattr(porcelain, _name, None)
        if _fn is not None:
            setattr(porcelain, _name, _make_porcelain_gate(_fn, _mode))

    # clone：source 可为 http(s) URL（网络地址不可 resolve），仅校验本地落盘 target
    _real_clone = getattr(porcelain, "clone", None)
    if _real_clone is not None:
        def gate_clone(source, target=None, *args, **kwargs):
            if target is not None:
                _resolve(target, require_writable=True)
            return _real_clone(source, target, *args, **kwargs)
        porcelain.clone = gate_clone


# ── 默认注册：subprocess 内建扩展（放行 import，危险入口由 _dulwich_setup stub）──
# 设计意图：dulwich 导入链裸 import subprocess（无法 try/except 绕过）。注册放行
# import，但所有命令执行入口（Popen/call/run 等）被 _block_subprocess 替换为抛错，
# 用户代码 import subprocess 同样只拿到 stub 版 → 命令执行面彻底关闭。
register_extension("subprocess", setup=_subprocess_stub_setup)

# ── 默认注册：socketserver 内建扩展（dulwich server.py 裸 import；纯 stdlib）──
# 设计意图：socketserver 无 subprocess/ctypes/mmap 等危险依赖（已核对源码），
# 仅 TCP 服务框架；net_policy=allow 下 socket 本已放行，不新增逃逸面。
register_extension("socketserver", setup=None)

# ── 默认注册：mmap 内建扩展（dulwich 的 pack/midx 导入链依赖，无条件放行）──
# 设计意图：mmap 是 fd 级库（mmap.mmap(fileno, ...) 无路径参数，自身不能打开
# 文件），沙箱内越界 fd 不可构造（os.open 已删、open 全走 _gate_open 闸门），
# 放行整个模块不引入路径逃逸面——与 sqlite3 的"路径直传 C 扩展"本质不同。
# 模块级注册而非 setup 内注册：setup 在 _EXTENSIONS 遍历期间执行，动态增删
# 会触发 RuntimeError（字典迭代中修改）；模块级注册在遍历前完成，天然安全。
register_extension("mmap", setup=None)

# ── 默认注册：dulwich 内建扩展（allow_git=True 默认可用，与 sqlite3 同构）──
# 设计意图：dulwich 为纯 Python 库，无 C 扩展直连文件系统的风险；写 .git 文件
# 全部经过 builtins.open 统一闸门，注册表放行 + 入口路径校验即可。
register_extension("dulwich", setup=_dulwich_setup)

# 网络库扩展清单（顶层模块名 → setup）：纯py网络库无写盘逃逸，setup 置 None；
# _socket/_ssl 为 C 层网络地基，复用 _network_setup。net_policy="allow" 时批量注册。
def _asyncio_setup(sandbox):
    """asyncio 放行 hack：预置假 subprocess（顶层 + asyncio.subprocess）。

    背景（实测）：urllib3.util.util 顶层 import asyncio（同步栈核心依赖）；
    asyncio/base_events.py 顶层 `import subprocess` → subprocess（黑名单）。
    方案：预置 PEP 562 假模块到 sys.modules（sys.modules 优先于 meta_path，
    ImportGate 不触发）。asyncio 的协程/socket/TLS 功能完整保留；subprocess
    （进程）功能彻底禁用（调用即 AttributeError）——比黑名单 ImportError 更
    严格，与"禁进程逃逸"安全目标一致。
    """
    import sys, types

    class _FakeModule(types.ModuleType):
        """PEP 562 通配假模块：任何属性访问返回 None。"""
        __all__ = ()  # star import 安全（迭代空列表）
        def __getattr__(self, name):
            return None

    # asyncio/__init__.py 结构（py3.13 实测）:
    #   line18: from .subprocess import *   ← star import，不绑定 subprocess 名字
    #   line35: subprocess.__all__          ← 需要 subprocess 名字可用
    # 因此假 asyncio.subprocess 需暴露 __all__=("subprocess",) + 自引用，
    # 使 star import 绑定 subprocess 名字（指向假模块自身），line35 不再 NameError。
    for _mod_name in ("subprocess", "asyncio.subprocess"):
        if _mod_name not in sys.modules:
            fake = _FakeModule(_mod_name)
            if _mod_name == "asyncio.subprocess":
                fake.__all__ = ("subprocess",)
                fake.subprocess = fake
            sys.modules[_mod_name] = fake


def _urllib3_setup(sandbox):
    """urllib3 _async hack：动态解析 __init__.py，预置假 _async 子模块。

    背景（实测）：urllib3 新版 __init__.py 顶层 `from ._async.connectionpool
    import ...`、`from ._async.poolmanager import ...` → 触发 import asyncio →
    asyncio 顶层 `from .subprocess import *` → subprocess（黑名单）→ 整链 BLOCKED。
    方案：用 sandbox._real_open（原始引用，扩展特权）读取 urllib3/__init__.py，
    正则解析所有 `from ._async.<sub> import <sym>`，为每个子模块创建假模块并
    注入 sys.modules（属性占位 None）。同步栈不触碰 _async，仅 async 功能不可用。
    动态解析优于硬编码：不依赖 urllib3 具体版本符号表。
    """
    import sys, types, re as _re, os as _os
    # 用 find_spec 定位路径（不执行模块，避免触发 asyncio 拦截）
    try:
        import importlib.util as _iu
        _spec = _iu.find_spec("urllib3")
        if _spec is None or not _spec.origin:
            return
        init_path = _os.path.join(_os.path.dirname(_spec.origin), "__init__.py")
    except Exception:
        return
    try:
        with sandbox._real_open(init_path, "r", encoding="utf-8") as _f:
            src = _f.read()
    except Exception:
        return
    # 解析所有 _async 子模块引用（仅子模块名；符号走 __getattr__ 通配）
    subs = set()
    for m in _re.finditer(r"from\s+\._async\.(\w+)\s+import", src):
        subs.add(m.group(1))
    if not subs:
        return

    class _FakeModule(types.ModuleType):
        """PEP 562 通配假模块：任何属性访问返回 None（from import 拿到占位）。"""
        __all__ = ()  # star import 时迭代空列表，避免 NoneType 报错
        def __getattr__(self, name):
            return None

    fake_pkg = _FakeModule("urllib3._async")
    fake_pkg.__path__ = []
    sys.modules["urllib3._async"] = fake_pkg
    for sub in subs:
        fake = _FakeModule(f"urllib3._async.{sub}")
        sys.modules[f"urllib3._async.{sub}"] = fake

    # urllib3.contrib.imcc（新版证书缓存）用 ctypes → 预置假模块，走纯py回退
    # imcc/__init__.py: `from ._ctypes import load_cert_chain as _ctypes_...`
    # 假模块 __getattr__→None → 调用处回退纯 Python（不触发 ctypes）
    for _m in ("urllib3.contrib.imcc", "urllib3.contrib.imcc._ctypes"):
        if _m not in sys.modules:
            sys.modules[_m] = _FakeModule(_m)


NET_LIB_EXTENSIONS: tuple[tuple[str, object | None], ...] = (
    ("socket", _network_setup),
    ("_socket", _network_setup),
    ("_ssl", _network_setup),
    ("ssl", None),          # 纯py TLS 包装（HTTPS 必需；无写盘逃逸）
    ("asyncio", _asyncio_setup),  # urllib3 同步栈依赖；假 subprocess hack 防进程逃逸
    ("mimetypes", None),    # requests.utils 依赖（guess_filename）；urllib 已放行
    ("urllib", None),
    ("http", None),
    ("ftplib", None),
    ("smtplib", None),
    ("email", None),
    # 注：imaplib/asyncio 已剔除 —— 均顶层依赖 subprocess（黑名单），无法放行
)

# 第三方纯py网络库（经 register_extension 放行 import；certifi 需 CA 证书 hack）
# 设计意图：requests/httpx 链路全部纯 Python（urllib3/httpcore 底层走 socket/ssl，
# 已被上述扩展放行）。certifi 的 cacert.pem 在 site-packages（白名单外），
# setup 时复制到 /tmp 并 patch where()，使 TLS 验证可用。
def _certifi_setup(sandbox):
    """certifi CA 证书 hack：将 cacert.pem 复制到白名单内 /tmp 并重定向 where()。"""
    try:
        import certifi
        src = certifi.where()
        dst = "/tmp/_sandbox_cacert.pem"
        with sandbox._real_open(src, "rb") as f:
            ca = f.read()
        with sandbox._real_open(dst, "wb") as f:
            f.write(ca)
        certifi.where = lambda: dst
    except Exception:
        pass  # certifi 未安装 → 静默（requests 将无法验证 TLS，但 import 不失败）

def _importlib_setup(sandbox):
    """importlib 放行（选项A：完整放行，含 import_module）。

    设计意图（经实测决策）：importlib.import_module 动态导入【不绕过】
    sys.meta_path 的 _ImportGate —— 黑名单库（subprocess/ctypes 等）仍被拦
    （实测 BLOCKED）。第三方库（requests/urllib3/httpx）顶层 import 必须调
    import_module（读 __version__ 走 importlib.metadata），故完整放行。
    安全兜底仍由路径白名单 _resolve + RLIMIT + os 手术承担（五层纵深）。
    """
    pass  # 完整放行；ImportGate 已对 importlib 放行（扩展注册）


THIRD_PARTY_NET_EXTENSIONS: tuple[tuple[str, object | None], ...] = (
    ("requests", None),
    ("urllib3", _urllib3_setup),   # _async hack：绕过顶层 asyncio→subprocess
    ("certifi", _certifi_setup),
    ("idna", None),
    ("charset_normalizer", None),
    # importlib 完整放行（第三方库读版本依赖；动态导入不绕过 ImportGate，实测）
    ("importlib", _importlib_setup),
    # h11：纯py HTTP/1 实现。urllib3 2.21+ 的 hface HTTP/1 协议栈依赖它
    # （实测 urllib3/contrib/hface/protocols/http1/_h11.py）。无危险依赖。
    ("h11", None),
    # 注：httpx/httpcore/anyio/sniffio 已剔除 —— anyio 顶层 import
    # subprocess（危险库）；httpx 为 async 栈，requests 已覆盖同步 HTTP 核心需求。
)

def main() -> None:
    """worker 入口：读 stdin JSON → 构建沙箱 → 执行 → 输出 __SANDBOX_RESULT__ 标记。

    params 支持键：
      - code / cwd / timeout_ms / extensions / net_policy / allow_sqlite / allow_git
      - rw_roots / ro_roots（受限模式）
      - unrestricted: bool — True 时跳过全部限制，直接宿主 exec（build-unsafe profile）
    """
    try:
        params = json.load(sys.stdin)
    except Exception as e:
        print("__SANDBOX_RESULT__" + json.dumps(
            {"ok": False, "stdout": "", "stderr": f"[sandbox] params 解析失败: {e}",
             "exit_code": 1}, ensure_ascii=False))
        return

    # 加载扩展模块（worker 进程内注册，共享宿主注册表代码）
    load_extensions(params.get("extensions") or [])

    if params.get("unrestricted"):
        # ── build-unsafe profile：无限制执行（任意路径/import/subprocess/网络）──
        code = params.get("code", "")
        cwd = params.get("cwd") or os.getcwd()
        # ── 数据目录保护：含 build-unsafe 模式（protected_dir 由宿主传入 agent 真实数据目录）──
        _protected_dir = os.path.realpath(
            params.get("protected_dir") or os.path.join(cwd, config.DATA_DIR_NAME))

        def _is_protected_path(p) -> bool:
            if isinstance(p, int):
                return False
            if isinstance(p, (bytes, bytearray)):
                p = os.fsdecode(p)
            try:
                rp = os.path.realpath(os.fspath(p))
            except Exception:
                return False
            return rp == _protected_dir or rp.startswith(_protected_dir + os.sep)

        def _guard_write_path(path) -> None:
            if _is_protected_path(path):
                raise PermissionError(f"[vfs] read-only (protected): {path}")

        _saved_os_writes: dict[str, object] = {}
        for _wn in ("remove", "unlink", "rmdir", "rename", "replace",
                    "makedirs", "mkdir", "open", "chmod", "chown", "truncate"):
            if hasattr(os, _wn):
                _saved_os_writes[_wn] = getattr(os, _wn)

        def _mk_wguard(rawfn, name: str):
            def _wg(*args, **kwargs):
                if args:
                    _guard_write_path(args[0])
                if "path" in kwargs:
                    _guard_write_path(kwargs["path"])
                if "src" in kwargs:
                    _guard_write_path(kwargs["src"])
                    if "dst" in kwargs:
                        _guard_write_path(kwargs["dst"])
                return rawfn(*args, **kwargs)
            _wg.__name__ = name
            return _wg

        for _wn, _raw_w in _saved_os_writes.items():
            setattr(os, _wn, _mk_wguard(_raw_w, _wn))

        _real_open_bu = builtins.open

        def _bu_gated_open(path, mode="r", *a, **k):
            if any(c in mode for c in "wax+") and _is_protected_path(path):
                raise PermissionError(f"[vfs] read-only (protected): {path}")
            return _real_open_bu(path, mode, *a, **k)

        builtins.open = _bu_gated_open
        try:
            import io as _io_mod
            _io_mod.open = _bu_gated_open
        except Exception:
            pass
        old_out, old_err = sys.stdout, sys.stderr
        buf_out, buf_err = io.StringIO(), io.StringIO()
        if params.get("stream_output", False):
            sys.stdout, sys.stderr = _TeeWriter(buf_out, old_out), _TeeWriter(buf_err, old_err)
        else:
            sys.stdout, sys.stderr = buf_out, buf_err
        try:
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                # build-unsafe 下同样使用隔离命名空间（对齐受限模式 exec(code, g)）：
                # 否则顶层 import/赋值写入函数帧临时 locals，而模块级 def 的 __globals__
                # 指向 sandbox 模块全局 → 函数内引用模块级名称/导入模块报 NameError。
                # 单参 exec(code, g) 令 globals=locals=g，顶层绑定与函数 __globals__ 同源。
                # __builtins__ 注入已 gate 的 builtins（open 写保护仍生效）。
                _unsafe_g = {
                    "__builtins__": builtins,
                    "__name__": "__main__",
                    "__file__": "<pythonrt-unsafe>",
                }
                exec(compile(code, "<pythonrt-unsafe>", "exec"), _unsafe_g)
                result = {"ok": True, "stdout": buf_out.getvalue(),
                          "stderr": buf_err.getvalue(), "exit_code": 0}
            except SystemExit as e:
                code_ = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                result = {"ok": code_ == 0, "stdout": buf_out.getvalue(),
                          "stderr": buf_err.getvalue() or f"[pythonrt] SystemExit({e.code})",
                          "exit_code": code_}
            except BaseException as e:
                import traceback
                result = {"ok": False, "stdout": buf_out.getvalue(),
                          "stderr": traceback.format_exc(), "exit_code": 1}
        finally:
            builtins.open = _real_open_bu
            for _wn, _raw_w in _saved_os_writes.items():
                setattr(os, _wn, _raw_w)
            sys.stdout, sys.stderr = old_out, old_err
    else:
        # ── plan / build profile：受限沙箱 ──
        try:
            sb = Sandbox(
                cwd=params.get("cwd", os.getcwd()),
                rw_roots=params.get("rw_roots", []),
                ro_roots=params.get("ro_roots", []),
                net_policy=params.get("net_policy", "off"),
                allow_sqlite=params.get("allow_sqlite", True),
                allow_git=params.get("allow_git", True),
                allow_roots=params.get("allow_roots", ()),
                stream_output=params.get("stream_output", False),
            )
            result = sb.run(params.get("code", ""), timeout_ms=params.get("timeout_ms", 30_000))
        except Exception as e:
            result = {"ok": False, "stdout": "", "stderr": f"[sandbox] 初始化失败: {e}", "exit_code": 1}

    print("__SANDBOX_RESULT__" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
