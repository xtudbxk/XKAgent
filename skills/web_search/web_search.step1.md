## 🎯 步骤① — 分析需求，选择搜索源

根据用户问题的**内容类型**，从下表选择最适合的搜索源：

### 搜索源一览

| 源 | 命令参数 | 适用场景 | 自动 fallback |
|----|---------|---------|:------------:|
| **百度** | `-s baidu` | 中文通用搜索、国内信息 | — |
| **Google** | `-s google` | 英文/国际/多语言搜索 | 失败→自动切 bing |
| **必应 (Bing)** | `-s bing` | 英文备选、国际新闻 | — (fallback 终点) |
| **DuckDuckGo** | `-s duckduckgo` | 隐私搜索、简单查询 | 失败→自动切 bing |
| **GitHub** | `-s github` | 代码库、开源项目搜索 | — |
| **arXiv** | `-s arxiv` | 学术论文/预印本搜索 | — |
| **Semantic Scholar** | `-s semantic_scholar` | 学术文献/引用/作者图谱 | — |
| **Wikipedia** | `-s wikipedia` | 百科知识、概念定义 | — |
| **搜狗微信** | `-s weixin_sogou` | 微信公众号文章搜索 | — |
| **OpenAlex** | `-s openalex` | 开放学术图谱（免费API） | — |
| **Crossref** | `-s crossref` | 文献元数据/DOI 查询 | — |
| **DBLP** | `-s dblp` | 计算机科学论文 | — |
| **搜狗** | `-s sogou` | 中文通用（反爬不稳定） | — |
| **360** | `-s so360` | 中文通用 | — |
| **B站** | `-s bilibili` | 视频/专栏（JS渲染） | — |
| **CSDN** | `-s csdn` | 技术博客（JSON API） | — |
| **Sourcegraph** | `-s sourcegraph` | 代码搜索（GraphQL） | — |
| **Auto（智能）** | `-s auto` | 自动按 query 关键词选最佳源 | 首选失败→切次要源 |

### 各源特点与限制

| 源 | 限制说明 |
|----|---------|
| **百度** | 无特殊限制 |
| **Google** | 网络不稳定时可能失败，脚本会自动 fallback 到 bing，LLM 可继续正常分析 |
| **DuckDuckGo** | 同上，失败自动 fallback 到 bing |
| **Bing** | 稳定可靠，是 google/duckduckgo 的 fallback 终点 |
| **GitHub** | 未认证限 10 req/min；可设 `GITHUB_TOKEN` 环境变量提升至 60 req/hr |
| **arXiv** | API 稳定，无限制 |
| **Papers with Code** | API 稳定，无限制 |
| **Semantic Scholar** | API 免费无限，建议使用 |
| **Wikipedia** | API 极稳定，中英文自动切换 |
| **OpenAlex** | 免费无限，无 key；含引用数/年份/作者/摘要 |
| **Crossref** | 免费，DOI 权威；含期刊/年份/作者 |
| **DBLP** | 免费，CS 论文最全；含年份/期刊/作者 |
| **搜狗微信** | ⚠️ **每次请求需间隔 ≥3 秒**，否则可能触发验证码；如果搜狗不可用，备选方案为 `-s google "site:mp.weixin.qq.com {query}"` |

### 最佳搜索顺序（考虑代理）

```
场景                    首选 → 次选 → 兜底
─────────────────────────────────────────────
中文通用                 baidu → weixin_sogou → bing
微信公众号               weixin_sogou → bing(site:mp.weixin.qq.com)
英文/国际(需代理)        google → duckduckgo → bing
代码/开源(需代理)        github → bing
学术(需代理)             semantic_scholar → arxiv → bing
百科(需代理)             wikipedia → bing
混合/不确定              auto
```

> ⚠️ 需要代理的源：google / duckduckgo / github / arxiv / semantic_scholar / wikipedia。
> 代理不可达时这些源会失败，**自动降级到 bing**（国内可达，最终兜底）。

### 选择策略

```
问题类型                           → 推荐源
────────────────────────────────────────────────
中文通用/国内信息                  → baidu / so360 / sogou
技术博客                           → csdn
代码搜索                           → sourcegraph / github
英文/国际/技术/学术               → google (自动 fallback bing)
代码/开源项目                     → github
学术论文/研究                       → semantic_scholar / arxiv / openalex / crossref / dblp
概念/定义/百科                     → wikipedia
微信公众号文章                     → weixin_sogou (或 google site:mp.weixin.qq.com)
纯英文快速查询                     → bing
不确定或混合型                     → auto（智能路由）
```

### 选择方式

**方案 A：LLM 自行选择特定源**
```bash
python3 skills/web_search/search.py -s baidu "深圳北京高铁"
python3 skills/web_search/search.py -s google "transformer architecture"
python3 skills/web_search/search.py -s github "langchain agent"
```

**方案 B：使用 auto 智能路由**（LLM 自动分析 query 选择最佳源）
```bash
python3 skills/web_search/search.py -s auto "深圳到北京高铁"      # → baidu
python3 skills/web_search/search.py -s auto "transformer paper"   # → semantic_scholar
python3 skills/web_search/search.py -s auto "fastapi repo"        # → github
python3 skills/web_search/search.py -s auto "what is attention"   # → wikipedia
```

> 💡 **建议**：如果用户问题**涉及多个方面**（如"某技术的论文、代码和中文介绍"），建议**分源多次搜索**，每次聚焦一个方向。如果问题**单一**，`-s auto` 即可。

### 步骤① 输出示例

```
🎯 源选择: 用户问题涉及学术论文 → 选定 semantic_scholar
   备选: arxiv（补充）
   搜索方向: 按"multi-agent systems"、"LLM agent survey" 两个角度搜索
```


