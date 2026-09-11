## Phase 6: 验证

### 6.1 自动验证

文件生成后立即执行（两步）：

**① frontmatter/存在性检查**（skill.md）：

```python
# pythonrt 自动验证：存在性 + frontmatter 首行
import os

name = '<name>'
base = f'skills/{name}'
checks = []
sm = os.path.join(base, 'skill.md')
if os.path.isfile(sm):
    with open(sm, encoding='utf-8') as fh:
        first = fh.readline().strip()
    checks.append('OK: skill.md' if first == '---' else 'FAIL: frontmatter 首行非 ---')
else:
    checks.append('FAIL: skill.md 缺失')
print('\n'.join(checks))
```

**② 脚本 API 约定检查**（verify_skill_api.py，五项合一，见 rules_detail 规则 17；报告含 `paths` 路径诊断字段）：

```python
# 🔵🟢 plan/build 沙箱（build-unsafe 可直接 from skills.makeskill.verify_skill_api import verify_skill）
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "verify_skill_api", "skills/makeskill/verify_skill_api.py")
m = importlib.util.module_from_spec(spec); sys.modules["verify_skill_api"] = m
spec.loader.exec_module(m)
print(m.verify_skill(base, ['<script_name>.py', '<tool_script>.py']))
```

判定：WARN 项须修复后重跑；FAIL 项禁止发布。

### 6.2 推荐运行 /validate

```
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
