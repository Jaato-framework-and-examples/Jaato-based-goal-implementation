#!/usr/bin/env python3
"""Produce the unified diff of the agent's fix, without needing a shell.

The obvious instruction — `cd repo && git diff > ../fix.diff` — does not work
in a confined workspace, and the failures are not obvious from outside it. A
finished session, asked to run the command and report verbatim, found three
separate causes:

* **git cannot read `~/.gitconfig`.** It lives outside the workspace, so the
  read is denied and git fails *entirely* rather than degrading:
  "fatal: error occurred while reading config files". `GIT_CONFIG_NOSYSTEM=1`
  plus a HOME inside the workspace fixes it.
* **shell redirection is fragile through the tool layer.** `> ../fix.diff` came
  back as `cd: ../fix.diff: No such file or directory` — the redirect was
  mangled into an argument.
* **pipes need binaries nobody declared.** `| head` fails with
  "head: Permission denied" (exit 126), because exec authority in the //child
  subprofile is exactly what the AppArmor fragment lists, and it lists `sh`,
  `python3` and `git`.

None of those are the agent's problem to solve, and watching it improvise
around them — five attempts, ending in a hand-rolled Python one-liner — is
watching an example teach the wrong lesson. Producing a diff is plumbing. The
harness owns plumbing; the agent owns the bug.

Writes ``fix.diff`` in the workspace and prints what it did.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
WORKSPACE = HERE.parent
REPO = Path(os.environ.get("SWE_REPO", WORKSPACE / "repo"))
OUT = WORKSPACE / os.environ.get("SWE_PATCH_NAME", "fix.diff")


def main() -> int:
    if not (REPO / ".git").exists():
        print(f"no git checkout at {REPO} — run setup.py first", file=sys.stderr)
        return 2

    env = os.environ.copy()
    # Keep git away from configuration it cannot read. HOME points inside the
    # workspace so any file git decides to create lands somewhere writable.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["HOME"] = str(WORKSPACE)

    result = subprocess.run(
        ["git", "diff"], cwd=str(REPO), env=env,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"git diff failed ({result.returncode}): "
              f"{result.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    if not result.stdout.strip():
        print("no changes in the checkout — nothing to submit", file=sys.stderr)
        return 1

    OUT.write_text(result.stdout, encoding="utf-8")
    touched = sorted({
        line[6:].strip() for line in result.stdout.splitlines()
        if line.startswith("+++ b/")
    })
    print(f"wrote {OUT.name} ({len(result.stdout.splitlines())} lines)")
    print("files in the patch:")
    for path in touched:
        print(f"  {path}")
    print("\nremember: file_notes needs one entry per path above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
