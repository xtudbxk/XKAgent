# XKAgent Architecture Overview

[简体中文](codes_功能整理.md) | English

This page reflects the current implementation under `codes/`. It explains XKAgent's core design, runtime path, and extension boundaries. Refer to the topic-specific documents at the end for detailed parameters and commands.

## Three Core Pillars

XKAgent is a lightweight Agent Runtime that runs locally. Instead of continually adding a specialized tool for every task, it organizes its capabilities around three clearly defined pillars.

### `pythonrt`: Code Kernel

`pythonrt` is the LLM's general-purpose execution primitive. File processing, data transformation, library calls, and multi-step validation can all be composed into one Python program and completed in a single invocation. The model can either submit code or specify an existing `.py` file. The host launches a separate worker and collects its result through JSON input and a marker protocol on stdout.

When a step requires independent semantic judgment, the code task chain can also call `agent`. It completes a subtask in an isolated context, returns structured JSON, and lets the main workflow continue. The implementation gives `agent` its own Tool schema and worker, but conceptually it is closer to a function callable by the code kernel than to a parallel tool system. `searchinfo`, `searchskill`, `summary`, and `exit` add retrieval, persistence, and lifecycle semantics. User-entered `!shell` commands form a host command channel and are not part of the LLM tool surface.

### Skills: Capability Extension

A Skill packages domain knowledge, workflows, references, and scripts in a separate directory, leaving the core responsible only for discovery and loading. A valid Skill contains at least `skill.md`. A Skill under `<workdir>/.xkagent/skills/` overrides a built-in Skill with the same name under `skills/`, allowing capabilities to be customized without modifying the Agent's main loop.

Candidate Skills are selected primarily through ngram and frontmatter matching; embeddings supplement this process only when enabled and when their dependencies are available. The frontmatter cache is invalidated using file mtime and size, so modifications can be loaded while the process is running. When a Skill is selected for the first time, the Agent writes its content into the session as a self-declared commitment. Later turns recall the same version through an anchor instead of repeatedly injecting the full text.

### Status: Foundation for Long-Running Tasks and Memory

Status is not a background daemon. It is a context prefix generated dynamically on every turn. Before the model acts, Status describes the current time, execution mode, path permissions, suggested Skills, relevant retrieval excerpts, and the user's explicit requirements, allowing each request to continue from the current runtime state.

The current implementation already provides three layers of recoverable context: the effective conversation for the current turn; each session's SQLite history and state; and searchable documents written to `.xkagent/docs/<session>/` by `summary` or `/compact`. Recommendations search `skills`, `docs`, `historys`, and `logs` by default while excluding the current session's history. The `codes` source directory is not included in recommendations by default, but it can be added explicitly through search-scope configuration.

These mechanisms form the foundation for long-running tasks and memory; they are not the complete product capability. **Automatic orchestration of long-running tasks, plan scheduling, memory-importance assessment, forgetting and conflict resolution, and comprehensive long-term Memory management are not yet implemented.** `summary` and `/compact` must be triggered by the Agent or the user, and there is no standalone memory-management service.

## Request Flow

```text
CLI / Web
  → commands routes slash commands; plain messages enter the current session queue
  → Agent checks the session lock, resolves @ paths, and removes invalid Unicode
  → suggested Skills and relevant information are retrieved to generate Status
  → the user message is written to session SQLite
  → system prompt + effective history + images for this turn are sent to the LLM
  → llm.py produces a unified stream of reasoning / text / tool calls / usage
  → tool calls run sequentially; each result is persisted before the next LLM call
  → Agent emits structured events rendered by the CLI or WebSocket
```

`agent.py` coordinates this main path: it assembles context, drives the LLM/tool loop, and persists messages. `llm.py` uses `requests` to support OpenAI-compatible and Anthropic APIs while unifying SSE streaming, retries, interruption, usage, and tool-call fragments. Frontends consume events for thinking, text, tools, permissions, locks, errors, and turn completion without needing to understand Provider-specific details. Thinking records retained only for display are not sent back into the LLM context.

If the user interrupts a turn, the LLM stream stops at a checkpoint and the host terminates any running `pythonrt` worker. Before the next request, the Agent repairs incomplete assistant/tool pairings so that a malformed message sequence is not sent to the Provider.

## Code Organization

The entry layer stays thin. `__main__.py` and `main.py` parse unified arguments, determine the workdir, and then start `repl.py` or `web.py` as needed. `web_main.py` is only a compatibility entry point fixed to Web mode. `config.py` derives runtime directories in one place, while `provider_config.py` handles Providers, model aliases, and mtime-based hot reloading.

The runtime kernel is concentrated in these groups of files:

- `agent.py` and `manager.py` manage turns, per-session threads, queues, focus changes, and recovery.
- `tools.py` and `sandbox.py` define the LLM tool surface and the `pythonrt` execution environment.
- `llm.py` isolates differences between Provider protocols.
- `skill.py` and `search.py` provide capability discovery, content extraction, retrieval, and long-term document writing.
- `agent_runner.py` and `agent_worker.py` host the isolated sub-Agent loop.

Infrastructure is similarly separated: `history.py` manages SQLite, `lock.py` manages session leases, and `_log.py` manages standard-library logging and rotation. At the interaction layer, `commands.py` registers commands once so the CLI and Web share the same dispatch semantics. Each frontend then handles either terminal input or FastAPI, WebSocket, authentication, and file APIs.

## Execution Permissions Are Not a Security Container

`pythonrt` selects an execution profile based on the current mode:

- `plan`: The workdir and mounts are read-only, `/tmp` is writable, and import, process, and path capabilities are restricted.
- `build`: The workdir and mounts declared as `ro/rw` are writable, while path, import, and process gates remain in place.
- `build-unsafe`: Execution is close to host Python and can use arbitrary paths, third-party libraries, subprocesses, and the network, but write protection for the current `.xkagent/` data directory remains.

Restricted workers validate read and write roots after `realpath` resolution, narrow builtins and imports, intercept common file and process entry points, and apply CPU, memory, file-size, and file-descriptor limits where the platform supports them. SQLite, Dulwich Git, and the network stack are enabled through extension registration, with additional checks at relevant entry points. `register_extension` also lets a project explicitly add controlled library capabilities.

This remains a soft boundary intended to reduce the risk of accidental LLM operations. It is not designed to resist malicious Python, prompt injection, or host escape. `build-unsafe`, `!shell`, forced mounts, and custom extensions all expand the trust boundary; they cannot replace Git, operating-system permissions, containers, or backups.

## Sessions, State, and Concurrency

Each session has a SQLite database and an independent Agent thread with input and output queues at runtime. `AgentManager` sends new input only to the focused session, but other sessions can continue turns in the background. Stopping a thread does not delete its history, and switching back to the session can restore it.

SQLite currently persists messages and commands, the Provider and model, cumulative and most-recent-turn token counts, dynamic mounts, search scopes, and one-time image state. The execution mode and automatic Skill-selection toggle are runtime state: when the Agent restarts, they return to their defaults rather than being fully persisted. `/compact` preserves the original database records, rebuilds the effective context from a truncation marker and an LLM summary, and writes the summary to docs. `/drop` only adds a marker and clears the current effective context; it does not create a long-term summary.

Each session uses a `<session>.db.lockdir/` lease for exclusive writes. Owner metadata and heartbeats identify the holder, recover stale locks, and handle crashes within the same process. Other processes that open the session enter observer mode. This lock prevents concurrent writes from corrupting a session; it is not a database backup or transactional file rollback.

Sub-Agents use a message loop isolated from the main conversation. Their model, available tools, step limit, total timeout, and image input can be configured, and their final result must be valid JSON. By default, a sub-Agent cannot call `exit` or invoke another `agent`; nesting is allowed only when explicitly enabled.

## Runtime Directory

```text
<workdir>/
├─ .xkagent/
│  ├─ historys/<session>.db[/-wal/-shm]  # messages, commands, and session state
│  ├─ historys/<session>.db.lockdir/      # lease and owner metadata
│  ├─ logs/                               # process logs and rotated files
│  ├─ docs/<session>/                     # long-term summary / compact documents
│  ├─ skills/                             # user Skills; override built-ins of the same name
│  ├─ search_index/                       # optional semantic index
│  ├─ provider.config                     # workdir-level Provider configuration
│  ├─ permission.txt                      # static path permissions
│  ├─ search_ranges.txt                   # global search additions and exclusions
│  └─ history.txt                         # input history shared by CLI / Web
├─ skills/                                # built-in Skills distributed with the code
├─ system_prompt.txt                      # main Agent prompt
├─ system_prompt_compact.txt              # compaction prompt
└─ provider.config                        # fallback project-level configuration
```

When the workdir is the XKAgent repository root, both the project's topic documents and session documents generated at runtime are located under `.xkagent/docs/`; the latter are organized into per-session subdirectories.

## Extension Points

- Prefer adding a new capability as a Skill containing `skill.md`. Modify the Agent's main loop only when introducing new low-level execution semantics.
- Add Providers and models through `provider.config`; changes are reloaded automatically based on mtime.
- Enable sandboxed library capabilities through `register_extension`, and add checks to entry points that could bypass Python's file APIs.
- Define the schema and executor for a new helper tool in `tools.py`, then bind it to each Agent's independent tool table.
- Register a new slash command in `commands.py`; both the CLI and Web will receive it.
- Configure new search scopes through `/info` or `search_ranges.txt`. A new frontend can reuse the `AgentManager` queues and structured events.

## Topic Guides

- [01 · Entry and Startup](01-entry-and-startup.md): Startup sequence, arguments, and frontend dispatch.
- [02 · Configuration](02-configuration.md): Workdir, Providers, models, and hot reloading.
- [03 · Infrastructure](03-infrastructure.md): SQLite, logging, and session leases.
- [04 · LLM Layer](04-llm-layer.md): Protocol adapters, SSE, retries, interruption, and usage.
- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): `pythonrt`, execution profiles, and protection boundaries.
- [06 · Agent Engine](06-agent-engine.md): Main loop, events, multiple sessions, and sub-Agents.
- [07 · Skills](07-skills.md): Skill directories, frontmatter, selection, and caching.
- [08 · Search and Memory](08-search-and-memory.md): Retrieval scopes, ngram/embedding, and long-term documents.
- [09 · Command System](09-commands.md): Shared commands, state changes, and auditing.
- [10 · Frontends](10-frontends.md): CLI, Web, file APIs, and authentication.
