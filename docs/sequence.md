# Sequence: one suspend/resume cycle

```mermaid
sequenceDiagram
    participant D as Driver
    participant Dae as jaato daemon
    participant A as Goal actor session
    participant J as Slow job

    D->>Dae: cascade_budget_set(cid, limits)
    D->>Dae: create_session(profile, agent, cascade_driver_id=cid)
    Dae-->>D: session_id
    D->>A: send_message(goal)

    A->>J: start
    A->>J: status → running
    Note over A: must not block —<br/>ends the turn instead
    A->>Dae: signal_completion(outcome="suspended",<br/>resume_at, watch_handle, progress_note)
    Dae-->>D: AgentCompletedEvent(payload)
    Note over A: turn loop ends,<br/>runner slot released

    D->>D: persist due row (atomic write)
    D->>D: sleep until resume_at

    D->>Dae: session.wake(session_id, text=replayed state,<br/>event_id=goal-resume:sid:n)
    Note over D,Dae: text carries progress_note +<br/>watch_handle verbatim — the only<br/>state guaranteed to survive GC
    Dae->>A: revive from disk, drive a USER turn
    A->>J: status → failed (fixable)
    A->>A: fix job_config.json, restart job
    A->>Dae: signal_completion(outcome="suspended", …)
```

The cycle repeats until the agent reports `outcome: "finished"`, or the cascade
budget refuses further work.

## Why `session.wake` and not attach-then-send

`signal_completion` terminates the turn loop and releases the runner slot, so by
the time the resume is due the session is usually cold. `session.wake`:

- cold-revives from disk (`resume_session` → drive) in one call;
- resolves the workspace **server-side** from the persisted record, so a caller
  cannot point revival at a weaker sandbox root;
- wraps the text as untrusted content, so the woken turn treats it as data;
- dedups on `event_id`, making a crash between waking and recording safe.

The driver passes no `cascade_driver_id` on the wake, so the daemon's
deferred-turn path (which holds a turn pending a client attach) never engages —
the driver is attached and running by definition, since it holds the clock.
