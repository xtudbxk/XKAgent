# 06 · Agent 引擎

简体中文 | [English](06-agent-engine.md)

Agent 引擎负责把一条用户消息变成完整回合：补充当前状态，调用模型，执行工具，再把结果交给模型继续处理。`AgentManager` 在此之上管理多个 session，让它们并发运行而又各自保存上下文和状态。

## 一条消息如何完成

```text
用户输入
  → 解析 @路径并清理非法字符
  → 注入本回合 status，写入 session SQLite
  → 可选的 Skill 选择与提示注入
  → 接收 LLM 的 reasoning、正文、工具调用和 usage
  → 校验并依次执行工具，结果写回历史
  → 有工具调用则继续请求 LLM
  → 得到普通回复、被中断或工具要求停止时结束
```

主循环以 `complete_stream()` 为模型边界。正文、assistant tool call 和 tool result 会按协议保存并进入后续上下文；reasoning 只以 `thinking` 角色落库供界面展示，不会再次发送给模型。

模型一次可以返回多个工具调用，引擎按顺序执行。参数必须是 JSON 对象，并包含 schema 声明的必填字段；解析失败、缺少字段或未知工具都会变成明确的 tool result，让模型有机会修正。`summary` 成功后会立即结束本轮，并为尚未执行的调用补齐 skipped 结果，保持消息协议完整。

## Status 是每回合上下文

XKAgent 不只依赖固定 system prompt。每条用户消息都会附带当时的本地时间、运行模式、workdir 与挂载权限、建议 Skill、推荐信息，以及用户显式指定 Skill 的一次性要求。这个前缀和正文一起持久化，恢复会话时可以重建模型当时看到的状态；前端展示时会剥离前缀，只显示原始正文。

Status 只是告诉模型“现在在哪里、可以做什么”，并不执行权限控制。真正的路径、导入和进程限制由 `pythonrt` 运行时决定。README 中提到的长期任务编排与完整长期 Memory 仍是 Roadmap；当前已经实现的是状态注入、检索推荐、`summary` 文档写入和会话历史持久化，不能把这些等同于完整 Memory 系统。

自动 Skill 选择开启时，引擎会额外调用一次 LLM 选择技能：首次使用注入完整 `skill.md`，版本未变化的后续回合可只注入语义锚点。关闭选择后仍会构建基本 status，但不会执行这次选择调用。

## 模式与中断

`plan`、`build` 和 `build-unsafe` 直接决定下一次 `pythonrt` 使用的 profile。模式切换不会动态改变已经启动的 worker；详细权限和风险见 [05 · 沙箱与工具执行](05-沙箱与工具执行.md)。

Agent 会向 REPL/Web 输出 thinking、text、tool call、tool progress、tool result、usage、stats 和 interrupted 等结构化事件。前端负责渲染这些事件，执行状态仍由 Agent 线程持有。

Ctrl+C 通过线程安全 Event 向下传播：LLM 流约每 50 ms 检查一次，工具父进程也持续轮询并在命中时 kill worker；主循环在模型 chunk 和工具执行前还有额外检查点。内部 `_InterruptTurn` 只结束当前 turn，不会杀死 session 的 Agent 线程。未得到结果的工具调用会补入 killed 结果，避免恢复后留下不完整的工具消息。

## 多会话如何隔离

每个 session 有独立的 SQLite 数据库、输入/输出 Queue 和 Agent 线程。`AgentManager` 可以同时保留多个线程，但输入和界面读取只面向当前 focus；切换 focus 不会停止其他仍在工作的 session。

Provider、模型、token 统计、动态挂载和搜索范围等状态按 session 保存。线程意外退出后，后续输入或控制命令可触发重启；非当前 focus 的空闲线程也可由管理器回收。

同一个 session 被不同进程打开时，会使用 lease 锁保护会话写入。没有拿到锁的实例进入观察者模式：可以同步读取外部变化，但拒绝新的 LLM 输入和写类命令；锁过期后可尝试接管。这是会话一致性机制，不是工作区文件锁或沙箱。

## 子 Agent

`agent` 工具会把独立 prompt、可选 system prompt、模型、图片、工具列表、最大步数和总超时交给 `agent_worker` 子进程。子 Agent 不继承主对话全文，但继承当前 Provider、模型、模式、workdir 和挂载；它的 `pythonrt` 不会获得额外权限。

worker 中只有 `pythonrt`、`exit`、`searchskill`、`agent` 四个候选工具，再由调用参数选择实际暴露项。`searchinfo` 和 `summary` 不属于子 Agent 工具集。嵌套 `agent` 与 `exit` 默认关闭，必须分别通过 `allow_agent_tool` 和 `allow_exit` 显式启用；提示词要求最终回复为单个 JSON 对象，但当前校验器实际接受任意合法 JSON，首次校验失败时会再请求一次 JSON 格式结果。

worker 已实现多步工具循环、总超时、中断、工具筛选和 JSON 收尾。每轮 LLM 返回后，引擎先读取 `extras` 中的工具调用，再输出进度并按顺序执行；没有工具调用时则校验最终 JSON。

## Compact、Drop 与 Restart

`/compact` 使用专用 system prompt 发起一个不带工具、也不做 Skill 选择的独立 LLM 回合，把当前历史压缩为总结。只有流式总结正常结束后，才写 compact marker、替换内存上下文，并尝试把总结写入 `.xkagent/docs/<session>/`。SQLite 中的旧消息不会删除，失败或中断也不会改写原上下文。

`/drop` 不调用 LLM，而是写入 drop marker，并把内存上下文替换为“历史已丢弃”的合成消息。旧记录仍留在数据库中，只是不再进入后续模型上下文，因此它不是安全删除。

`/restart` 负责重新启动进程并加载代码与 session 状态，不负责压缩或清空上下文，也不会撤销文件修改。它优先使用 `os.execv` 替换进程；不支持时再启动新进程并退出旧进程。

## 当前能力与 Roadmap

当前已实现单回合工具循环、流式事件、状态注入、SQLite 会话、多 session 线程管理、观察者模式、中断、子 Agent，以及 compact/drop/restart。

完整的长期任务调度、长期 Memory、全局子 Agent 树形编排、事务式文件回滚和敏感历史安全删除都没有实现。compact/drop 只改变后续上下文视图，`/restart` 只处理进程生命周期，这些能力不能替代相应的 Roadmap 功能。

## 相关文档

- [04 · LLM 调用层](04-LLM调用层.md)：事件流、重试与 Provider 适配
- [05 · 沙箱与工具执行](05-沙箱与工具执行.md)：三种模式和 worker 权限
- [07 · 技能系统](07-技能系统.md)：Skill 选择、全文注入与锚点
- [08 · 搜索与记忆](08-搜索与记忆.md)：推荐信息、summary 与 compact 文档
- [09 · 命令系统](09-命令系统.md)：session、compact、drop、restart 命令
