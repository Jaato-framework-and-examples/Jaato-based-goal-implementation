#!/usr/bin/env python3
"""Drive the cascade at a real bug, with the driver adjudicating the fix.

Same `GoalCascade` as the root example, unchanged. What differs is one
callback: the driver is handed a `verify_finished` that re-runs the agent's
patch on a checkout the agent never touched. That is the whole delta, and it is
the thing worth reading — the trust story changes completely without the driver
learning anything about sympy, pytest or patches.

Run `setup.py` first (network + git); this is offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from jaato_sdk import ClientType, IPCRecoveryClient

from goal_cascade.driver import GoalCascade

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from verify_patch import make_verifier            # noqa: E402
from setup import reset                          # noqa: E402


class ConfinedCascade(GoalCascade):
    """A cascade whose runner is kernel-confined to its workspace.

    Subclasses only to pass `config_root` and `apparmor`. Both are ordinary
    session bootstrap config that the root example's CLI has no reason to
    expose; confinement is what makes the driver's tree and its verification
    record unreachable by kernel policy rather than by convention.
    """

    def __init__(self, *args, config_root: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._config_root = config_root

    def _new_client(self) -> IPCRecoveryClient:
        return IPCRecoveryClient(
            self.socket,
            client_type=ClientType.API,
            auto_start=True,
            env_file=self.env_file,
            workspace_path=self.workspace,
            config_root=self._config_root,
            apparmor=True,
            on_status_change=lambda s: self.log(
                f"[connection] {getattr(s, 'state', s)}"
            ),
        )


GOAL = """\
A bug has been reported against the sympy checkout in ./repo — your own working
copy. Edit it freely.

--- issue as filed ---
{issue}
--- end issue ---

Find the cause and fix it.

Then RUN THE PROJECT'S TEST SUITE and get it green before you claim anything:
`python fixtures/run_tests.py start`, then `python fixtures/run_tests.py status`.
It runs the whole package, not just the test from the issue, because a change
that repairs one test and breaks another is not a fix — and you cannot know
which you have made without looking. Reporting `finished` on an untested patch
is claiming something you have not checked.

The suite takes about two minutes, so do NOT wait for it inside a turn:
suspend and come back, as your persona says.

When you are satisfied: run `python fixtures/make_patch.py` to write fix.diff
(it handles git's configuration and the file writing for you — producing the
diff is plumbing, not part of your task), write REPORT.md explaining the bug
and your fix, and report finished with patch_path='fix.diff'.

A note on this environment, so you do not waste turns discovering it: you are
confined, and the only binaries you can execute are `sh`, `python3` and `git`.
Pipes to things like `head` or `grep` will fail with "Permission denied" — use
python, or the file-reading tools, instead of shell text utilities.

You may change ANY file, tests included — nothing is off-limits, and finding
the bug is your job, not something this harness pre-empts. But account for
every file you touch in `file_notes`, one entry per path, saying what you
changed and why.

How your work is judged: the driver applies your patch to a CLEAN checkout and
runs the suite twice — once with your tests as you left them, once with the
project's own test files restored. The SECOND run decides. So if you change a
test, expect the verdict to come from the project's version of it: explain
yourself in `file_notes` and a human will judge whether that test was wrong.
Weakening a test is not a route to `finished`.

If it fails you will be woken with the reason and can try again.
"""


def _scope() -> str:
    """The test package to verify, as setup.py derived it from the instance.

    Read rather than recomputed. The agent's fixture needs the same answer and
    cannot derive it — it runs confined, against a checkout, with no instance
    row — so there is one computation, in setup.py, and one file.
    """
    return (HERE / "scope.txt").read_text(encoding="utf-8").strip()


def main() -> int:
    instance_file = HERE / "instance.json"
    if not instance_file.exists():
        print("run setup.py first — it fetches the instance and the checkouts")
        return 2
    inst = json.loads(instance_file.read_text(encoding="utf-8"))

    # Every run starts from the state setup.py left behind. Two runs are only
    # comparable if they began in the same place, and this example exists to be
    # compared — the evidence that a change to the profile or the persona did
    # anything is a diff between runs. Loud rather than silent: deleting things
    # in someone's workspace is not something to do quietly.
    for line in reset():
        print(f"[reset] {line}")

    test_file = next(
        (line[6:].strip() for line in inst["test_patch"].splitlines()
         if line.startswith("+++ b/")), None)

    verify = make_verifier(
        repo=HERE / "upstream",           # the driver's tree; the agent has ./repo
        base_commit=inst["base_commit"],
        test_paths=[test_file] if test_file else [],
        test_patch=inst["test_patch"],
        # The PACKAGE, not just the file carrying the target test. A fix that
        # repairs one test and breaks four others is not a fix, and verifying
        # only the target file cannot see that — the regression net would have
        # a hole exactly where regressions happen.
        scope=os.environ.get("SWE_VERIFY_SCOPE", _scope()),
        python=sys.executable,
        workspace=HERE,
        # Inside the example. The receipt is this example's output, and a
        # second example writing receipts would otherwise land in the same
        # directory at the repo root.
        receipts=Path(os.environ.get("SWE_RECEIPTS", HERE / "receipts")),
        # The benchmark's own expectations. Gating on "the whole package is
        # green" refuses a correct patch whenever the scope carries failures
        # that predate it — sympy__sympy-24562 has four, from a 2023 sympy
        # meeting a 2026 numpy, none of them in PASS_TO_PASS.
        fail_to_pass=json.loads(inst["FAIL_TO_PASS"]),
        pass_to_pass=json.loads(inst["PASS_TO_PASS"]),
    )

    cascade = ConfinedCascade(
        GOAL.format(issue=inst["problem_statement"].strip()[:1500]),
        workspace=str(HERE),
        env_file=str(HERE / ".env"),
        config_root=str(HERE / ".jaato"),
        # A turn here reads an unfamiliar codebase and edits it; the root
        # example's 300s default would cut that off mid-work.
        turn_timeout=1200.0,
        budget={"turns": 30, "usd": 2.5, "seconds": 3600},
        degrade=[{"at": 95.0, "action": "finalize"},
                 {"at": 100.0, "action": "abort"}],
        verify_finished=verify,
    )
    return asyncio.run(cascade.run())


if __name__ == "__main__":
    sys.exit(main())
