"""Hierarchical symbol glob matching shared across gates, reducers, and triage."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["matches_any_glob", "matches_glob"]


def matches_glob(symbol_id: str, pattern: str) -> bool:
    """Match a symbol_id against a glob pattern with fnmatchcase and hierarchical '**' support."""
    if fnmatchcase(symbol_id, pattern):
        return True
    if "**" not in pattern:
        return False
    regex = _compile_hierarchical_glob(pattern)
    return bool(regex.fullmatch(symbol_id))


def matches_any_glob(symbol_id: str, patterns: Iterable[str]) -> bool:
    """Return True if symbol_id matches at least one glob pattern in patterns."""
    return any(matches_glob(symbol_id, pattern) for pattern in patterns)


@lru_cache(maxsize=512)
def _compile_hierarchical_glob(pattern: str) -> re.Pattern[str]:
    parts: list[str] = ["^"]
    idx = 0
    length = len(pattern)
    while idx < length:
        if pattern[idx : idx + 3] in {"**/", ".**"}:
            if pattern[idx : idx + 3] == "**/":
                parts.append("(?:.+/)?")
            else:
                parts.append(r"(?:\..+)?")
            idx += 3
        elif pattern[idx : idx + 2] == "**":
            parts.append(".*")
            idx += 2
        elif pattern[idx] == "*":
            parts.append(r"[^./:]*")
            idx += 1
        elif pattern[idx] == "?":
            parts.append(r"[^./:]")
            idx += 1
        else:
            parts.append(re.escape(pattern[idx]))
            idx += 1
    parts.append("$")
    return re.compile("".join(parts))
