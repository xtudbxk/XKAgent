## Phase 4: 细节检查 + 修复循环

对 Phase 3 的每个子任务，生成细粒度检查项并逐项验证。通过循环修复直到全部通过。

### 使用批量检查工具（推荐）

本技能提供 `check_runner.py` 脚本，可通过 bash 一次性执行多个检查命令：

```python
# 将多个检查项打包为一条调用
checks = [
    {"name": "Python 版本 >= 3.10", "cmd": "python3 --version", "expected": "3.10"},
    {"name": "GPU 可用",             "cmd": "python3 -c 'import torch; print(torch.cuda.is_available())'", "expected": "True"},
    {"name": "磁盘空间",             "cmd": "df -h /data", "expected": "/data"},
    {"name": "配置文件存在",          "cmd": "test -f config.yaml && echo exists", "expected": "exists"},
    {"name": "输出目录可写",          "cmd": "test -w /output && echo writable", "expected": "writable"},
]
result = subprocess.run(["python3", "skills/plan/check_runner.py", json.dumps(checks)], capture_output=True, text=True)
```

返回格式化的终端风格报告：

```
  [OK] Python 版本 >= 3.10
  [OK] GPU 可用
  [FAIL] 磁盘空间
     cmd: df -h /data
     out: /data  5G  ... (需 >= 20G)
  [OK] 配置文件存在
  [OK] 输出目录可写
```

### 手动检查项格式（不批量时用）

```
  [状态] 检查描述
         - 类型: env / file / config / precondition / result
         - 验证方式: 命令 / 文件存在 / 人工确认
         - 失败修复: 建议的修复方案或回退策略
```

### 常见检查项模板

#### 环境检查

```
  Python 版本 >= 3.10          -> python3 --version
  Conda 环境可激活             -> conda activate ...
  GPU 可用                    -> python3 -c "import torch; print(torch.cuda.is_available())"
  关键依赖已安装               -> python3 -c "import torch, numpy, ..."
```

#### 文件检查

```
  待修改文件存在               -> ls <path>
  目标目录存在                 -> ls <dir>
  文件权限可写                 -> test -w <path>
  参考文件/checkpoint 存在     -> ls <ref_path>
```

#### 配置检查

```
  超参文件格式正确             -> source scripts/jit.sh 无报错
  路径配置有效                 -> 展开后路径可达
  与基线分支的参数 diff 合理   -> diff <基线>/jit.sh <当前>/jit.sh
```

#### 运行检查

```
  提交命令 dry-run 通过        -> exp_submit.py ... --dry-run
  输出目录已清理/已准备        -> ls output_dir
  端口/资源不冲突              -> exp_status.py 确认
```

### 执行规则

1. **逐项执行**检查项的验证命令
2. 每完成一项，**立即更新进度显示**
3. 失败项记录：
   - 失败原因（命令输出）
   - 建议修复方案
   - 是否自动修复 or 需用户确认

### 进度显示格式

```
  Phase 4: 细节检查  [######....]  6/10
  Round 2/3
  - [OK] Python 版本        通过
  - [OK] GPU 可用           通过
  - [FAIL] Conda 环境       失败 -> 修复中
  - [OK] 关键依赖           通过

  总进度: [######......]  6/15 (40%)
```

### 修复策略

| 失败类型 | 修复策略 | 是否需要用户确认 |
|----------|----------|----------------|
| 环境未激活 | 自动激活 | 可跳过确认 |
| 依赖缺失 | 自动 pip install | 建议确认 |
| 文件不存在 | 创建/复制 | 建议确认 |
| 配置错误 | 自动修正 | 建议确认 |
| 条件不满足 | 提示用户操作 | 必须确认 |

### 修复后标记

- 修复完成 -> 该检查项标记为 `通过`
- 无法修复 -> 标记为 `blocked`，记录原因，进入下面的循环判断

### 循环判断逻辑

```
所有检查项是否通过？
  - 是 -> 进入 Phase 5（复杂度评估）
  - 否，有 blocked 项 -> 向用户报告 blocker，商议是否继续
  - 否，有修复项 ->
      - 检查停止规则
      - 停止 -> 强制退出，输出报告
      - 继续 -> 返回 Phase 4 开头，只保留失败项重检
```

### 停止规则

| 规则 | 条件 | 行为 |
|------|------|------|
| 规则 A | 循环超过 **5 轮** | 强制退出，报告"Plan 过于复杂，请简化方案" |
| 规则 B | 同一检查项**连续 2 轮**同一原因失败 | 标记为 blocked，停止循环，退出 |
| 规则 C | 修复需要**改变核心方案方向** | 退回 Phase 2 重来，不继续在当前 path 下循环 |

### 进度累积规则

- 已通过的检查项**不再重复检查**
- 每轮循环只重新检查上一轮失败项
- 总进度累计，不归零

### 循环示例

```
Round 1: 15 checks, 3 failed -> 修复
Round 2: 3 re-checks, 1 failed -> 修复
Round 3: 1 re-check, all pass [OK]
总进度: [############] 15/15 (100%)
```
