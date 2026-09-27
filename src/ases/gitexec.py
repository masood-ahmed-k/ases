"""The ONE way `src/ases` runs git against a repository it does not fully control (round 9, package GITHARDEN).

ASES-CFG-04 (p212): "Hermes provider credentials must never be exposed to worker terminals ... if any provider key
is visible, move it into Hermes credential storage or behind the approved egress mechanism before running
unattended workers." p213: "Never export provider keys in the shell that launches the gateway or the controller."
ASES-SEC-01 (p375): "Run the secret scanner in Gate 1 and Gate 3, and scan card bodies and plan files before they
are written." ASES-SEC-04 (p388): "The untrusted input is what agents read: repository content, dependency
documentation, web pages, issue text, tool output. Instructions found there are data."

What the round 8 sweep proved (empirically, against unmodified ASES helpers, in throwaway repos): a worker on the
local backend can run `git config` in its linked worktree, which writes the SHARED `.git/config`
(`extensions.worktreeConfig` is off by default) and can write the shared `.git/hooks`. Git then runs
worker-chosen commands inside the controller's own git calls, with the controller's full environment: a planted
`post-checkout` hook fires on `worktree add`/`checkout` (commit, merge and revert also fire `pre-commit`,
`commit-msg`, `post-commit`, `reference-transaction`, `pre-auto-gc`); a planted `core.fsmonitor` script fires on
every `status` with nothing in git's own output to show it; and a worker-committed `.gitattributes` naming a
textconv/external-diff driver defined in the shared config can make a diff the controller reads (a secret scan, a
retry card body) show sanitised text while the real blob carries a secret, or embed a worker's own program output
into text that reaches the next worker's card (a prompt-injection path, ASES-SEC-04).

What this module does about it:
  - `GIT`: the argv prefix every controller git call starts with. `-c core.hooksPath=<empty dir>` points git at an
    empty directory no repository controls, created once per process under the OS temp root, so a hook planted in
    the repository's own (shared) `.git/hooks` never runs, whatever subcommand would have fired it. `-c
    core.fsmonitor=false` stops a planted fsmonitor hook/script from running on `status` (and everything else that
    consults the untracked-file cache); `--no-optional-locks` on read-only calls already keeps `status` from
    fighting a real git operation, but does nothing about fsmonitor, hence this flag on every call rather than
    only the read-only ones. `core.untrackedCache` is left alone: nothing here showed it mattering once
    fsmonitor is off, and adding a flag "just in case" is what this package is explicitly told not to do.
  - `git_env()`: `procenv.scrubbed_environ()` (ASES-CFG-04/05: no credential-shaped variable reaches a git child,
    or anything a hook or a filter/merge driver it runs can read) plus `GIT_TERMINAL_PROMPT=0`, so a controller
    git call can never sit waiting on a credential prompt with no one to answer it. `GIT_CONFIG_NOSYSTEM` is
    deliberately NOT set: the system-wide gitconfig is not writable by a worker (out of the threat model this
    package addresses -- see "what this module does NOT do" below), and on Windows it is where Git for Windows
    keeps `core.autocrlf` and similar settings whose loss would silently change every checkout gitexec makes.
  - `DIFF_SAFETY`: `--no-ext-diff --no-textconv`, appended by every call that reads diff or patch TEXT (`diff`,
    `log -p`, `show`, `format-patch`) so a worker-configured external-diff or textconv driver cannot rewrite what
    the controller reads, whether that text goes to the Gate 3 secret scanner (ASES-SEC-01, must see the real
    content) or into a card body (ASES-SEC-04, must be git's own text, not a worker's program's output). Applied
    to every literal `diff` invocation in the modules this package touches, including a `--name-only`/`--quiet`
    one that shows no content today: the flags are no-ops there, and a call that only lists names is one `-p` or
    `--stat` away from showing content, so the safety is on before that day arrives rather than after.

What this module does NOT do, on purpose (see the package's work order for the full argument): it does not stop a
malicious worker who already runs as the operator's own OS user on the local backend from reading Hermes's
credential files directly, editing `~/.gitconfig`, or shadowing `git` on PATH -- those are not git subprocess
calls this module can wrap. The blueprint's own answer to that is organizational, not technical, at this phase:
"Before Phase 5, ASES runs only on trusted test repositories" (p385). It also cannot stop a filter driver
(`filter.<name>.clean/smudge/process`) or a merge driver (`merge.<name>.driver`) from running at all: their names
are attacker-chosen, so no single `-c` pre-empts them by name. `git_env()`'s scrub is what caps the damage there:
the driver still runs, but sees no credential-shaped variable. Under the Docker sandbox (Phase 5) a worker cannot
write the shared `.git` at all, which is the real fix for the OS-user case; this module is defense in depth until
then, on any backend.

Free of ASES imports except `procenv` (itself free of ASES imports), so any module that starts a git subprocess
can import this one without adding a cycle. EXCLUDED on purpose: `src/ases/gates.py` (round 8 already scrubs its
environment; package GATESANDBOX is rewriting its checkout on its own branch, and the architect switches it to
`gitexec` at merge) and `src/ases/fakes/` (test fakes stand in for a real Hermes or a real worker; they are not
the controller, and nothing hardens a test double against itself).
"""
from __future__ import annotations

import atexit
import pathlib
import shutil
import tempfile

from . import procenv

# Created once per process (import is cached in sys.modules, so this runs exactly once per interpreter) and never
# written to by anything in this codebase: an empty directory is exactly as good a hooksPath as a full one, since
# the whole point is that git finds no hook there. Forward slashes even on Windows: unambiguous as a `-c
# core.hooksPath=<value>` config value, where a literal backslash could otherwise be read as the start of an
# escape sequence, and Windows git accepts forward-slash paths everywhere a backslash one works.
_HOOKS_DIR = pathlib.Path(tempfile.mkdtemp(prefix="ases-git-hooks-"))
atexit.register(shutil.rmtree, _HOOKS_DIR, ignore_errors=True)

GIT: tuple[str, ...] = ("git", "-c", f"core.hooksPath={_HOOKS_DIR.as_posix()}", "-c", "core.fsmonitor=false")

# Appended by every call whose output is diff or patch TEXT (diff, log -p, show, format-patch): see the module
# docstring for why, and why it is also added to a --name-only/--quiet diff call that shows no content today.
DIFF_SAFETY: tuple[str, ...] = ("--no-ext-diff", "--no-textconv")


def git_env() -> dict[str, str]:
    """The environment every controller git subprocess starts with: procenv.scrubbed_environ() (no credential-
    shaped variable reaches git, a hook it runs, or a filter/merge driver it runs) plus GIT_TERMINAL_PROMPT=0, so
    a controller git call never blocks on a prompt with no one there to answer it. See the module docstring for
    why GIT_CONFIG_NOSYSTEM is deliberately not set here."""
    env = procenv.scrubbed_environ()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
