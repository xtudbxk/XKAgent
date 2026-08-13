# pythonrt_utils.overview — 沙箱总览

> 树节点：`pythonrt_utils` → `overview`（背景知识，audit/hack 的前置）

## 1.1 威胁模型（决定判定标准的前提）

```
只防 LLM 误用（写错路径 / 越权 import / 误碰系统文件）
不防对抗性诱导（__subclasses__ gadget 等）
可疑代码 → 由调用方引导 build-unsafe
```

**推论**：判定库是否影响硬盘 IO，目标是**防止"库自己绕过文件闸门写越界路径"**，而非防恶意攻击。这决定了判定标准以"库的 IO 实现方式"为核心，而非"库是否被信任"。

## 1.2 五层防护架构

```
层1 内置函数处理   BUILTIN_BLOCK 剔除 + builtins/io.open 兜底 patch（_gate_open）
层2 os 模块手术    删除逃逸函数（system/open 等）+ 路径写函数包装（过闸门）
层3 库 hacking     register_extension 扩展通道（sqlite3 入口 patch + 网络库 hack）
层4 import 白名单  meta_path allowlist（stdlib 全放行 + 危险黑名单 + 扩展放行 + 第三方拒绝）
层5 路径白名单     _resolve（realpath 防逃逸 + rw/ro roots 判定）—— 防硬盘IO越界核心
横切 资源限制       RLIMIT_CPU/AS/FSIZE/NOFILE
```

**关键认知**：硬盘 IO 防控的核心是**层5 `_resolve` 路径闸门**。所有文件读写最终都经过它。判定"库是否影响硬盘 IO" = 判定"库的写路径是否可能绕过 `_resolve`"。

## 1.3 绕过 `_resolve` 的三种方式（判定必须检查）

```
绕过方式               典型库类型        防控手段
─────────────────────────────────────────────────────────────
① 路径直传 C 扩展      sqlite3(connect)  → 入口 patch（在库自己的入口层拦截）
   （C 层直接调系统 open，绕过 builtins.open patch）
② 裸 os.open/fd        os.open 已被删    → os 手术删除；dulwich GitFile 例外
   （用户代码直接拿裸 fd）                  → 受控包装（_gate_os_open）
③ subprocess 命令执行   git hooks 等      → subprocess stub（可 import 不可执行）
   （fork 子进程绕沙箱）
```

## 1.4 核心概念：路径级 vs fd级

| 维度 | 路径级库 | fd 级库 |
|------|---------|---------|
| 入口形态 | `connect("/path")` 路径直传 | `mmap(fd, len)` 只接受 fd |
| 能否自己打开文件 | 能（绕过 builtins.open） | 不能（必须由调用方先 open） |
| 逃逸面 | 有（路径校验需在库入口做） | 无（闸门在 open 层已覆盖） |
| 示例 | sqlite3 | mmap |

**判定第一问**：库的 IO 入口是"路径"还是"fd"？
- fd 级 → 天然安全（沙箱内越界 fd 不可构造，os.open 已删、open 全走闸门）
- 路径级 → 需入口 patch 或拒绝

## 1.5 扩展通道定位（hack 的入口）

```
register_extension(name, setup) 注册表 _EXTENSIONS
    │
    ├─ ImportGate.find_spec 放行 import（root in _EXTENSIONS → 不拦截）
    ├─ setup(sandbox) 在沙箱初始化时调用（可 patch 库入口、stub 危险函数）
    └─ worker 子进程通过 params["extensions"] 加载扩展模块
```

接入一个库 = 完成"注册 + 放行 import + 自定义处理（setup）"三件事。
