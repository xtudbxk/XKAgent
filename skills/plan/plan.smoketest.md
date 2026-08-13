## Phase 2.5: Smoke Test（可选）

### 核心问题

> **在进入任务分解之前，方案依赖的基础条件靠谱吗？**

Smoke Test 是一条"快速通道验证"——不深入每个细节，只验证**最基础的条件是否满足**：文件存在？环境可用？API 可达？磁盘够？

### 适用判断

**必须执行**的场景（任一满足即走 Smoke）：
- 方案涉及**外部环境/依赖**（pip install、conda、CUDA、GPU）
- 方案需要**外部资源**（下载模型、连接 API、读写文件、挂载盘）
- 方案依赖**特定硬件**（GPU、大内存、特定架构）
- 方案有**前置数据/checkpoint** 必须就位才能执行

**建议跳过**的场景：
- 纯文本/纯代码修改（改变量名、写文档、重构无外部依赖的逻辑）
- 纯设计/规划类任务（不涉及执行）
- 信息查询类任务

### 执行方式

Smoke Test 使用 `check_runner.py` 批量执行一组快速检查命令：

```python
checks = [
    {"name": "磁盘空间 >= 10G",  "cmd": "df -h /data | tail -1 | awk '{print \$4}'", "expected": "G"},
    {"name": "Python 可用",      "cmd": "python3 --version", "expected": "3."},
    {"name": "GPU 可用",         "cmd": "python3 -c 'import torch; print(torch.cuda.is_available())'", "expected": "True"},
    {"name": "配置文件存在",      "cmd": "test -f config.yaml && echo exists", "expected": "exists"},
    {"name": "输出目录可写",      "cmd": "test -w /output && echo writable", "expected": "writable"},
]
# 一次调用，批量执行
result = subprocess.run(["python3", "skills/plan/check_runner.py", json.dumps(checks)], capture_output=True, text=True)
```

### 检查内容清单（按需选用）

#### 环境层
```
  Python 版本 >= 3.10          -> python3 --version
  Conda 环境可激活             -> conda activate <env> && which python
  GPU 可用                     -> python3 -c "import torch; print(torch.cuda.is_available())"
  显存余量                     -> python3 -c "import torch; print(torch.cuda.mem_get_info())"
  关键依赖已安装               -> python3 -c "import torch, numpy, ..."
```

#### 文件层
```
  输入数据存在                 -> test -f <path>
  Checkpoint 存在              -> test -f <ckpt_path>
  输出目录可写                 -> test -w <output_dir>
  脚本可执行                   -> test -x <script.sh>
```

#### 资源层
```
  磁盘空间                     -> df -h <path>
  内存余量                     -> free -h
  网络可达                     -> curl -s -o /dev/null -w "%{http_code}" <url>
  API key 已设置               -> echo $API_KEY | head -c 10
```

### 输出格式

```
  ─── Smoke Test ───
  [OK] Python 版本       python3 --version → 3.11.5
  [OK] GPU 可用          torch.cuda.is_available() → True
  [OK] 磁盘空间          df -h /data → 230G 可用
  [FAIL] 配置文件        test -f config.yaml → 不存在

  结果: 1 FAIL, 3 OK
  → 💡 修复 FAIL 后重新执行 Smoke Test，或确认后跳过继续
```

### 硬规则

1. **Smoke Test 全部通过 → 进入 Phase 3**
2. **Smoke Test 有 FAIL 但非阻断 → 提示用户后可选进入 Phase 3**
3. **Smoke Test 有阻断性 FAIL（无磁盘、无 GPU、无数据）→ 强制停止，修复后再来**
4. **跳过 Smoke Test 时，在输出中标注 ⏭️ Skipped**
5. **所有检查项耗时合计 < 60 秒**（超时说明不适合 Smoke）

### 与 Phase 4 的关系

| 维度 | Smoke Test（Phase 2.5） | 细节检查（Phase 4） |
|:----:|:-----------------------:|:------------------:|
| 时序 | Phase 2 之后，分解之前 | Phase 3 之后 |
| 深度 | 表面快速验证 | 逐项深入检查 |
| 耗时 | < 60s，批量执行 | 可长可短，循环修复 |
| 阻断级 | **全盘阻断**（环境不行方案白做） | **单任务阻断** |
| 适用性 | 可选（依赖外部条件时必做） | 必做 |
