# A real bug, and a verdict the agent cannot write

The root example demonstrates the pattern: an agent that must wait ends its turn
and says when to come back. This one keeps that and adds the question the root
example does not answer — **who decides whether the goal was actually met?**

There, the agent reports `finished` and the driver believes it. Here it hands
over a patch, and the driver applies it to a checkout the agent has never
touched and runs the suite there itself — twice, once with the agent's tests
and once with the project's, because with tests editable a single run cannot
tell a fix from a deletion.

The task is a real one: [SWE-bench Verified][swe] instance
`sympy__sympy-24539`. `PolyElement.as_expr()` accepts replacement symbols and
then ignores them. The agent is given the issue exactly as filed — a
wrong-output symptom — and nothing else. It has to read the code and work out
why.

[swe]: https://www.swebench.com/

## Why this needs a second example rather than a flag

The root fixture's failure message names its own fix ("retries=0, needs at least
2"), so an agent can succeed by transcribing. That is fine for teaching the
suspend/resume mechanics and useless for testing whether reasoning survives a
suspend. Here the failure is a traceback, the fault is somewhere in 27MB of
unfamiliar code, and what has to survive the wait is a *hypothesis*.

## Running it

```bash
python examples/swe-bench/setup.py    # network + git; ~27MB, once
python examples/swe-bench/run.py
```

`setup.py` fetches one row of the dataset as JSON and makes two shallow
checkouts. **No Docker.** SWE-bench's official harness needs containers so that
500 heterogeneous repos score identically on every machine — a benchmarking
concern. We want one repo and our own instrumentation, so the dataset is the
only dependency.

Two checkouts, not one:

| | who writes it | what it is for |
|---|---|---|
| `repo/` | the agent | its working copy — it may do anything here |
| `upstream/` | nobody | the driver's tree, used only for verification |

## What the driver does with the patch

```
fresh worktree at base_commit
  → apply the agent's patch, in full and unrestricted
  → RUN 1: the suite with the agent's tests, exactly as it left them
  → restore the project's test files over anything the patch touched
  → RUN 2: the same suite with the project's tests        ← this one decides
  → pass: accept · fail: wake the agent with the reason and let it try again
```

**Nothing is forbidden — not even editing tests.** Localising the bug is the
agent's job, and a harness that says "only edit `rings.py`" has done that job
for it and measured something weaker. A path allowlist would not have worked
anyway: code that subverts a test run can live in whatever file the fix
legitimately touches.

**So why two runs?** Because if tests are editable and you run whatever is in
the tree, "green" is cheapest to reach by deleting the failing test — a single
run cannot tell a fix from a deletion. Restoring the project's tests for the
deciding run keeps the agent unrestricted while keeping the signal meaningful.

**And when the two disagree, that is the finding.** Agent's tests pass,
project's tests fail, and the agent has said in `file_notes` why it changed
that test: either it found a test encoding the bug — which happens — or it
weakened the measure. A machine cannot tell those apart, so the record is
flagged `needs_human_review` and a person reads the reason beside the diff.

**The gate is the reference run, deliberately.** It costs something real: a fix
that legitimately needs a test change cannot land on the agent's say-so. The
alternative costs more — gating on the agent's own run makes `finished`
self-certifying.

**Every touched file must be explained.** `file_notes` carries one entry per
path. The in-workspace processor checks the accounting is complete — cheaply,
locally, in milliseconds — because an unexplained change is one a reviewer
cannot judge, and judging is the whole reason the notes exist.

**A refusal is feedback, not failure.** The driver suspends and wakes the agent
with the reason, carried on the same replay channel as any other state. A
rejected finish is just another resume.

## What the agent is allowed to execute

`apparmor: true` is only half of it. Since jaato-server's profile template v18,
the `//child` subprofile — the agent-controlled exec context — carries no broad
`/usr/bin/** ix`, so **fragments are the sole source of exec authority there**.
A workload declares the binaries it needs and gets nothing else; a profile that
declares no fragment can exec nothing at all.

This example's `.jaato/apparmor-fragments/swe-bench.rules` is three entries
long, and each is something the goal genuinely requires:

| | why |
|---|---|
| `sh` | `run_tests.py` spawns the suite via `sh -c` |
| `python3` | runs pytest, and the fixture itself |
| `git` | the agent produces its patch with `git diff` |

What is *absent* matters, but less than it first appears. `curl`, editors and
build tools are genuinely unreachable — from inside, `curl --version` comes
back `[Errno 13] Permission denied`. That scoping is real only because the
broad rule is gone: the v18 change was prompted by an agent that improvised
`curl` when its build tool failed and got it, because a blanket grant shadowed
a fragment listing only java and mvn.

**It is not a network boundary, and it is not a sandbox.** This fragment scopes
which *binaries* may be executed. `pip` is not a binary — it is `python3 -m
pip` — and `python3` has to be here for the suite to run at all. We put the
question to a session living inside this confinement, which reported
`pip install --target ./deps six` succeeding, the package importing, PyPI
answering `200` over urllib, and even `cffi` installing from a prebuilt
manylinux wheel with no compiler involved. An earlier version of this section
claimed the opposite; it was wrong, and the transcript that corrected it is the
sort of thing [`tools/interrogate/`](../../tools/interrogate/) exists for.

Anything reachable from a Python interpreter with a socket is still reachable.
Scope the binaries, and know that is what you scoped.

The profile names its fragment explicitly (`apparmor_fragments: [swe-bench]`)
rather than leaving the field unset. Unset composes *every* fragment on the
host, which is both wider than needed and silently dependent on what else
happens to be installed.

If you run this and the agent suspends saying it lacks shell access, that is
this mechanism working: something it needs is not declared. Add it to the
fragment — the profile template denies the agent write access to that
directory, so a confined session cannot grant itself exec rights for the next
one.

## Why the agent's own tests are pollable

`fixtures/run_tests.py` wraps the same suite in a `start` / `status` interface.
The agent could obviously run `pytest` directly — and if it can, it will, inline,
for the two minutes the suite takes. The first version of this example did
exactly that and completed the whole task in a single four-minute turn:
excellent verification story, no suspend/resume at all, which is the pattern the
repo exists to argue for. Making the local wait pollable puts it back.

Those runs are the agent's own business and decide nothing — the developer's
local test run, not CI. An agent that weakened them would only be lying to
itself.

## What this proves, and what it does not

**Proves**, each verified rather than argued:

- a forged verdict fails — the record lives outside the workspace, and with
  `apparmor: true` an attempt to write there is refused by the kernel while the
  agent's own workspace writes still succeed;
- a weakened suite fails — a patch that replaces the failing test with
  `assert True` is refused, because the project's tests are restored over it;
- a stale pass fails — the tree is fingerprinted and compared at check time;
- a claim with nothing behind it fails — `patch_path` is required and re-run.

**Does not prove** the fix is *correct*, only that the suite the project
supplies passes against it. And it does not defend against an agent that
deliberately writes code to deceive the harness: the verification executes
agent-authored code, and you cannot verify by running an adversary's code and
trusting its exit status. It defends against mistakes, drift, and convenient
self-reporting — which is what real agents actually do.

## Requirements

- network + `git` for `setup.py` (the root example needs neither)
- Python 3.12 (this instance's sympy is 1.12.dev)
- for the confinement claim: AppArmor on the host **and** jaato-server
  ≥ `9f97f44e`, where `config_root` became settable on `IPCRecoveryClient`.
  Without them the session runs unconfined and everything else still holds —
  set `JAATO_REQUIRE_APPARMOR=1` if a missing boundary should be fatal rather
  than silent.
