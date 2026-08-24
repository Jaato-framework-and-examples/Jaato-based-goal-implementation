#!/usr/bin/env python3
"""The agent's own test suite, made pollable so waiting for it costs a suspend.

Same ``start`` / ``status`` / ``reset`` shape as the root example's
``slow_job.py``, and for the same reason — except nothing here is simulated.
This runs sympy's real ``polys`` tests, which take about two minutes.

WHY THIS WRAPPER EXISTS AT ALL, since the agent could obviously just run pytest:
because then it would block inside its turn for two minutes, and the whole
argument of this repo is that an agent which must wait should end its turn and
say when to come back. Handed a direct ``pytest``, an agent reasonably waits
inline — the first version of this example did exactly that and completed the
whole task in one four-minute turn, demonstrating the verification story and
none of the suspend/resume one. Making the wait pollable is what puts the
pattern back in the example.

These runs are the agent's own business: they inform its iteration and decide
nothing. The verdict comes from the driver re-running the project's tests on a
clean checkout (see ../verify_patch.py). An agent that weakened the tests here
would only be lying to itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / ".test-status.json"
LOG = HERE / ".test-output.txt"
DONE = HERE / ".test-exitcode"

REPO = Path(os.environ.get("SWE_REPO", HERE.parent / "repo"))
PYTHON = os.environ.get("SWE_PYTHON", sys.executable)
# Written by setup.py from the instance itself. No default: a wrong scope
# runs a suite that has nothing to do with the bug and reports it green.
SCOPE = os.environ.get("SWE_SCOPE") or (
    HERE.parent / "scope.txt").read_text(encoding="utf-8").strip()



def _env_with_deps(workspace: Path) -> dict:
    """The child's environment, with the workspace's `.deps` on PYTHONPATH.

    Computed here rather than inherited. The agent's side of this runs inside a
    confined runner whose environment is assembled by the daemon, and relying on
    a variable surviving that path is a dependency this does not need — both
    spawn sites already know where the workspace is.

    The directory is added whether or not it exists yet. Python skips absent
    sys.path entries at import time, so a package the agent installs mid-run
    resolves on the next import with nothing to restart. That is the same reason
    the telegram client wires its tools venv onto sys.path before the venv is
    created.

    The driver and the agent MUST see the same directory. If the agent installs
    a dependency the verifier cannot import, the suite passes for the agent and
    fails for the driver, and the run is refused over an environment skew rather
    than anything about the patch.
    """
    deps = workspace / ".deps"
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{deps}{os.pathsep}{existing}" if existing else str(deps)
    return env


def _read() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def start() -> int:
    """Kick the suite off in the background and return immediately."""
    run = int(_read().get("run", 0)) + 1
    LOG.write_text("", encoding="utf-8")
    DONE.unlink(missing_ok=True)
    # Completion is signalled by a FILE, never by the process table:
    # `os.kill(pid, 0)` succeeds for a ZOMBIE, so a finished-but-unreaped child
    # reads as running forever. That cost an earlier version of this example a
    # goal that polled a suite which had passed ten minutes before.
    subprocess.Popen(
        ["sh", "-c",
         f"{PYTHON} -m pytest {SCOPE} -q --no-header -p no:cacheprovider "
         f"> {LOG} 2>&1; echo $? > {DONE}"],
        cwd=str(REPO), start_new_session=True,
        env=_env_with_deps(HERE.parent),
    )
    STATE.write_text(json.dumps(
        {"run": run, "started_at": time.time(), "status": "running"}, indent=2),
        encoding="utf-8")
    print(f"started run #{run} (pytest {SCOPE}); poll `status` — takes ~2 min")
    return 0


def _contract() -> tuple[list, list]:
    """The tests this instance is actually judged on.

    Read from the instance row that setup.py already fetched. This is the same
    contract the driver verifies against, and the agent is told it deliberately:
    being judged on a rule you cannot see is not a test of anything useful.
    """
    instance = HERE.parent / "instance.json"
    if not instance.is_file():
        return [], []
    row = json.loads(instance.read_text(encoding="utf-8"))
    return json.loads(row["FAIL_TO_PASS"]), json.loads(row["PASS_TO_PASS"])


def _summarise(output: str) -> tuple[str, str]:
    """Judge the run the way the driver will, not by whether everything is green.

    A scope can carry failures that have nothing to do with the bug — this
    instance's does, four of them, where a 2023 sympy meets a 2026 numpy. An
    agent told only "the suite failed" will go and try to fix those, which it
    cannot do and was never asked to. So the answer names the two things that
    matter: did the bug's tests start passing, and did anything that used to
    pass stop.
    """
    failed = set()
    for line in output.splitlines():
        if line.startswith("FAILED") and " " in line:
            ident = line.split(None, 1)[1].split(" - ")[0].strip()
            failed.add(ident)
            failed.add(ident.split("::")[-1])
    tail = (output.strip().splitlines() or [""])[-1]

    fail_to_pass, pass_to_pass = _contract()
    unfixed = [t for t in fail_to_pass if t in failed]
    regressed = [t for t in pass_to_pass if t in failed]
    unrelated = len([ln for ln in output.splitlines()
                     if ln.startswith("FAILED")]) - len(unfixed) - len(regressed)

    if unfixed or regressed:
        detail = ""
        if unfixed:
            detail += f"still failing, must pass: {', '.join(unfixed[:4])}. "
        if regressed:
            detail += f"broken by your change: {', '.join(regressed[:4])}. "
        return "failed", detail.strip()
    note = (f" ({unrelated} unrelated failure(s) in this package predate your "
            f"change and are not yours to fix)" if unrelated > 0 else "")
    return "passed", f"{tail}{note}"


def status() -> int:
    state = _read()
    if not state:
        print(json.dumps({"status": "not_started"}))
        return 0
    if state.get("status") == "running":
        if not DONE.exists():
            print(json.dumps({
                "status": "running",
                "elapsed_seconds": round(time.time()
                                         - float(state.get("started_at", 0)), 1),
            }))
            return 0
        outcome, detail = _summarise(
            LOG.read_text(encoding="utf-8", errors="replace"))
        state.update(status=outcome, detail=detail)
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps({k: state[k] for k in ("run", "status", "detail")
                      if k in state}))
    return 0


def reset() -> int:
    for path in (STATE, LOG, DONE):
        path.unlink(missing_ok=True)
    print("reset")
    return 0


def main() -> int:
    commands = {"start": start, "status": status, "reset": reset}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"usage: {sys.argv[0]} {{{'|'.join(commands)}}}", file=sys.stderr)
        return 2
    return commands[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
