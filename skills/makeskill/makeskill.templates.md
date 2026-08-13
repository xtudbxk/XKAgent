# 代码模板（懒加载）

> 📎 **加载条件**：仅在 Phase 5 需要生成代码文件时加载。
> 使用 `pythonrt 读取 skills/makeskill/makeskill.templates.md`

## skill.md 模板

```python
# pythonrt 内写 skill.md（备份+写入+验证三合一）
import os
os.makedirs('skills/<name>', exist_ok=True)
content = (
    '---\n'
    'name: <name>\n'
    'version: 1.0.0\n'
    'description: <30字描述>\n'
    'category: <workflow|tool>\n'
    'compatible_modes:\n'
    '  - plan\n'
    '  - build\n'
    'triggers:\n'
    '  - <触发词1>\n'
    'requires: {}\n'
    'author: system\n'
    '---\n\n'
    '## 概述\n\n主文件内容...\n'
)
with open('skills/<name>/skill.md', 'w', encoding='utf-8') as fh:
    fh.write(content)
print('written', len(content))
```
> 💡 宿主侧（build-unsafe 或用户 `!xxx`）也可用 bash heredoc 写入，但 **LLM 侧一律用 pythonrt**。


## 工具脚本模板

```python
"""<工具说明>

Usage:
    from skills.<name>.<script> import <func>
"""
import json
import sys
from pathlib import Path


def <func_name>(<params>) -> str:
    """执行功能，返回格式化字符串供 LLM 使用"""
    result = ...
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    result = <func_name>(*sys.argv[1:])
    print(result)
```

## 规范化模式备份模板

```python
# pythonrt 规范化：备份 → 重组 frontmatter + body → 写入
import os
src = 'skills/<name>/skill.md'
with open(src, 'rb') as f:          # ① 备份（纯 open 二进制复制）
    data = f.read()
with open(src + '.bak', 'wb') as f:
    f.write(data)
content = open(src, encoding='utf-8').read()  # ② 读取原 body
body = content.split('---', 2)[2] if content.startswith('---') else content
new_fm = '---\nname: <name>\nversion: 2.0.0\ndescription: ...\ncategory: ...\ncompatible_modes:\n  - plan\n  - build\ntriggers: [...]\nauthor: system\n---\n'
with open(src, 'w', encoding='utf-8') as f:   # ③ 写入新 frontmatter + body
    f.write(new_fm + '\n' + body)
print('normalized', src)
```

