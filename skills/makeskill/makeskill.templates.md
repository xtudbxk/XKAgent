# 代码模板（懒加载）

> 📎 **加载条件**：仅在 Phase 5 需要生成代码文件时加载。
> 使用 `pythonrt 读取 skills/makeskill/makeskill.templates.md`

## skill.md 模板

```python
# pythonrt 内写 skill.md（备份+写入+验证三合一）
# requires 字段：无依赖保持 {}；有 pip/技能依赖时改为 requires.pip / requires.skills（见 phase5 5.2）
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
> 💡 **一律使用 pythonrt 写入**（bash 仅限用户 `!xxx` 或 build-unsafe 宿主侧手动操作，LLM 工具侧不使用 bash）


## 工具脚本模板

```python
"""<工具说明：一句话总览>

Deps:
    stdlib only   # 或列出第三方库清单（格式见 makeskill.rules_detail.md 规则 14）
                  # 含第三方库时注明「需 build-unsafe 环境」

Usage:
    # 🔵🟢 plan/build 沙箱：importlib 动态加载（沙箱禁 import 项目内模块，见规则 13）
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "<script>", "skills/<name>/<script>.py")  # 相对技能库根基准；实际加载按环境映射（规则 18）
    m = importlib.util.module_from_spec(spec); sys.modules["<script>"] = m
    spec.loader.exec_module(m)
    m.<func_name>(...)
    # 🔥 build-unsafe：直接 import
    # from skills.<name>.<script> import <func_name>
"""
import json
import sys

__all__ = ["<func_name>"]   # 对外 API 白名单（规则 15）


def <func_name>(<params>) -> str:
    """<功能一句话>

    Args:
        <param> (<type>): <说明>
    Returns:
        str: JSON 字符串，结构示例: {"key": "value"}
    Example:
        m.<func_name>(<示例参数>)
        # -> {"key": "value"}
    """
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

