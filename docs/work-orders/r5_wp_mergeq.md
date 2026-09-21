# Package MR: merge queue, review lane and usage wiring (stop checks, intents, tamper checks, model mismatch)

Files you own: `src/ases/mergeq.py`, `src/ases/review.py`, `src/ases/usage.py`, `src/ases/gates.py`, `tests/unit/test_mergeq.py`,
`tests/unit/test_review.py`, `tests/unit/test_usage.py`, `tests/unit/test_gates.py`. Nothing else. Read `r2_rules.md`, `r5_rules.md` and
`r5_contracts.md` first (the contract section "mergeq and review" is what the controller package will call). The controller package
(CT) is being edited at the same time and will call your new parameters; do not edit controller.py.

## Requirements (blueprint.txt [p272] to [p280], [p184] to [p187], [p349] to [p357], section 19.1 rows "Merge conflict or red Gate 3", "Controller crash")
- ASES-QG-03 and ASES-GIT-07: the tamper check and the secret scan belong in Gate 1 (and Gate 3 already scans). The tamper module
  (`src/ases/tamper.py`: `check_range`, `format_findings`, `blocking`, `TamperCheckError`) exists and is fully tested; NOTHING calls it.
- ASES-QG-02: "A diff that changes gate configuration, CI scripts or test runner settings needs an explicit plan task that allows it."
  `tamper.check_range(..., allow_paths=touches, gate_config_paths=...)` reports it; you supply the paths.
- ASES-REC-06: "stop the merge queue between steps". ASES-REC-03/04: "Every multi-step action writes an intent record before acting and a
  completion record after: create cards, run a gate, build a candidate, fast-forward, complete a merge card, revert."
- ASES-RTE-01: "No hidden fallback; the provider and model actually used are recorded."
- ASES-SEC-01: nothing secret-shaped in stored gate output or in text handed to a card.

## 1. `mergeq.py`
- `merge_task(..., project=None, should_stop=None)` and `MergeOutcome.stopped` exactly as in `r5_contracts.md`. Poll `should_stop`
  before building the candidate, before Gate 3 and before the fast-forward; on True remove the candidate worktree and branch, leave
  `merge_records` exactly as it was (no half row), and return `merged=False, stopped=True` with detail "stopped by the kill switch
  before <step>". A `should_stop` that raises is treated as False (a broken callable must not block merging) and recorded as an event.
- With `project` given, wrap the candidate build plus Gate 3 in `intents.intent(conn, project, intents.KIND_BUILD_CANDIDATE, task_key)`
  and the fast-forward in `intents.KIND_FAST_FORWARD`; `revert_merge(..., project=None)` wraps in `KIND_REVERT`. The intent is
  completed only when the step finished cleanly (the context manager already does that); a crash leaves it open for `reconcile`.
- FIX (found by the reconcile builder): the candidate upsert into `merge_records` never resets `reverted`, `squash_commit` or
  `completed_at`, so after a revert, a fix card and a second merge `reverted` stays 1 and `reconcile.check()` reports
  `done_but_reverted` forever. A new candidate for a task resets all three (a row that is already completed and NOT reverted must not
  be touched: only a new candidate build resets it; read `merge_task` to see where the row is written).
- `MergeOutcome.detail` and the text stored for a failure must be secret-free: pass command output through `events.redact_text`.

## 2. `review.py`
- In `check_branch` (used by both `gate_before_review` and `check_branch_for_merge`), after the scope check and before Gate 1 runs,
  call `tamper.check_range(repo, base, head, allow_paths=touches, gate_config_paths=<paths named by the gate commands>)`, where `base` is
  what the scope check already uses (the merge-base with the integration branch; use the same range so the two checks cannot
  disagree). Blocking findings (`tamper.blocking`) give `BranchCheck(False, "tamper", tamper.format_findings(...), head)`;
  `TamperCheckError` gives `BranchCheck(False, "tamper_check_error", <short reason>, head)`.
- `gate_config_paths(gate_commands, repo, head) -> list[str]` (public helper, tested): split each command with `shlex.split` (fall back
  to `str.split` on a ValueError), keep tokens that do not start with `-`, contain a path separator or end in a script or config
  extension (`.sh .py .js .ts .mjs .cjs .json .yaml .yml .toml .cfg .ini .mk .bat .ps1 .gradle`), and exist as a file at `head`
  (`git cat-file -e <head>:<token>`, normalised to forward slashes). Duplicates removed, order kept, never raises.
- `gate_before_review`: a `tamper` result sends the card back exactly like a red Gate 1 (the findings text is the evidence, trimmed the
  same way); a `tamper_check_error` result records a `tamper_check_error` event and does NOT send the card back (the merge-time check
  is authoritative and fails closed): it returns True.
- `check_branch_for_merge`: returns the `tamper` / `tamper_check_error` BranchCheck as-is (the controller decides).
- The secret scan is part of `tamper.analyze_diff` (`secret_added`), so Gate 1 now scans too; nothing else to add for ASES-GIT-07.

## 3. `usage.py`
- ASES-RTE-01: when a session's model (from `hermes.session_usage`) differs from the model pinned for its profile
  (`policy.profile_provider(role, models_config)`, compared after stripping a leading `<provider>/`), record ONE `model_mismatch`
  event per session (`profile`, `expected`, `actual`, `session_id`), never twice for the same session, and still count the usage
  against the provider it actually hit when that can be determined, else the profile's provider. This is detection only: no card is
  failed. Keep every existing behaviour and test.

## 4. `gates.py`
- `run_gate`: redact the command output once (`events.redact_text`) BEFORE it is stored in `gate_runs.detail` and before it is
  returned in `GateResult.detail`, so no consumer can copy a secret into a card body. Everything else stays.

## Tests
mergeq: the stop callable at each of the three checkpoints (candidate removed, no `merge_records` row change, `stopped=True`), a
raising callable, the intents (open during the step, completed after, left open when the step raises), the reset of
`reverted`/`squash_commit`/`completed_at` after revert then re-merge, redacted detail. review: tamper findings blocking (one test per
kind that matters: deleted test, skip marker, `|| true`, gate config, artifact, secret) on real temp git repos, the same range as
the scope check, a config file allowed by the task's touches, `TamperCheckError` giving `tamper_check_error`, the send-back text,
no send-back on the error, `gate_config_paths` (a script path, a config path, a flag, a missing file, a quoted path, no raise).
usage: mismatch recorded once, not recorded on a match, provider prefix stripped. gates: redaction of stored and returned detail.
