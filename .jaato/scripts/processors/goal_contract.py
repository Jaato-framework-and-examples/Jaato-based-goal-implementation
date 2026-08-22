"""Completion processor (validate-only): enforce the two-branch goal contract.

`completion_payload_schema` in goal-actor.yaml declares which fields *may*
appear, but it cannot express "resume_at is required when outcome is
suspended" — the profile states that in prose, and prose does not validate.
The gap is not academic: a `suspended` payload with no `resume_at` passes
schema validation today, which is why the driver carried a silent "+1 minute"
default. That default invented a wait the agent never asked for.

Two branches, two contracts:

* ``suspended`` — must say WHEN to come back and WHAT to look at. A
  ``resume_at`` already in the past is rejected rather than accepted, because
  the driver treats a past timestamp as immediately due: the wait silently
  does not happen, the wake races the previous turn's wind-down, and the goal
  advances while appearing to have paused. Observed live as timestamps a full
  YEAR in the past before the agent was taught to read the clock.

* ``finished`` — must be TRUE, not merely claimed. The goal is "get the job to
  pass, then write a report", so both halves are checked against real state:
  the job's own status file, and the report on disk. An agent that watched the
  job pass and reported success without writing the report has not finished
  the goal it was given.

A non-empty return blocks the completion and the framework re-prompts the agent
with these strings, so each one is written as an instruction for the next
attempt rather than a post-mortem.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Where the demo's job records its own state. The agent's claim about the job
# is checked against this file rather than against what the agent says.
JOB_STATUS_FILE = "fixtures/.job-status.json"
REPORT_FILE = "REPORT.md"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating the trailing ``Z`` models emit."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_suspended(payload: Dict[str, Any]) -> List[str]:
    """A pause must name its own end, and what to re-inspect when it gets there."""
    errors: List[str] = []

    if not payload.get("resume_reason"):
        errors.append(
            "outcome='suspended' requires 'resume_reason': state what you are "
            "waiting for, in one line."
        )
    if not payload.get("watch_handle"):
        errors.append(
            "outcome='suspended' requires 'watch_handle': the path, id or URL "
            "to re-inspect when you wake. It is replayed back to you verbatim."
        )

    stamp = payload.get("resume_at")
    if not stamp:
        errors.append(
            "outcome='suspended' requires 'resume_at': an absolute UTC "
            "timestamp like 2026-08-22T14:05:00Z. Call get_environment with "
            "aspect='datetime' to read the current UTC time, then add your wait."
        )
        return errors

    try:
        when = _parse_iso(str(stamp))
    except (ValueError, TypeError):
        errors.append(
            f"'resume_at' is not a valid ISO-8601 timestamp: {stamp!r}. "
            "Use the form 2026-08-22T14:05:00Z."
        )
        return errors

    now = datetime.now(timezone.utc)
    if when <= now:
        errors.append(
            f"'resume_at' ({stamp}) is already in the past — now is "
            f"{now.isoformat()}. A past timestamp resumes instantly, so the "
            "wait you asked for never happens. Read the current UTC time with "
            "get_environment(aspect='datetime') and add enough margin that the "
            "instant is still in the future when this turn ends."
        )
    return errors


def _validate_finished(payload: Dict[str, Any], workspace: Path) -> List[str]:
    """'Finished' is a claim about the world; check the world, not the claim."""
    errors: List[str] = []

    if not payload.get("result"):
        errors.append(
            "outcome='finished' requires 'result': an object recording what "
            "was achieved, e.g. {\"attempts\": 3, \"report_path\": \"REPORT.md\"}."
        )

    status_path = workspace / JOB_STATUS_FILE
    if not status_path.exists():
        errors.append(
            f"the job has never been started ({JOB_STATUS_FILE} does not "
            "exist), so the goal cannot be finished. Start it with "
            "`python fixtures/slow_job.py start`."
        )
    else:
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")).get("status")
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"could not read {JOB_STATUS_FILE}: {exc}")
            status = None
        if status is not None and status != "passed":
            errors.append(
                f"the job's status is '{status}', not 'passed', so the goal is "
                "not achieved. If it is still running, report outcome="
                "'suspended' with a fresh resume_at instead of 'finished'."
            )

    if not (workspace / REPORT_FILE).exists():
        errors.append(
            f"the goal also requires writing {REPORT_FILE}, and it does not "
            "exist yet. Write it — what was wrong, and how many attempts it "
            "took — then report 'finished'."
        )

    return errors


def validate(payload: Dict[str, Any], context: Any) -> List[str]:
    """Return a list of contract violations; empty means the completion stands.

    Args:
        payload: The validated ``signal_completion`` payload. Schema-level
            checks have already passed, so this only enforces the per-branch
            rules the schema cannot express.
        context: The processor's ``RenderContext``. ``workspace_path`` anchors
            the on-disk checks for the ``finished`` branch.

    Returns:
        Error strings, each phrased as what to do next — the framework replays
        them to the agent, which then gets another attempt.
    """
    outcome = payload.get("outcome")

    if outcome == "suspended":
        return _validate_suspended(payload)

    if outcome == "finished":
        workspace = Path(getattr(context, "workspace_path", None) or ".")
        return _validate_finished(payload, workspace)

    # The schema's enum already constrains this; reaching here means the schema
    # and this processor have drifted apart, which is worth saying out loud
    # rather than silently passing.
    return [
        f"unknown outcome {outcome!r}: expected 'suspended' or 'finished'."
    ]
