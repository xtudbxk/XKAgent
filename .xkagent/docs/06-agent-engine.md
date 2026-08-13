# 06 · Agent Engine

[简体中文](06-Agent引擎.md) | English

The Agent engine turns a user message into a complete turn: it adds the current status, calls the model, executes tools, and returns the results to the model for further processing. Above this layer, `AgentManager` manages multiple sessions, allowing them to run concurrently while each retains its own context and state.

## How a Message Becomes a Complete Turn

```text
User input
  → Parse @paths and remove invalid characters
  → Inject the turn's status and write it to the session SQLite database
  → Optionally select a Skill and inject its instructions
  → Receive LLM reasoning, text, tool calls, and usage
  → Validate and execute tools in sequence, then write results back to history
  → Request another LLM response if tools were called
  → End on a normal response, interruption, or a tool-requested stop
```

The main loop uses `complete_stream()` as its model boundary. Text, assistant tool calls, and tool results are stored according to the protocol and included in subsequent context. Reasoning is stored only under the `thinking` role for display in the UI and is not sent back to the model.

The model may return multiple tool calls in one response, which the engine executes in order. Arguments must be JSON objects containing every required field declared by the schema. Parse errors, missing fields, and unknown tools become explicit tool results, giving the model an opportunity to correct them. After a successful `summary`, the turn ends immediately and any calls not yet executed receive skipped results to keep the message protocol complete.

## Status Provides Per-Turn Context

XKAgent does not rely only on a fixed system prompt. Every user message is accompanied by the current local time, runtime mode, workdir and mount permissions, suggested Skills, recommended information, and any one-time request for a Skill explicitly named by the user. This prefix is persisted with the message, allowing a restored session to reconstruct the state the model saw at the time. The frontend removes the prefix for display and shows only the original message body.

Status only tells the model where it is and what it can do; it does not enforce permissions. Actual path, import, and process restrictions are enforced by the `pythonrt` runtime. The long-running task orchestration and full long-term Memory described in [README](../../README.md) remain on the Roadmap. What exists today is status injection, retrieval-based recommendations, document persistence through `summary`, and session history persistence. These must not be treated as a complete Memory system.

When automatic Skill selection is enabled, the engine makes an additional LLM call to choose a Skill. On first use, the complete `skill.md` is injected; subsequent turns may inject only a semantic anchor if the version has not changed. When selection is disabled, the engine still builds the basic status but does not make this extra selection call.

## Modes and Interruption

`plan`, `build`, and `build-unsafe` directly determine the profile used by the next `pythonrt` call. Switching modes does not dynamically alter a worker that has already started. See [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md) for detailed permissions and risks.

The Agent emits structured events to the CLI and Web UI, including thinking, text, tool calls, tool progress, tool results, usage, stats, and interrupted events. The frontend renders these events, while the Agent thread remains responsible for execution state.

Ctrl+C propagates through a thread-safe Event. The LLM stream checks it approximately every 50 ms, and a tool's parent process also polls continuously and kills the worker when the event is set. The main loop has additional checkpoints before model chunks and tool execution. The internal `_InterruptTurn` ends only the current turn; it does not terminate the session's Agent thread. Tool calls that did not produce a result receive killed results so that restoring the session does not leave incomplete tool messages.

## Multi-Session Isolation

Each session has its own SQLite database, input and output Queues, and Agent thread. `AgentManager` can retain multiple threads concurrently, but input and UI reads target only the currently focused session. Changing focus does not stop other sessions that are still working.

Provider, model, token statistics, dynamic mounts, search ranges, and other state are stored per session. Later input or control commands can restart a thread after an unexpected exit, and the manager may reclaim idle threads that are not currently focused.

When different processes open the same session, a lease lock protects session writes. An instance that cannot acquire the lock enters observer mode: it can synchronize and read external changes but rejects new LLM input and write commands. It can attempt takeover after the lock expires. This mechanism preserves session consistency; it is not a workspace file lock or sandbox.

## Sub-Agents

The `agent` tool passes an independent prompt, optional system prompt, model, images, tool list, maximum step count, and overall timeout to an `agent_worker` subprocess. A sub-Agent does not inherit the full main conversation, but it does inherit the current Provider, model, mode, workdir, and mounts. Its `pythonrt` receives no additional permissions.

The worker has four candidate tools—`pythonrt`, `exit`, `searchskill`, and `agent`—and the call arguments determine which are actually exposed. `searchinfo` and `summary` are not available to sub-Agents. Nested `agent` and `exit` calls are disabled by default and must be enabled explicitly through `allow_agent_tool` and `allow_exit`, respectively. The prompt requires the final response to be a single JSON object, but the current validator accepts any valid JSON. If initial validation fails, the worker makes one additional request for a JSON-formatted result.

The worker implements a multi-step tool loop, an overall timeout, interruption, tool filtering, and final JSON handling. After each LLM response, the engine reads tool calls from `extras`, reports progress, and executes them in order. When no tool call is present, it validates the final JSON response.

## Compact, Drop, and Restart

`/compact` uses a dedicated system prompt to start an independent LLM turn without tools or Skill selection, compressing the current history into a summary. Only after the streamed summary completes successfully does it write a compact marker, replace the in-memory context, and attempt to save the summary under `.xkagent/docs/<session>/`. Old messages are not deleted from SQLite, and a failure or interruption leaves the original context unchanged.

`/drop` does not call the LLM. It writes a drop marker and replaces the in-memory context with a synthetic message stating that the history was discarded. Old records remain in the database but are no longer included in later model context, so this is not secure deletion.

`/restart` restarts the process and reloads code and session state. It does not compact or clear context, nor does it undo file changes. It first attempts to replace the process with `os.execv`; where that is unsupported, it starts a new process and exits the old one.

## Current Capabilities and Roadmap

The current implementation includes single-turn tool loops, streamed events, status injection, SQLite-backed sessions, multi-session thread management, observer mode, interruption, sub-Agents, and compact/drop/restart.

Full long-running task scheduling, long-term Memory, global tree-based sub-Agent orchestration, transactional file rollback, and secure deletion of sensitive history have not been implemented. Compact and drop only alter the context view used in subsequent turns, while `/restart` handles only the process lifecycle. These features do not replace the corresponding Roadmap capabilities.

## Related Documentation

- [04 · LLM Calling Layer](04-llm-layer.md): event streams, retries, and Provider adaptation
- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): the three modes and worker permissions
- [07 · Skills](07-skills.md): Skill selection, full-text injection, and anchors
- [08 · Search and Memory](08-search-and-memory.md): recommended information, summary, and compact documents
- [09 · Command System](09-commands.md): session, compact, drop, and restart commands
