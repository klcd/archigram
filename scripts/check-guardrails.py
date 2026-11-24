#!/usr/bin/env python3
"""Prevent worker worktrees from tampering with architectural, lint, or hook guardrails."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

PROTECTED_PATHS = (
    "pyproject.toml",
    "orca.yaml",
    "scripts/verify.sh",
    "scripts/check-test-discipline.py",
    "scripts/check-guardrails.py",
    "scripts/commit-msg-check.sh",
    "scripts/worktree-setup.sh",
    "scripts/worktree-teardown.sh",
    ".beads/hooks/pre-commit",
    ".beads/hooks/commit-msg",
    ".beads/hooks/pre-push",
)


def main() -> int:
    if os.environ.get("ARCHIGRAM_ALLOW_GUARDRAIL_EDIT") == "1":
        return 0

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    # Only enforce tamper-protection inside linked worker worktrees (.git is a file in worktrees)
    git_marker = repo_root / ".git"
    if git_marker.is_dir():
        return 0

    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    staged = {line.strip() for line in res.stdout.splitlines() if line.strip()}
    violated = sorted(staged.intersection(PROTECTED_PATHS))
    if violated:
        print(
            "ERROR: Worker branches are forbidden from modifying repository guardrails:",
            file=sys.stderr,
        )
        for path in violated:
            print(f"  - {path}", file=sys.stderr)
        print(
            "Ask the orchestrator if a dependency or architectural contract needs updating.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
