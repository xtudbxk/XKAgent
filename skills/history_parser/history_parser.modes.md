# 提取模式详解（7 种模式）

所有函数通过 `pythonrt` 工具调用，返回 JSON 字符串。

---

## 模式 A: raw_dump — 原始转储

```python
from skills.history_parser.message_extractor import dump_messages
import json

result = json.loads(dump_messages(
    session="default",
    output_format="json",   # "json" | "markdown" | "text"
    limit=100,              # 最多返回条数
    offset=0                # 偏移量
))
```

## 模式 B: by_keyword — 关键词搜索

```python
from skills.history_parser.message_extractor import filter_by_keyword
import json

result = json.loads(filter_by_keyword(
    session="default",
    keyword="API设计",
    regex=False,                # 是否作为正则解释
    case_sensitive=False,       # 是否大小写敏感
    role="user",                # 可选：限制角色
    output_format="json",
    limit=50
))
```

## 模式 C: by_time — 时间范围

```python
from skills.history_parser.message_extractor import filter_by_time
import json

result = json.loads(filter_by_time(
    session="default",
    start="2026-07-21 10:00",   # 或 "2026-07-21"
    end="2026-07-21 18:00",
    role=None,                   # 可选：限制角色
    output_format="json"
))
```

## 模式 D: by_role — 按角色

```python
from skills.history_parser.message_extractor import filter_by_role
import json

result = json.loads(filter_by_role(
    session="default",
    role="assistant",        # user / assistant / tool / compact
    output_format="json",
    limit=50
))
```

## 模式 E: extract_code — 提取代码块

```python
from skills.history_parser.message_extractor import extract_code_blocks
import json

result = json.loads(extract_code_blocks(
    session="default",
    language="python",       # 可选：指定语言过滤
    output_format="json"     # 返回: id, language, code, context
))
```

## 模式 F: extract_json — 提取 JSON

```python
from skills.history_parser.message_extractor import extract_json_objects
import json

result = json.loads(extract_json_objects(
    session="default",
    output_format="json"
))
```

## 模式 G: custom — 自定义查询

```python
from skills.history_parser.message_extractor import query_custom
import json

result = json.loads(query_custom(
    session="default",
    where_clause="role='user' AND turn > 5",
    output_format="json"
))
```
