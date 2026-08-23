"""Completion processor: the cheap checks, done where they are cheap.

Enforces what JSON Schema cannot say — "resume_at is required when outcome is
suspended" — and, for a finish, that the completion actually NAMES a patch that
exists. Nothing more.

It does NOT decide whether the fix works, and it could not: it runs on the
agent's turn-exit path, in the agent's session, over the workspace the agent
edits. The driver re-runs the named patch on a clean checkout with the
project's own tests restored, and that is what accepts or refuses the finish.

The division is deliberate. This catches an agent that forgot the patch or
pointed at a file that is not there — mistakes, instantly, before a 110-second
verification is spent on them. The driver catches everything that matters.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List

REPORT_FILE = "REPORT.md"
LOCAL_SUITE = "fixtures/.test-status.json"


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_suspended(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not payload.get("resume_reason"):
        errors.append(
            "outcome='suspended' requires 'resume_reason': what are you "
            "waiting for?"
        )
    if not payload.get("watch_handle"):
        errors.append(
            "outcome='suspended' requires 'watch_handle': what to re-inspect "
            "when you wake. It is replayed back to you verbatim."
        )
    stamp = payload.get("resume_at")
    if not stamp:
        errors.append(
            "outcome='suspended' requires 'resume_at', an absolute UTC "
            "timestamp. Call get_environment with aspect='datetime' for the "
            "current time, then add your wait."
        )
        return errors
    try:
        when = _parse_iso(str(stamp))
    except (ValueError, TypeError):
        errors.append(f"'resume_at' is not valid ISO-8601: {stamp!r}")
        return errors
    now = datetime.now(timezone.utc)
    if when <= now:
        errors.append(
            f"'resume_at' ({stamp}) is already past — now is {now.isoformat()}. "
            "A past timestamp resumes instantly, so the wait never happens. "
            "Re-read the clock and leave margin for the rest of this turn."
        )
    return errors


def _validate_finished(payload: Dict[str, Any], workspace: Path) -> List[str]:
    """Check only that the claim is CHECKABLE — the driver checks the claim."""
    errors: List[str] = []
    if not payload.get("result"):
        errors.append(
            "outcome='finished' requires 'result' recording what was achieved."
        )

    patch_rel = payload.get("patch_path")
    if not patch_rel:
        errors.append(
            "outcome='finished' requires 'patch_path': a workspace-relative "
            "unified diff of your fix, e.g. `cd repo && git diff > ../fix.diff`. "
            "Without it the driver has nothing to re-run on a clean checkout, "
            "so the claim cannot be checked at all."
        )
    elif not (workspace / patch_rel).is_file():
        errors.append(
            f"patch_path {patch_rel!r} does not exist in the workspace."
        )
    elif not (workspace / patch_rel).read_text(
            encoding="utf-8", errors="replace").strip():
        errors.append(f"patch_path {patch_rel!r} is empty — no fix to check.")

    # Every file the patch touches must be accounted for. Cheap and local:
    # parse the diff, compare against file_notes. Nothing is forbidden — the
    # agent may change tests — but an unexplained change is one a reviewer
    # cannot judge, and judging is the whole reason the notes exist.
    if patch_rel and (workspace / patch_rel).is_file():
        diff = (workspace / patch_rel).read_text(encoding="utf-8", errors="replace")
        touched = []
        for line in diff.splitlines():
            if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
                path = line[4:].strip()
                path = path[2:] if path[:2] in ("a/", "b/") else path
                if path and path not in touched:
                    touched.append(path)
        explained = {n.get("path") for n in (payload.get("file_notes") or [])
                     if isinstance(n, dict)}
        missing = [t for t in touched if t not in explained]
        if missing:
            errors.append(
                "file_notes must account for every file your patch touches; "
                f"missing: {', '.join(missing)}. Say what you changed there and "
                "why — including for tests, which you may change, but a human "
                "will read your reason."
            )

    # The agent's OWN suite must be green. Not to make the example suspend —
    # because a change that repairs one test and breaks another is not a fix,
    # and claiming `finished` without having looked is claiming something
    # unchecked. The driver re-runs everything on a clean tree regardless; this
    # catches the omission in milliseconds instead of after a two-minute
    # verification, and it is the agent's own diligence being checked, not its
    # honesty — an agent that faked this file would only be deceiving itself.
    suite = workspace / LOCAL_SUITE
    if not suite.exists():
        errors.append(
            "you have not run the project's test suite. Start it with "
            "`python fixtures/run_tests.py start` and check it with "
            "`status` — a fix that breaks other tests is not a fix, and you "
            "cannot know which you have made without running them."
        )
    else:
        try:
            state = json.loads(suite.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"could not read {LOCAL_SUITE}: {exc}")
            state = {}
        if state.get("status") == "running":
            errors.append(
                "your test run has not finished — you cannot know the result "
                "yet. Report `suspended` with a fresh resume_at instead."
            )
        elif state.get("status") != "passed":
            errors.append(
                f"your last test run came back {state.get('status')!r}: "
                f"{state.get('detail', '')}. Fix that before claiming finished."
            )

    if not (workspace / REPORT_FILE).exists():
        errors.append(
            f"the goal also requires {REPORT_FILE} — what the bug was, the fix, "
            "and how you verified it. Write it, then report 'finished'."
        )
    return errors


def validate(payload: Dict[str, Any], context: Any) -> List[str]:
    """Return contract violations; empty means the completion stands."""
    outcome = payload.get("outcome")
    if outcome == "suspended":
        return _validate_suspended(payload)
    if outcome == "finished":
        workspace = Path(getattr(context, "workspace_path", None) or ".")
        return _validate_finished(payload, workspace)
    return [f"unknown outcome {outcome!r}: expected 'suspended' or 'finished'."]
