#!/usr/bin/env python3
"""Re-run the agent's fix on a clean checkout, with the project's own tests.

The agent finds the bug, designs the fix, writes the code, and tests it however
it likes — that is its work and nothing here constrains it. What it cannot do
is decide whether it succeeded. It hands over a patch; this applies that patch
to a tree it never touched, restores the test files from the repository, and
runs them.

Why restoring the tests is the whole point: without it, a patch that edits a
test travels into the verification tree and the agent has simply moved its
weakened suite one directory over. `git checkout` of the test paths after
applying the patch discards any such edit, so what runs is the project's
assertion of correct behaviour, not the agent's.

AUDIT, NOT PERMISSION. The touched paths are recorded and reported; none are
forbidden. Localising the bug is the agent's job, and a harness that says
"only edit rings.py" has done that job for it and measured something weaker.
A path allowlist would also not work: code that subverts the test run can live
in whatever file the fix legitimately touches.

WHAT THIS DOES NOT PROVE, stated here rather than discovered later. The
verification executes code the agent wrote, so it defends against mistakes,
drift, and convenient self-reporting — a forged receipt, a weakened suite, a
stale pass — and not against an agent that deliberately writes code to deceive
the harness. You cannot verify by executing attacker-controlled code and
trusting its exit status. For that, the run needs to be disposable and the
signals need to be ones the agent never sees.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run(args: List[str], cwd: Path, timeout: int = 900):
    return subprocess.run(args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def _touched(patch_text: str) -> List[str]:
    """Paths the patch claims to modify, for the record."""
    seen: List[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            path = path[2:] if path[:2] in ("a/", "b/") else path
            if path and path not in seen:
                seen.append(path)
    return seen


def make_verifier(*, repo: Path, base_commit: str, test_paths: List[str],
                  test_patch: str, scope: str, python: str,
                  workspace: Path, receipts: Path, log=print):
    """Build the driver's `verify_finished` callable."""

    def verify(payload: Dict[str, Any]) -> Optional[str]:
        rel = payload.get("patch_path")
        if not rel:
            return ("the completion named no patch_path — the finish cannot be "
                    "checked, so it is not accepted")
        patch_file = (workspace / rel).resolve()
        if not patch_file.is_file():
            return f"patch_path {rel!r} does not exist in the workspace"
        patch_text = patch_file.read_text(encoding="utf-8", errors="replace")
        if not patch_text.strip():
            return f"patch_path {rel!r} is empty"

        touched = _touched(patch_text)
        log(f"[verify] patch touches {len(touched)} path(s): {', '.join(touched)}")

        tree = Path(tempfile.mkdtemp(prefix="swe-verify-"))
        worktree = tree / "wt"
        try:
            add = _run(["git", "worktree", "add", "--detach", str(worktree),
                        base_commit], repo)
            if add.returncode != 0:
                return f"could not create a clean checkout: {add.stderr.strip()[:200]}"

            applied = _run(["git", "apply", "--verbose", str(patch_file)],
                           worktree)
            if applied.returncode != 0:
                return ("the patch does not apply to a clean checkout at "
                        f"{base_commit[:12]}: {applied.stderr.strip()[:300]}")

            # RUN 1 — the tree exactly as the agent left it, its own tests
            # included. This is the agent's claim on its own terms. Nothing is
            # restricted: it may change any file, tests among them.
            log(f"[verify] run 1/2: {scope} with the agent's tests")
            as_left = _run([python, "-m", "pytest", scope, "-q", "--no-header",
                            "-p", "no:cacheprovider"], worktree)

            # RUN 2 — the same source with the PROJECT's tests restored over
            # whatever the patch did to them. This is the reference.
            #
            # Two runs rather than one because a single run cannot tell a fix
            # from a deletion: if the agent may edit tests and we run whatever
            # is in the tree, "green" is cheapest to achieve by gutting the
            # failing test. Restoring instead of forbidding keeps the agent
            # unrestricted while preserving a signal that means something —
            # and when the two disagree, that disagreement is exactly the case
            # a human has to judge, so it is recorded rather than resolved.
            _run(["git", "checkout", "--", scope], worktree)
            for path in test_paths:
                _run(["git", "checkout", "--", path], worktree)
            if test_patch.strip():
                (tree / "tests.diff").write_text(test_patch, encoding="utf-8")
                tp = _run(["git", "apply", str(tree / "tests.diff")], worktree)
                if tp.returncode != 0:
                    return ("could not restore the project's tests onto the "
                            f"patched tree: {tp.stderr.strip()[:200]}")

            log(f"[verify] run 2/2: {scope} with the project's tests")
            result = _run([python, "-m", "pytest", scope, "-q", "--no-header",
                           "-p", "no:cacheprovider"], worktree)
            tail = (result.stdout.strip().splitlines() or [""])[-1]
            failed = [ln for ln in result.stdout.splitlines()
                      if ln.startswith("FAILED")]
            as_left_tail = (as_left.stdout.strip().splitlines() or [""])[-1]
            touched_tests = [t for t in touched
                             if t.startswith(scope) or t in test_paths]

            # One receipt per run, not one receipt overwritten by each run.
            # A fixed filename means the newest run destroys the evidence the
            # previous one produced, which for a harness whose entire output IS
            # the evidence is the wrong default. Named by the instant the
            # verification completed, so they sort chronologically.
            receipts.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (receipts / f"verification-{stamp}.json").write_text(json.dumps({
                "patch_path": rel,
                "base_commit": base_commit,
                "touched_paths": touched,
                "touched_tests": touched_tests,
                "file_notes": payload.get("file_notes") or [],
                "with_agents_tests": {"exit_code": as_left.returncode,
                                      "summary": as_left_tail},
                "with_project_tests": {"exit_code": result.returncode,
                                       "summary": tail,
                                       "failed": failed[:20]},
                "needs_human_review": bool(
                    touched_tests
                    or (as_left.returncode == 0 and result.returncode != 0)),
            }, indent=2), encoding="utf-8")

            if touched_tests:
                log(f"[verify] NOTE: the patch changed {len(touched_tests)} "
                    "test file(s) — recorded for review, not blocked")

            # THE GATE IS THE REFERENCE RUN, deliberately.
            #
            # Gating on the agent's own run instead would make `finished`
            # self-certifying: with tests editable, the cheapest way to green
            # is to delete the failing test. Gating here costs something real —
            # a fix that LEGITIMATELY needs a test change cannot land on the
            # agent's say-so — and that is the trade taken. The refusal tells
            # it why, the disagreement is recorded with needs_human_review, and
            # a person decides whether the test was wrong or merely
            # inconvenient. A machine cannot tell those apart.
            if result.returncode != 0:
                detail = "; ".join(failed[:4]) or tail
                extra = ""
                if as_left.returncode == 0:
                    extra = (" — note your own tests passed, so if you changed "
                             "a test deliberately, say why in file_notes; that "
                             "is a judgement for a human, and it will not land "
                             "on your say-so")
                return (f"on a clean checkout with the project's own tests, the "
                        f"suite failed: {detail}{extra}")
            log(f"[verify] clean-checkout run passed — {tail}")
            return None
        finally:
            _run(["git", "worktree", "remove", "--force", str(worktree)], repo)
            shutil.rmtree(tree, ignore_errors=True)

    return verify
