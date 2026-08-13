# 09 · Command System

[简体中文](09-命令系统.md) | English

---

In both the REPL and Web interface, plain text is passed to the Agent, input beginning with `/` is handled by XKAgent, and input beginning with `!` is sent directly to the host shell. The two interfaces share the same slash-command registry; their main differences are interactive confirmation, session selection, and how they exit. Enter `/help` at any time to view the built-in help.

## Distinguishing the Three History Operations

`/clear`, `/drop`, and `/compact` may look similar, but they serve different purposes:

- `/clear` deletes the current session's messages and resets token statistics.
- `/drop` clears the context currently used by the Agent while retaining old messages in the database and writing a boundary marker.
- `/compact` asks the model to summarize the current conversation, retains old database messages, uses the summary as subsequent context, and writes the summary to `.xkagent/docs/<session>/`.

If the context has simply grown too long, use `/compact`. To continue with an empty context while preserving the database record, use `/drop`. Use `/clear` only when you are sure the current session's messages are no longer needed.

## Inspecting Runtime State

- `/help`: Show command help.
- `/logfile`: Show the log path for the current process and its last 60 lines.
- `/cmds [n]`: Show recent slash and `!` commands for the current session; the default is 50.
- `/sessions`: List running and stopped sessions.
- `/session`: Show the current session, model, mode, and token statistics.

`/cmds` records command operations. It is not a file-by-file change history and cannot replace Git or backups.

## Managing Sessions

```text
/session add experiment
/session fork experiment-copy
/session experiment
```

- `/session <name>`: Switch sessions. An exact name takes precedence; a unique fuzzy match is also accepted.
- `/session add <name> [--no-switch]`: Create a session and switch to it by default.
- `/session fork <name>`: Copy the current session and its database state.
- `/session rename <name>`: Rename the current session.
- `/session remove <name>`: Delete a session other than the current one.
- `/session stop <name>`: Stop the session's Agent while preserving its data.
- `/session sync [name]`: Run a SQLite WAL checkpoint.

The REPL can request interactive confirmation when deleting or renaming a session, or when a fuzzy match produces multiple results. Because Web cannot offer terminal-style multi-selection, it returns the candidate names instead. If another process holds a session, the current instance enters read-only observer mode and rejects chat input and state-changing commands.

## Models and Execution Modes

- `/model`: Show the current provider, model, reasoning effort, and available models.
- `/model <name>`: Switch models.
- `/model @<provider>`: Switch providers.
- `/model <name>:<effort>`: Switch models and temporarily override reasoning effort for the current session.
- `/model test [name]`: Test all configured models or one specified model.
- `/plan`: Switch to read-only planning mode.
- `/build`: Switch to restricted write mode.
- `/build-unsafe`: Switch to a high-risk mode close to host Python.
- `/mode`: Cycle through `plan → build → build-unsafe → plan`.

These modes constrain the Agent's `pythonrt` execution path; they do not restrict the direct `!shell` channel described below.

## Skills

- `/skills`, `/showskills`: List loadable Skills.
- `/validate [names]`: Validate all Skills or the specified Skills.
- `/skill <name>`: Read a Skill and add it to the current Agent's active Skill list.
- `/turnonskill`, `/skillson`: Enable automatic Skill selection.
- `/turnoffskill`, `/skillsoff`: Disable automatic Skill selection.

At present, `/skill` does not mean “force this Skill to run on the next turn.” Candidate retrieval, automatic selection, and content injection for ordinary messages are still handled by the turn workflow. See [Skills](07-skills.md) for details.

## Search and Images

- `/info`, `/info list`: Show the global, current-session, and merged search scopes.
- `/info add [--global] <path> [scope]`: Add a search path.
- `/info deny [--global] <path>`: Exclude targets by path prefix.
- `/info remove [--global] <path>`: Remove one configuration entry.
- `/info clear [--global]`: Clear the current configuration layer.
- `/updateembedding`, `/updateskillembedding`: Rebuild the vector index from scratch.
- `/image add <path>`: Attach a local image to the current session.
- `/image list`: List images waiting to be sent.
- `/image clear`: Clear image attachments.

Each image is limited to 10 MB, and its path is stored in the current session. The attachment list is cleared automatically after the images are injected into the next user message. By default, the vector index is not updated incrementally. `/updateembedding` may also prepare a local model, so its run time depends on the environment.

## Mounting Paths

- `/mount <path> [ro|rw|ro/rw] [--force]`: Dynamically mount a path for the current session.
- `/mount list`: Show default, global, and dynamic mounts.
- `/mount save`: Append dynamic mounts to the global `permission.txt`.
- `/mount refresh`: Reload `permission.txt`.
- `/unmount <path>`: Remove a dynamic mount from the current session.

Dynamic mounts are saved in the session database and restored after restart. `/mount save` converts them into global configuration. In `plan` mode, all mounts are treated as read-only. Dangerous paths require confirmation: the REPL prompts the user, while Web requires the command to be explicitly resubmitted with `--force`. Mounts and the sandbox are soft boundaries that reduce the risk of accidental operations; they are not security containers.

## Compaction, Restart, and Exit

- `/compact`: Summarize the session in a dedicated model turn and reset the context used from that point forward.
- `/restart [overrides]`: Stop the current Agent and restart the process, optionally overriding startup arguments such as `--mode`, `-s`, `--port`, `--resume`, and `--workdir`.
- `/exit`: Exit the REPL; in Web, shut down the server.

`/compact` is not part of the ordinary command registry. The frontend rewrites it into a dedicated message, and a streaming model turn generates the summary; it is not an immediately completed local command. If summarization fails or the turn is interrupted, the original history remains unchanged.

## `!shell`: Direct High-Privilege Access

```text
!git status
!python -m pytest
```

The REPL or Web interface passes the text after `!` directly to the host's default shell. stdout, stderr, and the exit code are displayed and recorded in the current session's command history. This channel bypasses Agent tool calls and is not constrained by `plan`, `build`, or the `pythonrt` sandbox.

Run only commands you fully understand. Do not paste untrusted one-line scripts, secrets, or destructive commands. Exceptions from slash commands are converted into error text and returned to the frontend; `!shell` instead runs with host privileges and therefore has a fundamentally different risk boundary.

## Related Documentation

- [03 · Infrastructure](03-infrastructure.md): Sessions, history, and logs.
- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): Modes, mounts, and execution boundaries.
- [07 · Skills](07-skills.md): Skill definitions, overrides, and loading.
- [08 · Search and Memory](08-search-and-memory.md): Status, scopes, embeddings, and Compact.
- [10 · Frontends](10-frontends.md): Interaction differences between the REPL and Web.
