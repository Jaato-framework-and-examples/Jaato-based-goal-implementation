#!/usr/bin/env python3
"""A deliberately slow, deliberately broken job for the agent to shepherd.

Stands in for the real thing this pattern exists for — a CI run, an evaluation,
a training job: something that takes minutes, cannot notify anyone when it
finishes, and has to be polled.

It fails the first two runs with a fixable fault, so a full demo takes three
attempts and at least three suspend/resume cycles.

Usage::

    python fixtures/slow_job.py start     # begin a run (returns immediately)
    python fixtures/slow_job.py status    # running | passed | failed
    python fixtures/slow_job.py reset     # clear all state

Set ``JOB_DURATION_SECONDS`` to shorten it for tests (default 90).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE = Path(__file__).parent / ".job-status.json"
DURATION = float(os.environ.get("JOB_DURATION_SECONDS", "90"))
CONFIG = Path(__file__).parent / "job_config.json"


def _read() -> dict:
    """Current job state, or an empty dict when no run has started."""
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    STATE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def start() -> int:
    """Begin a run. Returns immediately — the job 'completes' on a timer."""
    state = _read()
    run = int(state.get("run", 0)) + 1
    _write({"run": run, "started_at": time.time(), "status": "running"})
    print(f"started run #{run}; poll `status` — takes about {DURATION:.0f}s")
    return 0


def status() -> int:
    """Report the run's status, computing completion from elapsed time.

    The fault is real and fixable: runs fail while ``job_config.json`` has
    ``retries`` below 2. Setting it higher makes the next run pass — which is
    the thing the agent has to notice and act on between suspends.
    """
    state = _read()
    if not state:
        print(json.dumps({"status": "not_started"}))
        return 0
    if state.get("status") == "running":
        elapsed = time.time() - float(state.get("started_at", 0))
        if elapsed < DURATION:
            print(json.dumps({
                "status": "running",
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(DURATION - elapsed, 1),
            }))
            return 0
        retries = 0
        if CONFIG.exists():
            try:
                retries = int(json.loads(CONFIG.read_text()).get("retries", 0))
            except (json.JSONDecodeError, OSError, ValueError):
                retries = 0
        if retries >= 2:
            state["status"] = "passed"
            state["detail"] = "all checks green"
        else:
            state["status"] = "failed"
            state["detail"] = (
                "flaky-network check exhausted its retries: "
                f"job_config.json has retries={retries}, needs at least 2"
            )
        _write(state)
    print(json.dumps({k: state[k] for k in ("run", "status", "detail")
                      if k in state}))
    return 0


def reset() -> int:
    """Clear job state so the demo can be run again from scratch."""
    STATE.unlink(missing_ok=True)
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
