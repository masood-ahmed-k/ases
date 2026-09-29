"""The reviewer contract: what a reviewer profile must never do, and what it looks like when it stops instead
of judging the code (round 19, package REVIEWLADDER; blueprint ASES-ROL-05, ASES-ROL-06, ASES-REV-05,
ASES-REC-01, ASES-REC-05). Design: C:/Users/masoo/ases-wt/_research/r19/REVIEWER.md, section "DESIGN (a)".

Pure: every function here is given data and returns data. Nothing imports hermes, events, sqlite3 or a board.
That is deliberate, not incidental: the classifier is shared by two callers that have nothing else in common --

  - controller.process_reviewer_contract (Layer 2), which runs inside the ASES venv with a database connection
    and the hermes.py wrapper, and
  - src/ases/hooks/reviewer_evidence_guard.py (Layer 1, hook R2), a standalone script Hermes's shell-hook
    bridge spawns as its OWN subprocess, under WHATEVER Python that command line names -- never assume it is
    the ASES venv's interpreter or that this package is on its sys.path the ordinary way (see that script's own
    module docstring for how it resolves this module without one).

so the two layers can never quietly disagree about what counts as "the reviewer asked the controller to run
its tests instead of judging the code".

R1 (Hook, deny_tool.py) and R2 (Hook, reviewer_evidence_guard.py) are the two pre_tool_call shell hooks
profiles.py writes into a REVIEWER-role profile's config.yaml (`kanban_lifecycle_only` profiles only: reviewer
and any specialisation played by it). Hermes 0.21.3 source, read 2026-09-29, HEAD c661785f872b:

  - agent/shell_hooks.py:124 `matches_tool`: the configured `matcher` is `re.compile(matcher).fullmatch(tool_name)`
    -- a full match against the bare tool name, never a substring search, so DENY_MATCHER/EVIDENCE_MATCHER below
    are wrapped in `(?:...)` and never anchored again by a caller.
  - agent/shell_hooks.py:270-279 `_parse_single_entry`: `fail_closed` only ever applies to `pre_tool_call` (the
    one member of `_BLOCKING_EVENTS`, line 44); a `matcher` is only honoured for `pre_tool_call`/`post_tool_call`
    (line 255-258, `_TOOL_EVENTS`).
  - agent/shell_hooks.py:350-381 `_evaluate_result`: exit code 2 on a blocking event blocks, message from stdout
    JSON `{"action": "block", ...}` if present, else stderr (trimmed to 400 chars), else a default. A spawn error
    or timeout fails open unless `fail_closed`; a NORMAL exit (0, or any code that is not 2) with EMPTY stdout is
    read as "no verdict" and allows the call, `fail_closed` or not -- there is no way for `fail_closed` to save a
    hook that crashes silently with nothing on stdout, only one that fails to even start, times out, or returns
    unparseable stdout while stdout is non-empty. This is exactly why both hook scripts never let an exception
    escape uncaught: a script that dies before printing anything fails OPEN regardless of `fail_closed`, which is
    the one outcome R1 (fail_closed: true) must never produce for a genuinely denied tool.
  - agent/shell_hooks.py:141-176 `register_from_config`: registration is gated on the shell-hook allowlist
    (first-use consent) unless `--accept-hooks` / `HERMES_ACCEPT_HOOKS=1` / `hooks_auto_accept: true` in
    config.yaml. kanban_db_dispatch.py always passes `--accept-hooks` to a dispatcher-spawned worker (research
    report finding F4), so a card the Kanban dispatcher spawns registers both hooks with no TTY prompt;
    `hooks_auto_accept: true` additionally covers a non-dispatched `hermes -p reviewer -z` critic run, which
    never gets `--accept-hooks`.
  - agent/shell_hooks.py:147-149 `HERMES_SAFE_MODE`: `register_from_config` returns `[]` at once when this env
    var is truthy, before it even parses `hooks:` -- neither hook registers, so every tool the reviewer would
    otherwise be denied is silently allowed again. profiles.py's doctor check and swarm doctor's own row warn
    on this rather than pretending the hooks are protecting anything while it is set.
  - tools/kanban_tools.py:657-700 `kanban_request_review`'s handler takes the implementer from the CURRENT
    assignee (kanban_db.py:3209-3219 `request_review`), so a reviewer calling it corrupts Hermes's own
    provenance for the card's lifetime (research report finding D6) -- R1 closes this at the source.
  - tools/kanban_tools.py:626-654 `kanban_block`, :703-713 `kanban_request_changes`: both take a `reason` field
    in `args` (`tool_input.reason` in the hook's stdin payload), which is exactly what Hermes stores on the run
    as `summary` (kanban_db.py:3343-3345 `_end_run(..., summary=reason)` for request_changes; :3103-3105
    `_end_or_synthesize_run(..., summary=reason)` for block_task) -- `_run_text` below reads the same field.

Kept out of this module on purpose: the config.yaml SHAPE those hooks are written into (profiles.py owns every
Hermes profile config decision), the ladder that acts on a ReviewerStop (controller.process_reviewer_contract:
gate records, ANSWER_ONCE, asking the owner, the D4/D5 fixes), and automatic reviewer-model switching or the
archive-and-recreate replacement route, both DEFERRED by the architect pending an eligible second reviewer
model (see controller.py's own docstring on process_reviewer_contract).
"""
from __future__ import annotations

import dataclasses
import re

# ---------------------------------------------------------------------------------------------------------------
# R1 (deny_tool.py): the tools a reviewer profile must never call.
# ---------------------------------------------------------------------------------------------------------------

# write_file, patch (toolsets.py:120-124, the one combined `file` toolset -- see profiles.RESIDUAL_RISKS for why
# it cannot be split) and skill_manage (toolsets.py:101-104, the `skills` toolset's own write tool) are the
# product-file / command-adjacent write paths ASES-ROL-05/ASES-ROL-06 ask to remove. kanban_request_review,
# kanban_unblock and kanban_link are Kanban lifecycle tools, but not verdict tools: ASES-ROL-05 keeps "only the
# Kanban lifecycle tools needed to issue the review verdict" (kanban_complete, kanban_request_changes,
# kanban_block), and a reviewer using any of these three either corrupts provenance (request_review, D6) or does
# the controller's own job (unblock, link). Order is fixed and is also the order profiles.py writes the matcher
# alternation in, so a diff of config.yaml is stable run to run.
DENIED_TOOLS = ("write_file", "patch", "skill_manage", "kanban_request_review", "kanban_unblock", "kanban_link")

# agent/shell_hooks.py:124: re.fullmatch against the bare tool_name. `(?:...)` so a caller that wraps this in a
# bigger pattern (there is none today, but profiles.py's doctor check re-parses this exact string) never has to
# guess whether the alternation is grouped.
DENY_MATCHER = "(?:" + "|".join(DENIED_TOOLS) + ")"

# R2 (reviewer_evidence_guard.py): the two verdict tools a reviewer can misuse to ask the CONTROLLER to run tests
# instead of judging the diff (tools/kanban_tools.py:626-654, :703-713). kanban_complete is deliberately absent:
# a PASS is never evidence-gated, and gating it would let a matching reason text block a legitimate approval.
EVIDENCE_TOOLS = ("kanban_block", "kanban_request_changes")
EVIDENCE_MATCHER = "(?:" + "|".join(EVIDENCE_TOOLS) + ")"

# design (b), item 1: both hooks get the same short timeout. 10s is generous for a classifier that is a handful
# of compiled regexes over a few hundred characters of text; a hook this simple that cannot finish in 10s is a
# stuck host, not a slow hook, and R1's fail_closed: true means a timeout blocks anyway (shell_hooks.py:350-361).
HOOK_TIMEOUT_SECONDS = 10

# The design's own example message (REVIEWER.md line 159) is for exactly this tool: the D6 case. The other five
# denied tools get a parallel message naming the same three verdict tools, because a one-message-fits-all
# reading would leave "a reviewer never hands off for review" attached to a write_file call, which is not what
# write_file even is. This is the smallest extension of the design's wording that stays true for every tool R1
# can fire on; recorded as a build decision in this package's report, not silently substituted.
_DENY_MESSAGES = {
    "kanban_request_review": (
        "ASES: a reviewer never hands off for review. Give your verdict with kanban_complete, "
        "kanban_request_changes or kanban_block."
    ),
    "kanban_unblock": (
        "ASES: a reviewer never unblocks a card; that is the controller's job. Give your verdict with "
        "kanban_complete, kanban_request_changes or kanban_block."
    ),
    "kanban_link": (
        "ASES: a reviewer never links cards; that is the controller's job. Give your verdict with "
        "kanban_complete, kanban_request_changes or kanban_block."
    ),
}
_DEFAULT_DENY_MESSAGE = (
    "ASES-ROL-05/ASES-ROL-06: {tool} is not available to the reviewer. The reviewer keeps only the Kanban "
    "lifecycle tools needed to issue a verdict (kanban_complete, kanban_request_changes, kanban_block)."
)


def deny_message(tool_name: str) -> str:
    """The stderr text R1 (deny_tool.py) prints before it exits 2. `tool_name` is whatever the hook's stdin JSON
    named; an unrecognised one (should never happen: the matcher only fires on DENIED_TOOLS) still gets a message
    naming it, never a blank block."""
    name = str(tool_name or "").strip()
    return _DENY_MESSAGES.get(name, _DEFAULT_DENY_MESSAGE.format(tool=name or "this tool"))


# design (a) line 162, verbatim: the message R2 (reviewer_evidence_guard.py) prints on EVIDENCE. Kept here, not
# inlined in the hook script, so a test can assert on it without spawning a subprocess and so there is exactly
# one place this exact sentence is spelled.
EVIDENCE_GUARD_MESSAGE = (
    "ASES: running tests is the controller's job. The card's comment headed 'ASES gate record' is the "
    "evidence. Do not block or request changes for test evidence. Judge the code and give PASS or "
    "CHANGES_REQUIRED with concrete code findings. If you have a genuine requirements or design question, "
    "ask only that question."
)


# ---------------------------------------------------------------------------------------------------------------
# R2 / Layer 2: the conservative EVIDENCE classifier (design (a), "EVIDENCE:" list).
# ---------------------------------------------------------------------------------------------------------------

KIND_PROTOCOL = "PROTOCOL"
KIND_EVIDENCE = "EVIDENCE"

# "ASK": the reviewer says it cannot itself produce or verify test evidence.
_ASK_PATTERNS = (
    re.compile(
        r"\b(?:can ?not|can't|unable to|could not|couldn't)\b.{0,80}?\b(?:verify|confirm|run|execute|validate)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bprovide\b.{0,60}?\b(?:evidence|proof|results?|output)\b", re.IGNORECASE),
    re.compile(r"\b(?:test|verification)\s+(?:evidence|results?|output|verification|execution)\b", re.IGNORECASE),
)
# "TEST" / "TOOL": the ASK phrase must be ABOUT tests or about running a command, or it is not this failure mode.
_TEST_PATTERN = re.compile(r"\b(?:tests?|pytest|test suite|gates?)\b", re.IGNORECASE)
_TOOL_PATTERN = re.compile(r"\bterminal\b|\bcommand[- ]execution\b", re.IGNORECASE)
# The veto: a sentence that is actually a requirements or design question, never gated as evidence-stalling.
_VETO_PATTERN = re.compile(
    r"\bshould (?:we|i|the|it|this)\b|\bdo you want\b|\bwhich (?:option|approach|behaviou?r|one)\b"
    r"|\bplease (?:decide|clarify|choose)\b|\bis it intended\b"
    r"|\brequirements? (?:is|are) (?:unclear|ambiguous|contradictory)\b",
    re.IGNORECASE,
)
# Sentence split on . ? ! or a newline, KEEPING the terminator (a bare re.split on the class would throw it away,
# and the veto rule needs to know which sentences ended in "?"). The last sentence may have terminator "".
_SENTENCE_SPLIT = re.compile(r"([.?!\n])")


def _sentences(text: str) -> list[tuple[str, str]]:
    """(sentence, terminator) pairs of `text`, terminator one of ".", "?", "!", "\\n" or "" (end of string with
    no closing punctuation). Blank sentences (consecutive terminators, leading/trailing whitespace) are dropped."""
    parts = _SENTENCE_SPLIT.split(text)
    out: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        sentence = parts[index].strip()
        if not sentence:
            continue
        terminator = parts[index + 1] if index + 1 < len(parts) else ""
        out.append((sentence, terminator))
    return out


def _matches_ask_test_or_tool(sentence: str) -> bool:
    return bool(
        any(pattern.search(sentence) for pattern in _ASK_PATTERNS)
        or _TEST_PATTERN.search(sentence)
        or _TOOL_PATTERN.search(sentence)
    )


def classify_evidence_text(text: object) -> bool:
    """True when `text` (a reviewer's kanban_block or kanban_request_changes reason) asks the CONTROLLER to run
    tests instead of giving a verdict on the code -- design (a)'s conservative EVIDENCE classifier, shared
    verbatim by R2 (the in-run hook) and controller.process_reviewer_contract (the Layer 2 ladder), so the two
    can never disagree about what a given piece of text means.

    Deliberately conservative in both directions:
      - it requires an ASK phrase (a claimed inability to verify/run/execute/validate, or a request to be given
        evidence/results/output) AND that the same text names tests or a terminal/command execution -- a
        reviewer that merely mentions "the tests" while judging code (`the tests already cover this branch`)
        matches TEST but never ASK, and is not flagged;
      - it is vetoed sentence by sentence: any sentence that is actually a requirements or design question (ends
        in "?" without itself matching ASK/TEST/TOOL, or matches the fixed veto phrases: "should we/I/the/it/
        this", "do you want", "which option/approach/behaviour/one", "please decide/clarify/choose", "is it
        intended", "requirements is/are unclear/ambiguous/contradictory") turns the whole text back into a
        genuine question, never evidence-stalling, even if an earlier sentence alone would have matched.

    `text` is coerced with str(); None or "" is never EVIDENCE."""
    body = str(text) if text is not None else ""
    if not body.strip():
        return False
    ask_hit = any(pattern.search(body) for pattern in _ASK_PATTERNS)
    test_or_tool_hit = bool(_TEST_PATTERN.search(body) or _TOOL_PATTERN.search(body))
    if not (ask_hit and test_or_tool_hit):
        return False
    for sentence, terminator in _sentences(body):
        if _VETO_PATTERN.search(sentence):
            return False
        if terminator == "?" and not _matches_ask_test_or_tool(sentence):
            return False
    return True


@dataclasses.dataclass(frozen=True)
class ReviewerStop:
    """One reviewer-profile run that broke the contract instead of giving a verdict.

    `kind` is KIND_PROTOCOL (the reviewer called kanban_request_review, corrupting Hermes's own implementer
    provenance for this card, research report finding D6) or KIND_EVIDENCE (the reviewer blocked or requested
    changes citing an inability to run tests itself, when running tests is the controller's job: ASES-REV-05).
    `run_id` is exactly `run["id"]` (whatever type Hermes's --json gives it, kept opaque: this dataclass never
    re-types it, so a caller's own event lookups by run_id compare equal). `reason` is a short, fixed, ASCII-safe
    note for logs and events -- never the reviewer's own text verbatim (that can be arbitrarily long and is
    already on the card as the block/changes-request reason; repeating it in an event would just double the
    exposure of anything ASES-SEC-01 would want redacted, for no benefit an event reader does not already have
    from the card itself)."""

    kind: str
    run_id: object
    profile: str
    outcome: str
    reason: str


def _run_profile(run: dict) -> str:
    return str(run.get("profile") or "").strip()


def _run_outcome(run: dict) -> str:
    return str(run.get("outcome") or "").strip().lower()


def _run_text(run: dict) -> str:
    """The text a reviewer gave for a block or a changes-request. Hermes stores it on the run's own `summary`
    field for both (kanban_db.py:3343-3345, :3103-3105); `error` is read too, only because some non-Hermes
    caller in this codebase's own test fakes has in the past put a block/changes reason there instead (never
    both at once for a real Hermes run, so there is no ambiguity about which one a real run would set)."""
    return str(run.get("summary") or run.get("error") or "")


def classify_reviewer_run(run: dict, reviewer_profiles) -> ReviewerStop | None:
    """The ReviewerStop `run` represents, or None when it is not a reviewer-profile run, or is one that ended
    normally (a verdict: completed/changes_requested-with-a-real-finding/blocked-on-a-genuine-question).

    `reviewer_profiles` is a collection of profile names (typically one: the profile config/swarm.yaml's `roles:`
    maps to "reviewer", resolved through policy.resolve_assignee -- see controller._reviewer_profile) that count
    as "the reviewer" for this check. A run whose `profile` is not in it is never classified, whatever its
    outcome or text: this function only ever looks at reviewer-profile runs, by design, so a coder's own block or
    changes-request (which can say anything, including the word "test") is never mistaken for a contract stop.

    PROTOCOL: `run["outcome"] == "review_requested"` and the run's profile is a reviewer profile -- a reviewer
    that handed off for review instead of giving a verdict (D6). Checked before EVIDENCE: a review_requested run
    has no block/changes-request text to classify, and PROTOCOL is the more specific, more serious failure.

    EVIDENCE: `run["outcome"] in ("blocked", "changes_requested")` and classify_evidence_text(...) on the run's
    own text (see `_run_text`) is True.

    Every other outcome (completed, a genuine question, scheduled, still running/no outcome yet, or a
    reviewer-profile run whose text does not match the EVIDENCE classifier) is None: a reviewer that gave a real
    verdict, or asked a real question, is not a contract stop and this function never second-guesses the verdict
    itself -- only Hermes's own protocol and the design's narrow evidence-stalling pattern are in scope here."""
    if not isinstance(run, dict):
        return None
    profile = _run_profile(run)
    if not profile or profile not in reviewer_profiles:
        return None
    outcome = _run_outcome(run)
    run_id = run.get("id")
    if outcome == "review_requested":
        return ReviewerStop(
            KIND_PROTOCOL, run_id, profile, outcome, "reviewer profile called kanban_request_review (D6)",
        )
    if outcome in ("blocked", "changes_requested"):
        if classify_evidence_text(_run_text(run)):
            return ReviewerStop(
                KIND_EVIDENCE, run_id, profile, outcome,
                "reviewer stopped asking the controller to run tests, not with a code finding",
            )
    return None


# ---------------------------------------------------------------------------------------------------------------
# Event kind names Layer 2 (controller.process_reviewer_contract) records. Named here, once, so a caller that
# only needs to recognise them (recovery.refresh_review_rounds excludes an EVIDENCE changes_requested from the
# review-round count, the D5 fix; tests assert on them) never has to spell the string out again and risk a typo
# that silently stops matching.
# ---------------------------------------------------------------------------------------------------------------

# Recorded exactly once per run_id (controller's dedup): {project, task_key, card_id, run_id, commit, kind,
# profile, model, provider, action}. `action` is one of "answer_once", "ask_owner", "protocol", "evidence_noted"
# (the D5 changes_requested case, action 7 of the ladder: no verdict decided, just recorded and excluded).
EVENT_DECISION = "reviewer_contract_decision"

# Recorded alongside EVENT_DECISION for every stop that is a genuine contract failure (both kinds): counted per
# (task_key, commit, model), the bound a `reviewer_switches_per_task` budget would read if switching were built.
EVENT_FAILURE = "reviewer_contract_failure"

# ANSWER_ONCE's own marker: never twice for the same (task_key, commit).
EVENT_ANSWERED = "reviewer_evidence_answered"

# A PROTOCOL stop marks the card so no ASES step ever unblocks or reopens it again (design step 6): Hermes would
# hand the card straight back to "reviewer" as implementer, exactly the corruption D6 already caused once.
EVENT_PROVENANCE_BROKEN = "provenance_broken"
