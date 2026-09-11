---
name: paper_collect
description: 多agent收集arXiv论文并生成中英双语HTML清单
version: 1.0.0
---

# paper_collect 论文收集与清单生成 v1.0.0

## 概述
面向「按时间范围+主题收集 arXiv 论文并产出可读清单」任务的完整流程技能。
从多 agent 并行收集 → 去重汇总 → 抓取完整摘要 → 中文翻译 → 生成带链接的 HTML 清单。

核心设计：
- **callagent 长程托管**：6+ 子 agent 并行收集（真并行需投不同会话，绕过 in_flight 串行闸门）
- **多源交叉**：arXiv API / Semantic Scholar / GitHub / 顶会反查 / 综述，多源判定热点
- **去重归一**：arxiv_id 主键去重，标题归一化兜底
- **摘要补全**：arXiv API 批量抓取完整 abstract（id_list 分批 50/次，间隔 3s 防限流）
- **中译流水线**：agent 分批翻译（50 篇/批，写 /tmp 文件防超长回传），断点续传
- **HTML 输出**：md → HTML，URL 自动加 `<a>` 链接，领域导航 + 双语摘要排版

## 执行导航
| 时机 | 指令 |
|------|------|
| 加载技能后 | 无需加载，常驻本文件 |
| Phase 1 收集 | pythonrt 读取 paper_collect.phase1.md（callagent 编排任务书模板） |
| Phase 2 汇总 | pythonrt 读取 paper_collect.phase2.md（去重合并规则） |
| Phase 3 摘要 | pythonrt 读取 paper_collect.phase3.md（arXiv API 抓取流程） |
| Phase 4 翻译 | pythonrt 读取 paper_collect.phase4.md（agent 分批翻译流程） |
| Phase 5 输出 | 直接调用 md_to_html.py（见 API 速查） |
| 遇到边缘/错误 | pythonrt 读取 paper_collect.faq.md |

## 硬规则
1. 子 agent 任务书必须自包含（≤3500B），默认 plan 模式只读，下载写 /tmp
2. 子 agent 数据 >3500B 必须分片，主管及时回信确认并明确「停止重发」
3. 去重以 arxiv_id 为主键；无 ID 的用标题归一化（小写/去标点/去空格）
4. arXiv API 抓取分批 50 篇/次，间隔 ≥3s，断点续传（/tmp 缓存）
5. 翻译子 agent 产出写 /tmp 文件（勿回传大 JSON），超时部分从缓存补齐
6. URL 必须加 `<a href target="_blank">` 链接；输出 HTML 而非 md
7. 所有脚本通过 pythonrt 执行，stdlib only（requests 属受控网络白名单）
8. 模式感知：plan 只读时仅分析/生成到 /tmp，落盘 docs/ 需 build 模式

## 📦 依赖

| 脚本 | 依赖 | 运行模式 |
|------|------|---------|
| md_to_html.py | stdlib only（re/html/os） | plan / build / build-unsafe |

## 📦 API 速查

**加载**（沙箱双路径；build-unsafe 可 `from skills.paper_collect.md_to_html import md_to_html`）：

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("md_to_html", "skills/paper_collect/md_to_html.py")
m = importlib.util.module_from_spec(spec); sys.modules["md_to_html"] = m
spec.loader.exec_module(m)

# 生成 HTML（md 清单 → 带链接 HTML）
html = m.md_to_html("docs/arxiv_paper_collect/papers_full_translated.md", "2026年 Image Generation 论文清单")
open("/tmp/out.html", "w", encoding="utf-8").write(html)
```

**其他核心脚本（工作流内嵌，无独立模块）**：
- arXiv 摘要抓取：见 phase3.md 的 fetch_abstracts() 代码块
- 去重合并：见 phase2.md 的 dedup_papers() 代码块
- 翻译编排：见 phase4.md 的子 agent 任务书模板

## 使用流程（5 阶段）
1. **Phase 1 收集**：按主题拆子方向 → callagent 派发子 agent（不同会话真并行）→ 回信分片收集
2. **Phase 2 汇总**：合并各子 agent 产出 → arxiv_id 去重 → 生成 index.json
3. **Phase 3 摘要**：arXiv API 批量抓取完整 abstract → arxiv_abstracts.json
4. **Phase 4 翻译**：agent 分批翻译标题+摘要 → 合并 arxiv_abstracts_zh.json
5. **Phase 5 输出**：md_to_html.py 生成 HTML → 落盘 docs/

## 局限说明
- 无 arXiv ID 的论文（部分第三方源）无法抓取摘要/链接，仅保留标题
- 免费翻译 API 不稳定（MyMemory 超时/LibreTranslate 403），翻译须走 agent 子任务
- 子 agent 超时（LLM stream timeout）属常见，需断点续传 + 缺失补齐
