# 11 · Multi-Agent Communication (mail and callagent)

[简体中文](11-多Agent通信.md) | English

How do sessions send messages to each other, how does a task "wake up at a scheduled time," and how can a long-running task be hosted across sub-agents? This page explains the mail primitives and the `callagent` tool; step-by-step guides for the three standard workflows live in `skills/callagent_workflows/`.

## Three Use Cases

| Use case | Shape | Entry point |
|---|---|---|
| Scheduled wake-up | Set a "time alarm" for a session (including yourself); the task inside the mail runs when it fires | `callagent(delay_seconds=…)` / `callagent(deliver_at=…)` |
| Information exchange | Ask an existing session a question or align on data/state; the conclusion lands directly in that session's context | `callagent(need_reply=True)` |
| Long-task hosting | Split a long task across sub-agents, keep inspecting progress, and correct drift against acceptance criteria | see `skills/callagent_workflows/longtask_manage.md` |

For the fuller suitability checklist and workflow templates, see `skills/callagent_workflows/skill.md`.

## mail: a Global Message Bus

All cross-session mail lands in one append-only file: by default `<project root>/mail.jsonl` (next to `session_registry.json`). The path can be overridden with the `XKAGENT_MAIL` environment variable; `XKAGENT_MAIL=off` disables mail entirely (no writes).

- **One line per event**: a `send` line opens a mail; subsequent `delivered / done / failed / dead / rejected` status lines describe its progress;
- **State machine**: `send → delivered → done`; failures are recorded as `failed` (retry limit 2) and eventually reach the terminal `dead` state; cancellation is `rejected`;
- **Byte cursor with torn-line handling**: readers consume incrementally by byte offset; half-written lines are awaited, and if one can never be completed only that half-line is skipped — without dropping the complete mails before it and without stalling;
- **Atomic lock**: writes go through a millisecond-scale critical section in `mail.jsonl.lockdir` (atomic mkdir mutex; stale locks are taken over automatically);
- **At-least-once**: after crashes or restarts, the carrier rescans and re-delivers orphaned mail, so duplicates are possible — **design task letters to be idempotent**;
- **Compaction**: once the bus reaches 5,000 mail entries, fully terminal mails are pruned; mails with in-flight or pending recipients are kept together with each recipient's latest state, so replay never re-delivers to completed recipients (archive long-term audit logs separately).

## MailPostman: the Per-Round Carrier

When `AgentManager` starts its first Agent, it lazily starts a MailPostman thread that actually delivers bus mail into target sessions:

- Two-phase delivery with `turn_id` tracking decides precisely when the recipient has finished a round;
- Sessions currently running a turn are skipped and retried once idle; mail to one session is delivered serially (an `in_flight` gate);
- Failed deliveries are retried after a cooldown (`MAIL_MAX_RETRY=2`); exhausted retries become terminal `dead`, which prevents infinite re-delivery loops;
- Mail to a valid-but-unregistered session name auto-creates the session (new sessions default to `plan` mode with no external mounts).

## The callagent Tool

The model sends mail through `callagent` (fully asynchronous; returns immediately):

| Parameter | Description |
|---|---|
| `to` | Recipient session name; a list = broadcast (one mail, many recipients, each delivered/retried independently; broadcasts are notification-only and cannot use `need_reply`); you may target yourself (self-scheduling) |
| `message` | Body, ≤3500 bytes; **task letters must be self-contained** (attach background, data, and goals) |
| `reply_to` | Required when replying = the `msg_id` of the incoming mail (builds a traceable reply chain) |
| `delay_seconds` | Delayed delivery in seconds; `to=yourself` turns this into a self-scheduling loop (scheduled wake-up) |
| `deliver_at` | Absolute delivery timestamp (Unix seconds, 1s precision; takes precedence over `delay_seconds`) |
| `priority` | Delivery priority (higher goes first; default 0) |
| `provider` | LLM provider for the recipient's turn (e.g. `myprovider` or `provider/model:effort`; effective only for this turn and does not change the session's own configuration) |
| `need_reply` | `true` means a reply is expected: the recipient's instruction includes reply guidance; the default `false` is notification-only and does not solicit answers |

Quick examples:

```python
# Dispatch a task and request a reply
callagent(to="worker_a", message="Summarize dataset X as JSON; reply when done.", need_reply=True)

# Reply after receiving a task (set reply_to to the incoming msg_id)
callagent(to="<incoming from>", message="Done: …", reply_to="<incoming msg_id>")

# Wake yourself up in 30 minutes to inspect progress
callagent(to="<current session>", delay_seconds=1800, message="Check task progress and record drift")
```

## Long-Running and Scheduled Task Patterns

- **Self-scheduling chain**: after each step, the task session calls `callagent(to=itself, reply_to=previous)` to advance; the chain ends naturally when no further mail is sent;
- **Checkpoint idempotence**: persist step state to a task file or session history; on duplicate delivery, check `step_id` and report "already executed" instead of running again;
- **Parallel fan-out**: the orchestrator dispatches subtasks to sub-agents one by one; each sub-agent replies to the orchestrator on completion (aggregate by `reply_to`);
- **Inspection and correction**: the host regularly wakes itself, checks progress against acceptance criteria, and corrects drift (see `longtask_manage.md`).

Worked example: `skills/paper_collect` demonstrates a full fan-out/aggregate flow ("parallel collection → dedupe → translation → HTML catalog").

## Operations

- `/mail list|get|pending|cancel|send`: inspect recent mail, fetch one mail's details, list pending deliveries, cancel undelivered mail, and compose mail manually;
- Common questions (delivery, states, naming, recovery) are covered in `skills/callagent_workflows/faq.md`.

## Limits and Boundaries

- Delivery relies on carrier polling (~1s granularity); `delay_seconds` is relative time, so prefer `deliver_at` (absolute time) for long timers to reduce drift;
- `deliver_at` across containers carries a risk of minute-level clock skew (NTP alignment is advisable);
- Mail semantics are at-least-once: duplicates or delays are possible in extreme cases, so use idempotence plus checkpoints for important tasks;
- A single mail body is limited to 3500 bytes; keep large state in task files and put only pointers in mail.

## Related Documentation

- [06 · Agent Engine](06-agent-engine.md): sessions, threads, and turns
- [09 · Command System](09-commands.md): `/mail` and other commands
- `skills/callagent_workflows/`: full templates for the three workflows and FAQ
- `docs/mail_longtask_plan.md`: design note for long-running/scheduled tasks (status at the top)
