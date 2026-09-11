#!/usr/bin/env python3
# deps: stdlib only
"""
OpenReview 论文 Review + Rebuttal 下载器

功能:
  1. 按论文标题搜索 OpenReview，获取 forum ID
  2. 下载 forum 下所有 notes（含分页）
  3. 按 invitation 字段分类为 review / rebuttal / meta_review
  4. 输出结构化 JSON 文件

兼容性:
  - 纯 Python 标准库（urllib），无需 pip install
  - 支持 WASM 沙箱（通过 http_request callback）
  - 支持 build-unsafe 模式（bash 命令行直接调用）

用法:
  # Python 导入
  from skills.openreview.openreview_downloader import download_paper
  result = download_paper("GPSToken")

  # 命令行
  python3 skills/openreview/openreview_downloader.py "GPSToken"
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

__version__ = "1.0.0"

# ── 常量 ──────────────────────────────────────────────

OPENREVIEW_BASE = "https://api.openreview.net"
USER_AGENT = "OpenReviewDownloader/1.0 (Academic Tool; +https://github.com/)"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 2.0  # 指数退避基数（秒）

# ── WASM 沙箱 HTTP 回调 ──────────────────────────────

_HTTP_REQUEST_CALLBACK = None

def set_http_request_callback(cb):
    """设置 http_request 回调（供 WASM 沙箱内使用）
    
    用法:
        from skills.openreview import openreview_downloader as od
        od.set_http_request_callback(http_request)
        od.download_paper("GPSToken")
    """
    global _HTTP_REQUEST_CALLBACK
    _HTTP_REQUEST_CALLBACK = cb


# ── HTTP 请求封装 ─────────────────────────────────────

def _fetch(url, params=None, method="GET", timeout=DEFAULT_TIMEOUT):
    """统一 HTTP GET 请求，支持 urllib 和 WASM callback 两种路径"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    full_url = url
    if params:
        full_url += '?' + urllib.parse.urlencode(params, doseq=True)

    # 尝试 urllib（标准路径）
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(full_url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                time.sleep(delay)
                continue
            # 最后一次失败 → 尝试 WASM callback
            if _HTTP_REQUEST_CALLBACK is not None:
                try:
                    r = _HTTP_REQUEST_CALLBACK(
                        method=method,
                        url=full_url,
                        headers=headers,
                        body=None
                    )
                    if r and r.get('status') in (200, 201):
                        body = r.get('body', '')
                        if isinstance(body, str):
                            return json.loads(body)
                        return body
                except Exception:
                    pass
            raise


# ── 核心函数 ──────────────────────────────────────────

def search_forum_by_title(title: str, limit: int = 10) -> str:
    """按论文标题搜索 OpenReview，返回第一个匹配的 forum ID

    Args:
        title: 论文标题（支持模糊匹配）
        limit: 返回结果数上限

    Returns:
        forum_id 字符串

    Raises:
        ValueError: 未找到匹配论文
    """
    params = {
        "content.title": title,
        "limit": limit
    }
    data = _fetch(f"{OPENREVIEW_BASE}/notes", params=params)

    notes = data.get("notes", [])
    if not notes:
        raise ValueError(f"未找到标题包含 '{title}' 的论文")

    # 优先匹配精确标题或最相关的论文笔记
    # OpenReview 中论文本身的 invitation 通常含 "Submission" 或 "Blind_Submission"
    for note in notes:
        inv = note.get("invitation", "")
        if "Submission" in inv or "Blind" in inv:
            return note["forum"]

    # 退而求其次，返回第一个结果的 forum
    return notes[0]["forum"]


def get_all_notes(forum_id: str, limit: int = 1000) -> list:
    """获取指定 forum 下的全部 notes（自动处理分页）

    Args:
        forum_id: OpenReview forum ID
        limit: 每页数量（最大 1000）

    Returns:
        notes 列表
    """
    all_notes = []
    cursor = None

    while True:
        params = {
            "forum": forum_id,
            "limit": limit
        }
        if cursor:
            params["cursor"] = cursor

        data = _fetch(f"{OPENREVIEW_BASE}/notes", params=params)
        notes = data.get("notes", [])
        all_notes.extend(notes)

        cursor = data.get("next", None)
        if not cursor:
            break

    return all_notes


def classify_notes(notes: list) -> dict:
    """将 notes 按 invitation 字段分类

    分类规则（通过 invitation 字段匹配）:
      - 论文本身:    "Submission" 或 "Blind_Submission"
      - Review:      含 "/Review" 但不含 "Meta" 或 "Submission"
      - Rebuttal:    含 "/Rebuttal" 或 "Author_Response" 或 "Official_Comment"
      - Meta Review: 含 "Meta_Review"

    Returns:
        {
            "paper": {dict} 或 None,
            "reviews": [{dict}, ...],
            "rebuttals": [{dict}, ...],
            "meta_review": {dict} 或 None,
            "other": [{dict}, ...]
        }
    """
    result = {
        "paper": None,
        "reviews": [],
        "rebuttals": [],
        "meta_review": None,
        "other": []
    }

    for note in notes:
        inv = note.get("invitation", "")

        # 论文本身
        if re.search(r'(?:Blind_)?Submission', inv):
            result["paper"] = note
        # Meta Review
        elif "Meta_Review" in inv:
            result["meta_review"] = note
        # Review（不含 Meta Review）
        elif "/Review" in inv and "Meta" not in inv:
            result["reviews"].append(note)
        # Rebuttal / Author Response / Official Comment
        elif any(kw in inv for kw in ["Rebuttal", "Author_Response", "Official_Comment"]):
            result["rebuttals"].append(note)
        else:
            result["other"].append(note)

    return result


def _extract_reviewer(note: dict) -> str:
    """从 note 中提取审稿人标识"""
    content = note.get("content", {})
    # OpenReview 常见字段名
    for key in ["reviewer", "anonymized_reviewer", "signatures"]:
        val = content.get(key, None)
        if val:
            if isinstance(val, list):
                return str(val[0])
            return str(val)
    # 从 signatures 提取
    sigs = note.get("signatures", [])
    if sigs:
        return sigs[0]
    return "anonymous"


def _simplify_note(note: dict, note_type: str = "review") -> dict:
    """简化 note 为易读的结构"""
    content = note.get("content", {})

    # 如果 content 本身是 dict，提取各字段
    simplified = {
        "id": note.get("id", ""),
        "invitation": note.get("invitation", ""),
        "forum": note.get("forum", ""),
        "date": note.get("tmdate", note.get("mtime", "")),
        "content": content
    }

    if note_type == "review":
        # 提取评分和置信度
        for key in ["rating", "confidence", "recommendation"]:
            if key in content:
                simplified[key] = content[key]
        simplified["reviewer"] = _extract_reviewer(note)

    return simplified


def download_paper(title: str, output_path: str = None, simplify: bool = True) -> dict:
    """一站式下载论文 Review + Rebuttal

    Args:
        title: 论文标题
        output_path: 输出 JSON 文件路径（None 则不写文件）
        simplify: 是否简化输出（只保留关键字段）

    Returns:
        {
            "title": str,
            "forum_id": str,
            "paper": {...},
            "reviews": [{...}],
            "rebuttals": [{...}],
            "meta_review": {...} or None,
            "stats": {"reviews": N, "rebuttals": N, "total_notes": N}
        }
    """
    # Step 1: 搜索 forum ID
    print(f"🔍 正在搜索论文: '{title}' ...")
    forum_id = search_forum_by_title(title)
    print(f"   ✅ forum ID: {forum_id}")

    # Step 2: 下载全部 notes
    print(f"📥 正在下载 notes (forum: {forum_id}) ...")
    notes = get_all_notes(forum_id)
    print(f"   ✅ 共下载 {len(notes)} 条 notes")

    # Step 3: 分类
    print(f"📋 正在分类 notes ...")
    classified = classify_notes(notes)
    print(f"   ✅ paper: {'找到' if classified['paper'] else '无'}")
    print(f"   ✅ reviews: {len(classified['reviews'])} 条")
    print(f"   ✅ rebuttals: {len(classified['rebuttals'])} 条")
    print(f"   ✅ meta_review: {'找到' if classified['meta_review'] else '无'}")

    # Step 4: 构建输出
    if simplify:
        output = {
            "title": title,
            "forum_id": forum_id,
            "paper": _simplify_note(classified["paper"], "paper") if classified["paper"] else None,
            "reviews": [_simplify_note(n, "review") for n in classified["reviews"]],
            "rebuttals": [_simplify_note(n, "rebuttal") for n in classified["rebuttals"]],
            "meta_review": _simplify_note(classified["meta_review"], "meta_review") if classified["meta_review"] else None,
            "stats": {
                "reviews": len(classified["reviews"]),
                "rebuttals": len(classified["rebuttals"]),
                "total_notes": len(notes)
            }
        }
    else:
        output = {
            "title": title,
            "forum_id": forum_id,
            "paper": classified["paper"],
            "reviews": classified["reviews"],
            "rebuttals": classified["rebuttals"],
            "meta_review": classified["meta_review"],
            "stats": {
                "reviews": len(classified["reviews"]),
                "rebuttals": len(classified["rebuttals"]),
                "total_notes": len(notes)
            }
        }

    # Step 5: 写文件
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"💾 已保存到: {output_path}")

    return output


# ── 命令行入口 ────────────────────────────────────────

def main():
    """命令行入口"""
    if len(sys.argv) < 2:
        print("用法: python openreview_downloader.py <论文标题> [输出文件路径]")
        print("示例: python openreview_downloader.py 'GPSToken'")
        sys.exit(1)

    title = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else f"{title}_openreview.json"

    try:
        result = download_paper(title, output_path)
        print(f"\n✅ 完成！统计:")
        print(f"   Reviews:  {result['stats']['reviews']}")
        print(f"   Rebuttals: {result['stats']['rebuttals']}")
        print(f"   输出文件: {output_path}")
    except Exception as e:
        print(f"\n❌ 失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
