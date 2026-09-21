"""Helpers for the evaluation harness (blueprint Appendix D; src/ases/evals.py is the entry point).

model.py      the small types every part shares (Candidate, EvalTask, Score, InvokeResult, the two errors)
text.py       text normalising, keyword topic scoring, code fence and JSON extraction
codeeval.py   applying a model's patch to a temp copy and running pytest there
texttasks.py  the tasks scored from text: E1 Requirements, E2 Architecture, E3 Repository understanding, E7 Security,
              E9 Tool use, E10 Review
codetasks.py  the tasks that run code: E4 Debugging, E5 Refactor, E6 Test design, and the E8 Long-horizon descriptor
tasks.py      the registry E1 to E11 (E11 is a comparison), how task ids are read, and why E8 and E11 are not run standalone

Nothing in this package calls a model. evals.py owns the runner and the only function that does (default_invoke).
"""
