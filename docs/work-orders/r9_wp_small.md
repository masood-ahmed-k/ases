# Round 9 small Tier 1 packages (read `r9_rules.md` first)

Five independent packages, each in its own worktree `C:\Users\masoo\ases-wt\<name>` on branch `r9/<name>`.

## CIPIN (`cipin`): pin the CI and test-runner files, the open half of ASES-QG-02

Requirement (p277): "Gate commands come from the approved plan's gate profiles and are pinned in controller config with a hash. A
diff that changes gate configuration, CI scripts or test runner settings needs an explicit plan task that allows it. [ASES-QG-02]"

The gate PROFILES are pinned (`gates.hash_gate_profiles`, `controller.pin_gate_profiles`, `verify_gate_pin`). Gate 0 rejects a
wildcard `touches` broad enough to cover gate/CI configuration unless the task sets `allow_gate_config_changes: true`. The register
calls "hashing/pinning a fixed CI-file list" "a different, still-open half". Read the full ASES-QG-02 and ASES-QG-03 notes in
`spec/requirements.yaml`, `tamper.py`'s `gate_config_changed` finding, `review.check_branch`'s order of checks, and the round 6
entries in `docs/work-orders/builder-findings.md` that discuss this, and first write down precisely what is still unenforced (for
example: a task whose `touches` literally lists `pytest.ini` needs no marker today, so it can change test runner settings without an
explicit allowance; or the CI files' content is never pinned so a change that reaches the integration branch by any route goes
unnoticed). Then build the smallest change that makes "a diff that changes gate configuration, CI scripts or test runner settings
needs an explicit plan task that allows it" true at the point the diff is checked, with the list of CI/test-runner paths defined in
ONE place (reuse `tamper.py`'s list if it has one). Tests with a before/after proof. Files you own: whichever of `tamper.py`,
`review.py` (the check order only), `plan.py`, `gates.py`/`controller.py` pin functions you need, and their tests. The GATESANDBOX
package also extends the pin functions on its own branch (it adds a task field to what is hashed): keep your change to those
functions minimal and say exactly what you changed there.

## IDLEWT (`idlewt`): the idle-worktree check's false positive, ASES-GIT-12

Requirement (p185): "Before a worker starts and after it stops, the controller snapshots git status --porcelain and HEAD of the
primary checkout and of every other active worktree. Any change outside the worker's own worktree fails the card and raises a
security event. [ASES-GIT-12]"

`guards.check_idle_worktrees` reports changes in worktrees no running card owns as a WARNING only, because "the first version has
known false positives (a card re-dispatched into its worktree between two polls)". Reproduce that false positive in a test first,
then fix it (for example by recognising a worktree that has become owned again, or that belongs to a card whose own work explains
the change, from what the board and `worktree_snapshots` already record). Then decide, with evidence, whether any known
false-positive class remains; if none does AND a change can be attributed to exactly one running card, make it fail that card and
raise a security event as the requirement says; if attribution is ambiguous (several cards running), keep the warning and say why in
the docstring. Tests for each case. Files you own: `src/ases/guards.py` (the idle-worktree check and its helpers only; another
package is changing `guards.py`'s `_git` helper on its own branch, do not touch `_git`), and its tests.

## PAUSEREASON (`pausereason`): a paused project keeps its reason, ASES-CTL-01

Requirement (p200): "A project is finished when every merge card is done, Gates 4 and 5 are green on the integration HEAD, and the
release report is written. It is stopped, not finished, when any global bound is reached. Bounds are configuration with these
defaults. [ASES-CTL-01]"

The register's known gap: "`bounds.set_status(paused)` drops the reason (kept in a project_paused event)". Make the reason part of
the project's recorded state and show it wherever the project's status is shown (`swarm status`, `swarm report`, the stop/resume
path), without a migration if an existing column can hold it (check the projects table; if a migration is truly needed, follow
`db.py`'s rules and say so). Also check `docs/architecture.md`'s old note that "two `Bounds` classes and two `stop_requested`
functions ... disagree (four fields against eight; stopped against stopped-or-paused; a missing daily reserve reads 0 in one place
and 10 in another)": report whether that is still true today with file:line; do not unify them in this package. Files you own:
`src/ases/bounds.py`, the status/report display lines in `src/ases/report.py`/`src/ases/cli.py` that show a project's status, and
their tests.

## DOCTOR (`doctor`): source URLs and exported provider keys, ASES-VER-01 and p213

Requirements: ASES-VER-01 (p128): "These numbers were verified from current provider documentation on 18 September 2026. They can
change. swarm doctor MUST display the value it is using, the source URL and the checked date. [ASES-CAP-01] [ASES-VER-01]".
Blueprint p213: "Never export provider keys in the shell that launches the gateway or the controller." (ASES-CFG-04 and ASES-CFG-05
depend on it.)
1. `config/models.yaml` records `verified_on` per provider and doctor displays it; the source URL is not displayed. Add a source
   field where the value came from a known page: take URLs ONLY from the blueprint's Appendix E (`blueprint.txt`) or from URLs
   already written in `models.yaml`'s own comments; never invent one. Load it in `config.py`, display value, source and date in
   `swarm doctor`, and WARN (not FAIL) for an entry with no source.
2. A new `swarm doctor` check: for every provider `key_env` NAME in `config/models.yaml`, WARN if that variable is set and non-empty
   in the controller's own environment, naming the variable, never printing its value, and quoting p213. Also list (names only, as
   INFO) any other credential-shaped variable present, using `procenv`'s pattern so the definition stays in one place. Decide WARN
   versus FAIL and justify it in the docstring (ASES now scrubs every subprocess it starts, but a gateway started from the same shell
   would inherit the key).
Tests on fakes (monkeypatch the environment and the config); `swarm doctor` itself shells out to `hermes`, so never run it for real:
test the check functions directly. Files you own: `src/ases/doctor.py` (your two checks; another package adds a Docker check on its
own branch), `src/ases/config.py` (the source field only), `config/models.yaml`, and their tests.

## CAPDOC (`capdoc`): a repeatable checklist for adding a provider, ASES-CAP-06

Requirement (p136): "Capacity SHOULD come from provider diversity: use additional free-tier providers that the user is legitimately
entitled to use, such as native Hermes providers or a compatible endpoint. Do not assume that extra API keys for the same account
increase a provider quota. Each addition goes through discovery, smoke test, data-policy check and evaluation. [ASES-CAP-06]"
The register: no repeatable artifact makes discovery and evaluation hold for the NEXT provider. Write
`docs/provider-onboarding.md`: the four steps as a checklist, each naming the ASES command, config field or check that supports it
(`config/models.yaml` fields such as `verified_on`, `context_length`, `data_policy_*`; `policy.check_data_class`; the key-pooling
doctor check from ASES-CFG-02; the evaluation harness in `evals.py`/`evalkit`, including that `--spend-quota` is a real, user-
approved spend), the same-account-keys warning, the stop-condition actions involved (account creation is never done by ASES,
ASES-CFG-03), and a copy-paste `models.yaml` stub with every field commented. Read `docs/operations.md` and the dated provider
sections of `docs/architecture.md` (the xKiro, OpenRouter and UnoRouter evaluations) for how it was actually done last time, and
make the checklist match reality. Link it from `docs/operations.md`. Verify every command and field you name exists (Grep). Files
you own: `docs/provider-onboarding.md`, one link line in `docs/operations.md`. No code.
