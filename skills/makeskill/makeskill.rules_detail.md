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

### 规则 13: pythonrt 沙箱双路径调用（v3.2 修订）

⚠️ 实证（2026-09-05）：pythonrt 沙箱（plan/build）对 `import` 是**双层封锁**——
`_ImportGate`（meta_path 首位 finder）按模块名拒第三方模块 + 文件搜索层（FileFinder）对
skills 目录不可见（目录存在但 `PathFinder.find_spec` 找不到）；且文件执行时
`__name__='__sandbox__'`，`if __name__ == "__main__"` 块不触发。
唯一可行通道：`importlib.util.spec_from_file_location` 直接文件加载（绕过整个 sys.path 搜索体系）。

工具脚本须兼容以下调用方式：
- 🔵🟢 **plan/build 沙箱**：importlib 动态加载（已验证可行）
  ```python
  import importlib.util, sys
  spec = importlib.util.spec_from_file_location("<script>", "skills/<name>/<script>.py")
  m = importlib.util.module_from_spec(spec); sys.modules["<script>"] = m
  spec.loader.exec_module(m)
  m.<func>(...)
  ```
  限制：仅入口模块可加载，内部 `import` 项目内模块仍被 `_ImportGate` 拦 → 脚本必须**自包含**
- 🔥 **build-unsafe**：直接 `from skills.<name>.<script> import func; func()`
- `if __name__ == "__main__"` 保留（宿主侧 CLI 入口），沙箱内不触发、不依赖
- 函数返回字符串，`__main__` 入口 `print()` 输出

### 规则 14: 依赖声明与核对

每个工具脚本头部必须声明依赖（docstring 或注释首行）：
- `# deps: stdlib only` — 纯标准库（默认；未声明视为 stdlib only）
- `# deps: <库1>=<版本>, <库2>` — 第三方库清单；含第三方库时脚本只能在 build-unsafe 运行，执行前提示用户切换模式
- Phase 6 用 ast 统计 import 清单与声明核对：不一致或缺失声明 → 验证输出 WARN 提示补齐
- 元数据层：`requires.pip` 只出现在有 pip 依赖时（与脚本头部 deps 保持同步）

### 规则 15: API 文档化（脚本 = 可直接导入执行的 API）

- 模块级 docstring：总览 + `Deps:` + `Usage:`（含沙箱加载示例）
- 每个公开函数/类 docstring 必含三段：
  - `Args:` — 参数名与类型说明
  - `Returns:` — 返回类型与结构（含 JSON 示例）
  - `Example:` — 可直接复制的调用示例
- `__all__` 白名单明确对外 API 面（未定义则默认非下划线开头的模块级函数/类）
- **自包含**：只依赖 stdlib/pip 包，不 import 项目内其他模块
- 公开 API 纯函数化（无副作用），便于冒烟测试

### 规则 16: skill.md 嵌入使用示例

- 每个脚本在 skill.md 中必须有对应调用示例（`📦 API 速查` 段或流程内嵌代码块）
- 示例统一用 importlib 加载写法（沙箱兼容），注明 build-unsafe 简化写法
- 示例须真实可执行：函数名/路径与脚本一致（Phase 6 校验）
- 示例覆盖脚本主要公开 API（未覆盖 → Phase 6 WARN）

### 规则 18: 路径约定（相对基准 + 运行时映射）

**基准写法**：技能内文档（skill.md 指令/API 速查）与脚本 docstring Usage 中的路径，一律用「相对技能库根」写法 `skills/<name>/<file>`（跨环境稳定）。

**禁止**：硬编码环境相关绝对路径（含 home/mnt/data 前缀的路径）——换环境即失效。

**运行时映射**（LLM 实际加载时，从 pythonrt workdir 视角）：
| 技能库位置 | 映射写法 | 示例 |
|---|---|---|
| workdir 下（项目根 skills/） | 直接相对路径 | `skills/<name>/<file>.py` |
| 挂载/其他位置 | `os.path.relpath` 计算 | `../../xkagent_v0902/skills/<name>/<file>.py` |

**校验**（Phase 6）：`verify_skill_api.py` 的 `check_paths` 诊断——
- 检测脚本/md 中硬编码绝对路径 → WARN 列出（应改相对基准）
- 输出相对路径建议（`rel_dir`/`rel_suggestions`，如 `../../xkagent_v0902/skills/web_search/search.py`）
- WARN 项修复后重跑；禁忌：绝对路径仅允许 `/tmp/`（沙箱临时目录约定）

### 规则 17: verify_skill_api.py 自动校验（Phase 6 必做）

生成/修改脚本后运行：

```python
# 🔵🟢 plan/build 沙箱（build-unsafe 可直接 from skills.makeskill.verify_skill_api import verify_skill）
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "verify_skill_api", "skills/makeskill/verify_skill_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["verify_skill_api"] = m
spec.loader.exec_module(m)
m.verify_skill("skills/<name>", ["<script>.py"], "skills/<name>/skill.md")
```

五项检查：语法 / deps 声明核对 / docstring 完整性（Args/Returns/Example）/ skill.md 示例一致性 / 冒烟测试。
WARN 项须修复后重跑，FAIL 项禁止发布。

**模式感知**（v3.2.1+）：双向分析——
① 声明侧（`check_deps`）：verify_skill 自动解析 skill.md frontmatter 的 `compatible_modes`（也可显式传 `modes=` 覆盖）：
   - 第三方依赖 + 声明含 `build-unsafe` → deps **PASS**（注明需 build-unsafe 运行；build-unsafe 无库限制）
   - 第三方依赖 + 声明不含 `build-unsafe` → deps **WARN**（目标模式沙箱内不可用）
   - 冒烟测试：第三方库在当前环境不可 import → 状态 **SKIP**（需 build-unsafe 下验证），不误报 FAIL
② 脚本侧（`check_mode_fit`，反向推断）：从脚本内容分析适用模式，输出 `fit`（plan/build/build-unsafe 各 OK/受限/不可用）+ `reasons`（原因）+ `affected_libs`（受影响库）：
   - 第三方库 / 危险模块（ctypes/cffi/pickle/marshal/subprocess）/ 动态执行（exec/eval/compile）/ 进程调用（system/popen）→ plan+build **不可用**，受影响库列出
   - 写操作（open w/a、os.remove/makedirs/rename、Path.write_*）→ plan **受限**（仅 /tmp 可写），build OK
   - 纯 stdlib 无写操作 → 三模式全 **OK**
