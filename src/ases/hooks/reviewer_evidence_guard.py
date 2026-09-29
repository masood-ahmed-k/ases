"""R2 (pre_tool_call shell hook, matcher kanban_block|kanban_request_changes): stops a reviewer profile from
blocking or requesting changes to ask the CONTROLLER to run tests, instead of giving a code verdict (design
(a)'s Layer 1; blueprint ASES-REV-05: "the controller re-runs Gate 1 itself"). Uses the SAME classifier as
controller.process_reviewer_contract (reviewcontract.classify_evidence_text), so the in-run hook and the
Layer 2 controller ladder can never quietly disagree about what counts as evidence-stalling.

fail_closed: false (this hook's own config.yaml entry, profiles.py) -- deliberately: this hook only NARROWS what
reaches Layer 2, it does not replace it. A false negative here (an EVIDENCE reason that slips through) still
reaches the Layer 2 ladder on the controller's next pass and is handled there; a false positive here would block
a reviewer's GENUINE verdict in-run, which nothing then corrects (unlike Layer 2's veto, which only stops ASES
from acting, never blocks the reviewer's own tool call). So this script fails open on absolutely anything
unexpected: a stdin it cannot parse, a tool_input with no reason, the reviewcontract import failing -- every one
of those exits 0 with nothing printed, which agent/shell_hooks.py's `_evaluate_result` (:372-381) reads as "no
verdict, allow" exactly like fail_closed: true would for deny_tool.py's own crash case (see that script's module
docstring for the exact contract this relies on). Only a confirmed EVIDENCE classification exits 2, with the
message on stderr (agent/shell_hooks.py:369 reads stderr when stdout carries no {"action": "block", ...} JSON).

Runs under Hermes's own Python exactly like deny_tool.py: see that script's module docstring and its
`_ases_src()` (copied here rather than imported, so this script has no dependency on deny_tool.py loading first)
for why the import of `ases.reviewcontract` is resolved from this file's own path, never from an active venv.
"""
from __future__ import annotations

import json
import pathlib
import sys


def _ases_src() -> str:
    """The `src` directory that holds the `ases` package: src/ases/hooks/reviewer_evidence_guard.py ->
    parents[2] is `src`. See deny_tool.py's identical helper for why this is resolved from disk, not sys.path."""
    return str(pathlib.Path(__file__).resolve().parents[2])


def _reason() -> str | None:
    """tool_input.reason from stdin (tools/kanban_tools.py:631, :708: both kanban_block and
    kanban_request_changes take a `reason` field), or None if stdin cannot be read, is not JSON, or carries no
    such field. Never raises."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    reason = tool_input.get("reason") if isinstance(tool_input, dict) else None
    return reason if isinstance(reason, str) else None


def main() -> int:
    try:
        reason = _reason()
        sys.path.insert(0, _ases_src())
        from ases import reviewcontract
        if reviewcontract.classify_evidence_text(reason):
            sys.stderr.write(reviewcontract.EVIDENCE_GUARD_MESSAGE + "\n")
            return 2
    except Exception:
        pass  # fail open: see the module docstring; a crash here must never look like a genuine allow OR block
    return 0


if __name__ == "__main__":
    sys.exit(main())
