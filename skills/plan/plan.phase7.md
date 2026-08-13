## Phase 7: Final Verification（可选）

### 核心问题

> **执行完 Todo List 后，方案真的 work 了吗？**

Phase 7 是全部执行完毕后的**一次轻量收尾验证**。不重新跑整个流程，而是用几条关键命令验证最终状态是否符合成功标准。

### 适用判断

**必须执行**的场景（任一满足即走 Final Verification）：
- 方案有**可执行的验证命令**（`diff`、`python test.py`、`pytest`）
- 方案产生了**可检查的输出**（文件、日志、目录结构）
- 用户明确要求"完成后验证一下"

**建议跳过**的场景：
- 纯文档/配置修改（改完即完，无执行步骤）
- 已经通过 Phase 6 执行中的自查充分验证过
- 方案本身就是一次验证（如"看看这个日志有什么问题"）

### 执行方式

LLM 根据具体的 Todo List 生成验证命令，逐条执行：

```
  验证项                               验证命令
  ───────────────────────────────────────────────────
  [ ] 改动文件 diff 合理               diff <备份> <修改后>
  [ ] 关键依赖可导入                   python3 -c "import <模块>"
  [ ] 输出文件已生成                   test -f <output_path>
  [ ] 日志无异常                       grep -i "error\|exception\|OOM" <log>
  [ ] 功能端到端测试                   python3 test.py --quick
```

### 检查内容模版（按需选用）

#### 代码修改类
```
  文件与预期 diff 一致           -> diff <backup> <modified>
  Syntax 检查无报错              -> python3 -m py_compile <file>
  单元测试通过                   -> python3 -m pytest tests/ -x -q
  Lint 通过                      -> ruff check <file> 或 pylint <file>
```

#### 实验运行类
```
  训练日志无异常                 -> grep -i "nan\|inf\|error\|OOM" <log>
  Loss 正常下降                  -> tail -5 <log> | grep loss
  Checkpoint 已保存              -> ls -lh <ckpt_dir>/latest.pth
  评估结果合理                   -> cat <eval_report>
```

#### 数据/配置类
```
  输出目录结构正确               -> ls -R <output_dir> | head -20
  关键参数对齐                   -> diff <config> <backup_config>
  数据完整性                     -> wc -l <data> (行数符合预期)
```

### 输出格式

```
  ─── Phase 7: Final Verification ───
  [OK] diff 检查        代码改动与预期一致
  [OK] syntax 检查      python3 -m py_compile -> 无报错
  [OK] 单元测试         3 passed in 0.42s
  [OK] 输出文件         输出文件已生成: output/result.json

  结果: 4/4 通过 ✅
  -> 方案验证完毕，所有检查通过。
```

### 硬规则

1. **Phase 7 不得修改任何文件** — 只检查，不修复（修复回到 Phase 4/6）
2. **Phase 7 全部通过 → 输出 ✅ 方案完成**
3. **Phase 7 有 FAIL 但不涉及核心目标 → WARN 用户，可不修复**
4. **Phase 7 有 FAIL 且涉及核心目标 → 建议回到 Phase 6 修复**
5. **跳过 Phase 7 时，在输出中标注 ⏭️ Skipped**
6. **验证命令总数建议 ≤ 5 条**（多了变成重跑整个流程）
