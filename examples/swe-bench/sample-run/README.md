# One real run, kept

Everything here was produced by a single unedited run of this example against a
live daemon — the agent driven by `anthropic/claude-haiku-4.5`, fixing
`sympy__sympy-24539`. It is committed because the example is otherwise only
readable by people who have a daemon, an API key and twenty minutes.

| file | what it is |
|---|---|
| [`transcript.md`](transcript.md) | every turn: prose, each tool call with arguments, each result |
| [`REPORT.md`](REPORT.md) | what the agent wrote for a human to read |
| [`verification.json`](verification.json) | what the *driver* concluded, independently |

Regenerate the transcript for any run of your own with
`python tools/dump_turns.py <config_root>/sessions/<id>.json`. Absolute paths
are rewritten to `<workspace>` on the way out.

## What to notice

**The agent looks before it assumes.** The run opens `get_environment`,
`glob_files`, `glob_files` — the clock, then the layout, then the layout again
before writing paths into a directory it had not yet read from. Four earlier
runs did not have `glob_files` offered eagerly and every one of them wasted a
call on `cd repo && python3 fixtures/run_tests.py`, guessing that the fixture
lived inside the checkout rather than beside it. Adding persona prose about it
changed nothing three times; preloading the tool fixed it on the first run.
The lesson is in `.jaato/profiles/goal-actor.yaml`, not in the persona.

**It suspends twice, and waits for real.** The test suite takes ~110 seconds.
The agent starts it, ends the turn `suspended` with a `resume_at`, and the
driver sleeps and wakes it. Nothing blocks inside a turn.

**The report's "before" snippet is quoted, not remembered.** It matches
`upstream/sympy/polys/rings.py` at the base commit character for character.
That is not a given: the run in
[`tools/interrogate/transcripts/01-fabricated-root-cause.md`](../../../tools/interrogate/transcripts/01-fabricated-root-cause.md)
produced a correct fix and described it with code that had never been in the
file, reconstructed from memory across a suspend. The persona section *Writing
a report* exists because of that run, and this transcript is the check that it
still holds.

**The receipt disagrees with nothing here — but it is the thing that counts.**
`verification.json` is the driver's own finding, produced by applying the
agent's patch to a pristine checkout and running the suite twice: once with the
agent's tests as it left them, once with the project's tests restored. The
agent's `progress_note` is testimony; the receipt is evidence. When they
conflict, the receipt wins, and `needs_human_review` goes true.

## What this run does not show

A failure. It exits 0, both verification runs pass, and `touched_tests` is
empty. Runs that go wrong are more instructive and are not preserved here —
the ceiling case (`EXIT=2` when the turn budget refuses a resume) and the
refusal case (a patch the verifier will not accept, which suspends the agent
and sends it back to work) both have tests in `tests/` instead, because those
run without a daemon.
