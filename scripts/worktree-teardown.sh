#!/usr/bin/env bash
# Clean up a Git worktree before archiving/removal in Orca.
# Safe to run repeatedly; never deletes real Dolt data in the main checkout.
set -euo pipefail

worktree_root="${1:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
if [[ ! -d "$worktree_root" ]]; then
  exit 0
fi
worktree_root="$(cd -- "$worktree_root" && pwd -P)"

# 1. Revoke direnv trust so stale entries do not accumulate
if [[ -f "$worktree_root/.envrc" ]] && command -v direnv >/dev/null 2>&1; then
  direnv deny "$worktree_root" >/dev/null 2>&1 || true
fi

# 2. Remove worktree-only Beads symlinks (ONLY if they are symlinks!)
for beads_link in "$worktree_root/.beads/dolt" "$worktree_root/.beads/dolt-server.port"; do
  if [[ -L "$beads_link" ]]; then
    rm -f -- "$beads_link"
  fi
done

# 3. Remove transient per-worktree caches & virtualenvs only on non-main linked worktrees
if [[ -f "$worktree_root/.git" ]]; then
  rm -rf -- \
    "$worktree_root/.agents" \
    "$worktree_root/.venv" \
    "$worktree_root/.direnv" \
    "$worktree_root/.pytest_cache" \
    "$worktree_root/.hypothesis" \
    "$worktree_root/.ruff_cache" \
    "$worktree_root/.benchmarks"
fi

echo "ArchiGram worktree torn down cleanly: $worktree_root"
