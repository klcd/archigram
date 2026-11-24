#!/usr/bin/env bash
# Enforce Conventional Commits with <=72-char subject line.
set -euo pipefail

msg_file="${1:?missing commit-msg file}"
subject="$(head -n 1 "$msg_file" | tr -d '\r')"

# Allow Git merge commits on main
if [[ "$subject" =~ ^Merge\  ]]; then
  exit 0
fi

if (( ${#subject} > 72 )); then
  echo "ERROR: Commit subject exceeds 72 characters (${#subject}): $subject" >&2
  exit 1
fi

pattern='^(feat|fix|docs|style|refactor|perf|fuzz|chore|build|ci)(\([a-z0-9_./-]+\))?!?: .+'
if ! [[ "$subject" =~ $pattern ]]; then
  echo "ERROR: Commit subject must follow Conventional Commits (e.g. 'feat(ingest_dot): normalize clusters'):" >&2
  echo "  Got: $subject" >&2
  exit 1
fi
