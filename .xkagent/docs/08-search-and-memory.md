# 08 · Search and Memory

[简体中文](08-搜索与记忆.md) | English

---

XKAgent already provides local search, session history, per-turn Status injection, and the ability to save summaries as searchable documents. Together, however, these features do not constitute full long-term Memory. Understanding each feature's role helps avoid confusing "stored once" with "guaranteed to be recalled later."

## Four Concepts and Their Roles

- **Search** finds short excerpts relevant to the current question within specified directories. It may or may not find a match.
- **Session history** is stored in the current session's msgz store (a single zlib-compressed file) and can continue serving as conversation context when that session is restored.
- **Status** is a temporary state prefix generated before every normal message, showing the model the current time, mode, path permissions, candidate Skills, and recommended information. Its **Status Info** layer is a session-level, writable key-value board (`addinfo` / `listinfo` / `rminfo`) that is injected every turn and preserved across compaction.
- **Long-term Memory** remains on the Roadmap. There is currently no factual entity store, conflict resolution, forgetting policy, or user-profile management.

Status is therefore not a standalone memory store, and search results do not guarantee reliable recall. More precisely, the current features provide a state and retrieval foundation that a future Memory system can use.

## What Happens During a Normal Turn

After receiving a normal message, the Agent searches for candidate Skills and relevant information based on its content, then constructs Status:

```text
Time
System mode (plan / build / build-unsafe)
Permission summary for the project root, /tmp, .xkagent, and mounted paths
Suggested Skills
Recommended information
Status Info (session-level KV, injected when non-empty)
Message body
```

This prefix is written to the current session history and sent to the model together with the message body. The CLI and Web UI display the status separately, preserving only the readable body when showing past user messages.

The Status Info field is the one part of the prefix a session can write itself: the Agent and users maintain small key-value entries (progress, preferences, agreements, lessons) through `addinfo` / `listinfo` / `rminfo` (or `/addinfo` and friends). It is persisted with the prefix and remains effective after `/compact`. Use `summary` for long-form notes and conclusions—together they form a “short runtime memory / searchable long-form documents” division of labor.

Recommended information searches documents and other session histories by default (logs and `skills` are excluded). Results from `skills` are handled separately by the Skill recommendation channel, and the current session's history is excluded because it is already present in the active context. Thinking, tool results, and helper messages used for Skill injection are filtered out. At most two excerpts are retained from each file, and only a small number of paths and short excerpts are ultimately injected. Retrieval failures degrade silently and do not block the conversation.

## Default Search Scopes

All relative paths are resolved from `<workdir>`:

```text
skills   → <workdir>/skills
docs     → <workdir>/.xkagent/docs
historys → <workdir>/.xkagent/historys
logs     → <workdir>/.xkagent/logs
```

`codes` is excluded from search by default but can be added when needed. `historys` is the fixed spelling used by the current code. The Skill loader and the search system's `skills` scope share the same directories (user-level `<workdir>/.xkagent/skills` first, built-in `skills` as fallback), so Skills stay consistent between loading and retrieval.

### Adjusting Scopes

`/info` displays the global configuration, the current session configuration, and their merged result:

```text
/info
/info add docs/design docs
/info add codes codes
/info deny secrets
/info remove codes
/info clear
```

By default, these commands modify the current session's `search_state`. Adding `--global` updates `.xkagent/search_ranges.txt` for use by all sessions:

```text
/info add --global shared/docs docs
/info deny --global private
```

`deny` excludes a target path prefix and its entire subtree. The current automatic recommendation process across scopes does not pass session parameters through every content-collection call. If automatic recommendations must consistently use a particular scope, prefer global configuration. Session configuration is still displayed and saved, but may not take full effect in some scenarios.

## Retrieval Methods

The default primary channel is local ngram search, which works well for Chinese text, keywords, and phrases without requiring a model service. Semantic content first uses ngram matching and falls back to grep if there are no results. Exact content such as configuration, logs, and code symbols prefers grep. Semantic files larger than 1 MB are not structurally extracted.

Embeddings are an optional supplement and are disabled by default. Set the following environment variable before startup to enable them:

```text
XKAGENT_EMBEDDING=1
```

`true` and `yes` are also recognized. Once enabled, the system asynchronously warms up a local ONNX model and merges vector results into the ngram results. If FAISS, ONNX Runtime, Transformers, or NumPy is unavailable, it still falls back to ngram search.

Use `/updateembedding` to fully rebuild `.xkagent/search_index/<scope>.db`; `/updateskillembedding` is an alias. This command performs vector encoding even when embeddings are not enabled. The index is not currently updated incrementally as files change.

> If the local model is incomplete, the first vector encoding attempt tries to download a model package 【非官方】. The download endpoint is empty by default (set `_ONNX_MODEL_DOWNLOAD_URL` to enable); when unset, prepare the model directory manually. In an internal network environment, confirm the software source and network policy first. Keep embeddings disabled, as they are by default, if vector retrieval is unnecessary.

## Session History, Compact, and Summary

Messages for each session are stored in `.xkagent/historys/<session>.msgz` (a single file). A normal restore reloads the effective history. This differs from cross-session retrieval: the former is the current conversation context, while the latter only searches other sessions' stores for relevant excerpts.

When the context grows too long, run `/compact`. The system generates a summary in a dedicated model turn. On success, it:

- Writes a `compact` marker to the session store while retaining old messages.
- Replaces the in-memory context sent to the model in subsequent turns with the summary.
- Writes `.xkagent/docs/<session>/<时间戳>_compact.md`.

Compaction neither deletes history nor clears the session. If generation fails or is interrupted, finalization does not occur.

The Agent can also call the `summary` tool to save important decisions, conventions, external facts, or critical procedures to:

```text
.xkagent/docs/<session>/<时间戳>_summary.md
```

A successful call ends the current turn immediately. Summary and Compact documents both enter the `docs` scope and may be retrieved later, but they are not injected unconditionally and are not guaranteed to match future searches. They are searchable persistent records, not an implemented long-term Memory system.

## Related Documentation

- [03 · Infrastructure](03-infrastructure.md): session storage and `.xkagent` data
- [06 · Agent Engine](06-agent-engine.md): how Status, Skills, and retrieval enter a turn
- [07 · Skills](07-skills.md): Skill directories, loading, and retrieval
- [09 · Command System](09-commands.md): `/info`, `/updateembedding`, and `/compact`
