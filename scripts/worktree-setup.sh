#!/usr/bin/env bash
# Prepare a freshly created Git worktree for ArchiGram workers.
# Safe and idempotent to run repeatedly inside or outside Orca.
set -euo pipefail

worktree_root="${1:-$(git rev-parse --show-toplevel)}"
worktree_root="$(cd -- "$worktree_root" && pwd -P)"
cd "$worktree_root"

# Resolve main repository checkout (passed via $2 / ORCA_ROOT_PATH, or derived from git-common-dir)
source_checkout="${2:-${ORCA_ROOT_PATH:-}}"
if [[ -z "$source_checkout" ]]; then
  git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$git_common_dir" && "$(basename "$git_common_dir")" == ".git" ]]; then
    source_checkout="$(cd -- "$(dirname "$git_common_dir")" && pwd -P)"
  fi
fi

if [[ -n "$source_checkout" && -d "$source_checkout" ]]; then
  source_checkout="$(cd -- "$source_checkout" && pwd -P)"
fi

# 1. Copy local env overrides and share Beads Dolt state in linked worktrees
if [[ -n "$source_checkout" && "$source_checkout" != "$worktree_root" ]]; then
  for env_file in .env .env.local; do
    if [[ -f "$source_checkout/$env_file" && ! -e "$worktree_root/$env_file" ]]; then
      cp -f -- "$source_checkout/$env_file" "$worktree_root/$env_file"
    fi
  done

  mkdir -p "$worktree_root/.beads"
  for beads_item in dolt dolt-server.port; do
    if [[ -e "$source_checkout/.beads/$beads_item" && ! -e "$worktree_root/.beads/$beads_item" ]]; then
      ln -s "$source_checkout/.beads/$beads_item" "$worktree_root/.beads/$beads_item"
    fi
  done

  if [[ -d "$source_checkout/.agents/skills" && ! -d "$worktree_root/.agents/skills" ]]; then
    mkdir -p "$worktree_root/.agents"
    cp -rf -- "$source_checkout/.agents/skills" "$worktree_root/.agents/"
  fi
fi

# 2. Enforce 0700 permissions and Dolt port file for Beads
if [[ -d "$worktree_root/.beads" ]]; then
  chmod 700 "$worktree_root/.beads"
  if [[ -f "$worktree_root/.beads/metadata.json" && ! -e "$worktree_root/.beads/dolt-server.port" ]]; then
    printf '3306\n' > "$worktree_root/.beads/dolt-server.port"
  fi
fi

# 3. Enforce mandatory Git hooks (Beads + Hexagonal/Complexity/Type/Fuzz/Perf gates)
git config core.hooksPath .beads/hooks
chmod +x \
  "$worktree_root/scripts/worktree-setup.sh" \
  "$worktree_root/scripts/worktree-teardown.sh" \
  "$worktree_root/scripts/verify.sh" \
  "$worktree_root/scripts/commit-msg-check.sh" \
  "$worktree_root/scripts/check-test-discipline.py" \
  "$worktree_root/scripts/check-guardrails.py"

for hook in pre-commit commit-msg pre-push post-merge prepare-commit-msg; do
  if [[ -f "$worktree_root/.beads/hooks/$hook" ]]; then
    chmod +x "$worktree_root/.beads/hooks/$hook"
  fi
done

# 4. Provision local uv Python 3.12 environment and trust direnv (.envrc)
if [[ -f "$worktree_root/pyproject.toml" ]] && command -v uv >/dev/null 2>&1; then
  if [[ -f "$worktree_root/uv.lock" ]]; then
    uv sync --frozen --quiet
  else
    uv sync --quiet
  fi
fi

if [[ -f "$worktree_root/.envrc" ]] && command -v direnv >/dev/null 2>&1; then
  direnv allow "$worktree_root"
fi

echo "ArchiGram worktree ready at $worktree_root"
