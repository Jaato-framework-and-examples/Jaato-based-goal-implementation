# What actually failed when you tried to produce the patch?

**What prompted it.** A session took five attempts to write `fix.diff`, saying
"Git config issue", then "The shell is interpreting `..` in a strange way",
before giving up on the instruction and writing the file with Python. It
succeeded, but a third of its turn went on plumbing. From outside its
confinement none of the causes were visible: the daemon log showed no file or
exec denials for the paths involved, and the agent's own prose was chunked in
the trace.

**The ask.** Run these, report verbatim output including exit codes, and *do not
work around a failure — I want the failure*.

**The answer** isolated three independent causes, none of which was the one I
had guessed:

1. **Pipes fail.** `| head` returns `/bin/sh: 1: head: Permission denied`,
   exit 126. Exec authority in the `//child` subprofile is exactly what the
   AppArmor fragment lists, and the fragment listed `sh`, `python3`, `git`.
   Working as designed; the fragment simply did not match how a coding agent
   works.

2. **Git cannot read its own config.**
   `warning: no es posible acceder '/home/apanoia/.gitconfig': Permiso denegado`
   followed by `fatal: error occurred while reading config files`. `~/.gitconfig`
   is outside the workspace, and git fails *entirely* rather than degrading. The
   session found the workaround itself: `GIT_CONFIG_NOSYSTEM=1 HOME=/tmp`.

3. **Redirection is mangled.** `git diff > ../fix2.diff` came back as
   `cd: ../fix2.diff: No such file or directory` — the redirect became an
   argument to `cd`. `> /tmp/nope.diff` was correctly denied as outside the
   workspace, but this one was a parsing problem, not a policy one.

**What it changed.** `fixtures/make_patch.py` — the harness now produces the
diff with a sanitised git environment and no shell at all — and the goal states
the exec limits up front so the agent does not spend turns discovering them.

**Why it needed asking.** I had one hypothesis (git wants HOME) and would have
patched that alone, leaving the pipe failure and the redirect quirk to be
rediscovered later. The session found all three by running them, which is
something only it could do: the failures exist only inside its confinement.
