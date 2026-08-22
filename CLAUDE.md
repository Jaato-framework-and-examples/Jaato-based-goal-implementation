# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this repo is

A **public example** of a jaato cascade that pursues a goal across many turns:
the agent suspends itself whenever it must wait, and a driver resumes it until
the goal is met or a budget ceiling stops it.

It is a teaching artifact first and a program second. Clarity and honesty about
limits outrank cleverness. If a change makes the pattern harder to read, it is
probably the wrong change.

## Current state — read this before planning work

**The loop has never been run end-to-end against a live daemon.** The unit tests
pass (15, daemon faked out) and the profile validates clean against the live
registry, but no real model has ever driven a suspend/resume cycle. Treat the
first live run as the top priority and expect to find bugs there.

Everything else is committed and pushed to `main`.

## Verifying a change

```bash
python3 -m venv .venv
.venv/bin/pip install -e . jaato-sdk jaato-server pytest

.venv/bin/python -m pytest                                    # 15 tests, no daemon needed
.venv/bin/jaato-scaffold validate .jaato/profiles/goal-actor.yaml   # exit 0 = clean
.venv/bin/jaato-doctor --workspace . --env-file .env          # preflight before a live run
```

Prefer `jaato-scaffold` / `jaato-doctor` over reading jaato's source — they
introspect the *installed* framework, so they never go stale. The
`jaato-sdk-client` skill covers them.

For a live run, shorten the fixture: `JOB_DURATION_SECONDS=10 .venv/bin/goal-cascade`.

## Invariants — changing these breaks the pattern

Each of these was learned the hard way. Do not "simplify" one without
understanding why it is there.

1. **`ClientType.API` is load-bearing.** `signal_completion` is hidden from
   terminal-role clients, and it is the agent's only exit here. A terminal-type
   client makes the agent unable to finish a turn.

2. **Resume goes through `session.wake`, never attach-then-send.**
   `signal_completion` terminates the turn loop *and releases the runner slot*,
   so by resume time the session is usually cold. `session.wake` cold-revives it
   from disk, resolves the workspace server-side, and dedups on `event_id`.

3. **The driver replays `progress_note` and `watch_handle` verbatim on every
   resume.** jaato's `GCPolicy` tiers govern *instruction sources*, not
   conversation messages — there is no way to pin a message, so anything the
   agent said in an earlier cycle may have been summarised away. Correctness must
   never depend on session history surviving. This is the single most important
   line in the driver; do not remove it as redundant.

4. **The wake carries no `cascade_driver_id`.** With a cid set and no client
   attached, the daemon defers the turn until a client attaches — which for an
   unattended resume means never. If you add a cid to the wake, you must handle
   the deferred path.

5. **`cli(preload)` and `file_edit(preload)`.** Without `(preload)` those tools
   are discovery-gated, and a cheap model may never call `list_tools` to find
   them. `jaato-scaffold validate` reports which tools are gated.

6. **The credential is `${JAATO_OPENROUTER_API_KEY}` interpolated in the
   profile.** A `pass://` URI needs a resolver that ships in the private
   jaato-premium package, and an unregistered scheme **fails silently** — jaato
   warns, then sends the literal URI string as the API key. This repo must run
   from a public checkout.

7. **`turns` in the cascade budget is a turn counter, not a resume count.** One
   resume cycle usually costs several turns. The driver reports resumes for
   observability; the budget enforces the ceiling. Never conflate them in docs
   or output.

## Non-goals — do not add these

The scope is deliberate. Each of these was considered and excluded:

- **A daemon-side scheduler.** The clock lives in the driver, which is what lets
  this run on shipped jaato with no framework changes. The cost — driver lifetime
  is the durability boundary — is stated plainly in the README and must stay
  stated. Do not "fix" it here; that work belongs beside the daemon's wake
  ingress, in the framework.
- **A reactor variant.** Routing the suspend through the reactor engine needs
  jaato-premium, which is private. This repo must run from public dependencies.
- **Tightening the permission policy.** `defaultPolicy: allow` is correct for an
  unattended example and is explained in both the profile and the README.

## Conventions

- Docstrings are not optional. If you touch a class or function whose docstring
  is missing, stale, or misleading, fix it in the same change. State transitions
  and non-obvious parameters get documented on the type that owns them.
- The README's "what this deliberately does not do" section is a feature. Keep it
  accurate; do not quietly widen scope without updating it.
- Tests run without a daemon, a model, or credentials. Keep it that way — a test
  that needs an API key will not run in CI or for a reader.

## Background

The design rationale, the comparison that produced it, and the framework gaps
this pattern works around live in the `jaato` repo, on branch
`claude/prime-agent-jaato-comparison-imds01`:

- `docs/design/example-repo-goal-cascade-suspend-resume.md` — this repo's design
- `docs/compare-jaato-prime-agent.md` §4 — why suspend/resume exists, what jaato
  lacks (a clock), and what it already has (`wake_session`, `cascade_budget_set`,
  completion schemas)

They are context, not requirements. This repo stands on its own.
