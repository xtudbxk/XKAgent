## Phase 3: 输出报告

### 报告格式

```
## 📋 Check Report: <检查对象名>

### 🎯 Phase 1: 逻辑检查  [███████░] 8/9

  ─── 9 项检查维度 ───
  ✅ 目标对齐       方案直接解决"降低 FID"目标
  ✅ 因果链合理     larger CFG → more contrast → lower FID
  ⚠️ 假设显式化    假设 GPU 显存 ≥ 24G → 确认 A100 40G
  ✅ 竞品方案       已确认无更简单同成本方案
  ❌ 完整性缺口    漏了 reference stats 准备步骤
  ✅ 风险预估       OOM 回退 → reduce batch size
  ⚠️ 证据等级      基于 1 次观察，建议增加验证
  ✅ 可执行性       资源匹配

  → 💡 结论: 1 FAIL (完整性缺口), 2 WARN → 建议修改后进入 Phase 2

### 🔧 Phase 2: 细节检查  [████░░░░] 4/8

  ── Layer 1: 硬阻断 ──
  ✅ checkpoint-last.pth 存在    💻 test -f /path/to/checkpoint-last.pth
  ✅ 输出目录可写                💻 test -w /output/
  ❌ 磁盘空间                    💻 df -h → /data 只剩 5G，需要 ≥ 20G ← 🛑 STOP
  ── (以下未执行，Layer 1 阻断) ──

### 📊 总结

  ┌──────────────────────────────────────────────────┐
  │ 🎯 Phase 1: 逻辑检查    [8/9] ████ 通过 (建议修改)  │
  │ 🔧 Phase 2: 细节检查    [1/3] ████░ 阻断于 Layer 1  │
  │                                                    │
  │ ❌ 严重问题: 1 (完整性缺口)                           │
  │ ⚠️ 细节问题: 1 (磁盘空间不足)                         │
  │                                                    │
  │ 💡 建议: 修复后重新 check                            │
  └──────────────────────────────────────────────────┘
```

### 可选：留存检查历史

每次 check 同时输出一份机读 JSON，便于后续 drift 对比：

```
check-report-{timestamp}.md      # 人读
check-report-{timestamp}.json    # 机读，用于 diff
```

下次 check 时自动读取上次 JSON，只输出**状态发生变化的检查项**。

---

