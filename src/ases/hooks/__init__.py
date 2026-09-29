"""Hermes shell-hook scripts shipped inside the ASES package (round 19, package REVIEWLADDER).

Nothing here is imported by the rest of ASES: profiles.py only ever computes an absolute PATH to a script
under this directory (never `import ases.hooks.deny_tool`), because these scripts are meant to run as
STANDALONE processes Hermes spawns from a reviewer profile's config.yaml `hooks:` block, under whatever Python
that command line names. See deny_tool.py's module docstring for exactly what that means for imports.
"""
from __future__ import annotations
