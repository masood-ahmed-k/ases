# Round 9 addendum to the shared rules (read `r8_rules.md` and the files it names, then this, then your package file)

Everything in `r8_rules.md` still applies (zero quota: never a real Hermes, a real model provider or Docker; never commit or push;
never a bare `git stash`; no em dash or section sign; Write/Edit for backslash-heavy files; quote requirement IDs from
`C:\Users\masoo\ases-workspaces\tools\blueprint.txt`, the source, and where it disagrees with the register the blueprint wins).

## Round 9 runs many builders at once, each in its OWN git worktree
- Package T2A (register only, no tests) works in the primary checkout `C:\Users\masoo\ases`. Every other package gets a
  pre-created worktree under `C:\Users\masoo\ases-wt\<package>` on branch `r9/<package>`. Work ONLY inside the path your dispatch
  names. Never touch another package's worktree or the primary checkout. The work-order files themselves live in the PRIMARY
  checkout (`C:\Users\masoo\ases\docs\work-orders\`) and are not in your worktree: read them from there.
- Round 8 (a credential-scrubbed environment for gate commands, in `src/ases/gates.py` and `src/ases/procenv.py`) is still being
  finished in the primary checkout while you work, so your branch may not contain it. That is expected; the architect merges.
- **Never use `git stash` at all this round, not even with a pathspec.** The stash is ONE list shared by every worktree of this
  repository, so another package's `stash pop` can take yours. For a before/after proof: copy your changed file to a temp path
  outside the repo, write the old version with `git show HEAD:<path> > <path>`, run the tests, then copy your version back and
  confirm with `git diff --stat` that the tree is exactly as you left it.
- **Imports.** The venv (`C:\Users\masoo\ases\.venv`) has ases installed in editable mode pointing at the PRIMARY checkout's `src`.
  pytest's own `pythonpath = ["src"]` puts YOUR worktree's `src` first, so always run tests as
  `C:/Users/masoo/ases/.venv/Scripts/python.exe -m pytest ...` from your worktree root. For any ad-hoc `python -c` or script, set
  `PYTHONPATH=src` from your worktree root first, or you will silently import the primary checkout's code. Prove it once at the
  start: `PYTHONPATH=src C:/Users/masoo/ases/.venv/Scripts/python.exe -c "import ases; print(ases.__file__)"` must print a path
  inside your worktree.
- **pytest temp directories.** Many full suites run at once this round and pytest's default shared temp base deletes other runs'
  directories. ALWAYS pass `--basetemp=C:/Users/masoo/ases-wt/_pytest/<package>` (a directory of your own, outside OneDrive; pytest
  clears it at start). Full suite, from your worktree root:
  `python C:/Users/masoo/.claude/scripts/quiet.py -l pytest -- C:/Users/masoo/ases/.venv/Scripts/python.exe -m pytest -q --tb=line
  --ignore=tests/integration/test_doctor_real_hermes.py --basetemp=C:/Users/masoo/ases-wt/_pytest/<package>`.
  Baseline: whatever the suite shows on your branch before you start (record it in your report); it must never go down.
- **Package boundaries.** Each package file lists the files you own. Other packages are editing other files of the same modules in
  parallel on their own branches, and the architect merges the branches afterwards, so keep your diff to what your package needs: no
  drive-by reformatting, no renames of things other packages might call, no edits to `spec/requirements.yaml`,
  `docs/architecture.md` or `docs/work-orders/` (the architect updates those after merging). If you need something outside your
  files, say so in your report.
- **Schema migrations.** If your package adds a database migration, say so in your report and use the next free version number on
  your branch; the architect renumbers at merge time if two packages both add one.
- Your final report is appended verbatim to `docs/work-orders/builder-findings.md`: what you changed and why, the requirement IDs
  with their quoted sentences, the exact test counts you observed (baseline and final), the before/after proof where the package
  asks for one, and anything you found but did not fix.
