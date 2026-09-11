# deps: stdlib only
"""web_search 技能 — 多源搜索工具（stdlib 版，兼容 WASM 沙箱 + Host Callbacks）

用法:
    from skills.web_search import search

    # 设置宿主回调（WASM 沙箱内用）
    search.set_http_request_callback(http_request)

    # 执行搜索
    results = search.search_baidu("搜索词")
    results = search.search_github("fastapi")
    results = search.search_auto("智能路由")
"""
from skills.web_search import search

__all__ = ["search"]
