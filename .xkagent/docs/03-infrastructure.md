# 03 · Infrastructure (Storage, Locks, and Logging)

[简体中文](03-基础设施.md) | English

Where is session data stored, what happens when another process already holds a session with the same name, and where are logs written? This guide brings together the underlying mechanisms that rarely matter in day-to-day use but become important during backups and troubleshooting.

## Where runtime data is stored

XKAgent keeps runtime data under `<workdir>/.xkagent/`. Depending on the enabled features, a typical layout is:

```text
<workdir>/.xkagent/
├── historys/
│   ├── <session>.msgz          # single-file session store (zlib-compressed JSON)
│   └── <session>.lockdir/      # single-writer lease (owner.json)
├── state/
│   └── <session>.json          # per-session KV (status board)
├── logs/
│   └── <启动时间>.log
├── docs/<session>/  # summary/compact runtime memory
├── skills/
├── permission.txt
└── search_ranges.txt
```

Session locks and some configuration files appear only when their related features are enabled. If legacy `*.db` files remain in the directory, they are kept as read-only history. When the repository root is the workdir, project documentation and runtime memory share `.xkagent/docs/`; each session's runtime memory remains in its own subdirectory.

## One msgz store per session

Each session corresponds to `historys/<session>.msgz`—a single zlib-compressed JSON file that holds messages and session state (the selected Provider and model, token counts, dynamic mounts, search scopes, images, and more). Session names are limited to 100 characters and may contain only letters, numbers, underscores, hyphens, and periods.

Storage model:

- **Memory-first**: messages and state live in memory; writes complete in memory and return immediately, so storage I/O failures cannot block the conversation.
- **Periodic sync**: every 30 seconds by default, an in-memory snapshot is persisted atomically (write a temporary file, then `os.replace`), so the on-disk file is always a complete, consistent version.
- **Single-file portability**: backups, copies, and migrations only need one `.msgz` file—there are no `-wal`/`-shm` sidecars.
- **Legacy compatibility**: old `*.db` files are not deleted automatically and remain as read-only history; `codes/msgz_migrate.py` migrates SQLite sessions to `.msgz` in one pass.

Slash commands and `!` commands are written to history with `role='command'`, with results truncated to 4,000 characters. Before insertion, XKAgent attempts to mask common token, key, secret, password, authorization, and bearer values. This is only a basic safeguard and cannot identify every credential format, so commands should still avoid unnecessary sensitive information.

## Session files and backups

Session management operates on the single-file store:

- Creating a session initializes an empty `.msgz` store and Agent state; the file is created on first persist.
- Fork first flushes the source session, then copies its `.msgz` as the starting point of the new session (truncated copies are also supported).
- Rename renames the corresponding `.msgz` file and updates the session registry.
- Delete removes the session's `.msgz` and `.lockdir` (legacy `.db` files are kept as read-only history).
- Sync force-flushes the latest in-memory state to disk (equivalent to the old WAL checkpoint semantics).

Because persistence uses "temporary file + atomic replace," the on-disk `.msgz` is always a complete and consistent version; copying it while the process runs still yields a valid snapshot (possibly slightly stale). For the freshest data, run `/session sync` first or stop the relevant process.

## mkdir lease: a cross-process single-writer lock

Instead of `flock`, XKAgent competes for a session write lock by atomically creating a directory:

```text
historys/<session>.lockdir/owner.json
```

`owner.json` records the process-instance ID, PID, thread ID, hostname, owner, and lock acquisition time. `mkdir` is atomic on the NFS server as well, so only one of multiple competing clients can succeed.

After acquiring the lock, a daemon heartbeat thread refreshes the mtime of `owner.json` every 10 seconds. A lock is considered stale only after more than 35 consecutive seconds without a refresh. A process taking over first atomically renames the old lockdir to a temporary directory, then creates a new lockdir, writes its own owner data, starts its heartbeat, and finally removes the old directory. If `owner.json` has not yet been created, stale detection temporarily uses the mtime of the lockdir itself.

A process can also leave behind a lock owned by a thread that has exited. Once recovery confirms that the original owner thread is dead, it stops the old heartbeat, removes the remnants, and retries lock acquisition.

## When a session is busy: observer mode

If another process opens a session with the same name while it is in use, it does not crash immediately; it enters observer mode. The interface displays the busy state and any readable owner information. LLM input, store writes, and write commands are rejected so the single-writer constraint cannot be bypassed.

While idle, an observer continues polling for the lock. After the original owner exits normally, or after a stopped heartbeat makes the lock eligible for takeover, the observer automatically upgrades to the owner.

Do not manually delete a `.lockdir` whose lease is still being renewed normally, as this can break the single-writer guarantee. The 35-second interval is a crash-recovery threshold, not a session runtime limit; a healthy process renews the lease every 10 seconds.

## Logging and rotation

When the logging module is first imported, it creates the following file for the current process:

```text
<workdir>/.xkagent/logs/YYYY-MM-DD_HH-MM-SS.log
```

Each line includes the time to millisecond precision, level, module, function, line number, and message. The file records `DEBUG` and higher, while the console initially displays `INFO` and higher. After entering the CLI, the console threshold changes to `ERROR` so routine logs do not interrupt input.

After a file reaches approximately 100 MB, it rotates through `.1`, `.2`, and so on, retaining up to seven numbered backups. Seven refers to the number of backups, not days. The implementation uses the standard library's `logging.FileHandler`; logs are written synchronously and do not depend on a third-party logging library.

Run `/logfile` to view the current process's log path and its last 60 lines, which is usually enough to diagnose the most recent error.

## Continue reading in the source

`codes/history_msgz.py` implements the single-file store (memory-first, periodic sync, atomic persistence); `codes/history.py` provides session-level APIs and file operations; `codes/lock.py` implements the mkdir lease, heartbeat, and stale-lock takeover; `codes/_log.py` configures logging and rotation; and `codes/msgz_migrate.py` provides the SQLite migration. Observer mode and automatic upgrades are implemented in `codes/agent.py`.

## Related documentation

- [Entry and Startup](01-entry-and-startup.md)
- [Configuration](02-configuration.md)
- [Agent Engine](06-agent-engine.md)
- [Command System](09-commands.md)
