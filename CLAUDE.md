# ASES project instructions

This file is loaded automatically by every Claude Code session working in this repository. See
`docs/architecture.md` for what's actually built; `ASES_Swarm_Implementation_Blueprint_v1.2.docx`
(`spec/requirements.yaml` = its Appendix F) is the authoritative source of requirements.

## Stop condition (ASES-DOC-04)

From the blueprint, section 16, verbatim: Claude Code MUST stop and ask the user before any action
that:

1. Spends money, including a one-time credit purchase.
2. Deletes user data.
3. Overwrites an existing repository or Hermes configuration.
4. Downloads and installs new software, such as the egress proxy.
5. Sends code from a private project to a provider outside its allow-list.
6. Needs a secret that was not already configured.

Everything else can be implemented automatically where safe.

This is a rule for whoever is *building and operating* ASES (originally Claude Code Sonnet, per the
blueprint's own preface), not a runtime behavior of the `ases` Python package itself -- there is no
corresponding module in `src/ases/`, by design. `spec/requirements.yaml`'s `verified_by: Inspection`
for this ID reflects that: it's checked by whether this instruction exists and gets followed, not by a
unit test.

Real precedent already exists in `docs/architecture.md`'s dated log: before pushing this repo to GitHub
(2026-09-19), the full git history and working tree were scanned for leaked credentials first, and only
after the user's explicit "you can push and store in this repo" (category 3/5: this is a private
repository being made public, and a provider-adjacent handoff). The lead role's move to a paid OpenAI
key, and later to xKiro, were both the user's explicit call (category 1: money), not a unilateral
change. Keep that standard: don't infer permission for these six categories from an adjacent, differently
-scoped instruction.
