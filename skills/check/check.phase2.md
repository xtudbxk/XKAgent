## Phase 2: 细节检查

### 核心问题

> **执行条件都满足了吗？每个螺丝都拧紧了吗？**

### 五层顺序检查（新增第 0 层语法检查）

检查按依赖关系分 5 层，**前一层全通过才进入下一层**：

```
第 0 层（语法）：Python 语法检查（新增）
  ├── 检查项：所有新建/修改的 .py 文件 ast.parse 通过
  ├── 工具：python skills/check/check_syntax.py <文件/dir>
  └── 阻断条件：任意 FAIL → 修复后重跑第 0 层

第 1 层（硬阻断）：文件存在、权限、磁盘空间
  ├── 检查项：文件存在、目录可写、磁盘余量、脚本可执行
  └── 阻断条件：任意 FAIL → 停止，修复后从头重跑

第 2 层（环境）：Python/CUDA/依赖版本
  ├── 检查项：版本匹配、conda 环境可激活、关键依赖 import 测试
  └── 阻断条件：任意 FAIL → 停止，修复后重跑第 2 层

第 3 层（加载）：import 测试、配置解析、数据加载
  ├── 检查项：模块加载、配置解析、数据集 sample 测试
  └── 阻断条件：任意 FAIL → 停止，修复后重跑第 3 层

第 4 层（深层）：一致性校验、确定性检查、基准性能
  ├── 检查项：参数跨文件一致、seed 设置、dry-run 通过
  └── 阻断条件：任意 FAIL → 停止，修复后重跑第 4 层
```

### 详细检查项矩阵

| 维度 | 检查项 | 验证方式 | 层级 | 耗时 |
|------|--------|----------|------|------|
| **语法** | Python 文件无语法错误 | `ast.parse()` via `check_syntax.py` | L0 | S |
| **文件存在** | 输入文件/checkpoint/reference 存在 | `test -f` | L1 | S |
| **文件存在** | 输出目录存在且可写 | `test -d` + `test -w` | L1 | S |
| **文件存在** | 脚本可执行 | `test -x` | L1 | S |
| **资源** | 磁盘空间充足 | `df -h` | L1 | S |
| **资源** | GPU 可用、显存足够 | `nvidia-smi`, `torch.cuda` | L1 | S |
| **环境** | conda 环境可激活 | `conda activate` + `which python` | L2 | S |
| **环境** | Python 版本匹配 | `python3 --version` | L2 | S |
| **环境** | CUDA 版本匹配 | `nvcc --version` 或 `torch.version.cuda` | L2 | S |
| **环境** | 关键依赖已安装 | `python3 -c "import torch, numpy, ..."` | L2 | S |
| **加载** | 项目模块可导入 | `python3 -c "import JiT.models"` | L3 | S |
| **加载** | 配置解析无报错 | `source scripts/jit.sh` | L3 | S |
| **加载** | 数据集 sample 可读取 | `python3 -c "dataloader test"` | L3 | M |
| **一致性** | 同参数跨文件对齐 | `diff` / `grep` 关键参数 | L4 | S |
| **一致性** | 与基线 diff 合理 | `diff <基线>/jit.sh <当前>/jit.sh` | L4 | S |
| **确定性** | 随机种子已固定 | 检查 seed 配置 | L4 | S |
| **运行** | dry-run 通过 | `exp_submit.py ... --dry-run` | L4 | S |
| **运行** | git 干净、无未提交变更 | `git status` | L4 | S |

**耗时说明**：`S`(<5s), `M`(5-60s), `L`(>1min)

### 层间阻断规则

```
第 0 层 FAIL → 修复语法错误，重跑第 0 层。输出 "Syntax Error: 代码有语法问题"
第 1 层 FAIL → 立即停止，不继续。输出 "Hard Block: 文件/权限问题"
第 2 层 FAIL → 立即停止，不继续。输出 "Env Block: 环境不匹配"
第 3 层 FAIL → 立即停止，不继续。输出 "Load Block: 加载失败"
第 4 层 FAIL → 修复后重跑第 4 层
```

第 0 层就失败的场景下，后面的检查全是浪费 —— 代码都解析不了就不用测环境了。

---

