## 💻 步骤③ — 执行搜索

使用 `pythonrt` 调用 `search.py`：

### 方式1: pythonrt（build-unsafe 无限制模式推荐）

```python
from skills.web_search import search

# 按源搜索
results = search.search_baidu("搜索词")
results = search.search_github("fastapi")
results = search.search_arxiv("multi-agent survey")
results = search.search_auto("深圳到北京高铁")

# 如需加速（WASM 沙箱内），传入宿主回调
search.set_http_request_callback(http_request)
results = search.search_baidu("搜索词")  # 自动走回调
```

### 方式2: bash 工具（build-unsafe 模式）

```bash
python3 skills/web_search/search.py -s baidu "搜索词"
python3 skills/web_search/search.py -s auto "混合内容" -n 5
python3 skills/web_search/search.py --list-sources
```

### 通用参数

```
-n, --max-results    结果数 (默认: 10)
--timeout            超时秒数 (默认: 20)
--delay              请求间隔秒数 (默认: 1.0)
```

> 💡 所有搜索函数返回统一格式的 `list[dict]`：
> `[{"title": "...", "summary": "...", "url": "..."}, ...]`
