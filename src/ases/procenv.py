"""The environment ASES starts a subprocess with (ASES-CFG-05; ASES-SEC-01 for model-written code).

Blueprint 10.2: "Never export provider keys in the shell that launches the gateway or the controller". Nothing stops
a person doing it anyway, so every process ASES starts on Hermes's behalf (hermes.py's `_run`, which every hermes
subcommand goes through, and the evaluation harness's one-shot call in evals.py) and every model-written program
evalkit.codeeval runs gets a copy of the current environment with the credential-shaped variables removed. This is
the project's ONE definition of "credential-shaped", so the callers never drift apart.

Its own module, and not a public function of hermes.py, on purpose: hermes.py's public names are the Hermes call
surface, which ases.fakes.board.FakeHermes replaces wholesale in tests (and test_fakes checks that every public
function there has a fake), and an environment scrub is not a Hermes call. Free of ASES imports, so the lowest
module that starts a process can import it.
"""
from __future__ import annotations

import os
import re

_CREDENTIAL_ENV = re.compile(r"(key|token|secret|passw|credential|auth|cookie|session)", re.IGNORECASE)


def scrubbed_environ() -> dict[str, str]:
    """A copy of the current environment without any variable whose name looks like a credential (key, token,
    secret, passw, credential, auth, cookie, session; case does not matter). Everything else (PATH, SYSTEMROOT, the
    profile and temp directories, Python's own settings) is kept exactly as it is, so a subprocess launches as it
    did before; a caller adds what it needs on top of the copy."""
    return {name: value for name, value in os.environ.items() if not _CREDENTIAL_ENV.search(name)}
