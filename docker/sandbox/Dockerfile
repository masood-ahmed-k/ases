# ases-sandbox:py311-2 (round 15, SANDBOXIMG then WORKERGIT; ASES-SEC-03 p385, ASES-QG-04 p279)
#
# The image the controller's own sandboxed gate runs (sandbox.docker_run_argv, gates.resolve_runner) and a worker's
# own Docker terminal backend use once a worker profile's terminal block names it. Small on purpose: git, a pinned
# pytest so "python -m pytest -q" needs no network install (ASES-SEC-05: the sandbox network is default-deny, so a
# gate that had to `pip install` its own test runner would fail closed), nothing else.
#
# Why py311-2 replaced py311-1 (2026-09-28): py311-1 was built on python:3.11-slim (Debian 13), whose newest git is
# 2.47.3, even in trixie-backports. WORKERGIT gives worker worktrees RELATIVE gitdir paths
# (worktree.useRelativePaths=true, so a container that mounts the worktree and the repository's .git can resolve
# them), and git 2.48 marks such a repository with extensions.relativeWorktrees, which git older than 2.48 refuses
# to open. So the image needs git 2.48 or later. The official python:3.11-alpine image (Alpine 3.24) ships git
# 2.54.0 and the same Python 3.11.16. Trade-off, stated plainly: Alpine uses musl, not glibc, so a project whose
# tests need glibc-only binary wheels would need a different image; pure-Python gates (this project's) are unaffected.
#
# Base pinned by content digest, not a mutable tag (sandbox._unpinned_reason and doctor's sandbox image check require
# a tag other than latest or an @sha256 digest). Digest read on 2026-09-28 with:
#   docker pull python:3.11-alpine
#   docker inspect python:3.11-alpine --format "{{json .RepoDigests}}"
# which returned python@sha256:cd04730b8511def3fbf14204d66a0c1536f290b8e896ed5a94cd64cb15ac1356.
FROM python:3.11-alpine@sha256:cd04730b8511def3fbf14204d66a0c1536f290b8e896ed5a94cd64cb15ac1356
# git pinned to the exact version apk policy showed for this base's Alpine 3.24 main repository on 2026-09-28.
# --no-cache keeps no package index in the layer. safe.directory: the worktree is bind-mounted at /workspace and is
# owned by a different user than the container's `sandbox` user, so git's "dubious ownership" guard refused every
# command there (found by scripts/workergit_live_check.py on 2026-09-28: "fatal: detected dubious ownership in
# repository at '/workspace'", exit 128). The guard protects a user from a repository planted by someone else; inside
# the sandbox the worker IS the untrusted party and the repository's .git/config is mounted read-only (WORKERGIT), so
# it protects nothing here. Exactly /workspace is trusted, not '*'.
RUN apk add --no-cache git=2.54.0-r0 \
    && git config --system --add safe.directory /workspace
# pytest pinned to this project's own venv version.
RUN pip install --no-cache-dir --no-compile pytest==9.1.1
# Container user (ASES-SEC-03: "the container running as the host user"). docker_run_argv passes --user only when
# sandbox.host_user_spec() returns one, and it returns None on native Windows (no os.getuid), so the container falls
# back to the IMAGE's default user; the official base's default is root. A fixed non-root user is not the same
# guarantee as "the host user" (a POSIX host still passes --user <uid>:<gid> and overrides this), but on native
# Windows it is strictly better than root. No home directory is needed; docker_run_argv always sets -w.
RUN adduser -D -H -u 1000 sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace
USER sandbox
# Note: the base image sets GPG_KEY, the PUBLIC fingerprint of the Python release manager's signing key (used to
# verify the CPython tarball when the base was built). It is not a credential; Docker cannot unset an inherited ENV,
# and no provider key is ever forwarded into this container (docker_run_argv forwards no environment at all).
