---
name: paper_discuss
version: 3.0.0
description: arXiv 论文定位+深入+对比讨论
category: workflow
compatible_modes:
  - plan
  - build
triggers:
  - 讨论论文
  - 对比论文
  - 论文讨论
  - paper discuss
  - 论文检索
  - 下载论文讨论
requires:
  skills:
    - download_arxiv
    - web_search
author: system
open_source: true
---

## 概述

arXiv 论文讨论工作流：**定位 → 深入 → 对比 → 引用追踪** 循环 2~3 轮，综合得出结果。

**论文库资产**（核心）：所有下载/抓取的数据持久化到 `docs/arxiv/{id}/`，由独立检索库 `paper_search.py` 索引，跨会话复用：

```
docs/arxiv/
  {arxiv_id}/
    tex/unpacked/*.tex    ← tex 源码（download_arxiv 落盘，可索引）
    html/{id}.html        ← ar5iv HTML（save_html 落盘，可索引）
    {id}.pdf              ← PDF（归档，不索引）
    refs.json             ← 引用追踪结果（refs_search 落盘）
```

- **定位**：从用户 query 生成检索关键词，多源搜索候选论文（三源 + arXiv API + Semantic Scholar）
- **深入**：download_arxiv 下载 TeX 源码（失败降级 ar5iv HTML / PDF），paper_search 检索论文库
- **对比**：多篇论文方法/实验综合对比
- **引用追踪**：refs_search.py 从论文参考文献中发现同领域未收集论文，喂回下一轮定位

## 🧭 Mode Pre-Check

进入正式流程前，先检测当前运行模式。

| 模式 | 搜索 | 下载 | 检索 |
|------|------|------|------|
| 🔵 **plan** | ✅（只读） | ⚠️ 跳过，提示切 build | ✅（只读） |
| 🟢 **build** | ✅ | ✅ download_arxiv | ✅ |

## 📖 执行导航（按阶段加载）

| 加载时机 | 指令 | 说明 |
|---------|------|------|
| 进入 Phase 1-2 | `pythonrt 读取 skills/paper_discuss/paper_discuss.locate.md` | 定位：关键词生成 + 多源搜索 |
| 进入 Phase 3 | `pythonrt 读取 skills/paper_discuss/paper_discuss.download.md` | 下载：三级降级链 + 落盘 |
| 进入 Phase 4-5 | `pythonrt 读取 skills/paper_discuss/paper_discuss.deep.md` | 深入：paper_search + tex_search |
| 进入 Phase 6 | `pythonrt 读取 skills/paper_discuss/paper_discuss.compare.md` | 对比：跨论文综合 |
| 进入 Phase 7 | `pythonrt 读取 skills/paper_discuss/paper_discuss.refs.md` | 引用追踪：发现新论文 |
| 进入 Phase 8 | `pythonrt 读取 skills/paper_discuss/paper_discuss.iterate.md` | 循环精化：结合前轮状态 |
| 需要脚本加载方式 | `pythonrt 读取 skills/paper_discuss/paper_discuss.loading.md` | __sandbox__ 运行方式 |
| 遇到边界/错误 | `pythonrt 读取 skills/paper_discuss/paper_discuss.faq.md` | 边界与错误处理 |

## 🔄 执行流程（总览）

```
Phase 1-2 定位 → Phase 3 下载落盘 → Phase 4-5 深入 → Phase 6 对比
    ↑                                        ↓
 Phase 8 循环精化 ←──────── Phase 7 引用追踪（发现新论文喂回）
```

各阶段详细步骤见「执行导航」对应子文件。

## 讨论状态跟踪（核心机制）

每轮维护一个**讨论状态**（JSON 结构，跨轮传递）：

```json
{
  "round": 2,
  "downloaded": ["2406.02507", "2303.07909"],
  "discussed": ["2406.02507"],
  "compared": [],
  "queries": ["diffusion model self-conditioning"],
  "findings": ["自条件化是核心方法"]
}
```

**规则**：
1. **定位时**：新轮 query 结合前几轮 findings，排除已下载论文
2. **深入时**：检索全部已下载论文，用 `exclude` 排除已讨论
3. **对比时**：对比范围=全部已下载论文中相关者

## 工具脚本清单

| 脚本 | 用途 | 调用 |
|------|------|------|
| arxiv_search.py | 三源候选搜索 | `search_candidates(query, n)` |
| paper_search.py | **独立检索库**（论文库索引） | `search_library(query, tex_root, exclude, include)` |
| tex_search.py | tex 检索（兜底） | `search_tex(query, tex_root, exclude, include)` |
| ar5iv_fetch.py | ar5iv 抓取 + 落盘 | `save_html(id, library_root)` / `extract_references(id)` |
| refs_search.py | 引用追踪 | `trace_references([ids])` |

## 论文库索引（核心资产）

- **落盘**：所有下载/抓取数据 → `docs/arxiv/{id}/`（tex/html/refs）
- **索引**：paper_search.py 遍历论文库，支持 .tex/.html
- **复用**：后续任何 paper_discuss 讨论，用 `search_library` 直接检索已下载论文
- **剥离 codes 依赖**：paper_search.py 完全独立（无 codes._log 沙箱问题）

## 与 web_search / download_arxiv 的分工

- **web_search**：通用网络搜索（含 Semantic Scholar/arXiv API），适合宽泛探索
- **paper_discuss**：专用论文讨论流程，多源搜索 + 本地 tex/ar5iv 深入 + 引用追踪
- **download_arxiv**：TeX 源码/PDF 下载（paper_discuss 的 Phase 3 第一级）
