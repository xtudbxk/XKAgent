## Phase 2: 分类决策

### 核心区分

| 主类型 | 本质 | 示例 |
|--------|------|------|
| workflow | 定义流程、步骤、思维框架，LLM 按步骤执行 | check（检查流程）、plan（计划流程）、makeskill（本技能） |
| tool | 提供外部能力——调用网络、API、本地数据等 | web_search（搜索需要 requests + BeautifulSoup） |

这个分类决定技能的主要定位，但**不决定是否拥有工具**。一个 workflow 技能也完全可以有工具脚本。

### 目标运行模式对分类的影响

在选择 workflow 还是 tool 时，务必回顾 Phase 1 确定的「目标运行模式」：

| 目标模式 | 建议 | 原因 |
|---------|------|------|
| 🔵 **仅 plan** | → **workflow** 更合适 | plan 下不能写 `skills/` 目录，tool 类脚本部署受限 |
| 🟢 **build** | → 均可 | 标准读写权限 |
| 🔥 **build-unsafe** | → 均可，tool 更灵活 | pythonrt 无限制，能实现系统级工具 |
| 🔀 **多模式** | → **workflow** 更灵活 | 可通过 Mode Pre-Check 分支适配各模式 |

### 决策问题

向用户确认：

- **这个技能的核心是「教 LLM 怎么做」还是「给 LLM 什么能力」？**
  - 前者 → workflow（流程驱动）
  - 后者 → tool（能力驱动）

同时结合目标运行模式给出建议。

用户确认后进入 Phase 3。

---
