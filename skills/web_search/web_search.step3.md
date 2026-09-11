## 💻 步骤③ — 执行搜索

使用 `pythonrt` 调用 `search.py`（沙箱双路径，见 makeskill 规则 13）：

### 方式1: plan/build 沙箱（importlib 动态加载——沙箱禁 import 项目内模块）

```python
# 路径 = 相对技能库根基准（规则 18）；挂载环境映射为 ../../xkagent_v0902/skills/web_search/search.py
import importlib.util, sys
spec = importlib.util.spec_from_file_location("search", "skills/web_search/search.py")
m = importlib.util.module_from_spec(spec); sys.modules["search"] = m
spec.loader.exec_module(m)

# 按源搜索（返回 list[dict]）
results = m.search_baidu("搜索词")
results = m.search_github("fastapi")
results = m.search_arxiv("multi-agent survey")
results = m.search_auto("深圳到北京高铁")
print(m.format_output("auto", "深圳到北京高铁", results))

# 如需加速（WASM 沙箱内），传入宿主回调
m.set_http_request_callback(http_request)
results = m.search_baidu("搜索词")  # 自动走回调
```

### 方式2: build-unsafe（直接 import）

```python
from skills.web_search import search
results = search.search_auto("深圳到北京高铁")
```

### 方式3: bash 工具（宿主执行，相对技能库根路径）

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
