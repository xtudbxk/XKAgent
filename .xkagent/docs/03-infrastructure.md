# 03 · Infrastructure (Storage, Locks, and Logging)

[简体中文](03-基础设施.md) | English

Why is each session stored in a SQLite file? What happens when another process is already using a session with the same name, and where are logs written? This guide brings together the underlying mechanisms that rarely matter in day-to-day use but become important during backups and troubleshooting.

## Where runtime data is stored

XKAgent keeps runtime data under `<workdir>/.xkagent/`. Depending on the enabled features, a typical layout is:

```text
<workdir>/.xkagent/
├── historys/
│   ├── <session>.db
│   ├── <session>.db-wal
│   ├── <session>.db-shm
│   └── <session>.db.lockdir/
├── logs/
│   └── <启动时间>.log
├── docs/<session>/  # summary/compact runtime memory
├── skills/
├── permission.txt
└── search_ranges.txt
```

SQLite sidecars, session locks, and some configuration files appear only when their related features are enabled or while a database is in use. When the repository root is the workdir, project documentation and runtime memory share `.xkagent/docs/`; each session's runtime memory remains in its own subdirectory.

## One SQLite database per session

Each session corresponds to `historys/<session>.db`. Session names are limited to 100 characters and may contain only letters, numbers, underscores, hyphens, and periods.

New databases use WAL mode and create all six current tables at once:

- `messages` stores user, assistant, tool, compaction, command, and other messages, together with extended metadata, turn numbers, and timestamps.
- `agent_state` stores the Provider and model currently selected for the session.
- `token_state` stores cumulative and most-recent-turn token counts, turn counts, and the last-used model.
- `mount_state` stores dynamic mounts.
- `search_state` stores search scopes and deny rules.
- `image_state` stores paths to attached images.

The schema version is recorded when a database is first created. When an older database is opened, tables or columns are added only if its version is behind. SQLite connections use a 10-second wait timeout.

Slash commands and `!` commands are written to history with `role='command'`, with results truncated to 4,000 characters. Before insertion, XKAgent attempts to mask common token, key, secret, password, authorization, and bearer values. This is only a basic safeguard and cannot identify every credential format, so commands should still avoid unnecessary sensitive information.

## Session files and backups

Session management handles not only the primary `.db` file but also its WAL sidecars:

- Creating a session initializes the database schema and Agent state.
- Before a fork, XKAgent runs a WAL checkpoint and then copies any existing `.db`, `.db-wal`, and `.db-shm` files.
- Rename also checkpoints first; if the operation fails partway through, it attempts to roll back files that have already moved.
- Delete removes the primary database and its sidecars. If the main file is missing while sidecars remain, the session is reported as corrupted.
- Sync uses `PRAGMA wal_checkpoint(TRUNCATE)` to write as much WAL content as possible back to the main database and truncate the sidecar. If the database is still busy, WAL data that has not yet been persisted is retained.

Do not copy only the `.db` file while a session is still being written; doing so may omit content that has not yet been checkpointed from the WAL. The safest backup procedure is to stop the relevant process first. File-level session operations should likewise use the built-in sync/checkpoint flow beforehand.

## mkdir lease: a cross-process single-writer lock

Instead of `flock`, XKAgent competes for a session write lock by atomically creating a directory:

```text
historys/<session>.db.lockdir/owner.json
```

`owner.json` records the process-instance ID, PID, thread ID, hostname, owner, and lock acquisition time. `mkdir` is atomic on the NFS server as well, so only one of multiple competing clients can succeed.

After acquiring the lock, a daemon heartbeat thread refreshes the mtime of `owner.json` every 10 seconds. A lock is considered stale only after more than 35 consecutive seconds without a refresh. A process taking over first atomically renames the old lockdir to a temporary directory, then creates a new lockdir, writes its own owner data, starts its heartbeat, and finally removes the old directory. If `owner.json` has not yet been created, stale detection temporarily uses the mtime of the lockdir itself.

A process can also leave behind a lock owned by a thread that has exited. Once recovery confirms that the original owner thread is dead, it stops the old heartbeat, removes the remnants, and retries lock acquisition.

## When a session is busy: observer mode

If another process opens a session with the same name while it is in use, it does not crash immediately; it enters observer mode. The interface displays the busy state and any readable owner information. LLM input, database writes, and write commands are rejected so the single-writer constraint cannot be bypassed.

While idle, an observer continues polling for the lock. After the original owner exits normally, or after a stopped heartbeat makes the lock eligible for takeover, the observer automatically upgrades to the owner.

Do not manually delete a `.db.lockdir` whose lease is still being renewed normally, as this can break the single-writer guarantee. The 35-second interval is a crash-recovery threshold, not a session runtime limit; a healthy process renews the lease every 10 seconds.

## Logging and rotation

When the logging module is first imported, it creates the following file for the current process:

```text
<workdir>/.xkagent/logs/YYYY-MM-DD_HH-MM-SS.log
```

Each line includes the time to millisecond precision, level, module, function, line number, and message. The file records `DEBUG` and higher, while the console initially displays `INFO` and higher. After entering the REPL, the console threshold changes to `ERROR` so routine logs do not interrupt input.

After a file reaches approximately 100 MB, it rotates through `.1`, `.2`, and so on, retaining up to seven numbered backups. Seven refers to the number of backups, not days. The implementation uses the standard library's `logging.FileHandler`; logs are written synchronously and do not depend on a third-party logging library.

Run `/logfile` to view the current process's log path and its last 60 lines, which is usually enough to diagnose the most recent error.

## Continue reading in the source

`codes/history.py` defines the database schema and session-file operations; `codes/lock.py` implements the mkdir lease, heartbeat, and stale-lock takeover; `codes/_log.py` configures logging and rotation. Observer mode and automatic upgrades are implemented in `codes/agent.py`.

## Related documentation

- [Entry and Startup](01-entry-and-startup.md)
- [Configuration](02-configuration.md)
- [Agent Engine](06-agent-engine.md)
- [Command System](09-commands.md)
