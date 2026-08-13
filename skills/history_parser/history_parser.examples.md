# 典型工作流

## Step 1: 发现可用 sessions

```python
from skills.history_parser.session_scanner import list_sessions
import json

sessions = json.loads(list_sessions())
# 返回: [{name, total_messages, time_from, time_to, roles_distribution}, ...]
```

## Step 2: 选择提取模式

根据用户需求从 7 种模式中选择一种或多种组合。

## Step 3: 提取 + 分析

调用对应函数获取数据，LLM 对结果进行总结/归纳。

## Step 4 (可选): 解析 extras

```python
from skills.history_parser.extras_parser import parse_extras
import json

extras_info = json.loads(parse_extras("default"))
# 返回: tool_calls, tool_results, compact_markers, stats
```

---

# 使用示例

### 示例 1: 用户问"看看 default session 里我们聊了什么"

```
1. 调用 list_sessions() 确认 default 存在
2. 调用 dump_messages("default", output_format="markdown", limit=30)
3. 根据返回内容，总结对话要点给用户
```

### 示例 2: 用户问"帮我找出上次讨论的 API 设计方案"

```
1. 调用 filter_by_keyword("default", keyword="API", output_format="json")
2. 从结果中定位到相关对话段
3. 提取关键信息呈现给用户
```

### 示例 3: 用户问"把 session_xxx 中的 Python 代码都提取出来"

```
1. 调用 extract_code_blocks("session_xxx", language="python", output_format="markdown")
2. 将代码块列表呈现给用户
```

### 示例 4: 用户问"今天上午助手回复了什么"

```
1. 调用 filter_by_time("default", start="2026-07-21 00:00", end="2026-07-21 12:00", role="assistant")
2. 整理摘要
```

### 示例 5: 用户问"之前调用了哪些工具"

```
1. 调用 parse_extras("default")
2. 查看 tool_calls 列表中的函数名
3. 如需详情，再调 filter_by_role("default", role="tool")
```

---

# 注意事项

1. **读取前 sync**：读取 session 前先调用 `sync_session()`（见 codes/history.py）确保 WAL 日志合并
2. **session 校验**：session 名称仅允许字母、数字、下划线、连字符、点号
3. **大数据**：默认限制 1000 条，使用 `limit` + `offset` 分页
4. **compact 处理**：`get_messages()` 已自动处理 compact 标记
5. **role='git'**：Git 快照消息内部使用，通常不参与提取
6. **custom 模式安全**：WHERE 子句禁止 DROP/DELETE/INSERT/UPDATE/ALTER 等 DML 关键词
