# Asking a finished session what it did

The driver resumes a session to push a goal forward. This does the opposite: it
wakes a session whose goal is already `finished` and asks it a question,
capturing the answer as prose rather than as a completion payload.

It exists because two of the more useful findings in this repo could not have
been reached any other way.

```bash
python tools/interrogate/interrogate.py \
    <session_id> <workspace> <env_file> <question-file> [config_root]
```

Session ids are the filenames under `<workspace>/.jaato/sessions/`.

## Why ask rather than reason

A session knows things about its own run that are not recoverable from outside
it — what it was looking at when it decided something, and what an environment
does when *it* is the one inside the confinement.

Both worked examples in [`transcripts/`](transcripts/) turn on that:

- [**A fabricated root cause**](transcripts/01-fabricated-root-cause.md). A
  correct fix, and a report quoting code that had never been in the file. My
  hypothesis was context loss across the suspend, which would have pointed at
  replaying more state. The session's answer — *"I did not re-read the original
  file… I reconstructed what I believed it looked like"* — pointed at one
  sentence of persona instead, which fixed it.
- [**Confined shell friction**](transcripts/02-confined-shell-friction.md). Five
  attempts to write a patch file. I had one hypothesis; the session ran the
  commands and came back with three independent causes, including one — `| head`
  failing with exit 126 — that is invisible outside the confinement because
  nothing logs it as a denial.

- [**Confinement is not a sandbox**](transcripts/03-confinement-is-not-a-sandbox.md).
  An AppArmor fragment claimed it allowed "no package managers". The session
  inside it ran `pip install --target` successfully, imported the result, and
  reached PyPI over urllib — while `curl` stayed blocked, which is the control
  that proves the mechanism works and only the description was wrong. Nothing
  outside could have shown this: a *successful* install logs no denial.

The pattern in all three: I had *a* hypothesis, it was partially right, and
acting on it alone would have left the rest to be rediscovered later.

## Asking well

The [question template](question-template.md) encodes what worked. Three things
matter more than they look:

**Say whether anything is wrong.** A session that believes it is in trouble
writes apologies instead of accounts. Both useful answers here began by being
told the work was accepted and only the *report* was in question.

**Give evidence, not your reading of it.** Quote the log line, the file, the
diff. A session handed an interpretation tends to agree with it; one handed
evidence goes and checks. The second transcript is entirely the result of
saying "run these and report verbatim output **including failures** — do not
work around a failure, I want the failure", because an agent's instinct is to
route around a broken thing and report success.

**Ask what would have worked.** Phrased as *"as you would want to receive it"*.
The session is better placed than you are to say what instruction would have
saved it four attempts.

## How the turn ends

`session.wake` revives a session under **its own** profile — the contract
belongs to the session, and an interrogator cannot impose a different one. A
session created under a goal profile will therefore answer your question and
then argue with its own completion processor about a patch the question never
involved.

Two ways round it:

- **Ask it to end `suspended`.** It is not finishing a goal, only pausing again,
  so this is also the honest outcome. Works with any two-branch profile and
  needs no configuration. The template does this.
- **Create the session under [`profiles/interrogator.yaml`](profiles/interrogator.yaml)**,
  whose contract asks for an answer and nothing else, and whose persona is
  [`agents/interrogator.md`](agents/interrogator.md). Both are resolved through
  `config_root` — the optional fifth argument — which replaces the workspace
  tier of the config lookup, so the daemon reads them from this directory
  instead of `<workspace>/.jaato/`. That route needs no closing instruction
  about how to end the turn, and no reminder to report failures verbatim: the
  persona carries both. Only available if you decided before the session
  existed that you would want to question it — which, for anything you are
  evaluating rather than merely running, you probably will.

## What it is not

Not a test, and not a verifier. A session's account of itself is evidence about
what it *believes* it did, which is exactly why the first transcript is
interesting — the account was accurate about a report that was not. Treat the
answers as testimony: useful, checkable, and not self-certifying. Where the
answer names something checkable, go and check it. Both transcripts above were
confirmed against the file and the kernel log before being acted on.
