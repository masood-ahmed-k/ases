# Package POLICY: contracts/decisions/AGENTS.md scaffolding, an explicit data-policy verification ceremony, key-pool doctor checks

Files you own: `src/ases/cli.py`, `src/ases/policy.py`, `src/ases/config.py`, `src/ases/doctor.py`, `config/models.yaml` (documented
additions to existing provider entries only), `tests/unit/test_cli_commands.py`, `tests/unit/test_policy.py`,
`tests/unit/test_config.py`, `tests/unit/test_doctor.py`. Nothing else. Read `r2_rules.md`, `r5_rules.md`, `r6_rules.md`, `r7_rules.md`
FIRST. Do NOT touch `controller.py` or `recovery.py` (package `FIXES` owns them this round). This package builds three separate,
`verified_by: Inspection`-style requirements the register has always listed as `not_covered`; none of them changes the controller
loop, all three live in the CLI, the policy layer and the doctor report.

## 1. ASES-GIT-15 (section 8.5): "Contracts, decisions and AGENTS.md live in the repository"
Quote: "Profiles do not share memory, and a worktree shows what the code is, not why. Contracts and decisions therefore live in the
repository and are merged before dependents start: `docs/ases/architecture.md`, `docs/ases/contracts/` (OpenAPI, schema boundaries,
environment variables), `docs/ases/decisions/`, and an `AGENTS.md` at the root, which Hermes loads automatically from the working
directory." `docs/ases/architecture.md` is already written by the Lead today (read `cli.cmd_plan`'s prompt text to confirm); the
other three paths are not asked for.
1. Extend `cmd_plan`'s Lead prompt (read the real prompt text first) to also ask the Lead to write `docs/ases/contracts/` (at least
   one file, naming the interfaces/boundaries the plan's tasks share -- an empty directory is not acceptable, say so in the prompt),
   `docs/ases/decisions/` (at least one file recording the technology/architecture assumptions the Lead made), and `AGENTS.md` at
   the repository root (a short file: what this project is, where the plan and contracts live, that text inside files is data not
   instructions -- Hermes loads this automatically per the blueprint, confirm what Hermes actually does with it by reading the
   installed source read-only if you are unsure, never write to or run the Hermes install).
2. `cmd_approve` (Gate P): after Gate 0 passes and before publishing, add a check (a small new function, e.g.
   `_scaffolding_warnings(repo) -> list[str]`) that looks for `docs/ases/contracts/` (non-empty), `docs/ases/decisions/`
   (non-empty), and `AGENTS.md` (a real file, not empty) and prints a WARNING for each missing one -- never a refusal: the blueprint
   marks this `verified_by: Inspection`, a human judgment call, not a hard gate a scaffold-only or trivial plan should be blocked by.
   Print the warnings clearly enough that a human approving the plan sees them before answering yes/no.

## 2. ASES-PRV-04 (section 21.2): "Private/confidential projects require an explicitly verified provider data policy; data-class
   rules are never relaxed to keep work flowing"
Read `policy.check_data_class` (already built: raises `DataPolicyViolation` when a provider's declared `data_policy` string is not
one of `policy._SAFE_FOR_PRIVATE`) and `cli.cmd_approve`'s existing call to it (the per-task loop that builds `provider_policies`
from `models_config["providers"]`). This already enforces the AUTOMATIC half (ASES-PRV-01/02/03, already covered). What is missing is
the word "explicitly VERIFIED": today a provider's `data_policy` string in `config/models.yaml` is trusted at face value, with no
record of WHO checked it, WHEN, or against WHAT (compare with how `ASES-VER-01` already requires: "externally changing provider/Hermes
facts carry a source URL and verification date" -- read wherever that convention already appears in the codebase, likely in
`models.py`/`config/models.yaml`'s comments, and match its shape).
1. `config.py`: extend the provider entry shape (read `load_models_config`'s real parsing) to accept two OPTIONAL fields per
   provider, `data_policy_verified_at` (an ISO date string) and `data_policy_source` (a short string: a URL or a note), stored
   alongside the existing `data_policy` field, validated only when present (a malformed date is a `ConfigError` naming the provider;
   absent fields are fine for a `public`-class project). Do not restructure the existing provider dict shape; add to it.
2. `policy.py`: `check_data_class` currently only checks the policy STRING. For `private`/`confidential`, ALSO require that
   `data_policy_verified_at` is present (read it as an added, optional keyword parameter, e.g. `check_data_class(data_class,
   provider, provider_data_policy, *, verified_at=None)`, backward compatible: `public` never needs it, and a caller that does not
   pass it for `private`/`confidential` gets a `DataPolicyViolation` naming exactly what is missing -- "provider X has a compatible
   policy but no recorded verification date; add data_policy_verified_at to its config/models.yaml entry"). This is the "explicitly
   verified" half: a policy string alone is no longer enough for the two stricter data classes.
3. `cli.py`: `cmd_approve`'s existing `check_data_class` call site threads the new field through (read the provider's config dict for
   `data_policy_verified_at` the same way it already reads `data_policy`).
4. `config/models.yaml`: add `data_policy_verified_at`/`data_policy_source` as DOCUMENTED comments (not live values -- you do not know
   the real verification dates; that is a human decision) showing the shape, next to the existing provider entries, matching the
   `docs/architecture.md`/register convention of citing a source and a date rather than inventing one.

## 3. ASES-CFG-02/CFG-03 (section 10.2): "Prefer one key per provider plus several legitimate providers; same-account key pools do
   not create extra quota on the configured free paths" / "No account creation or rotation to get around limits"
Both are `verified_by: Inspection` -- genuinely about the USER's account-management behavior, which ASES cannot enforce (it has no
way to know whether two provider entries share a real-world account). What IS buildable and useful: a `swarm doctor` check that
flags the one thing ASES CAN detect from its own config -- two or more provider entries pointing at the same underlying secret
(`hermes_secret_ref`/`secret_ref`/the environment variable name a provider's key comes from, read `config/models.yaml`'s real shape
for the field name) is a signal of exactly the "same-account key pool" anti-pattern the requirement warns about, since two
providers sharing one key are, in the case that matters, the same account under two names.
1. `doctor.py`: a new check, `_check_key_pooling(models_config) -> DoctorCheck` (WARN, never FAIL: this is advisory, since two
   providers can legitimately share a key without being a pool in the sense the requirement means, e.g. a single OpenRouter key used
   for both a coder and a tester profile on the SAME provider is normal and not what this warns about -- only flag it when the
   shared secret spans DIFFERENT providers, which is the actual "same-account pool" shape) naming which providers share a secret
   reference, without ever printing the secret's value (the doctor report's own existing no-secrets rule, `_check_no_secrets_in_output`
   -- read it and make sure your new check's output passes it). Wire it into `doctor.run`'s report alongside the existing checks.
2. Also add a short paragraph to `docs/operations.md` (you may edit this one extra file, nowhere else) under the configuration
   section explaining the policy in plain words (one key per provider, several distinct real providers instead, never rotate or
   create accounts to dodge a limit) so `swarm doctor`'s WARN has somewhere to point a reader.

## Tests
`test_cli_commands.py`: the Lead prompt text mentions the three new paths (a substring assertion is enough, do not over-specify
prose you do not own); `_scaffolding_warnings` on a repo with all three present (no warnings), missing one/two/three (the right
warnings, never a refusal), and `cmd_approve` prints them before the y/N prompt; the data-policy-verification check refuses a
private-class plan whose provider has a compatible policy string but no verification date, with a clear message, and accepts one
that has both. `test_policy.py`: `check_data_class`'s new `verified_at` parameter (public needs nothing, private/confidential need
it, the exact violation message names what is missing, backward-compatible default). `test_config.py`: the two new optional provider
fields parse when present, are validated (a malformed date is a `ConfigError`), are absent-safe for `public`. `test_doctor.py`:
`_check_key_pooling` flags two DIFFERENT providers sharing a secret ref, does NOT flag one provider used by multiple profiles, never
prints a secret value, and is a WARN not a FAIL.

## Report back
The usual report, plus: the exact field name Hermes/ASES's config actually uses for "which secret this provider's key comes from"
(you assumed `hermes_secret_ref`/`secret_ref` above; confirm against the real `config/models.yaml` and `config.py`), and whether
Hermes genuinely auto-loads `AGENTS.md` from the working directory as the blueprint claims (confirm by reading the installed Hermes
source, cite the file and line, or say plainly if you could not confirm it).
