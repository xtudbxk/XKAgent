# pythonrt_utils.audit — 库安全判定（需求1）

> 树节点：`pythonrt_utils` → `audit`（判定库是否影响硬盘 IO 防控）
> 前置：`overview`（威胁模型 + 五层防护 + 路径级/fd级概念）

## 2.1 判定标准（三问）

```
Q1: 库的 IO 入口是"路径"还是"fd"？
    ├─ fd 级   → 天然安全（沙箱内越界 fd 不可构造）→ 放行候选
    └─ 路径级 → 危险（可能绕过 builtins.open）→ 需入口 patch 或拒绝

Q2: 库是纯 Python 还是 C 扩展？
    ├─ 纯 Python → 写文件走 builtins.open/io.open（已被闸门 patch）→ 低危
    └─ C 扩展    → 直接调系统 open()（绕过一切 Python 层 patch）→ 高危，必须入口 patch

Q3: 库的导入链是否拉入危险模块？
    检查: subprocess / ctypes / cffi / mmap / socket / os.open / pickle ...
    ├─ 无危险依赖 → 放行
    ├─ 有可选危险依赖（try/except 包裹）→ 抛 ModuleNotFoundError 兼容回退
    └─ 有必需危险依赖 → 需 stub（subprocess）或放行（mmap/socketserver，fd级/纯stdlib）
```

## 2.2 判定流程（6 步）

```
Step 1: 定位库源码
    build-unsafe: 直接扫 site-packages/<lib>/
    plan/build:   只读扫描用户提供的路径（workdir 内）

Step 2: 扫描危险特征（audit_lib.py 自动）
    检查项:
      - import subprocess / from subprocess import ...
      - import ctypes / cffi（C 调用逃逸）
      - import mmap（fd 级，安全但需确认无路径参数）
      - os.open / os.system / os.fork（裸 fd / 命令执行）
      - .so / .pyd / .dll（C 扩展二进制）
      - open("/绝对路径")（硬编码越界路径）

Step 3: 判断 IO 入口类型（Q1）
    找库的文件写入函数（connect/open/write/save/init...）：
      - 参数是路径字符串 → 路径级
      - 参数是 fd/文件对象 → fd 级

Step 4: 判断纯py vs C 扩展（Q2）
    源码含 C 扩展调用（.so import）→ C 扩展
    全 .py → 纯 Python

Step 5: 判断危险依赖是否必需（Q3）
    裸 import（模块级）→ 必需（无法 try/except 绕过）→ 需 stub/放行
    try/except 包裹 → 可选 → 抛 ModuleNotFoundError 兼容回退

Step 6: 输出判定结论
    - 安全放行:    fd级 + 纯py + 无危险依赖
    - 入口patch:   路径级（如 sqlite3）/ C 扩展
    - stub引入:    subprocess 等（可 import 不可执行）
    - 模块级放行:  mmap/socketserver（fd级/纯stdlib 无逃逸面）
    - 拒绝:        路径级 + 无法 patch 的 C 扩展（引导 build-unsafe）
```

## 2.3 边界案例（实测实证）

```
[OK] fd 级库放行       mmap（mmap.mmap(fileno,...) 无路径参数）
[OK] 纯stdlib放行      socketserver（无 subprocess/ctypes/mmap）
[WARN] 纯py需stub      subprocess（dulwich 导入链裸 import → stub）
[WARN] 路径级需patch   sqlite3（C 扩展直调系统 open → connect 入口 patch）
[WARN] 纯py需patch入口 dulwich Repo.init/discover（路径校验前置）
[OK] 可选依赖回退      cdifflib（except ModuleNotFoundError → difflib）
```

## 2.4 辅助脚本 audit_lib.py

```bash
# 扫描一个库目录，输出危险特征报告 + 判定建议
pythonrt(workdir=".", code_or_filepath="skills/pythonrt_utils/audit_lib.py",
         args 由 sys.argv 传: <库目录路径>)
```

输出 JSON：
```json
{
  "target": "/path/to/lib",
  "danger_imports": {"subprocess": ["client.py:96"], "mmap": ["pack.py:138"]},
  "c_extensions": [],
  "hardcoded_abs_paths": [],
  "io_entry_type": "path-based",
  "verdict": "needs-entry-patch | safe-allow | needs-stub | reject",
  "reason": "..."
}
```

> 判定建议供 LLM 参考，最终安全论证由 LLM 结合沙箱实现给出。
