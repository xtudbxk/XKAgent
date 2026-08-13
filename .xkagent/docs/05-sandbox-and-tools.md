# 05 · Sandbox and Tool Execution

[简体中文](05-沙箱与工具执行.md) | English

XKAgent follows a **code over tools** approach: instead of selecting a dedicated tool for every file read, data transformation, or library call, the model writes Python for `pythonrt` and combines related steps into a single execution. A small set of supporting tools handles search, persistence, and flow control, while general-purpose computation remains concentrated in one runtime.

## A Deliberately Small Tool Surface

The main Agent currently exposes six tools to the model:

- `pythonrt`: Executes Python and serves as the primary entry point for file operations, data processing, and multi-step logic.
- `searchskill`: Continues searching the skill library when the currently recommended Skill is not suitable.
- `searchinfo`: Searches for file excerpts within specified, authorized directories.
- `summary`: Writes information worth retaining across sessions to `.xkagent/docs/<session>/`; a successful call ends the current turn.
- `exit`: Ends the current turn when the Agent encounters an infinite loop, an unrecoverable error, or a task it cannot complete.
- `agent`: Runs a short-lived sub-Agent loop in a separate subprocess.

`agent` reuses the same Provider, model, and sandbox permissions while completing delegated work in an independent context. See [06 · Agent Engine](06-agent-engine.md) for tool filtering, nesting controls, and the result format.

User-entered `!command` instructions use an explicit shell channel. They are not part of the LLM tool set and do not pass through a `pythonrt` profile. Because users invoke them directly, their risk boundary differs from model-initiated tool calls.

## How `pythonrt` Executes Code

The model should generally group related operations into one code block: read the necessary files, perform the work, verify the result, and use `print` to return a concise summary. This reduces round trips between the model and tools while keeping path and import checks within a single execution boundary.

`code_or_filepath` may contain source code or the path to an existing `.py` file. The supplied `workdir` controls only the worker's current directory and the base for relative paths; it cannot expand authorization. Accessible roots are always determined by the Agent workdir, `permission.txt`, and dynamic mounts together.

Each call starts an independent Python worker. Parameters are passed as JSON over stdin, and results are returned with the `__SANDBOX_RESULT__` marker. Reader threads continuously consume stdout and stderr and can convert them into tool progress events. On timeout or interruption, the parent process kills and reaps the worker, usually returning 124 for a timeout or 130 for a user interruption. File writes, database commits, and network requests that have already completed are not rolled back.

## Three Modes, Three Trust Levels

```text
plan         Restricted Python; workdir and mounts are read-only, /tmp is writable
build        Restricted Python; workdir and ro/rw mounts are writable, ro mounts are read-only
build-unsafe Close to host Python; arbitrary imports, paths, network access, and subprocesses are allowed
```

`plan` and `build` use the same lightweight sandbox and differ only in write permissions. Both allow the project's adapted network stack and SQLite by default; after dulwich is installed, they can also use the built-in Git path. Network destinations are not restricted by host or IP allowlists, so "restricted" does not mean offline.

`build-unsafe` bypasses the normal restricted execution path. It is appropriate for tasks that genuinely require a full test toolchain, third-party C extensions, or subprocesses. It is not a slightly relaxed version of `build`, but a substantial reduction in security. Commit with Git or back up the workspace before entering this mode.

## How Path Permissions Are Determined

Restricted modes merge accessible roots from three sources:

1. Agent workdir: read-only in `plan`, writable in `build`.
2. `permission.txt`: `ro` is always read-only; `ro/rw` is read-only in `plan` and writable in `build`.
3. `/mount`: adds dynamic mounts for the current session under the same rules.

`/tmp` is always writable. Dynamic mounts are stored in the session database and restored after restart. `/mount save` synchronizes them to `permission.txt`; it is not required to persist dynamic mounts.

The sandbox resolves each path with `realpath`, then applies the permission of the longest matching root. This blocks ordinary `..` and symlink escapes while allowing a more specific read-only child directory to override a writable parent. `<workdir>/.xkagent` contains configuration, secrets, history, and state and is intended to remain read-only in every mode. Restricted modes provide relatively complete protection, while `build-unsafe` only wraps some Python write entry points and must not be treated as impossible to bypass.

## Limits of the Lightweight Sandbox

`codes/sandbox.py` implements several layers of soft restrictions using the standard library: it removes built-ins such as `exec`, `eval`, and `compile`; wraps common file and `os` path operations; restricts dangerous modules and third-party libraries; and, on supported platforms, sets limits for CPU, memory, file size, and open file descriptors. SQLite, the network stack, dulwich, and similar capabilities are selectively enabled through extension registration and entry-point patches.

These measures are designed to reduce accidental LLM damage, such as writing to the wrong directory, deleting files outside the project, or unintentionally launching commands. This is not container, virtual-machine, or WASM isolation, and it does not defend against deliberate escapes, malicious dependencies, or kernel-level attacks. Third-party C extensions may bypass Python-level wrappers; unreviewed libraries should only be used with trusted code in `build-unsafe`.

Platforms such as Windows that do not support `resource` or `RLIMIT_*` skip the corresponding resource limits. Parent-process timeout enforcement remains effective, but memory and file size may not have hard limits. Restricted scripts cannot invoke commands through `subprocess` themselves, although the main program still launches `pythonrt` or `agent` workers for isolation.

## Writes Are Not Transactional

`pythonrt` modifies the real workspace directly. There are no pre-write snapshots, per-file transactions, or automatic rollback after failure. The Agent can use the current session record, backup files, and diffs to attempt reverse edits, but the result still requires human review. A worker that fails or is killed partway through a script may also leave partial changes behind.

The current implementation provides three profiles, path and import restrictions, worker isolation, progress forwarding, interruption, and graceful degradation of resource limits. Automatic snapshots, transactional rollback, container-level isolation, and network destination policies have not been implemented. They are possible future enhancements and must not be inferred from the current sandbox.

## Related Documentation

- [02 · Configuration](02-configuration.md): workdir, `permission.txt`, and Providers
- [04 · LLM Calling Layer](04-llm-layer.md): how tool calls emerge from the model stream
- [06 · Agent Engine](06-agent-engine.md): tool loops, sub-Agents, and interruption
- [07 · Skills](07-skills.md): how Skills organize scripts and extensions
- [09 · Command System](09-commands.md): mode switching, `/mount`, and `!command`
