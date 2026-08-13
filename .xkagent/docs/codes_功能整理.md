# XKAgent 架构总览

简体中文 | [English](architecture-overview.md)

本页以当前 `codes/` 实现为准，解释 XKAgent 的核心取向、运行路径和扩展边界。需要参数或命令细节时，再进入文末的专题文档。

## 三个核心支点

XKAgent 是一个本地运行的轻量 Agent Runtime。它没有为每项任务不断增加专用工具，而是围绕三个职责清晰的支点组织能力：

### `pythonrt`：代码执行内核

`pythonrt` 是 LLM 的通用执行原语。文件处理、数据转换、库调用和多步验证都可以组合成一段 Python，在一次调用中完成。模型既可以提交代码，也可以指定已有 `.py` 文件；宿主会启动独立 worker，通过 JSON 输入和 stdout 标记协议回收结果。

需要独立语义判断时，代码任务链还可以调用 `agent`。它在隔离上下文中完成子任务并返回结构化 JSON，再由主流程继续处理。实现上 `agent` 有独立的 Tool schema 和 worker，但在设计上更接近 code kernel 可调用的函数，而不是另一套平行工具系统。`searchinfo`、`searchskill`、`summary` 和 `exit` 则补充检索、持久化与生命周期语义；用户输入的 `!shell` 是宿主命令通道，不属于 LLM 工具面。

### Skills：能力扩展层

Skill 把领域知识、流程、参考资料和脚本放在独立目录中，核心只负责发现和加载。一个有效 Skill 至少包含 `skill.md`；`<workdir>/.xkagent/skills/` 中的同名 Skill 会覆盖仓库自带的 `skills/`，因此可以定制能力而无需修改 Agent 主循环。

候选 Skill 主要通过 ngram 和 frontmatter 匹配，embedding 只在启用并具备依赖时作为补充。frontmatter 缓存使用文件 mtime 与大小失效，修改后可以热加载。首次选用时，Agent 会把 Skill 内容作为自述承诺写入会话；后续同版本通过锚点召回，避免反复注入全文。

### Status：长程任务与 Memory 的底座

Status 不是后台守护进程，而是每轮动态生成的上下文前缀。它在模型行动前说明当前时间、执行模式、路径权限、建议 Skills、相关检索片段和用户的显式要求，让一次请求能够接上当前运行状态。

现有实现已经提供三层可恢复上下文：本轮有效对话、每个 session 的 SQLite 历史与状态，以及 `summary` 或 `/compact` 写入 `.xkagent/docs/<session>/` 的可检索文档。推荐信息默认搜索 `skills`、`docs`、`historys` 和 `logs`，同时排除当前 session 的历史；源码 `codes` 默认不参与推荐，可通过搜索范围配置显式加入。

这些机制是长程与记忆能力的底座，不等于完整产品能力。**自动长程任务编排、计划调度、记忆重要性评估、遗忘与冲突消解，以及完整的长期 Memory 管理目前都尚未实现。** `summary` 与 `/compact` 需要 Agent 或用户主动触发，也没有独立的记忆管理服务。

## 一轮请求如何流动

```text
REPL / Web
  → commands 分流 slash 命令，普通消息进入当前 session 队列
  → Agent 检查会话锁，解析 @路径并清理非法 Unicode
  → 检索建议 Skills 与相关信息，生成 Status
  → 用户消息写入 session SQLite
  → system prompt + 有效历史 + 本轮图片发送给 LLM
  → llm.py 统一输出 reasoning / text / tool calls / usage
  → 工具调用按顺序执行，结果落库后继续下一次 LLM 调用
  → Agent 产生结构化事件，由 REPL 或 WebSocket 渲染
```

`agent.py` 是这条主链的协调者：组装上下文、驱动 LLM/工具循环并持久化消息。`llm.py` 使用 `requests` 适配 OpenAI-compatible 与 Anthropic 两类 API，统一 SSE 流、重试、中断、usage 和工具调用片段。前端只消费 thinking、文本、工具、权限、锁、错误和回合结束等事件，不需要理解 Provider 细节；仅供展示的 thinking 记录也不会重新进入 LLM 上下文。

如果用户中断回合，LLM 流会在检查点停止，正在运行的 `pythonrt` worker 由宿主终止。下一次请求前，Agent 会修补不完整的 assistant/tool 配对，避免向 Provider 发送损坏的消息序列。

## 代码如何分层

入口层保持很薄：`__main__.py` 和 `main.py` 解析统一参数、确定 workdir，再按需启动 `repl.py` 或 `web.py`；`web_main.py` 只是固定 Web 模式的兼容入口。`config.py` 统一派生运行目录，`provider_config.py` 负责 Provider、模型别名和按 mtime 热加载的配置。

运行内核集中在以下几组文件：

- `agent.py` 与 `manager.py` 管理回合、多 session 线程、队列、焦点切换和恢复。
- `tools.py` 与 `sandbox.py` 定义 LLM 工具面和 `pythonrt` 执行环境。
- `llm.py` 隔离 Provider 协议差异。
- `skill.py` 与 `search.py` 提供能力发现、内容抽取、检索和长期文档写入。
- `agent_runner.py` 与 `agent_worker.py` 承载隔离的子 Agent 循环。

基础设施同样独立：`history.py` 管理 SQLite，`lock.py` 管理 session lease，`_log.py` 管理标准库日志和轮转。交互层的 `commands.py` 只注册一次命令，REPL 和 Web 复用相同分发语义；两端再分别处理终端输入或 FastAPI、WebSocket、认证与文件接口。

## 执行权限不是安全容器

`pythonrt` 按当前模式选择执行 profile：

- `plan`：workdir 与挂载只读，`/tmp` 可写，import、进程和路径能力受限。
- `build`：workdir 和声明为 `ro/rw` 的挂载可写，仍保留路径、import 与进程闸门。
- `build-unsafe`：接近宿主 Python，可使用任意路径、第三方库、子进程和网络，但仍保留对当前 `.xkagent/` 数据目录的写保护。

受限 worker 会检查 `realpath` 后的读写根，收窄 builtins 和 import，拦截常见文件与进程入口，并在平台支持时设置 CPU、内存、文件大小和文件描述符限制。SQLite、Dulwich Git 和网络栈通过扩展注册并在必要入口补充检查；`register_extension` 也允许项目显式增加受控库能力。

这仍是降低 LLM 误操作风险的软边界，不用于抵抗恶意 Python、提示注入或宿主逃逸。`build-unsafe`、`!shell`、强制挂载和自定义扩展都会扩大信任范围，不能替代 Git、系统权限、容器或备份。

## Session、状态与并发

每个 session 对应一个 SQLite 数据库，以及运行时中的独立 Agent 线程和输入输出队列。`AgentManager` 只把新输入发送到当前焦点，但其他 session 可以继续在后台完成回合；停止线程不会删除历史，再次切回时可以恢复。

SQLite 当前持久化消息与命令、Provider/模型、累计和最近一轮 token、动态挂载、搜索范围及一次性图片状态。执行模式和 Skill 自动选择开关是运行时状态，Agent 重启后回到默认值，而不是完整持久化。`/compact` 保留原始数据库记录，用截断标记和 LLM 总结重建有效上下文，并把总结写入 docs；`/drop` 只标记并清空当前有效上下文，不生成长期总结。

同一 session 通过 `<session>.db.lockdir/` lease 独占写入。owner 元数据和心跳用于识别持有者、恢复陈旧锁及处理同进程崩溃；其他进程打开该 session 时进入观察者状态。这个锁只防止并发写坏会话，不是数据库备份或事务式文件回滚。

子 Agent 与主对话使用隔离消息循环，可以配置模型、可用工具、步数、总超时和图片输入，最终结果必须是合法 JSON。它默认不能调用 `exit`，也不能继续调用 `agent`；只有显式开启后才允许嵌套。

## 运行目录

```text
<workdir>/
├─ .xkagent/
│  ├─ historys/<session>.db[/-wal/-shm]  # 消息、命令与会话状态
│  ├─ historys/<session>.db.lockdir/      # lease 与 owner 元数据
│  ├─ logs/                               # 进程日志与轮转文件
│  ├─ docs/<session>/                     # summary / compact 长期文档
│  ├─ skills/                             # 用户 Skill，覆盖内置同名项
│  ├─ search_index/                       # 可选语义索引
│  ├─ provider.config                     # workdir 级 Provider 配置
│  ├─ permission.txt                      # 静态路径授权
│  ├─ search_ranges.txt                   # 全局搜索增补与排除
│  └─ history.txt                         # REPL / Web 共享输入历史
├─ skills/                                # 随代码发布的内置 Skills
├─ system_prompt.txt                      # 主 Agent 提示
├─ system_prompt_compact.txt              # 压缩提示
└─ provider.config                        # 项目级配置后备
```

当 workdir 就是 XKAgent 仓库根时，项目专题文档和运行生成的 session 文档都位于 `.xkagent/docs/`，后者按 session 放入子目录。

## 从哪里扩展

- 新能力优先做成含 `skill.md` 的 Skill；只有新的底层执行语义才需要修改 Agent 主循环。
- Provider 和模型通过 `provider.config` 增加，保存后按 mtime 自动重载。
- 沙箱库能力通过 `register_extension` 放行，并为可能绕过 Python 文件 API 的入口补充校验。
- 新辅助工具在 `tools.py` 定义 schema 与执行器，再绑定到每个 Agent 的独立工具表。
- 新 slash 命令在 `commands.py` 注册，REPL 与 Web 会同时获得该能力。
- 新搜索范围通过 `/info` 或 `search_ranges.txt` 配置；新前端则复用 `AgentManager` 队列和结构化事件。

## 专题文档

- [01 · 入口与启动](01-入口与启动.md)：启动顺序、参数和前端调度。
- [02 · 配置管理](02-配置管理.md)：workdir、Provider、模型与热加载。
- [03 · 基础设施](03-基础设施.md)：SQLite、日志和 session lease。
- [04 · LLM 调用层](04-LLM调用层.md)：协议适配、SSE、重试、中断与 usage。
- [05 · 沙箱与工具执行](05-沙箱与工具执行.md)：`pythonrt`、执行 profile 与防护边界。
- [06 · Agent 引擎](06-Agent引擎.md)：主循环、事件、多会话与子 Agent。
- [07 · 技能系统](07-技能系统.md)：Skill 目录、frontmatter、选择与缓存。
- [08 · 搜索与记忆](08-搜索与记忆.md)：检索范围、ngram/embedding 与长期文档。
- [09 · 命令系统](09-命令系统.md)：共享命令、状态变更与审计。
- [10 · 前端界面](10-前端界面.md)：REPL、Web、文件接口与认证。
