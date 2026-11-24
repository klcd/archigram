#!/usr/bin/env python3
"""Enforce ArchiGram's strict Zero-Unit-Tests policy: ONLY Hypothesis fuzz & perf benchmarks."""

from __future__ import annotations

import ast
import pathlib
import sys

ALLOWED_DIRS = {"fuzz", "perf"}


def _has_hypothesis_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        dec_src = ast.unparse(dec)
        if any(tok in dec_src for tok in ("given(", "composite", "RuleBasedStateMachine")):
            return True
    return False


def _uses_benchmark_or_perf_budget(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    arg_names = {arg.arg for arg in node.args.args}
    if "benchmark" in arg_names:
        return True
    body_src = ast.unparse(node)
    return "perf_counter" in body_src or "tracemalloc" in body_src


def check_file(path: pathlib.Path, tests_root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    rel = path.relative_to(tests_root)
    parts = rel.parts
    if path.name in {"__init__.py", "conftest.py", "strategies.py"}:
        return errors

    if len(parts) < 2 or parts[0] not in ALLOWED_DIRS:
        errors.append(
            f"{rel}: test files MUST live inside tests/fuzz/ (fuzz_*.py) "
            "or tests/perf/ (perf_*.py); unit tests are strictly forbidden."
        )
        return errors

    category = parts[0]
    expected_prefix = f"{category}_"
    if not path.name.startswith(expected_prefix):
        errors.append(
            f"{rel}: file inside tests/{category}/ must be named '{expected_prefix}*.py'."
        )

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    test_funcs = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    ]
    state_machines = [
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef)
        and any("RuleBasedStateMachine" in ast.unparse(b) for b in n.bases)
    ]

    if not test_funcs and not state_machines:
        errors.append(f"{rel}: contains zero test_* functions or RuleBasedStateMachine classes.")
        return errors

    for fn in test_funcs:
        if category == "fuzz" and not _has_hypothesis_decorator(fn):
            errors.append(
                f"{rel}:{fn.lineno} '{fn.name}' in tests/fuzz/ MUST use Hypothesis @given(...) "
                "(static example unit tests are forbidden)."
            )
        if category == "perf" and not _uses_benchmark_or_perf_budget(fn):
            errors.append(
                f"{rel}:{fn.lineno} '{fn.name}' in tests/perf/ MUST accept 'benchmark' fixture "
                "or measure explicit latency/memory budgets."
            )
    return errors


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        print("ERROR: tests/ directory missing.", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    py_files = sorted(tests_root.rglob("*.py"))
    fuzz_files = [p for p in py_files if "fuzz" in p.parts and p.name.startswith("fuzz_")]
    perf_files = [p for p in py_files if "perf" in p.parts and p.name.startswith("perf_")]

    if not fuzz_files:
        all_errors.append("tests/fuzz/ must contain at least one fuzz_*.py Hypothesis suite.")
    if not perf_files:
        all_errors.append("tests/perf/ must contain at least one perf_*.py benchmark suite.")

    for py_file in py_files:
        all_errors.extend(check_file(py_file, tests_root))

    if all_errors:
        print("FAILED Test Discipline Check (No Unit Tests — Fuzz & Perf Only):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"Test discipline OK ({len(fuzz_files)} fuzz, {len(perf_files)} perf, 0 unit tests).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
