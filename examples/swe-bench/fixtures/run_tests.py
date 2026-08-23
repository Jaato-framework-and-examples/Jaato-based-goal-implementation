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
SCOPE = os.environ.get("SWE_SCOPE", "sympy/polys/tests")


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
    )
    STATE.write_text(json.dumps(
        {"run": run, "started_at": time.time(), "status": "running"}, indent=2),
        encoding="utf-8")
    print(f"started run #{run} (pytest {SCOPE}); poll `status` — takes ~2 min")
    return 0


def _summarise(output: str) -> tuple[str, str]:
    failed = [ln for ln in output.splitlines() if ln.startswith("FAILED")]
    tail = (output.strip().splitlines() or [""])[-1]
    if not failed and "passed" in tail:
        return "passed", tail
    return "failed", "; ".join(failed[:6]) or tail


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
