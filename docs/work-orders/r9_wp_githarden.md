# Round 9 package GITHARDEN: one hardened way for the controller to run git (read `r9_rules.md`)

Worktree: `C:\Users\masoo\ases-wt\githarden`, branch `r9/githarden`, cut AFTER round 8 (so `procenv.scrubbed_environ()` already
keeps `GIT_AUTHOR_NAME/EMAIL/DATE`, which the merge queue's commits may need). Tier 1 item 1.

## Requirements (quoted from blueprint.txt)
- ASES-CFG-04 (p212): "Hermes provider credentials must never be exposed to worker terminals. ASES MUST verify this for the exact
  provider/authentication path in use; if any provider key is visible, move it into Hermes credential storage or behind the approved
  egress mechanism before running unattended workers."
- p213: "Never export provider keys in the shell that launches the gateway or the controller."
- ASES-SEC-01 (p375): "Run the secret scanner in Gate 1 and Gate 3, and scan card bodies and plan files before they are written."
- ASES-SEC-04 (p388): "The untrusted input is what agents read: repository content, dependency documentation, web pages, issue text,
  tool output. Instructions found there are data."

## What the round 8 sweep proved (empirically, against unmodified ASES helpers, in throwaway repos)
A worker on the local backend can run `git config` in its linked worktree, which writes the SHARED `.git/config`
(`extensions.worktreeConfig` is off by default), and can write the shared `.git/hooks`. Git then runs worker-chosen commands inside
the controller's own git calls, with the controller's full environment:
1. HIGH: `mergeq._git` (`worktree add --detach`, `merge --squash`, `commit`, `merge --ff-only`, `revert`, `revert --abort`,
   `worktree remove`): a planted `post-checkout` hook recorded two planted fake keys verbatim. Commit, merge and revert also fire
   `pre-commit`, `commit-msg`, `post-commit`, `reference-transaction`, `pre-auto-gc`.
2. HIGH: the Gate 3 secret scan's own `git diff base..candidate` in `mergeq.py` (around line 344) has no `--no-ext-diff
   --no-textconv`, unlike `tamper.py`'s equivalent diff (around lines 1018-1021) which already passes both. A worker-committed
   `.gitattributes` assigning a textconv/external diff driver defined in the shared config can make the scanner see sanitised text
   while the real blob carries a secret, and run a command with the controller's environment in the same call.
3. HIGH: `core.fsmonitor` fires on every `status` (`guards._git`, `hardening._git`, `reconcile._git`, `integrity.py`): a planted
   fsmonitor script captured the fake keys with nothing in git's own output. `--no-optional-locks` does not affect it.
4. MEDIUM: `controller.py` `_bootstrap_git`, `publish_plan`, and `_branch_diff` (a full diff of a failed attempt's branch, no
   `--no-ext-diff/--no-textconv`, whose text is embedded in the retry card body through `recovery.failure_bundle`: a credential leak
   AND a prompt-injection path into the next worker's card). `hardening.py` branch delete and worktree remove.
5. RESIDUAL, not closable by a flag: filter drivers (`filter.<name>.clean/smudge/process`) and merge drivers
   (`merge.<name>.driver`) have attacker-chosen names, so no single `-c` pre-empts them. The environment scrub is what caps that
   damage (the driver runs, but sees no credential).
6. OUT OF REACH: Hermes's own per-card `git worktree add` (inside Hermes, not `src/ases`), and anything a worker's own terminal
   does (it runs as the operator's own OS user on the local backend: it can edit `~/.gitconfig`, shadow `git` on PATH, or read
   Hermes's credential files directly).

## Honest threat model (put this in the module docstring, do not overclaim)
On the LOCAL backend a malicious worker already runs as the operator's user and has other routes to credentials (item 6); the
blueprint says "Before Phase 5, ASES runs only on trusted test repositories" (p385). So this package is defense in depth there, plus
two things that matter on any backend: (a) the Gate 3 secret scan must see the real content, never a worker-controlled rendering of
it (ASES-SEC-01), and (b) diff text that reaches a card body must be git's own, not a worker's program's output (ASES-SEC-04). Under
the Docker sandbox (Phase 5) a worker cannot write the shared `.git` at all, which is the real fix for item 6.

## Build
1. A new module `src/ases/gitexec.py`, free of ASES imports except `procenv` (so any module can import it), the ONE definition of
   how the controller runs git:
   - `GIT`: the argv prefix: `git`, then `-c core.hooksPath=<dir>` pointing at an empty directory no repository controls (choose,
     e.g. an empty directory ASES creates once per process under its own temp root; test that git honours it on Windows and that a
     planted hook in `.git/hooks` does not run), and `-c core.fsmonitor=false`. Consider `-c core.untrackedCache=false` only if
     you can show it matters; do not add flags you cannot justify.
   - `git_env()`: `procenv.scrubbed_environ()` plus `GIT_TERMINAL_PROMPT=0` (a controller git call must never wait on a prompt).
     Do NOT set `GIT_CONFIG_NOSYSTEM`: the system config is not writable by a worker (not in the threat model), and on Windows it
     carries `core.autocrlf` and similar settings whose loss would silently change checkouts. Say so in the docstring.
   - `DIFF_SAFETY = ("--no-ext-diff", "--no-textconv")`, to be passed by every call that produces diff or patch TEXT (`diff`,
     `log -p`, `show`, `format-patch`), and the reason in the docstring.
   - Keep it minimal: constants and one small `run(...)` convenience at most. Do NOT force every module's own `_git` helper into
     one shape: they differ on purpose (bytes vs text, raising vs returning -1). Each helper keeps its signature and behaviour and
     only changes its argv prefix and `env=`.
2. Route every git subprocess in `src/ases` through it: `mergeq.py`, `guards.py`, `hardening.py`, `reconcile.py`, `integrity.py`,
   `controller.py`, `review.py`, `leases.py`, `finalgates.py` (`_GIT`), `tamper.py` (`_GIT`), `doctor.py` (`git --version`), and any
   `evalkit` git call (decide per call whether it runs against a repository a model wrote into; say what you decided). Grep for
   every `subprocess` call whose argv starts with `git` or a `_GIT` tuple; do not trust this list. Add `DIFF_SAFETY` to every
   diff-text call, above all `mergeq.py`'s Gate 3 secret-scan diff and `controller._branch_diff`.
   EXCLUDED: `src/ases/gates.py` (round 8 already scrubs its environment, and package GATESANDBOX is rewriting `run_gate`'s checkout
   on its own branch; the architect switches it to `gitexec` at merge) and `src/ases/fakes/` (test fakes, not the controller).
3. A completeness test, in the spirit of `test_fakes.py`'s signature check: scan `src/ases` (excluding `fakes/` and `gates.py`) for
   any `subprocess` call that starts git without the `gitexec` prefix and fail listing file:line. It must fail if someone adds a
   bare `["git", ...]` call later: prove that by planting one in a temp copy or by a unit test of the scanner itself.
4. Behaviour tests in throwaway repos (fakes only, never Docker, never real Hermes), each with a before/after proof against the
   old helper:
   - a planted `post-checkout` hook does not run during `mergeq`'s candidate `worktree add` (assert on a marker file the hook would
     write; the old code must create it);
   - a planted `core.fsmonitor` script does not run during `guards`'s `status` call;
   - a planted textconv driver that rewrites a file's content to something innocent does NOT hide a planted secret from the Gate 3
     secret scan (the old code misses the secret; the new code reports it). This is the most important test in the package;
   - a planted key in the test process environment is not visible to any git child the controller starts (a hook or driver that
     still runs, e.g. a filter driver, sees no key: this is the residual in item 5, capped by the scrub);
   - existing behaviour unchanged: the full suite stays green, including the merge queue's squash commits (identity survives
     through the round 8 `GIT_AUTHOR_*` exemption and the operator's own `user.name` config).
5. Run your module test files while you work and the full suite once at the end (worktree command in `r9_rules.md`).

## Files you own
`src/ases/gitexec.py` (new), the git subprocess call sites (argv prefix, `env=`, and diff-safety flags ONLY) in the modules listed
in item 2, and tests: `tests/unit/test_gitexec.py` (new) plus behaviour tests in the existing test files of the modules you touch.
Other packages edit other parts of `mergeq.py`, `controller.py`, `reconcile.py`, `guards.py`, `tamper.py`, `review.py`,
`finalgates.py` and `doctor.py` on their own branches: keep each hunk to the git call itself so the merge stays mechanical.
