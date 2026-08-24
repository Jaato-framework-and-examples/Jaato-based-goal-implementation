#!/usr/bin/env python3
"""Fetch the instance and lay down the agent's working copy.

Separate from the run because it needs the network and a git clone, and the
root example needs neither. Run it once; `run.py` afterwards is offline.

What it prepares:

* ``instance.json`` — one row of SWE-bench Verified, fetched from the
  HuggingFace datasets server as plain JSON. The official harness is not used:
  it exists to make 500 heterogeneous repos score identically on everyone's
  machine, and we want one repo with our own instrumentation, so a row of data
  is the whole dependency — no Docker, no 120GB of images.

  That works for instances whose test dependencies are absent or still resolve
  cleanly today. It is NOT true of all 500, and the images exist for the
  instances where it is false — see ``provision_deps`` for the two ways it
  breaks, both measured rather than predicted.
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
DEPS = HERE / ".deps"
DEPS_PROVISIONED = HERE / ".deps-provisioned"
INSTANCE_ID = os.environ.get("SWE_INSTANCE", "sympy__sympy-24539")

# Where extra test dependencies live. `.deps` is what the runs import from;
# `.deps-provisioned` is the pristine copy `--reset` restores it from — the same
# two-copy idea as repo/ and upstream/, for the same reason. An agent that
# installs something mid-run gets it, and loses it at the next reset, because an
# improvised dependency is run state and not part of the environment.
DEPS_FILE = os.environ.get("SWE_DEPS_FILE", "requirements-dev.txt")
ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test"
)


def fetch_instance() -> dict:
    """Pull the one row we need, paging until we find it."""
    target = HERE / "instance.json"
    if target.exists():
        present = json.loads(target.read_text(encoding="utf-8"))
        if present.get("instance_id") != INSTANCE_ID:
            raise SystemExit(
                f"this workspace is set up for {present.get('instance_id')}, "
                f"not {INSTANCE_ID}.\n"
                f"Switching instances needs a clean workspace — the checkouts "
                f"and the provisioned dependencies both belong to the old one:\n"
                f"    python {Path(__file__).name} --fresh")
        print(f"instance.json already present ({INSTANCE_ID})")
        return present

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



def provision_deps(repo_dir: Path) -> None:
    """Install the project's own declared test dependencies into a pristine dir.

    The source of truth is the checkout, not a table we maintain: SWE-bench rows
    carry no dependency list, and a curated per-instance map would be one more
    thing to keep in step with 500 instances. If the project declares its test
    requirements in a file (``requirements-dev.txt`` unless ``SWE_DEPS_FILE``
    says otherwise), that file is the answer; if it declares none, nothing is
    installed and nothing is guessed.

    Installed with ``--target`` rather than into a venv. Venvs do not chain — a
    venv created with ``--system-site-packages`` from another venv sees the base
    interpreter's packages, not the parent venv's — so layering has to happen on
    the import path. This is the same conclusion the telegram client reached with
    its ``host_tools_venv``, which wires site-packages onto ``sys.path`` rather
    than nesting environments.

    KNOWN LIMITS, both hit on ``psf__requests-6028`` and both real:

    1. **Layering adds; it does not replace.** That instance's requirements pin
       their own ``pytest``. The layer shadows the base venv's pytest, but the
       base venv's plugins still autoload against it, and one of them imports a
       module the older pytest does not have::

           anyio/pytest_plugin.py: from _pytest.scope import Scope
           ModuleNotFoundError: No module named '_pytest.scope'

       An isolated venv would fix this. Layering cannot.

    2. **A dated requirements file does not resolve on today's PyPI.** Nothing
       in that 2021 file pins transitives, so pip paired its Jinja2 with a
       modern markupsafe that had deleted the name Jinja2 imports::

           from markupsafe import soft_unicode
           ImportError: cannot import name 'soft_unicode'

       Isolation does NOT fix this one — it is resolution, not layering. Pinning
       the era is what the official Docker images do, and it is why they exist.

    So this handles the additive case honestly and stops there. sympy's 75
    instances need nothing at all; an instance that needs to pin its own test
    stack is out of scope for a harness this small, and saying so is better than
    a curated per-instance pin table that rots.
    """
    requirements = repo_dir / DEPS_FILE
    if not requirements.is_file():
        print(f"no {DEPS_FILE} in the checkout — no extra dependencies needed")
        return
    if DEPS_PROVISIONED.is_dir():
        print(f"{DEPS_PROVISIONED.name}/ already provisioned")
        return
    print(f"installing {DEPS_FILE} into {DEPS_PROVISIONED.name}/ …")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--target", str(DEPS_PROVISIONED), "-r", str(requirements)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"provisioning failed:\n{result.stderr.strip()[:1500]}")
    installed = sorted(d.name.split("-")[0] for d in DEPS_PROVISIONED.glob("*.dist-info"))
    print(f"  provisioned: {', '.join(installed) or '(nothing)'}")


def fresh() -> None:
    """Remove everything tied to one instance, so another can be set up.

    Deliberately not part of `reset()`. Reset returns a workspace to the state
    setup left it in; this discards that state entirely, including two git
    checkouts that cost a network fetch to rebuild.
    """
    for path in (HERE / "repo", HERE / "upstream", DEPS, DEPS_PROVISIONED):
        if path.is_dir():
            shutil.rmtree(path)
            print(f"  removed {path.name}/")
    for name in ("instance.json", "test_patch.diff"):
        target = HERE / name
        if target.exists():
            target.unlink()
            print(f"  removed {name}")


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

    # Dependencies go back to what was provisioned. Anything the agent
    # installed itself during a run is discarded here — that is the point of
    # keeping a pristine copy: an ad-hoc install must not silently become part
    # of the next run's environment, or two runs stop being comparable.
    if DEPS_PROVISIONED.is_dir():
        if DEPS.is_dir():
            shutil.rmtree(DEPS)
        shutil.copytree(DEPS_PROVISIONED, DEPS)
        removed.append(f"{DEPS.name}/ restored from {DEPS_PROVISIONED.name}/")
    elif DEPS.is_dir():
        shutil.rmtree(DEPS)
        removed.append(f"{DEPS.name}/ (nothing was provisioned to restore)")

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

    # The project's own test dependencies, read out of the checkout it just made.
    provision_deps(agent_repo)
    reset()

    print("\nready. the bug is present and its test fails:")
    print(f"  cd {agent_repo} && python -m pytest "
          f"{json.loads(row['FAIL_TO_PASS'])[0]} -q")
    print("\nthen:  python examples/swe-bench/run.py")
    return 0


if __name__ == "__main__":
    if "--fresh" in sys.argv:
        print(f"discarding the workspace's current instance:")
        fresh()
        print(f"now run: SWE_INSTANCE=<id> python {Path(__file__).name}")
        sys.exit(0)
    if "--reset" in sys.argv:
        for line in reset() or ["nothing to reset"]:
            print(f"  reset: {line}")
        sys.exit(0)
    sys.exit(main())
