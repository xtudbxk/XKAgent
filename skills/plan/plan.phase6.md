## Phase 6: 形成 Todo List

### 输出格式

```
## 执行计划: <任务标题>

总进度: [########....] 8/15 (53%)  总预估耗时: ~2h
复杂度评级: 中等

### Phase: 环境准备 (3/5 [OK] 60%)
  [x] T3 - 激活 conda 环境         [OK]
  [ ] T4 - 安装依赖包              文件: requirements.txt -> 命令: pip install -r
                                     耗时: S 完成条件: pip list 验证所有包已安装
  [ ] T5 - 验证 GPU 可用          命令: python3 -c "import torch; print(torch.cuda.is_available())"
                                     耗时: S 完成条件: 返回 True

### Phase: 配置修改 (0/3 [ ] 0%)
  [ ] T6 - 修改训练脚本参数        文件: scripts/launch.sh
                                     配置: BATCH_SIZE=64, LR=1e-4
                                     耗时: S 完成条件: diff 确认变更
  [ ] T7 - 设置 checkpoint 来源   命令: cp ../experiments/.../checkpoint-last.pth .
                                     耗时: S 完成条件: ls 确认文件存在
```

### 进度图表

```
  Phase 1: 意图澄清    [1/1] #### 100%
  Phase 2: 逻辑检查    [7/7] #### 100%
  Phase 3: 任务分解    [1/1] #### 100%
  Phase 4: 细节检查    [3/5] ###.  60%
  Phase 5: 复杂度评估  [1/1] #### 100%
  Phase 6: 执行训练    [0/2] ....   0%
```

---
