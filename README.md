# Goal cascades with suspend and resume

A jaato example: **an agent that pursues a goal across many turns, suspending
itself whenever it has to wait, until the goal is actually accomplished.**

The idea it exists to demonstrate is one line long:

> An agent that must wait should end its turn and say when to come back — not
> block, not sleep, not poll in a loop.

Waiting inside a turn burns context and budget while nothing happens. This repo
shows how to express the alternative safely, using only the public jaato SDK. No
framework changes, no premium dependency.

---

## The pattern

The agent's only exit is `signal_completion`, and its completion schema has two
branches:

```jsonc
// still waiting
{ "outcome": "suspended",
  "progress_note": "Fixed retries, restarted run #2.",
  "resume_at":     "2026-08-22T14:05:00Z",
  "resume_reason": "run #2 takes about 90s",
  "watch_handle":  { "status_file": "fixtures/.job-status.json", "run": 2 } }

// done
{ "outcome": "finished",
  "progress_note": "Job passed after two fixes.",
  "result": { "attempts": 3, "report_path": "REPORT.md" } }
```

A driver process watches for that payload and does the one thing the agent
cannot do for itself — bring it back later:

```
spawn goal actor ──► send the goal
                        │
              AgentCompletedEvent
                        │
        ┌───────────────┴───────────────┐
   outcome=finished              outcome=suspended
        │                               │
     report, exit          persist due row ──► sleep until due
                                               ──► session.wake
                                                    (replaying state)
                                               ──► back to the top
```

**Deferral is a capability the operator grants, not one the model discovers.**
Whether an agent may pause is a property of the schema in
`.jaato/profiles/goal-actor.yaml` — there is no "set yourself a timer" tool.

---

## Why same-session resume is safe

Every resume continues the *same* session, so the agent keeps its reasoning
context. That would normally be a slow context leak, and there is a subtlety
worth understanding before you copy this pattern:

**jaato's GC policy tiers (`LOCKED` / `PRESERVABLE` / …) govern instruction
sources, not conversation messages.** There is no way to pin a message. A
`progress_note` written in cycle 3 can legitimately be summarised away by
cycle 9.

So correctness must not depend on history surviving — and here it does not.
The driver already holds the validated payload (it arrived on
`AgentCompletedEvent`), so it **replays `progress_note` and `watch_handle`
verbatim** into the resume prompt. That splits continuity in two:

| | carried by | guarantee |
|---|---|---|
| **State** — what I was waiting on, what I achieved | the driver's resume prompt | guaranteed, survives any GC |
| **Reasoning context** — how I got here, what I ruled out | session history | best-effort, degrades under GC |

History GC can be as aggressive as it likes and the goal still advances. This is
why the profile can run `gc: budget` at a 75 % threshold without risk.

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e . jaato-sdk jaato-server

cp .env.example .env        # set ONE provider key
.venv/bin/jaato-doctor --workspace . --env-file .env   # preflight

.venv/bin/goal-cascade
```

Provider and model live in `.jaato/profiles/goal-actor.yaml`, deliberately not
in `.env` — one source of truth. Swapping them is a two-line change and nothing
else in the repo moves.

The profile references the credential as `${JAATO_OPENROUTER_API_KEY}`, the only
scheme that resolves from a public checkout. A `pass://` URI needs the premium
resolver, and an unregistered scheme **fails silently** — jaato warns, then sends
the literal URI string as the API key, so you get an auth error that never
mentions secret resolution.

### What the demo does

`fixtures/slow_job.py` stands in for the real thing this pattern is for — a CI
run, an evaluation, a training job: slow, unable to notify anyone, must be
polled. It fails its first runs with a fixable fault.

The goal is *"get the job to pass, then write a report."* A full run looks like:

1. Agent starts the job, sees `running`, **suspends** with a `resume_at`.
2. Driver sleeps, wakes it. Agent sees `failed`, reads the reason, fixes
   `fixtures/job_config.json`, restarts the job, **suspends** again.
3. Driver wakes it. Agent sees `passed`, writes `REPORT.md`, reports
   **`finished`**.

Three turns of real work with two long waits between them, and the waits cost
nothing. Shorten them with `JOB_DURATION_SECONDS=10` while experimenting.

---

## How it works

| Piece | File | Role |
|---|---|---|
| Completion schema | `.jaato/profiles/goal-actor.yaml` | the two-branch exit; declaring it is what exposes `signal_completion` at all |
| Persona | `.jaato/agents/goal-actor.md` | teaches the no-blocking rule and how to write a `progress_note` for its future self |
| Driver loop | `src/goal_cascade/driver.py` | owns the clock, replays state, enforces the ceiling |
| Durable state | `src/goal_cascade/store.py` | due rows that survive the driver restarting |

Three details that carry more weight than their size suggests:

- **`ClientType.API`** — `signal_completion` is hidden from terminal-role
  clients, and it is the agent's only exit here.
- **`IPCRecoveryClient`, not `IPCClient`** — a goal outlives any single daemon
  lifetime and must survive a restart mid-wait.
- **`session.wake` rather than attach-then-send** — `signal_completion` releases
  the runner slot on every suspend, so the session may be cold. `session.wake`
  revives it from disk and dedups on `event_id`, so a driver that dies between
  waking and recording cannot drive the same resume twice.

### The ceiling is the cascade budget

**Limits account; rungs act.** `cascade_budget_set()` declares both, and the
distinction is the whole mechanism. The `limits` — `usd`, `tokens`, `seconds`,
`tool_calls`, `turns` — are accounting: they accumulate continuously and decide
where on the ladder you are. What *happens* at a threshold is whatever the
`degrade` ladder says. A ladder with no terminal action correctly answers "over
budget" with "switch to the cheaper model", and a goal that never converges then
runs past every limit you set.

So the ceiling here is the last two rungs, not the numbers:

```python
degrade=[
    {"at":  80.0, "model_tiers": {"planner": {"model": "…haiku-4.5"}}},
    {"at":  95.0, "action": "finalize"},   # wrap up with what you have
    {"at": 100.0, "action": "abort"},      # hard stop; further turns refused
]
```

`abort` latches, so the session refuses subsequent turns rather than serving
them — a ceiling that only cancelled one turn could be talked past.

A separate path bounds *admission*: a spawn into a cascade whose pot is dry is
refused outright rather than started and killed, and the driver exits `2` for
that case so automation can tell "stopped at the ceiling" from "broke".

**A mid-flight ceiling stop currently exits `1`, not `2`.** When a rung aborts a
running session, the refusal reaches a `session.wake`-driven client as prose and
a log line — there is no terminal event and no typed reason to branch on. The
driver reports and exits non-zero, but cannot yet say *why*. Distinguishing it
would mean substring-matching the daemon's output, which is worse than the
honest ambiguity. Reported upstream; this paragraph goes when a typed signal
lands.

**`turns` is a turn counter, not a resume count.** One resume cycle usually
costs several turns (inspect → act → signal). The driver *reports* its resume
count; the budget *enforces* the ceiling. They are not the same number.

**Counting across suspends needs jaato-server ≥ `6261b10e`.** A suspended
session is unloaded — jaato evicts on orphan, and a driver holding the clock is
orphaned during every wait — so accumulated usage has to survive a reload for a
cross-turn ceiling to mean anything. Before that commit it did not, and the
dimensions that bound *long* goals were exactly the ones that reset: `usd` still
worked, because it can be crossed inside a single turn, while `turns` and
`seconds` silently restarted at zero on every resume. A ceiling that quietly
stops applying is worse than no ceiling, so check the version before relying on
one.

---

## What this deliberately does not do

- **No daemon-side scheduler.** The clock lives in the driver — that is what
  makes this run on shipped jaato with no framework changes. The cost:
  **driver lifetime is the durability boundary.** State survives a driver
  restart (due rows are on disk and recovered on start), but the *schedule does
  not advance while no driver is running* — a resume due during downtime fires
  when the driver next starts, not on time. For genuinely unattended operation
  the clock belongs beside the daemon's wake ingress.
- **No reactor variant.** Routing the suspend through the premium reactor engine
  (`where` on `outcome`) is a natural second example, but cannot ship in a repo
  meant to run from public dependencies.
- **Permissions are wide open.** The profile sets `defaultPolicy: allow` because
  nobody is present to answer a prompt. A real deployment gates a goal actor
  like any other agent.

## Tests

```bash
.venv/bin/python -m pytest
```

They cover the parts that decide whether a goal advances — restart recovery,
idempotent claiming, and the continuation contract — with the daemon faked out,
so they need no model and no credentials.
