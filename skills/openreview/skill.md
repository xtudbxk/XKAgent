---
name: openreview
version: 1.1.0
description: 从OpenReview下载论文Review和Rebuttal
category: tool
compatible_modes:
  - build
  - build-unsafe
triggers:
  - openreview
  - review
  - rebuttal
  - 论文评审
  - OpenReview下载
  - 审稿意见
  - 论文rebuttal
requires: {}
author: system
open_source: true
---

## 概述

从 OpenReview 公开 API 下载指定论文的 **Review（评审意见）** 和 **Rebuttal（作者回复）** 内容，输出为结构化 JSON 文件。

**工作流程**：
1. 按论文标题搜索 → 获取 OpenReview forum ID
2. 下载 forum 下全部 notes（自动处理分页）
3. 按 invitation 字段分类：review / rebuttal / meta_review
4. 输出结构化 JSON

---

## 🧭 Mode Pre-Check

本技能兼容 **build** 和 **build-unsafe** 两种模式。进入前先检测当前模式。

| 检测方式 | 说明 |
|---------|------|
| 查看用户消息前缀 | 「模式: build」或「模式: build-unsafe」 |
| 检查执行工具可用性 | build-unsafe 下 pythonrt 无限制（可访问宿主一切资源） |

### 模式策略

| 模式 | 执行策略 |
|------|---------|
| 🟢 **build** | 通过 `pythonrt` 工具调用脚本（可写受限模式）。<br>网络默认经受控通道；如网络不通提示用户用 `!xxx` 或切 build-unsafe |
| 🔥 **build-unsafe** | 通过 `pythonrt`（无限制）执行。<br>推荐：`pythonrt` 脚本调用，或提示用户 `!python3 skills/openreview/openreview_downloader.py "论文标题"` |

---


## 文件结构

| 文件 | 说明 |
|------|------|
| `skill.md` | 本文件（流程 + API 文档） |
| `__init__.py` | 包导出（`from skills.openreview import ...`） |
| `openreview_downloader.py` | 核心实现（纯 stdlib urllib，WASM 兼容） |

---

## 快速使用

### 方式 1: Python 导入（推荐，所有模式）

```python
from skills.openreview import openreview_downloader as od

# 一站式下载
result = od.download_paper("GPSToken")
# 或指定输出路径
result = od.download_paper("GPSToken", output_path="GPSToken_openreview.json")

# 分步调用（灵活控制）
forum_id = od.search_forum_by_title("GPSToken")
notes = od.get_all_notes(forum_id)
classified = od.classify_notes(notes)

# 查看结果
print(f"Reviews: {result['stats']['reviews']} 条")
print(f"Rebuttals: {result['stats']['rebuttals']} 条")
for r in result['reviews']:
    print(f"  审稿人: {r.get('reviewer')}, 评分: {r.get('rating')}")
```

### 方式 2: WASM 沙箱加速（build 模式，网络受限时）

```python
from skills.openreview import openreview_downloader as od

# 设置 http_request 回调（加速 HTTPS 请求）
od.set_http_request_callback(http_request)
result = od.download_paper("GPSToken")
```

### 方式 3: 命令行（build-unsafe 模式）

```bash
# 直接运行（自动输出 JSON 到当前目录）
python3 skills/openreview/openreview_downloader.py "GPSToken"

# 指定输出路径
python3 skills/openreview/openreview_downloader.py "GPSToken" my_output.json
```

---

## API 参考

### `download_paper(title, output_path=None, simplify=True)`

一站式下载入口。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `title` | str | 必填 | 论文标题（支持模糊匹配） |
| `output_path` | str | None | JSON 输出路径，None 则不写文件 |
| `simplify` | bool | True | 是否简化输出（仅保留关键字段） |

**返回结构**：
```json
{
  "title": "GPSToken",
  "forum_id": "...",
  "paper": {"id": "...", "content": {...}},
  "reviews": [
    {"id": "...", "invitation": "...", "reviewer": "...", 
     "rating": "...", "confidence": "...", "content": {...}, "date": "..."}
  ],
  "rebuttals": [
    {"id": "...", "invitation": "...", "content": {...}, "date": "..."}
  ],
  "meta_review": {...} or null,
  "stats": {"reviews": 3, "rebuttals": 2, "total_notes": 10}
}
```

### `search_forum_by_title(title, limit=10)`

按标题搜索论文，返回 forum ID。

### `get_all_notes(forum_id, limit=1000)`

下载 forum 下全部 notes，自动处理 cursor 分页。

### `classify_notes(notes)`

按 invitation 字段分类 notes。

**分类规则**：

| invitation 包含 | 归类 |
|----------------|------|
| `Submission` 或 `Blind_Submission` | paper（论文本身） |
| `/Review` 且不含 `Meta` | reviews[] |
| `Rebuttal` 或 `Author_Response` 或 `Official_Comment` | rebuttals[] |
| `Meta_Review` | meta_review |
| 其他 | other[] |

### `set_http_request_callback(cb)`

设置 WASM 沙箱 HTTP 回调（仅 build 模式需要）。

---

## 输出 JSON 格式

```json
{
  "title": "论文标题",
  "forum_id": "forum_id字符串",
  "paper": { ... },
  "reviews": [
    {
      "id": "note_id",
      "invitation": "xxx/-/Review",
      "reviewer": "审稿人标识",
      "rating": "评分（如 3: Good）",
      "confidence": "置信度（如 3: Good）",
      "content": { "title": "...", "review": "...", ... },
      "date": "时间戳"
    }
  ],
  "rebuttals": [
    {
      "id": "note_id",
      "invitation": "xxx/-/Rebuttal",
      "content": { ... },
      "date": "时间戳"
    }
  ],
  "meta_review": { ... },
  "stats": {
    "reviews": 3,
    "rebuttals": 2,
    "total_notes": 10
  }
}
```

---

## 注意事项

1. **公开论文**：本工具仅适用于 OpenReview 上公开可读的论文
2. **API 限流**：OpenReview API 没有公开的限流说明，建议间隔 ≥1 秒的请求
3. **标题搜索**：API 的 `content.title` 是模糊匹配，返回前 N 条；脚本自动优先匹配 `Submission` 类型的 note
4. **WASM 兼容**：脚本使用纯 `urllib`（Python 标准库），无需 `pip install`
5. **分页**：OpenReview API 使用 cursor 分页，脚本自动处理全部翻页
