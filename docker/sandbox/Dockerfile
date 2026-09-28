# ases-sandbox:py311-1 (SANDBOXIMG, round 15, ASES-SEC-03 p385, ASES-QG-04 p279)
#
# The image the controller's own sandboxed gate runs (sandbox.docker_run_argv, gates.resolve_runner) and, once a
# worker profile's terminal block names it, a worker's own Docker terminal backend use. Small on purpose: git for
# a self-contained checkout's own commands, a pinned pytest so "python -m pytest -q" (ASES-QG-04's "clean checkout
# ... inside the sandbox") needs no network install (ASES-SEC-05: the sandbox network is default-deny, so a gate
# that had to `pip install` its own test runner would fail closed), nothing else.
#
# Base: the official Python 3.11 slim image, pinned by content digest, not a mutable tag (sandbox._unpinned_reason
# and doctor's sandbox_image_configured check both require this same shape: a tag other than latest, or an
# @sha256 digest). Digest read on 2026-09-28 with:
#   docker pull python:3.11-slim
#   docker inspect python:3.11-slim --format "{{json .RepoDigests}}"
# which returned python@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e (recorded in
# scripts/sandbox_live_check.py's own report and the builder's findings, not just here, so the exact pull that
# produced it stays reproducible). Built as ases-sandbox:py311-1 on 2026-09-28; the resulting image ID
# (docker image inspect ases-sandbox:py311-1 --format "{{.Id}}") was
# sha256:891602d5488a35388fb34ee9074694c131f07a6f3abb53fdf61b9b870e6075fd.
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

# git: gates that run inside a self-contained checkout (gates._self_contained_checkout) may still need it for a
# command that itself calls git (for example a test that reads git metadata); the checkout arrives as a real
# .git directory with no path back to the host repository (round 9, GATESANDBOX), so having git present costs
# nothing in isolation and avoids a silent "git: not found" turning an unrelated gate command red.
# --no-install-recommends and an apt cache purge keep the image small; apt-get update runs only in this one
# layer, right before the installs it feeds, so a rebuild always sees current package metadata for the pin below.
# Version pinned to the candidate apt-cache policy showed for this exact base digest on 2026-09-28 (Debian 13
# "trixie", the base image's own OS release): 1:2.47.3-0+deb13u1. A future rebuild of this Dockerfile from the
# same pinned FROM digest will see the same candidate, since the base image never changes under a fixed digest.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned pytest, matching the version this project's own venv runs (pytest 9.1.1, checked 2026-09-28 with
# ".venv/Scripts/python.exe -m pytest --version"), so "python -m pytest -q" behaves the same whether a gate runs
# on the host runner or the sandboxed one. --no-cache-dir keeps the image small; no other package is installed,
# so a gate command that needs anything else must be a project dependency committed to the checkout itself.
RUN pip install --no-cache-dir --no-compile pytest==9.1.1

# Container user (ASES-SEC-03: "the container running as the host user"). docker_run_argv (sandbox.py) passes
# --user only when sandbox.host_user_spec() returns one, and that function returns None on native Windows: there
# is no os.getuid there, so ASES has no host UID to pass and the container falls back to the IMAGE's own default
# user (sandbox.py's module docstring, item 3, and the ASES-SEC-03 register note both already say this plainly:
# "docker_run_as_host_user does nothing on native Windows"). The official python:3.11-slim image's own default
# user is root, so leaving this unset would mean every sandboxed run on this machine is root today. A fixed
# non-root user baked into the image is not the same guarantee as "the host user" (a real POSIX host still gets
# --user <uid>:<gid> from host_user_spec and overrides this at `docker run` time), but on native Windows, where
# that path is unreachable, it is strictly better than the image's own root default: a worker or a gate command
# cannot chown/chmod system files, bind low ports, or otherwise rely on UID 0 inside the container, only inside
# its own /workspace mount (owned by this user below) and /tmp. useradd -m is skipped (no home directory is
# needed; docker_run_argv always sets -w /workspace).
RUN useradd --no-create-home --uid 1000 --user-group sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace
USER sandbox
