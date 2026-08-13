## Phase 6: 验证

### 6.1 自动验证

文件生成后立即执行：

```bash
# skill.md 存在 && frontmatter 格式正确
test -f skills/<name>/skill.md && echo "OK: skill.md"
head -1 skills/<name>/skill.md | grep -q "^---$" && echo "OK: frontmatter"

# 如果有脚本文件，检查存在
test -f skills/<name>/<script_name>.py && echo "OK: script"

# 如果有工具脚本，检查存在
test -f skills/<name>/<tool_script>.py && echo "OK: <tool_script>.py"
```

### 6.2 推荐运行 /validate

```bash
/validate <name>
```

### 6.3 最终输出

```
━━━ makeskill 完成 — <name>

主类型:  workflow
工具:    是（1 个工具脚本）

文件:
  ✅ skills/code_review/skill.md
  ✅ skills/code_review/complexity.py
  ✅ skills/code_review/complexity.py

建议:
  /validate code_review     检查格式
  /code_review              加载使用
```

---

---

### 6.4 embedding 刷新 + 召回验证（宿主适配，必做）

技能生成/修改后，刷新技能向量索引并验证召回：

```
# 方式 A：repl/web 端命令
/updateskillembedding <name>

# 方式 B：pythonrt 内验证
pythonrt: from codes.skill import searchskill; print(searchskill("<技能描述>", top_k=3))
```

验证标准：
- ✅ `searchskill` 结果包含 `<name>` → 完成
- ❌ 未命中 → 检查 `description`/`triggers` 关键词 → 修正后重跑 `/updateskillembedding`

> 若 `/validate <name>` 报告格式错误（缺 frontmatter 字段/name 不一致/category 非法/version 非 semver），
> 先修复再刷新 embedding，避免把坏元数据写入索引。
