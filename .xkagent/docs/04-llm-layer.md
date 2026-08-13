# 04 · LLM Layer

[简体中文](04-LLM调用层.md) | English

XKAgent deliberately keeps its model-calling layer lightweight: `codes/llm.py` uses `requests` directly to communicate with OpenAI-compatible and Anthropic APIs, then normalizes their responses into streaming events that the Agent can process uniformly. There is no OpenAI SDK, Anthropic SDK, or LiteLLM; the only core dependencies are `requests` and the standard library.

## From Providers to unified events

The Provider's `type` determines the request protocol:

```text
openai    → POST {base_url}/chat/completions
anthropic → POST {base_url}/messages
                    ↓
text / reasoning / tool_call_chunk / usage
                    ↓
Agent tool loop and interface events
```

The OpenAI-compatible path retains the OpenAI message and tool schemas and passes through `reasoning_effort`, `thinking`, and JSON response-format parameters. At the calling boundary, the Anthropic path performs the required conversions: system messages move to the top level, `tool_calls` and tool results become `tool_use` and `tool_result`, and images and tool schemas are converted to Anthropic blocks. This keeps Provider-specific differences out of the main Agent loop.

"OpenAI-compatible" means only that the project sends requests using the `/chat/completions` protocol; it does not imply compatibility with every gateway's private parameters. Actual support for images, thinking, JSON mode, and reasoning effort still depends on the specific model and server.

## Streaming is the primary path

`complete_stream()` performs the actual call, progressively yielding text, reasoning, tool-call deltas, and usage. When the generator finishes, it returns the complete text along with aggregated model, finish-reason, tool-call, usage, and other metadata. `complete()` is only a synchronous collection wrapper around this stream.

Tool arguments are assembled by call index. At the end of the stream, if `arguments` cannot be parsed as a JSON object, the calling layer records `tool_calls_invalid`. The main Agent requests a correction at most twice, then returns an explicit argument error to the model as the tool result instead of crashing the entire session. Token data split across Anthropic's `message_start` and `message_delta` events is also merged here.

SSE reading prefers `select` polling on the underlying socket, allowing interruption checks approximately every 50 ms even during long periods of thinking. `timeout` measures idle time since the last received byte, not the total duration of the generation. As long as the server continues sending data, a long response is not stopped merely because of its overall duration.

## Retries do not blindly replay requests

The calling layer retries only failures classified by the source code as transient, and only if no meaningful chunk has yet been emitted upstream:

1. Connection errors and status codes 429, 500, 502, 503, and 504: up to 3 total attempts, with backoffs of 0.5 and 1 second.
2. An initial 400 response from an OpenAI-compatible endpoint whose error mentions `temperature`: remove that parameter and retry.
3. Empty responses, stream idle timeouts, or interrupted SSE: up to 2 additional retries if no chunk has been emitted.

Once the stream starts producing output, XKAgent does not automatically replay it from the beginning, avoiding duplicate text, repeated tool calls, and additional charges. Authentication failures, missing models, and ordinary 4xx responses are not retried automatically either.

## How interruption is finalized

The caller signals interruption through a shared `interrupt_event`. The LLM layer closes the response, retains all accumulated text, reasoning, and usage, and sets `interrupted=true` in the result; incomplete tool calls are discarded. When the main Agent sees this marker, it ends the current turn instead of treating the partial content as a normal assistant response and continuing.

## Provider configuration

Providers are loaded only from the first existing configuration file:

1. `<workdir>/.xkagent/provider.config`
2. `<repository root>/provider.config`

Environment variables can only supply API keys for declared Providers; they cannot create a Provider. Key precedence is `api_key` in the Provider or `default` section, the variable referenced by `api_key_env`, and the automatically derived `<PROVIDER>_API_KEY`.

Each Provider can define `type`, `base_url`, `default_model`, and `models.*` aliases. When `type` is not explicitly configured, only model names prefixed with `claude-` or `anthropic/` are inferred as Anthropic; all others use OpenAI. Configuration is cached by mtime and hot-reloaded on subsequent calls after the file is saved.

Model configuration may include a `model:effort` suffix, which is separated from the actual model name before the request is sent. `provider/model` is used for routing only when its prefix matches a registered Provider. A custom `base_url` must include its own version prefix; the calling layer only appends `/chat/completions` or `/messages` and does not probe endpoints.

## Connectivity, cost, and implementation boundaries

`/model test` sends a very small non-streaming request to verify network access, authentication, and model availability. It neither switches the current model nor writes session state. Full test runs execute sequentially to reduce the likelihood of triggering rate limits.

Costs shown in the interface come from a static pricing table in `llm.py` and cover only the models listed there. Unknown models are estimated at zero, so a displayed cost of `$0` does not mean the Provider is free; use the server-side bill as the source of truth.

The current implementation includes the two API families described above, unified streaming aggregation, limited retries, interruption, and connectivity tests. Other protocols, the Responses API, automatic endpoint discovery, and broader gateway compatibility are not implemented and should not be inferred from the word "compatible." `start_prewarm()` remains only for compatibility with older callers and is a no-op.

## Related documentation

- [02 · Configuration](02-configuration.md): Provider files, model switching, and the working directory
- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): how tools are run
- [06 · Agent Engine](06-agent-engine.md): how streaming events enter the single-turn loop
- [09 · Command System](09-commands.md): `/model` and connectivity tests
- [10 · Frontends](10-frontends.md): presentation of thinking, usage, and error events
