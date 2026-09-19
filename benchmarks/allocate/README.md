# allocate benchmark: the swarm against a single agent

A small, precisely specified task for comparing the ASES swarm (lead, coder, reviewer, merge queue) with one
Claude agent that gets the same request directly. The task is a money-splitting function with subtle rules
(largest remainder, tie-breaking, negative totals, zero weights, exact big integers, input validation).

## Files

- `request.txt`: the task, word for word the same for both arms.
- `reference/allocate.py`: the reference solution, the oracle.
- `hidden/test_hidden_allocate.py`: 91 acceptance tests neither arm sees (worked examples, error cases, big
  numbers, properties, and differential tests against an independent Fraction-based oracle). Keep this file out
  of any repository a worker can read.
- `eval_arm.py`: grades one arm: its own tests on its own code, the hidden suite on its code, its own tests on
  the reference (portability), and its own tests against 15 seeded-bug mutants of the reference (test strength).

The grader was validated before the first run: the reference passes all 91 hidden tests and every one of the 15
mutants fails at least one of them (two mutants that turned out to be behaviourally identical to the reference
were replaced before the run).

## Running it again

1. Give each arm a repository with the same starting tree. The swarm arm needs a repo with a git identity and a
   Hermes project bound to it; a Sonnet arm can work in a `git init` copy of the same tree, committing with
   `git -c user.name=... -c user.email=... commit` so no git config is written.
2. Swarm arm: `swarm plan --request "<request.txt> plus a plan-shape line"`, then `swarm approve`, then
   `swarm run`. Do not edit the plan.
3. Agent arm: the same request plus "commit once on a branch, do not push".
4. Grade each: `python eval_arm.py <label> <directory containing allocate.py and test_allocate.py>`.

## First run (2026-09-19): both arms fully correct, the difference is in the tests

| | swarm (free models) | one Sonnet agent |
| ------ | ------ | ------ |
| hidden suite | 91 of 91 | 91 of 91 |
| own tests | 26, all passing | 150, all passing |
| seeded bugs caught by its own tests | 12 of 15 | 15 of 15 |
| survivors | negative weight not rejected; ties broken by weight; leftover handed out in index order | none |
| implementation | 42 lines, no docstrings | 88 lines, docstrings, a comment on why zero weights never get a cent |
| wall clock | 9 min 36 s plan to merge (lead 1 min, coder 5 min, reviewer 2 min 45 s) | 11 min 32 s |
| cost | free tiers: lead 6 calls, coder 14 calls (139k input tokens), reviewer 37 calls (933k input tokens) | about 155,600 tokens |
| independent review | yes (approved) | no |
| self-reported test count | said 28 passing, actual 26 (the reviewer repeated 28) | said 152 passing, actual 150 |

Caveats: one small task, one run per arm, and both arms reached the ceiling of the hidden suite, so this cannot
rank them on correctness. The agent got the full request; the swarm's coder got the lead's eleven acceptance
bullets, which kept every rule but dropped the worked example and the formulas. The swarm's reviewer has no
command tool, so its "all tests pass" is the coder's claim repeated, and it did not notice the thin tests.
