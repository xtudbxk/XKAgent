#!/usr/bin/env python3
# deps: stdlib only
"""web_search 连通性测试脚本 — 遍历全部搜索源，测试连通性与解析可用性。

用法:
    python3 test_connectivity.py                # 全量测试（默认查询词 "python"）
    python3 test_connectivity.py "transformer"  # 指定查询词
    python3 test_connectivity.py --timeout 5    # 指定每源超时

输出:
    connectivity_report.json   — 结构化结果（LLM/程序消费）
    connectivity_report.md     — 人类可读报告

Deps:
    stdlib only   # 需在可联网环境运行（build-unsafe 或宿主）

Usage(沙箱双路径):
    # 🔵🟢 plan/build 沙箱：importlib 动态加载
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("test_connectivity", "skills/web_search/test_connectivity.py")
    m = importlib.util.module_from_spec(spec); sys.modules["test_connectivity"] = m
    spec.loader.exec_module(m)
    report = m.test_source("baidu")   # 单源连通性测试
    # 宿主 CLI：python3 test_connectivity.py [query] [--timeout N]
"""
import argparse
import json
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_spec = importlib.util.spec_from_file_location("ws_search_mod",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "search.py"))
ws_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_mod)

# 每个源是否期望"可能反爬"（反爬源 0 条不算失败，仅警告）
ANTI_BOT_SOURCES = {"baidu", "weixin_sogou", "sogou", "bilibili", "zhihu"}
# 需代理的源（无代理时失败属预期，标注）
PROXY_SOURCES = {"google", "duckduckgo", "github", "arxiv", "semantic_scholar",
                 "wikipedia", "sourcegraph"}


def test_source(name, query, timeout):
    """测试单个源，返回结果状态。

    Args:
        name (str): 见上方说明
        query (str): 见上方说明
        timeout (str): 见上方说明
    Returns:
        dict: 连通性报告（ok/error/耗时等）
    Example:
        m.test_source("baidu")
    """
    func = ws_mod.SOURCE_FUNCS.get(name)
    if not func:
        return {"source": name, "status": "SKIP", "reason": "无实现函数", "count": 0}
    t0 = time.time()
    try:
        results = func(query, max_results=3, timeout=timeout)
        elapsed = round(time.time() - t0, 2)
        count = len(results)
        sample = results[0]["title"][:60] if results else ""
        if count > 0:
            status = "OK"
            reason = "正常"
        elif name in ANTI_BOT_SOURCES:
            status = "WARN"
            reason = "反爬/JS渲染（可能返回0）"
        elif name in PROXY_SOURCES:
            status = "WARN"
            reason = "需代理（当前未配置）"
        else:
            status = "FAIL"
            reason = "无结果"
        return {"source": name, "status": status, "reason": reason,
                "count": count, "elapsed": elapsed, "sample": sample}
    except Exception as e:
        return {"source": name, "status": "ERROR", "reason": f"{type(e).__name__}: {str(e)[:60]}",
                "count": 0, "elapsed": round(time.time() - t0, 2), "sample": ""}


def main():
    """CLI 入口（argparse 解析参数，遍历全源测试）

    Args:
        (无参数)
    Returns:
        None: 退出码
    Example:
        # CLI: python3 test_connectivity.py "python"
    """
    parser = argparse.ArgumentParser(description="web_search 连通性测试")
    parser.add_argument("query", nargs="?", default="python", help="测试查询词")
    parser.add_argument("--timeout", type=int, default=8, help="每源超时秒数")
    args = parser.parse_args()

    print(f"🔍 web_search 连通性测试 | 查询词: {args.query!r} | 超时: {args.timeout}s")
    print(f"   共 {len(ws_mod.SOURCE_NAMES)} 个源\n")

    results = []
    for name in ws_mod.SOURCE_NAMES:
        r = test_source(name, args.query, args.timeout)
        results.append(r)
        mark = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌", "ERROR": "💥", "SKIP": "⏭️"}.get(r["status"], "?")
        print(f"  {mark} {name:18s} {r['status']:5s} {r['count']}条 {r['elapsed']:5.1f}s {r['reason']}")

    # 汇总
    ok = sum(1 for r in results if r["status"] == "OK")
    warn = sum(1 for r in results if r["status"] == "WARN")
    fail = sum(1 for r in results if r["status"] in ("FAIL", "ERROR"))
    summary = {
        "query": args.query,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(results), "ok": ok, "warn": warn, "fail": fail,
        "sources": results,
    }

    # 保存 JSON
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectivity_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n📊 汇总: {ok} 正常 / {warn} 警告 / {fail} 失败")
    print(f"📁 JSON: {json_path}")

    # 保存 Markdown
    md_lines = [
        f"# web_search 连通性测试报告",
        "",
        f"- **时间**: {summary['timestamp']}",
        f"- **查询词**: `{args.query}`",
        f"- **结果**: {ok} 正常 / {warn} 警告 / {fail} 失败 / 共 {len(results)} 源",
        "",
        "| 源 | 状态 | 结果数 | 耗时(s) | 说明 |",
        "|---|---|---|---|---|",
    ]
    status_icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌", "ERROR": "💥", "SKIP": "⏭️"}
    for r in results:
        md_lines.append(
            f"| {r['source']} | {status_icon.get(r['status'],'?')} {r['status']} | {r['count']} | {r['elapsed']} | {r['reason']} |"
        )
    md_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectivity_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")
    print(f"📄 Markdown: {md_path}")


if __name__ == "__main__":
    main()
