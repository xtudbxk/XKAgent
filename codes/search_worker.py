"""codes/search_worker.py — searchinfo/searchskill 的 worker 子进程入口。

背景（2026-08-21）：搜索类工具原本在 agent 进程内同步执行，阻塞期间
无法响应中断事件（回合卡死、前端 busy 恒 True）。本模块作为子进程入口，
由 tools._run_search_worker 经 _run_worker_streaming 启动：50ms 轮询
中断/超时 → SIGKILL，与 pythonrt 获得同构的可中断性。

协议:
    stdin:  JSON {kind, dirs_paths?, query, top_k?, session?}
            kind: "searchinfo" | "searchskill" | "searchskill_detail"
    stdout: __SEARCH_RESULT__{json}
            {"ok": true, "items"/"names": [...]} 或 {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        params = json.load(sys.stdin)
    except Exception as e:
        print("__SEARCH_RESULT__" + json.dumps(
            {"ok": False, "error": f"params 解析失败: {e}"}, ensure_ascii=False))
        return

    kind = params.get("kind", "")
    query = params.get("query", "")
    top_k = int(params.get("top_k") or 5)
    session = params.get("session")
    try:
        from codes.search import searchinfo, searchskill, searchskill_detail
        if kind == "searchinfo":
            items = searchinfo(list(params.get("dirs_paths") or []),
                               query, top_k=top_k, session=session)
            payload = {"ok": True, "items": items}
        elif kind == "searchskill":
            names = searchskill(query, top_k=top_k)
            payload = {"ok": True, "names": names}
        elif kind == "searchskill_detail":
            items = searchskill_detail(query, top_k=top_k)
            payload = {"ok": True, "items": items}
        else:
            payload = {"ok": False, "error": f"unknown kind: {kind!r}"}
    except Exception:
        import traceback
        payload = {"ok": False, "error": traceback.format_exc()}
    try:
        out = json.dumps(payload, ensure_ascii=False)
    except Exception:
        out = json.dumps(payload, ensure_ascii=False, default=str)
    print("__SEARCH_RESULT__" + out)


if __name__ == "__main__":
    main()
