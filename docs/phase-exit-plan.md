# Phase exit plan (ASES-DOC-03)

**This is a plan for the owner to approve, not a report of anything that has been run.** Every real-provider
number below is an estimate, built from measured per-card usage on other cards, not from actually running the
phase it is attached to. Nothing in this document authorizes `--spend-quota`, `swarm eval run`, a credit
purchase, or any other spend; each stays behind its own stop-condition approval (ASES-DOC-04) when the owner
decides to run it. This plan is built from `C:/Users/masoo/ases-workspaces/tools/blueprint.txt` and from
`C:/Users/masoo/ases-wt/_research/r19/STOPDOC.md` topic B (ASES-DOC-03), a read-only research pass dated
2026-09-29 that this package treats as its source; nothing here re-derives that research from scratch.

Blueprint quotes this plan follows:

- [p300]: "This is the order Claude Code MUST follow. Each phase lists its exit tests from section 22. A phase
  is done only when they pass and a short changelog is written. Rate limits, the fake provider and the model
  choice come first because everything after them depends on them. [ASES-DOC-03]"
- [p396]: "Tests 22.1 to 22.16 run against the fake provider and a test board unless stated otherwise, so they
  cost no quota and are repeatable."
- [p398]: "Real providers are used only in 22.1, the Phase 2 evaluation and 22.17."
- [p432]: "Run each level ten times with real providers and record success without human help, requests per
  merged task and review rounds." Table 35's pass bar: L0 9 of 10, L1 8 of 10, L2 6 of 10, L3 "Completes once
  inside the agreed budget".

How to read the table: "Zero-quota status today" is what this repository's own test suite proves right now, by
file name, so the owner can re-run any of it (`docs/operations.md` section 7 explains the commands; the
project's own convention is to route a local run through
`python C:/Users/masoo/.claude/scripts/quiet.py -l LABEL -- <pytest command>`). "Needs a real provider" says
what section 22 or the blueprint requires that the fake rig cannot stand in for, and points at the estimate
below that covers it. Numbers were last confirmed against a real `pytest` run on 2026-09-29 (this package):
`tests/acceptance` 56 passed in 138s; unit files (from the 2026-09-29 research pass this plan is built from,
not independently re-run by this package except where a file this package touched is named) `test_doctor` 108,
`test_evals` 200, `test_report` 208, `test_models` 15, `test_sandbox` 720 (1 skipped), `test_profiles` 234,
`test_hardening` 124, `test_cli_commands` 299, `test_policy` 29, `test_containers` 29.

## Phases 0 to 9

| Phase | Exit (section 22 / blueprint) | Zero-quota status today | Needs a real provider |
| --- | --- | --- | --- |
| 0. Environment | 22.1, environment part | `tests/unit/test_doctor.py` (mocked Hermes). Real: `swarm doctor` HEALTHY on 2026-09-28 and 2026-09-29, zero completion requests (only `hermes doctor`'s GET /models probes). | No completions needed, but the environment DECISION itself has a literal gap: [p400] asks for "inside WSL Ubuntu on the Linux filesystem"; D1 = native was chosen 2026-09-18. `spec/requirements.yaml` marks ENV-02/ENV-04 `not_applicable` and `doctor.py`'s environment_decision row always passes. This phase needs the owner's own written acceptance of that decision in a Phase 0 changelog, not a provider request. |
| 1. Doctor, fakes, ledger | 22.1, 22.4, 22.9 | `tests/acceptance/test_22_4_context.py` (4), `tests/acceptance/test_22_9_quota.py` (3), `tests/unit/test_doctor.py` plus the real doctor runs above. | Same 22.1 as Phase 0 (zero completions). Two named gaps, not a request-cost problem: doctor checks reviewer/lead PROVIDER diversity only, not model FAMILY, against [p400]'s "differs ... in model family and provider"; and the two unfunded paid rows in `config/models.yaml` (xkiro `openai/gpt-5.6-terra`, `qwen/qwen3.8-max`) show PEND because smoke-testing them would spend real money, which 22.1 cannot honestly turn fully green without an owner decision to fund them. |
| 2. Model mini-evaluation | Evaluation report accepted by the user | N/A: this exit is a real evaluation by definition, never a fake-rig test. | **Stale, must be re-run.** Accepted 2026-09-18 (`docs/phase2-evaluation-report.md`) for GLM on UnoRouter as Lead and `cohere/north-mini-code:free` on OpenRouter as Reviewer. Since then UnoRouter was removed and Lead moved to xKiro `qwen/qwen3.8-max:free`, which was never put through E1/E9/E10 (only a smoke test and real plans). The current reviewer has also failed the reviewer contract live twice (`docs/stage-c-2026-09-28.md` addendum), so it needs replacing before a re-evaluation is worth spending requests on. Estimate: see "Phase 2 re-evaluation" below. |
| 3. Three-role prototype | 22.2, 22.5, 22.6, 22.8, 22.15 | All pass: `test_22_2_end_to_end.py` (3), `test_22_5_parallel.py` (1), `test_22_6_review.py` (3), `test_22_8_merge_conflict.py` (3), `test_22_15_idempotent.py` (6). | No. Also has real evidence: stage C merged one real task end to end. |
| 4. Plan gate and recovery | 22.3, 22.7, 22.13, 22.14 | All pass: `test_22_3_failure.py` (1), `test_22_7_crash_recovery.py` (3), `test_22_13_kill_switch.py` (2), `test_22_14_plan_rejection.py` (4). | No. |
| 5. Sandbox, privacy, security | 22.10, 22.11, 22.12, 22.16 | All pass: `test_22_10_gate_env.py` (1), `test_22_10_secrets.py` (3), `test_22_11_injection.py` (**4** as of this package: TESTSDOCS added a real-container scenario alongside the existing 3; see the note below), `test_22_12_gate_config_pin.py` (3), `test_22_12_tampering.py` (5), `test_22_16_data_class.py` (5). | No completions. `scripts/sandbox_live_check.py` (real Docker, no model) already proves the sandboxed gate path end to end by hand; TESTSDOCS (this package) folded its network-block half into the pytest suite itself, skipping cleanly when Docker or the pinned image is unavailable. What is still genuinely open, honestly: no real Hermes WORKER has run its OWN terminal tool call inside Docker yet (a different code path from the controller's own sandboxed gate runner that the new test exercises: see `docker/sandbox/Dockerfile`'s comment, "the image the controller's own sandboxed gate runs ... and a worker's own Docker terminal backend use"), so the SEC rows in `spec/requirements.yaml` should stay `in_progress` until that happens for real, which needs a real dispatched card, not just a real container. |
| 6. More roles | 22.17 at L2 | No test exists anywhere yet; this phase has not been built. | Yes, entirely. The Tester role is wired in (round 7, per the register) but its E11 evaluation was never run for real. Estimate: see "Phase 6" below. |
| 7. Full evaluation harness | Evaluation report | Harness built and unit-tested: `test_evals.py` (200 passing). The real report itself does not exist. | Yes, the report itself is a real run. Its cost is the same 22.17 ladder counted under Phase 9 below; this phase does not add a separate cost on top of it. |
| 8. Report page | Manual check | `test_report.py` (208 passing). | No. The exit is the owner opening `swarm report --html` for a finished project and reading it; that costs no provider request by itself. |
| 9. Hardening | "The whole of section 22 green" | 22.2 to 22.16 are green on fakes today (and, per Phase 5's note, 22.11's real-container half is now included in that green). | Yes: 22.1 real (already true, zero completions) and 22.17 at L0 to L3, all with real providers. Estimate: the ladder table below. |

## Real-provider request estimates

These are the numbers `STOPDOC.md` topic B derived from measured usage on this project's own real cards (stage
C, 2026-09-28) and from the blueprint's own planning figures, not from running any of these phases. Treat every
range as a planning number to be replaced by a real one the first time each phase actually runs.

**Binding constraint, read this before any of the numbers below:** the reviewer sits on OpenRouter's free tier,
about 45 usable requests a day after this project's own 10% reserve and 20-request review reserve
(`config/swarm.yaml` `budgets.daily_reserve_percent` and `review_reserve_requests`). xKiro (Lead and coder) has
an unpublished, token-bound daily allowance that has to be measured on day one of any real run. The current
pinned reviewer model has already failed the reviewer contract live twice, which is a reason to replace it
before spending requests on a re-evaluation, not a reason estimates below are wrong.

Measured basis (stage C, 2026-09-28, `docs/architecture.md`): a coder card costs about 9 to 10 requests; a
review costs 13 to 17 (a hard one, 37); a critique costs 1 to 2 (one-shot plus at most one repair). A Lead plan
was never directly measured this way; the blueprint's own [p131] gives 30 to 80 requests for a first pass, and
stage C's one real plan took 86 seconds wall-clock, not a request count.

1. **22.1 real** (Phases 0, 1, 9): `swarm doctor --repo <test repo>`. 0 completion requests, only `hermes
   doctor`'s GET /models probes. Add `swarm smoke-test --provider P --model M --spend-quota` only for any newly
   pinned model, about 2 requests each.

2. **Phase 2 re-evaluation**: `swarm eval run --tasks E1,E9,E10 --candidates <labels> --spend-quota`, at least
   once for the current Lead (`xkiro/qwen/qwen3.8-max:free`, never run through E1/E9/E10 before) and once per
   candidate reviewer the owner decides to try in place of the one that failed its contract live. Cost per
   candidate: 8 to 12 requests. Total: xKiro 8 to 12; OpenRouter 24 to 36 for three reviewer candidates, which
   is about one day of OpenRouter's usable daily capacity.

3. **Phase 6**: the Tester role's E11 evaluation against coder-1 on E4 to E6, xKiro only: the harness estimates
   2 requests per task; real cards have run about 10. Call it 30 to 150 for the three tasks together, before
   22.17 L0 and L1 even start (counted in the ladder below).

4. **22.17 ladder** (Phase 9, and Phase 7's real report), ten runs per level except L3:

   | Level | xKiro per run | OpenRouter per run | x10 xKiro | x10 OpenRouter | OpenRouter, in days |
   | --- | --- | --- | --- | --- | --- |
   | L0 (1 card) | 20 to 110 | 14 to 42 | 200 to 1,100 | 140 to 420 | 4 to 10 (more with change rounds) |
   | L1 (1 to 2 cards) | 30 to 230 | 15 to 84 | 300 to 2,300 | 150 to 840 | 4 to 19 |
   | L2 (3 to 5 cards) | 40 to 380 | 40 to 200 | 400 to 3,800 | 400 to 2,000 | 9 to 45 |
   | L3 (once, not x10) | about 350 to 500 | about 200 to 350 | n/a | n/a | 5 to 8 |

   Read plainly: a full, honest 22.17 ladder (L0 through L2 ten times each, plus one L3 run) is 20 to 80 days of
   OpenRouter's free daily capacity end to end, or 1 to 4 days after the owner buys the one-time $10 credit top-up
   that raises OpenRouter's cap from 50 to 1,000 requests a day (`ASES-CAP-05`; that purchase is itself a
   stop-condition action the owner makes, never ASES).

## Before any of this is run

`STOPDOC.md` topic A (a separate research pass, ASES-DOC-04) lists zero-quota code changes it recommends
landing before any real-provider phase above proceeds: a data-class check on `swarm plan` and `swarm critique`
(today only task-role cards are checked, so the Lead and Reviewer roles themselves are not), no-silent-install
guards (Hermes's own default LSP auto-install, and Hermes's default `--pull missing` for a worker's own
Docker-backed terminal, which ASES does not check before dispatch -- distinct from the controller's own
sandboxed gate runner this package's new test exercises, which was already built with `--pull never`
hardcoded, per `sandbox.docker_run_argv`'s own docstring, before this package touched anything), and honest
usage ledgering for `swarm plan`/`swarm critique` (neither is ledgered today). None of that design is part of
this package (TESTSDOCS); it is named here only so this plan does not read as a green light on its own. See
`STOPDOC.md`'s own PROPOSED DESIGN section for the detail, and `PROVIDERS.md` for the separate, narrower
finding that no currently configured provider can honestly be recorded as safe for private-class data at the
provider level (section 21.2, ASES-PRV-04) -- a fact worth knowing before any private project is planned
against Phase 2 or later, not just before spending a request.

## Changelog

This document exists because `spec/requirements.yaml`'s ASES-TST-02 note and `STOPDOC.md` both observe that
`docs/architecture.md` has per-round changelogs but no single per-phase exit record. It does not replace either:
`docs/architecture.md` stays the dated log of what actually happened each round, and `spec/requirements.yaml`
stays the requirement-by-requirement register. This is the one place that answers, phase by phase, "is this
phase's exit met, and if not, what real-provider work and what estimated cost stands between here and there."

Written 2026-09-29 (package TESTSDOCS, ASES round 19). Update it the next time a phase's zero-quota status
changes, or the next time a real-provider run in section "Real-provider request estimates" above actually
happens, with the real numbers next to the estimate they replace.
