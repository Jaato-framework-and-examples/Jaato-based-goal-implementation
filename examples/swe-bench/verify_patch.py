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
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _run(args: List[str], cwd: Path, timeout: int = 900,
         env: Optional[Dict[str, str]] = None):
    return subprocess.run(args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, env=env)



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



def _failed_names(stdout: str) -> set:
    """Every test the run reported as FAILED, by full id AND by bare name.

    Both forms are kept because the benchmark names tests inconsistently across
    repos: sympy's lists carry bare function names (``test_issue_24543``) while
    requests' carry parametrised ids
    (``tests/test_utils.py::test_x[a-b]``). Matching either form avoids a table
    of per-repo naming rules.
    """
    names = set()
    for line in stdout.splitlines():
        if not line.startswith("FAILED"):
            continue
        ident = line.split(None, 1)[1].split(" - ")[0].strip() if " " in line else ""
        if ident:
            names.add(ident)
            names.add(ident.split("::")[-1])
    return names


def _contract_verdict(stdout: str, fail_to_pass: List[str],
                      pass_to_pass: List[str]) -> tuple:
    """Judge the run against the benchmark's own contract.

    Returns ``(unfixed, regressed)`` — the FAIL_TO_PASS tests that did not flip,
    and the PASS_TO_PASS tests that stopped passing.

    This replaced a gate that required the whole package to be green, which
    sounded stricter and was simply wrong: on ``sympy__sympy-24562`` four tests
    in the scope fail before anything is touched, because a 2023 sympy meets a
    2026 numpy. That gate would refuse a perfect fix for a reason the agent
    could neither cause nor cure.

    The instance row already carries the answer and this harness ignored it.
    None of those four appear in PASS_TO_PASS — the benchmark curated them out.
    So the honest question is not "is everything green" but "did the bug's tests
    start passing, and did anything that used to pass stop".
    """
    failed = _failed_names(stdout)
    return ([t for t in fail_to_pass if t in failed],
            [t for t in pass_to_pass if t in failed])


def make_verifier(*, repo: Path, base_commit: str, test_paths: List[str],
                  test_patch: str, scope: str, python: str,
                  workspace: Path, receipts: Path,
                  fail_to_pass: Optional[List[str]] = None,
                  pass_to_pass: Optional[List[str]] = None, log=print):
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
            deps_env = _env_with_deps(workspace)
            as_left = _run([python, "-m", "pytest", scope, "-q", "--no-header",
                            "-p", "no:cacheprovider"], worktree, env=deps_env)

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
                           "-p", "no:cacheprovider"], worktree, env=deps_env)
            tail = (result.stdout.strip().splitlines() or [""])[-1]
            failed = [ln for ln in result.stdout.splitlines()
                      if ln.startswith("FAILED")]
            as_left_tail = (as_left.stdout.strip().splitlines() or [""])[-1]
            unfixed, regressed = _contract_verdict(
                result.stdout, fail_to_pass or [], pass_to_pass or [])
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
                # The benchmark's contract, which is the gate. `failed` above
                # can be non-empty on a good patch when the scope has failures
                # that predate it; these two cannot.
                "contract": {"unfixed": unfixed, "regressed": regressed,
                             "fail_to_pass": len(fail_to_pass or []),
                             "pass_to_pass": len(pass_to_pass or [])},
                "needs_human_review": bool(
                    touched_tests
                    or (as_left.returncode == 0 and (unfixed or regressed))),
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
            if unfixed or regressed:
                detail = ""
                if unfixed:
                    detail += (f"these were supposed to pass after your fix and "
                               f"did not: {', '.join(unfixed[:4])}. ")
                if regressed:
                    detail += (f"these passed before your change and now fail: "
                               f"{', '.join(regressed[:4])}. ")
                extra = ""
                if as_left.returncode == 0:
                    extra = (" — note your own tests passed, so if you changed "
                             "a test deliberately, say why in file_notes; that "
                             "is a judgement for a human, and it will not land "
                             "on your say-so")
                return (f"on a clean checkout with the project's own tests, "
                        f"{detail.strip()}{extra}")
            log(f"[verify] clean-checkout run passed — {tail}")
            return None
        finally:
            _run(["git", "worktree", "remove", "--force", str(worktree)], repo)
            shutil.rmtree(tree, ignore_errors=True)

    return verify
