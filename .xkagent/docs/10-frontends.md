# 10 · Frontends (CLI / Web)

[简体中文](10-前端界面.md) | English

The CLI and Web interface are two entry points to the same Agent Runtime. The CLI is designed for sustained work in a local terminal, while Web provides browser-based chat, multiple sessions, and file operations. Both share `AgentManager`, the command system, and session data under `<workdir>/.xkagent/`.

## Choosing an Interface

- Prefer the CLI for day-to-day development, keyboard-driven input, and scripted invocation.
- Use Web when you need browser chat, mobile access, a session sidebar, or file uploads.
- Both interfaces can access the same sessions. If multiple processes open the same session at once, only the lock holder can write; the other instances enter observer mode.

## CLI: Starting in the Terminal

When `--mode` is omitted, XKAgent starts in CLI mode:

```text
python -m codes --workdir <project-directory>
```

Common startup forms include:

```text
python -m codes --workdir <project-directory> --session demo
python -m codes --workdir <project-directory> --resume
python -m codes --workdir <project-directory> --prompt "Inspect the current project"
```

`--session` specifies a session name, `--resume` restores the most recent session, and `--prompt` completes a single request and exits. If no session is specified, XKAgent first selects the most recently used session; it creates a timestamp-based name only when no previous session exists.

Interactive mode provides line editing without additional dependencies:

- Use `↑` / `↓` to browse input history. The arrow keys, Home, End, and Delete edit the current line.
- Enter `\` at the end of a line to continue on the next line. Tab cycles through `plan`, `build`, and `build-unsafe` while preserving unsubmitted text.
- Ctrl+N / Ctrl+P switches between running sessions. Changing focus does not stop the previous session's turn.
- Ctrl+C interrupts the current turn, Ctrl+E edits the buffer with `$EDITOR`, Ctrl+L clears the screen, and Ctrl+W deletes the previous word.
- `@<path>` includes a file's contents in the request. `!<command>` executes a shell command directly on the host, bypassing the `pythonrt` sandbox.

Raw terminal editing depends on `termios` and is primarily intended for Unix-like TTYs. When standard input is not a TTY, the CLI falls back to line-by-line input without the editing shortcuts above.

## Web: Working in the Browser

Web requires FastAPI and Uvicorn and can be started through the unified entry point:

```text
python -m codes --mode web --workdir <project-directory>
```

The default address is `http://127.0.0.1:7860`. Use `--port` to change the port, or use the compatibility entry point that always starts in Web mode:

```text
python -m codes --mode web --workdir <project-directory> --port 9090
python -m codes.web_main --workdir <project-directory>
```

The service does not open a browser automatically. It selects the most recent session as the default and warms up the corresponding Agent in the background to reduce the delay on the first request.

### Sessions and Live Status

The sidebar can create, switch, stop, and fork sessions. Stopping a session ends only its Agent thread; it does not delete messages from the msgz store. Switching back starts the Agent again and restores the session. Changing focus also leaves turns running in other sessions uninterrupted.

The page initially loads the 50 most recent messages and can paginate backward. New messages and Agent events stream over WebSocket. The interface displays LLM, tool execution, lock, crash, and recovery states as they change. If another process holds the lock for the same session, the current instance can only observe it and cannot submit state-changing operations.

Model responses are appended as plain text while streaming, then rendered as Markdown with file-path links when the turn ends. Tool calls, progress, results, Thinking, and Summary use separate collapsible sections, and the same view is restored from history after a refresh or session switch. **Clear View** clears only the current browser view; it does not delete session data, so reopening the session loads its history again.

Mode buttons apply to the current session. A model can be set for either the current session or all sessions that have already started. Slash commands use the same registry as the CLI. Because Web cannot provide terminal-style confirmation, dangerous mounts must be explicitly resubmitted with `--force` when prompted.

### File Browsing and Uploads

The file page permits browsing, opening, and downloading only within these roots:

- The current workdir.
- Static mounts in `permission.txt`.
- Dynamic mounts saved for the current session.

The backend normalizes the target with `realpath` before checking whether it is within an allowed root, so a symbolic link pointing outside a root cannot bypass the restriction.

Uploaded files are written to `<workdir>/.xkagent/files/`. The service retains only the basename, adds a timestamp when a name already exists, and writes files in 1 MB chunks. Images can then be used as session attachments; other files can be referenced by their returned paths. A single upload is limited to 50 MB (exceeding it returns HTTP 413); disk usage should still be monitored.

## Authentication and Deployment Boundaries

Web authentication is disabled by default and enabled only when `--password` is provided. The default username is `admin`:

```text
python -m codes --mode web --workdir <project-directory> --user admin --password <password>
```

After a successful login, the service stores the token in process memory and writes it to a cookie with an expiry (12 hours by default); old tokens become invalid as soon as the process restarts. This mechanism provides only single-account access protection. It is not a multi-user authorization system and does not provide TLS; every authenticated user shares the same workdir and Agent capabilities.

Listening on `127.0.0.1` by default narrows the reachable surface, but it is not authentication. Other users or processes on the same host may still access a service with authentication disabled. Recommended practices:

- Keep the service bound locally; do not use port forwarding, tunneling, or public exposure.
- Before sharing the service on a controlled intranet, enable a strong password and follow your organization's access-control requirements.
- Treat uploads, downloads, slash commands, and `!shell` as high-privilege entry points.
- Do not store secrets in chat, command history, or uploaded files.
- Use `build-unsafe` and `!shell` only in trusted environments; neither is protected by the regular sandbox.

Finally, the CLI and Web interface share `.xkagent/history.txt` for input history, whereas `/cmds` reads command records from the current session store. `/exit` leaves the interactive loop in the CLI and requests server shutdown in Web.

## Further Reading

- [01 · Entry and Startup](01-entry-and-startup.md): Unified entry point, dependency checks, and command-line arguments.
- [03 · Infrastructure](03-infrastructure.md): Session data, locks, history, and logs.
- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): File permissions and the three execution modes.
- [09 · Command System](09-commands.md): Shared slash commands and `!shell`.
