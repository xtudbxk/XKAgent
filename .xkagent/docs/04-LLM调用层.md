# 04 · LLM 调用层

简体中文 | [English](04-llm-layer.md)

XKAgent 的模型调用层刻意保持轻量：`codes/llm.py` 直接使用 `requests` 对接 OpenAI-compatible 与 Anthropic 两类 API，再把不同响应整理为 Agent 能统一处理的流式事件。这里没有 OpenAI SDK、Anthropic SDK 或 LiteLLM，核心依赖只有 `requests` 和标准库。

## 从 Provider 到统一事件

Provider 的 `type` 决定请求协议：

```text
openai    → POST {base_url}/chat/completions
anthropic → POST {base_url}/messages
                    ↓
text / reasoning / tool_call_chunk / usage
                    ↓
Agent 工具循环与界面事件
```

OpenAI-compatible 分支沿用 OpenAI 消息和工具 schema，并按参数传递 `reasoning_effort`、`thinking` 与 JSON response format。Anthropic 分支则在调用边界完成必要转换：system 消息提升到顶层，`tool_calls`/tool result 转为 `tool_use`/`tool_result`，图片和工具 schema 也转换为 Anthropic block。这样 Provider 差异不会扩散到 Agent 主循环。

“OpenAI-compatible”只表示项目按 `/chat/completions` 协议发送请求，不代表兼容每个网关的私有参数。图片、thinking、JSON mode 和 reasoning effort 是否真正可用，仍取决于具体模型和服务端。

## 流式调用是主路径

`complete_stream()` 负责真实调用，逐步产出正文、reasoning、工具调用增量和 usage；生成器结束时返回完整正文以及 model、finish reason、tool calls、usage 等汇总信息。`complete()` 只是对这条流的同步收集封装。

工具参数会按调用 index 拼接。流结束后，如果 arguments 不能解析为 JSON 对象，调用层会写入 `tool_calls_invalid`；主 Agent 最多重新请求两次，之后把明确的参数错误作为工具结果交还模型，而不是让整个会话崩溃。Anthropic 分散在 `message_start` 与 `message_delta` 中的 token 数据也会在这里合并。

SSE 读取优先通过底层 socket 的 `select` 轮询，在等待长时间 thinking 时仍可约每 50 ms 检查一次中断。`timeout` 表示距离上次收到字节的空闲时间，不是整次生成的总时长；只要服务端持续发送数据，长回复不会因总时长而被停止。

## 重试不会盲目重放

调用层只重试源码认定为瞬时故障、且尚未向上层产出有效 chunk 的请求：

1. 连接错误及 429、500、502、503、504：最多尝试 3 次，退避 0.5 秒、1 秒。
2. OpenAI-compatible 首次返回 400 且错误提到 `temperature`：移除该参数后重试。
3. 空响应、流空闲超时或 SSE 中断：尚未产出 chunk 时最多额外重试 2 次。

一旦流已经开始输出，就不会自动从头重放，以免出现重复正文、重复工具调用或额外计费。鉴权失败、模型不存在、一般 4xx 也不会自动重试。

## 中断如何收尾

调用方通过共享的 `interrupt_event` 发出中断。LLM 层会关闭响应、保留已经累计的正文、reasoning 和 usage，并在结果中设置 `interrupted=true`；尚未完成的工具调用会被丢弃。主 Agent 看到该标记后结束当前 turn，不会把残缺内容当成一条正常 assistant 回复继续推进。

## Provider 配置

Provider 只从第一个存在的配置文件加载：

1. `<workdir>/.xkagent/provider.config`
2. `<代码仓库根>/provider.config`

环境变量只用于补充已声明 Provider 的 API Key，不能创建 Provider。Key 的优先级是 Provider 或 `default` 节中的 `api_key`、`api_key_env` 指向的变量、自动推导的 `<PROVIDER>_API_KEY`。

每个 Provider 可配置 `type`、`base_url`、`default_model` 和 `models.*` 别名。未显式配置 `type` 时，仅 `claude-` 或 `anthropic/` 前缀会推断为 Anthropic，其余按 OpenAI 处理。配置按 mtime 缓存，保存后可在后续调用中热加载。

模型配置可带 `model:effort` 后缀，发请求前会拆出真实模型名。`provider/model` 只有在前缀对应已注册 Provider 时才用于路由。自定义 `base_url` 应自行包含版本前缀，调用层只追加 `/chat/completions` 或 `/messages`，不会探测端点。

## 联通性、成本与实现边界

`/model test` 发送很小的非流式请求，验证网络、鉴权和模型是否可用，不切换当前模型，也不写入会话状态；全量测试按顺序执行以降低触发限流的概率。

界面中的成本来自 `llm.py` 静态价格表，只覆盖表内模型。未知模型按零估算，因此显示 `$0` 不表示 Provider 免费，账单应以服务端为准。

当前已经实现的是上述两类 API、统一流式聚合、有限重试、中断和联通性测试。其他协议、Responses API、自动端点发现或更广泛的网关兼容并未实现，也不应从“compatible”一词推断出来。`start_prewarm()` 仅为兼容旧调用保留，是 no-op。

## 相关文档

- [02 · 配置管理](02-配置管理.md)：Provider 文件、模型切换与工作目录
- [05 · 沙箱与工具执行](05-沙箱与工具执行.md)：工具如何运行
- [06 · Agent 引擎](06-Agent引擎.md)：流式事件如何进入单回合循环
- [09 · 命令系统](09-命令系统.md)：`/model` 与联通性测试
- [10 · 前端界面](10-前端界面.md)：thinking、usage 和错误事件的展示
