---
name: web_search
version: 2.6.0
description: 多源搜索（17源+学术API+auto路由）
category: tool
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 搜索
  - 查资料
  - search
  - 百度
  - 谷歌
  - 必应
  - 代码
  - GitHub
  - 论文
  - 学术
  - 百科
  - 微信公众号
  - 互联网
  - 查询
  - 调研
requires: {}
author: system
---

你已加载 **web_search (v2.2)** 技能。按以下**四步工作流**执行搜索任务。

## 四步工作流总览

```
步骤①: 分析需求，选择搜索源 → pythonrt 读取 web_search.step1.md
步骤②: 生成搜索词           → pythonrt 读取 web_search.step2.md
步骤③: 执行搜索             → pythonrt 读取 web_search.step3.md
步骤④: 循环分析             → pythonrt 读取 web_search.step4.md
```

## 快速使用

```python
# 方式1: pythonrt 内（推荐）
from skills.web_search import search

# 按源搜索
results = search.search_baidu("搜索词")          # 百度
results = search.search_github("fastapi")        # GitHub
results = search.search_arxiv("multi-agent")     # 学术论文
results = search.search_auto("混合内容")          # 智能路由
results = search.search_openalex("vision transformer")  # OpenAlex 学术
results = search.search_crossref("deep learning")       # Crossref 文献
results = search.search_dblp("transformer")             # DBLP 论文
results = search.search_sogou("中文资讯")              # 搜狗
results = search.search_so360("Python 教程")           # 360
results = search.search_csdn("python")                 # CSDN
results = search.search_sourcegraph("lang:go http")    # 代码搜索

# 覆盖 HTTP 回调（WASM 沙箱内网络加速）
search.set_http_request_callback(http_request)   # 传入宿主回调
results = search.search_baidu("搜索词")           # 自动走回调
```

```bash
# 方式2: 用户 !xxx（所有 mode）或 build-unsafe 下直接执行
!python3 skills/web_search/search.py -s baidu "搜索词"
!python3 skills/web_search/search.py -s auto "查询" -n 5
!python3 skills/web_search/search.py --list-sources

## 🔌 代理配置（部分源需要）

部分搜索源（**Google / DuckDuckGo / GitHub API / arXiv / Semantic Scholar**）在受限网络下需要代理。

```python
from skills.web_search import search

# 方式 A: 显式设置（http/https 同代理）
search.set_proxy("http://127.0.0.1:7890")

# 方式 B: 环境变量自动读取（HTTP_PROXY / HTTPS_PROXY）
# 设置环境变量后无需代码，首次请求自动生效

# 方式 C: 清除代理（恢复直连）
search.set_proxy(None)
```

> 💡 **国内直连源**（百度/搜狗微信/必应 cn）**不受代理影响**，即使配置代理也保持直连——避免国内源走代理反而失败。

## 🧭 最佳搜索顺序（考虑代理可达性）

| 场景 | 首选 | 次选 | 兜底 | 说明 |
|------|------|------|------|------|
| **中文通用** | 百度 | 搜狗微信 | 必应 | 国内直连，无需代理 |
| **微信公众号文章** | 搜狗微信 | 必应 `site:mp.weixin.qq.com` | — | 国内直连 |
| **英文/国际** | Google | DuckDuckGo | 必应 | **需代理**；代理不可达→直接必应 |
| **代码/开源** | GitHub | 必应 | — | GitHub API 需代理 |
| **学术论文** | Semantic Scholar | arXiv | 必应 | 需代理（arXiv 有时可达） |
| **百科/概念** | Wikipedia | 必应 | — | 需代理 |
| **混合/不确定** | auto 智能路由 | — | — | 按 query 关键词自动选 |

**代理不可达时的降级链**：`google → duckduckgo → bing`（bing 为最终兜底，国内可达）
```

## 文件引用

| 文件 | 内容 | 读取时机 |
|------|------|---------|
| [step1](web_search.step1.md) | 9搜索源一览 + 各源特点/限制 + 选择策略 | 执行步骤①前 |
| [step2](web_search.step2.md) | 生成搜索词方法 | 执行步骤②前 |
| [step3](web_search.step3.md) | 执行搜索命令 + 示例 | 执行步骤③前 |
| [step4](web_search.step4.md) | 循环分析 + 源切换 + 终止条件 | 执行步骤④前 |



## 🧪 连通性测试

```bash
# 运行全源连通性测试（需联网环境）
python3 skills/web_search/test_connectivity.py                 # 默认查 "python"
python3 skills/web_search/test_connectivity.py "transformer"   # 指定查询词
python3 skills/web_search/test_connectivity.py --timeout 5     # 指定超时
```

测试输出（已保存，可实时更新）：
- `connectivity_report.json` — 结构化结果
- `connectivity_report.md` — 人类可读报告

> 📊 **最近一次测试**（2026-08-07）: 11 正常 / 6 警告 / 0 失败
> 正常源: bing/google/duckduckgo/github/arxiv/openalex/crossref/dblp/so360/csdn/sourcegraph
> 警告源（反爬或需代理）: baidu/semantic_scholar/wikipedia/weixin_sogou/sogou/bilibili

## ⚠️ 已知限制（反爬与网络）

| 源 | 说明 |
|----|------|
| **百度** | 服务器 IP（数据中心）访问易触发验证码（302→wappass），可能返回 0 条；本地网络/浏览器环境正常 |
| **搜狗微信** | 有反爬，需请求间隔 ≥3s；触发验证码时返回 0 条 |
| **Google/DuckDuckGo/arXiv/Semantic Scholar/Wikipedia** | 需代理；代理不可达自动降级 bing（国内可达） |

> 💡 **推荐**：优先使用 `search_bing` / `search_auto`（必应国内可达、稳定），或配置代理后使用 google/学术源。

> 💡 注意：pythonrt 受限模式（plan/build）网络模块受限（urllib/http 默认禁），
> 搜索推荐在 build-unsafe 无限制模式执行，或提示用户用 `!xxx` 在宿主执行。
