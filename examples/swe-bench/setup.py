#!/usr/bin/env python3
"""Fetch the instance and lay down the agent's working copy.

Separate from the run because it needs the network and a git clone, and the
root example needs neither. Run it once; `run.py` afterwards is offline.

What it prepares:

* ``instance.json`` — one row of SWE-bench Verified, fetched from the
  HuggingFace datasets server as plain JSON. The official harness is not used
  and not needed: it exists to make 500 heterogeneous repos score identically
  on everyone's machine, which is a benchmarking concern, not ours. We want one
  repo and our own instrumentation, so a row of data is the whole dependency —
  no Docker, no 120GB of images.
* ``repo/`` — a shallow checkout of sympy at the instance's ``base_commit``,
  with the project's failing test applied, INSIDE the workspace. This is the
  agent's own working copy and it may do what it likes with it.
* ``upstream/`` — a second, pristine checkout the agent never touches. The
  driver verifies against this one.

Two copies is the point, not waste. Verification that runs in the tree the
agent edits is not verification.

``--reset`` returns the workspace to the state this script leaves it in,
without re-fetching anything. ``run.py`` calls it before every run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
INSTANCE_ID = os.environ.get("SWE_INSTANCE", "sympy__sympy-24539")
ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test"
)


def fetch_instance() -> dict:
    """Pull the one row we need, paging until we find it."""
    target = HERE / "instance.json"
    if target.exists():
        print(f"instance.json already present ({INSTANCE_ID})")
        return json.loads(target.read_text(encoding="utf-8"))

    for offset in range(0, 500, 100):
        url = f"{ROWS_URL}&offset={offset}&length=100"
        with urllib.request.urlopen(url, timeout=90) as response:
            rows = json.load(response).get("rows", [])
        for entry in rows:
            row = entry["row"]
            if row.get("instance_id") == INSTANCE_ID:
                target.write_text(json.dumps(row, indent=2), encoding="utf-8")
                print(f"fetched {INSTANCE_ID} ({row['repo']} @ "
                      f"{row['base_commit'][:12]})")
                return row
    raise SystemExit(f"{INSTANCE_ID} not found in SWE-bench Verified")


def checkout(dest: Path, repo_url: str, commit: str) -> None:
    """Shallow-fetch one commit. ~27MB, not the full sympy history."""
    if (dest / ".git").exists():
        print(f"{dest.name}/ already checked out")
        return
    dest.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=str(dest), check=True,
                                    capture_output=True)
    run("git", "init", "-q")
    run("git", "remote", "add", "origin", repo_url)
    print(f"fetching {repo_url} @ {commit[:12]} into {dest.name}/ …")
    run("git", "fetch", "-q", "--depth", "1", "origin", commit)
    run("git", "checkout", "-q", "FETCH_HEAD")


def _git_env() -> dict:
    """git, insulated from configuration a confined session cannot read.

    Identity is passed explicitly rather than read from global config: the
    agent's session is confined and cannot reach ``~/.gitconfig`` — git does not
    degrade when it cannot read a config file, it fails outright.
    """
    return {**os.environ,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "swe-bench setup",
            "GIT_AUTHOR_EMAIL": "setup@example.invalid",
            "GIT_COMMITTER_NAME": "swe-bench setup",
            "GIT_COMMITTER_EMAIL": "setup@example.invalid"}


def reset() -> list:
    """Return the workspace to exactly the state `setup.py` leaves it in.

    Called by `run.py` before every run, and available as `setup.py --reset`.
    Runs are only comparable if they start from the same place: the evidence
    that the persona and profile changes in this repo did or did not work is a
    diff between runs, and a workspace carrying the last run's leftovers
    silently invalidates that comparison.

    It also removes a class of confusion that cost a real run a tool call. An
    agent that lists its workspace and finds a `test_bug_reproduction.py` it
    did not write — left by a previous run that died mid-turn — will try to
    create it and be told the file already exists.

    Deliberately narrow. It restores the git checkout and deletes the artifacts
    THIS example creates, named individually. It does not sweep the workspace
    for anything that looks generated, because a reader's own notes are not
    ours to delete. `repo/` is restored with `git reset --hard` + `git clean`
    rather than a fresh checkout: the base commit and the project's failing
    test are already committed there, so HEAD *is* the intended start state.

    Returns what it removed, for the caller to print. Nothing here is silent —
    automatic deletion in a workspace has to be visible to be honest.
    """
    removed = []

    agent_repo = HERE / "repo"
    if (agent_repo / ".git").exists():
        env = _git_env()
        for argv in (["git", "reset", "--hard", "-q", "HEAD"],
                     ["git", "clean", "-qfd"]):
            subprocess.run(argv, cwd=str(agent_repo), env=env,
                           capture_output=True)
        removed.append("repo/ restored to the base commit + the filed test")

    for name in ("REPORT.md", "fix.diff"):
        target = HERE / name
        if target.exists():
            target.unlink()
            removed.append(name)

    # The agent's own scratch files. These patterns are the ones .gitignore
    # already claims for this directory — kept in step deliberately, since a
    # file worth ignoring is a file worth resetting.
    for pattern in ("test_*.py", "*.py.bak"):
        for stray in sorted(HERE.glob(pattern)):
            stray.unlink()
            removed.append(stray.name)

    state = HERE / ".goal-cascade-state"
    if state.is_dir():
        shutil.rmtree(state)
        removed.append(".goal-cascade-state/")

    # The fixture owns its own state files; ask it rather than encoding their
    # names here a second time.
    fixture = HERE / "fixtures" / "run_tests.py"
    if fixture.exists():
        subprocess.run([sys.executable, str(fixture), "reset"],
                       capture_output=True)
        removed.append("fixture test state")

    return removed

def main() -> int:
    row = fetch_instance()
    url = f"https://github.com/{row['repo']}.git"

    # The agent's copy, carrying the project's failing test.
    agent_repo = HERE / "repo"
    checkout(agent_repo, url, row["base_commit"])
    patch = HERE / "test_patch.diff"
    patch.write_text(row["test_patch"], encoding="utf-8")
    applied = subprocess.run(["git", "apply", str(patch)],
                             cwd=str(agent_repo), capture_output=True)
    if applied.returncode != 0 and b"already exists" not in applied.stderr:
        print(f"note: test patch not applied ({applied.stderr.decode()[:120]})")
    else:
        # COMMIT it, so the agent's `git diff` shows only the agent's work.
        # Left uncommitted, the project's failing test appears in the patch the
        # agent submits — it would have to account for a change it never made,
        # and the verifier would flag a test edit for human review that nobody
        # performed. Identity is passed explicitly rather than read from global
        # config, which a confined session cannot reach anyway.
        env = _git_env()
        subprocess.run(["git", "add", "-A"], cwd=str(agent_repo),
                       env=env, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m",
                        "the project's failing test, as filed"],
                       cwd=str(agent_repo), env=env, capture_output=True)

    # The driver's copy, pristine — never edited by anyone.
    checkout(HERE / "upstream", url, row["base_commit"])

    print("\nready. the bug is present and its test fails:")
    print(f"  cd {agent_repo} && python -m pytest "
          f"{json.loads(row['FAIL_TO_PASS'])[0]} -q")
    print("\nthen:  python examples/swe-bench/run.py")
    return 0


if __name__ == "__main__":
    if "--reset" in sys.argv:
        for line in reset() or ["nothing to reset"]:
            print(f"  reset: {line}")
        sys.exit(0)
    sys.exit(main())
