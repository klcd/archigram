"""Native zero-dependency Python AST structural codebase ingestor."""

from __future__ import annotations

import ast
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from archigram.domain import ProvenanceGrade, ProvenanceStamp
from archigram.domain.ir import (
    ClassMember,
    EdgeRelation,
    IREdge,
    IRNode,
    NodeKind,
    StructuralGraphIR,
    SyncKind,
    Visibility,
)
from archigram.ports.contracts import StructuralIngestorPort

if TYPE_CHECKING:
    from collections.abc import Iterable

_EXTRACTOR_NAME = "python_ast_v1"
_SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git", "node_modules", "build", "dist"})
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag"})
_DECORATOR_MODIFIERS: dict[str, str] = {
    "staticmethod": "static",
    "classmethod": "classmethod",
    "property": "property",
    "abstractmethod": "abstract",
}
_CLASS_TARGET_KINDS = frozenset({NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM})
_CALL_TARGET_KINDS = frozenset(
    {NodeKind.FUNCTION, NodeKind.CLASS, NodeKind.INTERFACE, NodeKind.ENUM}
)
_MAX_SIG_LEN = 120
_MAX_REEXPORT_HOPS = 4


@dataclass(frozen=True, slots=True)
class _SourceFile:
    """Parsed Python source file with canonical relative POSIX path and dotted module ID."""

    rel_posix_path: str
    module_id: str
    is_package: bool
    raw_bytes: bytes
    source_text: str
    tree: ast.Module


@dataclass(frozen=True, slots=True)
class _ModuleIndex:
    """Per-module local definitions and resolved import bindings."""

    source_file: _SourceFile
    local_defs: dict[str, str]
    import_bindings: dict[str, str]
    dotted_imports: dict[str, str]
    imported_modules: tuple[tuple[str, tuple[int, int]], ...]


@dataclass(frozen=True, slots=True)
class _EdgeCandidate:
    """Raw extracted edge prior to deduplication and canonical sorting."""

    source: str
    target: str
    relation: EdgeRelation
    file_path: str
    line_span: tuple[int, int]


def _try_existing_path(source: str) -> Path | None:
    """Return Path if string refers to an existing filesystem path, else None."""
    if not source or "\n" in source or "\x00" in source or len(source) > 1024:
        return None
    candidate = Path(source)
    try:
        return candidate if candidate.exists() else None
    except OSError:
        return None


def _discover_py_files(root_dir: Path) -> list[Path]:
    """Deterministically discover .py files under root_dir, pruning ignored/hidden dirs."""
    discovered: list[Path] = []
    stack: list[Path] = [root_dir]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        subdirs: list[Path] = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIRS:
                continue
            if entry.is_dir():
                subdirs.append(entry)
            elif entry.is_file() and name.endswith(".py"):
                discovered.append(entry)
        stack.extend(reversed(subdirs))
    discovered.sort(key=lambda p: p.relative_to(root_dir).as_posix())
    return discovered


def _find_top_package_dir(start_dir: Path, boundary: Path | None) -> Path | None:
    """Walk upward while directories contain __init__.py to find the outermost package."""
    if not (start_dir / "__init__.py").is_file():
        return None
    current = start_dir
    while current.parent != current:
        if boundary is not None and current.parent == boundary:
            break
        if not (current.parent / "__init__.py").is_file():
            break
        current = current.parent
    return current


def _normalize_dist_name(name: str) -> str:
    """PEP 503-style normalization mapped to an importable identifier."""
    return re.sub(r"[-_.]+", "_", name.strip()).lower()


def _declared_import_names(data: dict[str, object]) -> list[str]:
    """Collect explicitly declared top-level import names from build-backend tables."""
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return []
    hatch: object = tool.get("hatch", {})
    wheel: object = {}
    if isinstance(hatch, dict):
        build: object = hatch.get("build", {})
        targets: object = build.get("targets", {}) if isinstance(build, dict) else {}
        wheel = targets.get("wheel", {}) if isinstance(targets, dict) else {}
    pkgs: object = wheel.get("packages", []) if isinstance(wheel, dict) else []
    return [Path(str(p)).name for p in pkgs] if isinstance(pkgs, list) else []


def _pyproject_root_package(pkg_dir: Path) -> str | None:
    """Derive the root package ID from a pyproject.toml beside or above ``pkg_dir``.

    Returns None when no pyproject is found, when ``pkg_dir`` is already a declared
    package of the project, or when no name can be derived (caller keeps the basename).
    """
    for candidate_dir in (pkg_dir, pkg_dir.parent):
        try:
            text = (candidate_dir / "pyproject.toml").read_text(encoding="utf-8")
            data: dict[str, object] = tomllib.loads(text)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        declared = _declared_import_names(data)
        if pkg_dir.name in declared:
            return None
        if len(declared) == 1:
            return declared[0]
        project = data.get("project")
        raw_name = project.get("name") if isinstance(project, dict) else None
        if isinstance(raw_name, str) and raw_name.strip():
            name = _normalize_dist_name(raw_name)
            return name if name.isidentifier() and name != pkg_dir.name else None
    return None


def _parts_to_module_id(rel_path: Path, fallback: str) -> str:
    """Convert a relative .py path into a canonical dotted module identifier."""
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    cleaned = [p for p in parts if p]
    return ".".join(cleaned) if cleaned else fallback


def _resolve_dir_module_id(
    file_path: Path, root_dir: Path, root_has_init: bool, root_alias: str | None = None
) -> str:
    """Resolve canonical dotted module name for a file inside a scanned directory."""
    if root_has_init:
        top_pkg = _find_top_package_dir(root_dir, None)
        base = top_pkg.parent if top_pkg is not None else root_dir.parent
        if root_alias is not None and top_pkg == root_dir:
            rel = file_path.relative_to(root_dir)
            return _parts_to_module_id(Path(root_alias) / rel, root_alias)
    else:
        top_pkg = (
            _find_top_package_dir(file_path.parent, root_dir)
            if file_path.parent != root_dir
            else None
        )
        base = top_pkg.parent if top_pkg is not None else root_dir
    rel = file_path.relative_to(base)
    fallback = file_path.parent.name if file_path.name == "__init__.py" else file_path.stem
    return _parts_to_module_id(rel, fallback or "module")


def _resolve_single_file(file_path: Path) -> tuple[str, str, bool]:
    """Resolve (rel_posix_path, module_id, is_package) for a single .py file."""
    is_pkg = file_path.name == "__init__.py"
    top_pkg = _find_top_package_dir(file_path.parent, None)
    if top_pkg is not None:
        rel = file_path.relative_to(top_pkg.parent)
        mod_id = _parts_to_module_id(rel, file_path.parent.name or "module")
        return rel.as_posix(), mod_id, is_pkg
    fallback = file_path.parent.name if is_pkg else file_path.stem
    return file_path.name, fallback or "module", is_pkg


def _parse_python_source(raw_bytes: bytes, rel_posix_path: str) -> tuple[str, ast.Module]:
    """Decode UTF-8 bytes and parse Python AST, raising ValueError on syntax error."""
    source_text = raw_bytes.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(source_text, filename=rel_posix_path)
    except SyntaxError as exc:
        msg = f"Failed to parse Python source '{rel_posix_path}': {exc}"
        raise ValueError(msg) from exc
    return source_text, tree


def _load_directory_sources(root_dir: Path) -> tuple[str, list[_SourceFile]]:
    """Load and parse all .py files in a directory with collision-safe dotted module names."""
    py_files = _discover_py_files(root_dir)
    root_has_init = (root_dir / "__init__.py").is_file()
    root_alias = _pyproject_root_package(root_dir) if root_has_init else None
    raw_items: list[tuple[Path, str, str, bool]] = []
    seen_counts: dict[str, int] = {}

    for path in py_files:
        rel_posix = path.relative_to(root_dir).as_posix()
        mod_id = _resolve_dir_module_id(path, root_dir, root_has_init, root_alias)
        is_pkg = path.name == "__init__.py"
        seen_counts[mod_id] = seen_counts.get(mod_id, 0) + 1
        raw_items.append((path, rel_posix, mod_id, is_pkg))

    sources: list[_SourceFile] = []
    for path, rel_posix, mod_id, is_pkg in raw_items:
        final_mod_id = (
            _parts_to_module_id(path.relative_to(root_dir), mod_id)
            if seen_counts[mod_id] > 1
            else mod_id
        )
        raw_bytes = path.read_bytes()
        source_text, tree = _parse_python_source(raw_bytes, rel_posix)
        sources.append(
            _SourceFile(
                rel_posix_path=rel_posix,
                module_id=final_mod_id,
                is_package=is_pkg,
                raw_bytes=raw_bytes,
                source_text=source_text,
                tree=tree,
            )
        )
    graph_id = root_dir.name or "python_project"
    return graph_id, sources


def _load_sources(source: Path | str) -> tuple[str, list[_SourceFile]]:
    """Normalize a directory path, single .py file, or inline source string."""
    target_path: Path | None = source if isinstance(source, Path) else _try_existing_path(source)
    if target_path is not None:
        if not target_path.exists():
            msg = f"Python source path does not exist: {target_path}"
            raise FileNotFoundError(msg)
        resolved = target_path.resolve()
        if resolved.is_dir():
            return _load_directory_sources(resolved)
        rel_posix, mod_id, is_pkg = _resolve_single_file(resolved)
        raw_bytes = resolved.read_bytes()
        source_text, tree = _parse_python_source(raw_bytes, rel_posix)
        return mod_id, [_SourceFile(rel_posix, mod_id, is_pkg, raw_bytes, source_text, tree)]

    if isinstance(source, Path):
        msg = f"Python source path does not exist: {source}"
        raise FileNotFoundError(msg)

    raw_bytes = source.encode("utf-8")
    source_text, tree = _parse_python_source(raw_bytes, "<inline>.py")
    return "inline", [_SourceFile("<inline>.py", "inline", False, raw_bytes, source_text, tree)]


def _compute_input_sha256(sources: list[_SourceFile]) -> str:
    """Compute deterministic SHA-256 digest over sorted (rel_posix_path, raw_bytes) pairs."""
    hasher = hashlib.sha256()
    for sf in sorted(sources, key=lambda item: item.rel_posix_path):
        hasher.update(sf.rel_posix_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(len(sf.raw_bytes).to_bytes(8, "big"))
        hasher.update(sf.raw_bytes)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _node_line_span(node: ast.AST) -> tuple[int, int]:
    """Return 1-indexed inclusive (start_line, end_line) span for an AST node."""
    start = max(1, int(getattr(node, "lineno", 1)))
    raw_end = getattr(node, "end_lineno", None)
    end = max(start, int(raw_end)) if isinstance(raw_end, int) else start
    return (start, end)


def _file_line_span(source_text: str) -> tuple[int, int]:
    """Return 1-indexed inclusive line span covering a full Python file."""
    return (1, max(1, len(source_text.splitlines())))


def _unwrap_subscript(expr: ast.expr) -> ast.expr:
    """Unwrap nested ast.Subscript wrappers (e.g., Generic[T] -> Generic)."""
    current = expr
    while isinstance(current, ast.Subscript):
        current = current.value
    return current


def _extract_dotted_name(expr: ast.expr) -> str | None:
    """Extract dotted identifier chain from ast.Name or nested ast.Attribute."""
    parts: list[str] = []
    current: ast.expr = expr
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return ".".join(parts)


def _truncate_signature(sig: str) -> str:
    """Bound member signature length deterministically for compact IR storage."""
    compact = " ".join(sig.split())
    if len(compact) <= _MAX_SIG_LEN:
        return compact
    return f"{compact[: _MAX_SIG_LEN - 3]}..."


def _is_stub_body(stmts: list[ast.stmt]) -> bool:
    """Return True if statements consist solely of docstrings, pass, or ellipsis stubs."""
    for stmt in stmts:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and (stmt.value.value is Ellipsis or isinstance(stmt.value.value, str))
        ):
            continue
        return False
    return True


def _is_stub_class(cls_node: ast.ClassDef) -> bool:
    """Return True if all methods in cls_node are abstract/ellipsis stubs."""
    methods = [s for s in cls_node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not methods:
        return _is_stub_body(cls_node.body)
    return all(_is_stub_body(m.body) for m in methods)


def _classify_class_kind(cls_node: ast.ClassDef) -> NodeKind:
    """Classify a top-level class into INTERFACE, ENUM, or CLASS from its base classes."""
    base_leaves: list[str] = []
    for base in cls_node.bases:
        dotted = _extract_dotted_name(_unwrap_subscript(base))
        if dotted is not None:
            base_leaves.append(dotted.rsplit(".", 1)[-1])
    if "Protocol" in base_leaves:
        return NodeKind.INTERFACE
    has_port_base = any(leaf.endswith(("Port", "Protocol")) for leaf in base_leaves)
    if has_port_base and (cls_node.name.endswith(("Port", "Protocol")) or _is_stub_class(cls_node)):
        return NodeKind.INTERFACE
    if any(leaf in _ENUM_BASES for leaf in base_leaves):
        return NodeKind.ENUM
    return NodeKind.CLASS


def _member_visibility(name: str) -> Visibility:
    """Map Python naming conventions to UML visibility markers."""
    if name == "__init__" or not name.startswith("_"):
        return Visibility.PUBLIC
    return Visibility.PRIVATE


def _is_skipped_dunder(name: str) -> bool:
    """Return True for dunder names other than __init__."""
    return len(name) > 4 and name.startswith("__") and name.endswith("__") and name != "__init__"


def _format_arg(arg: ast.arg) -> str:
    """Format a single function parameter with optional type annotation."""
    if arg.annotation is None:
        return arg.arg
    return f"{arg.arg}: {ast.unparse(arg.annotation)}"


def _format_func_signature(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    drop_self: bool,
) -> str:
    """Format concise function or method signature including parameter and return types."""
    args = fn_node.args
    pos_args = list(args.posonlyargs) + list(args.args)
    if drop_self and pos_args and pos_args[0].arg in {"self", "cls"}:
        pos_args = pos_args[1:]
    parts = [_format_arg(a) for a in pos_args]
    if args.vararg is not None:
        parts.append(f"*{_format_arg(args.vararg)}")
    parts.extend(_format_arg(a) for a in args.kwonlyargs)
    if args.kwarg is not None:
        parts.append(f"**{_format_arg(args.kwarg)}")
    sig = f"({', '.join(parts)})"
    if fn_node.returns is not None:
        sig = f"{sig} -> {ast.unparse(fn_node.returns)}"
    return _truncate_signature(sig)


def _extract_method_modifiers(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    """Collect normalized method modifiers from async keyword and standard decorators."""
    mods: list[str] = []
    if isinstance(fn_node, ast.AsyncFunctionDef):
        mods.append("async")
    for dec in fn_node.decorator_list:
        dotted = _extract_dotted_name(dec)
        if dotted is None:
            continue
        leaf = dotted.rsplit(".", 1)[-1]
        mapped = _DECORATOR_MODIFIERS.get(leaf)
        if mapped is not None and mapped not in mods:
            mods.append(mapped)
    return tuple(mods)


def _extract_class_members(cls_node: ast.ClassDef) -> tuple[ClassMember, ...]:
    """Extract annotated attributes and methods from a class definition body."""
    members: list[ClassMember] = []
    seen: set[tuple[str, bool]] = set()
    for stmt in cls_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if _is_skipped_dunder(name) or (name, False) in seen:
                continue
            seen.add((name, False))
            members.append(
                ClassMember(
                    name=name,
                    visibility=_member_visibility(name),
                    signature=_truncate_signature(ast.unparse(stmt.annotation)),
                    is_method=False,
                )
            )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = stmt.name
            if _is_skipped_dunder(name) or (name, True) in seen:
                continue
            seen.add((name, True))
            members.append(
                ClassMember(
                    name=name,
                    visibility=_member_visibility(name),
                    signature=_format_func_signature(stmt, drop_self=True),
                    is_method=True,
                    modifiers=_extract_method_modifiers(stmt),
                )
            )
    return tuple(members)


def _find_parent_module_id(module_id: str, module_ids: frozenset[str]) -> str | None:
    """Find nearest existing ancestor package/module in the discovered module set."""
    parts = module_id.split(".")
    for idx in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:idx])
        if candidate in module_ids:
            return candidate
    return None


def _make_provenance(
    input_sha256: str,
    file_path: str,
    line_span: tuple[int, int],
) -> ProvenanceStamp:
    """Create validated OBSERVED ProvenanceStamp for python_ast_v1."""
    return ProvenanceStamp(
        grade=ProvenanceGrade.OBSERVED,
        extractor=_EXTRACTOR_NAME,
        input_sha256=input_sha256,
        file_path=file_path,
        line_span=line_span,
    )


def _extract_file_nodes(
    sf: _SourceFile,
    module_ids: frozenset[str],
    input_sha256: str,
    *,
    include_private: bool,
) -> tuple[list[IRNode], dict[str, str]]:
    """Extract package/module, top-level class, and top-level function IRNodes for one file."""
    extracted: list[IRNode] = []
    local_defs: dict[str, str] = {}

    mod_kind = NodeKind.PACKAGE if sf.is_package else NodeKind.MODULE
    mod_label = sf.module_id.rsplit(".", 1)[-1]
    mod_parent = _find_parent_module_id(sf.module_id, module_ids)
    extracted.append(
        IRNode(
            symbol_id=sf.module_id,
            label=mod_label,
            kind=mod_kind,
            parent_id=mod_parent,
            provenance=_make_provenance(
                input_sha256, sf.rel_posix_path, _file_line_span(sf.source_text)
            ),
        )
    )

    for stmt in sf.tree.body:
        if isinstance(stmt, ast.ClassDef):
            cls_id = f"{sf.module_id}.{stmt.name}"
            local_defs[stmt.name] = cls_id
            extracted.append(
                IRNode(
                    symbol_id=cls_id,
                    label=stmt.name,
                    kind=_classify_class_kind(stmt),
                    parent_id=sf.module_id,
                    members=_extract_class_members(stmt),
                    provenance=_make_provenance(
                        input_sha256, sf.rel_posix_path, _node_line_span(stmt)
                    ),
                )
            )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not include_private and stmt.name.startswith("_"):
                continue
            fn_id = f"{sf.module_id}.{stmt.name}"
            local_defs[stmt.name] = fn_id
            attrs = {"signature": _format_func_signature(stmt, drop_self=False)}
            if isinstance(stmt, ast.AsyncFunctionDef):
                attrs["async"] = "true"
            extracted.append(
                IRNode(
                    symbol_id=fn_id,
                    label=stmt.name,
                    kind=NodeKind.FUNCTION,
                    parent_id=sf.module_id,
                    attributes=attrs,
                    provenance=_make_provenance(
                        input_sha256, sf.rel_posix_path, _node_line_span(stmt)
                    ),
                )
            )

    return extracted, local_defs


def _is_type_checking_name(node: ast.expr) -> bool:
    """Return True for ``TYPE_CHECKING`` and ``<alias>.TYPE_CHECKING`` references."""
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


def _runtime_if_branches(stmt: ast.If) -> list[ast.stmt]:
    """Return the runtime-reachable branches of an ``if``, dropping TYPE_CHECKING-only ones."""
    test = stmt.test
    if _is_type_checking_name(test):
        return stmt.orelse
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_type_checking_name(test.operand)
    ):
        return stmt.body
    return [*stmt.body, *stmt.orelse]


def _iter_module_imports(stmts: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    """Collect runtime imports, incl. conditional and function-local (lazy) ones.

    Imports guarded by ``if TYPE_CHECKING:`` are type-only and are skipped.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    for stmt in stmts:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            found.append(stmt)
        elif isinstance(stmt, ast.If):
            found.extend(_iter_module_imports(_runtime_if_branches(stmt)))
        elif isinstance(stmt, ast.Try):
            found.extend(_iter_module_imports(stmt.body))
            for handler in stmt.handlers:
                found.extend(_iter_module_imports(handler.body))
            found.extend(_iter_module_imports(stmt.orelse))
            found.extend(_iter_module_imports(stmt.finalbody))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.extend(_iter_module_imports(stmt.body))
    return found


def _resolve_relative_module(sf: _SourceFile, level: int, module: str | None) -> str | None:
    """Resolve an ast.ImportFrom target module name including relative level >= 1."""
    if level == 0:
        return module or ""
    anchor = sf.module_id.split(".") if sf.is_package else sf.module_id.split(".")[:-1]
    up = level - 1
    if up > len(anchor):
        return None
    base_parts = anchor[: len(anchor) - up]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(base_parts)


def _longest_known_module(dotted: str, module_ids: frozenset[str]) -> str | None:
    """Return longest dotted prefix of dotted that exists in module_ids."""
    parts = dotted.split(".")
    for idx in range(len(parts), 0, -1):
        candidate = ".".join(parts[:idx])
        if candidate in module_ids:
            return candidate
    return None


def _process_import_stmt(
    stmt: ast.Import,
    module_ids: frozenset[str],
    import_bindings: dict[str, str],
    dotted_imports: dict[str, str],
    imported_modules: list[tuple[str, tuple[int, int]]],
) -> None:
    """Record bindings and internal module import edges for an ast.Import statement."""
    span = _node_line_span(stmt)
    for alias in stmt.names:
        full_name = alias.name
        if alias.asname:
            import_bindings[alias.asname] = full_name
        else:
            head = full_name.split(".", 1)[0]
            import_bindings.setdefault(head, head)
            dotted_imports[full_name] = full_name
        target_mod = _longest_known_module(full_name, module_ids)
        if target_mod is not None:
            imported_modules.append((target_mod, span))


def _process_import_from_stmt(
    sf: _SourceFile,
    stmt: ast.ImportFrom,
    module_ids: frozenset[str],
    import_bindings: dict[str, str],
    imported_modules: list[tuple[str, tuple[int, int]]],
) -> None:
    """Record bindings and internal module import edges for an ast.ImportFrom statement."""
    target_mod = _resolve_relative_module(sf, stmt.level, stmt.module)
    if target_mod is None:
        return
    span = _node_line_span(stmt)
    for alias in stmt.names:
        if alias.name == "*":
            if target_mod in module_ids:
                imported_modules.append((target_mod, span))
            continue
        local_name = alias.asname or alias.name
        candidate_full = f"{target_mod}.{alias.name}" if target_mod else alias.name
        import_bindings[local_name] = candidate_full
        if candidate_full in module_ids:
            imported_modules.append((candidate_full, span))
        elif target_mod in module_ids:
            imported_modules.append((target_mod, span))


def _build_module_index(
    sf: _SourceFile,
    local_defs: dict[str, str],
    module_ids: frozenset[str],
) -> _ModuleIndex:
    """Parse all imports for a module into a fast resolution index."""
    import_bindings: dict[str, str] = {}
    dotted_imports: dict[str, str] = {}
    imported_modules: list[tuple[str, tuple[int, int]]] = []
    for stmt in _iter_module_imports(sf.tree.body):
        if isinstance(stmt, ast.Import):
            _process_import_stmt(
                stmt, module_ids, import_bindings, dotted_imports, imported_modules
            )
        else:
            _process_import_from_stmt(sf, stmt, module_ids, import_bindings, imported_modules)
    return _ModuleIndex(
        source_file=sf,
        local_defs=local_defs,
        import_bindings=import_bindings,
        dotted_imports=dotted_imports,
        imported_modules=tuple(imported_modules),
    )


def _dunder_all_value(stmt: ast.stmt) -> ast.expr | None:
    """Return the assigned value if stmt assigns to __all__, else None."""
    if isinstance(stmt, ast.Assign):
        targets: list[ast.expr] = list(stmt.targets)
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        targets = [stmt.target]
    else:
        return None
    if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
        return stmt.value
    return None


def _literal_dunder_all(tree: ast.Module) -> list[str] | None:
    """Return names from a literal module-level __all__ list/tuple, else None."""
    names: list[str] | None = None
    for stmt in tree.body:
        value = _dunder_all_value(stmt)
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        items = [e.value for e in value.elts if isinstance(e, ast.Constant)]
        if len(items) != len(value.elts) or not all(isinstance(i, str) for i in items):
            continue
        names = (names or []) + [i for i in items if isinstance(i, str)]
    return names


def _wildcard_public_names(target: _ModuleIndex) -> list[str]:
    """Names a wildcard import of target brings in (__all__ if literal, else non-underscore)."""
    known = {*target.local_defs, *target.import_bindings}
    exported = _literal_dunder_all(target.source_file.tree)
    if exported is not None:
        return [n for n in exported if n in known]
    return [n for n in known if not n.startswith("_")]


def _apply_wildcard_bindings(mod_indices: dict[str, _ModuleIndex]) -> None:
    """Bind names brought in by statically resolvable `from mod import *` statements."""
    for _ in range(_MAX_REEXPORT_HOPS):
        changed = False
        for idx in mod_indices.values():
            sf = idx.source_file
            for stmt in _iter_module_imports(sf.tree.body):
                if not isinstance(stmt, ast.ImportFrom):
                    continue
                if not any(alias.name == "*" for alias in stmt.names):
                    continue
                target_mod = _resolve_relative_module(sf, stmt.level, stmt.module)
                target = mod_indices.get(target_mod) if target_mod else None
                if target is None or target is idx:
                    continue
                for name in _wildcard_public_names(target):
                    if name not in idx.import_bindings:
                        idx.import_bindings[name] = f"{target_mod}.{name}"
                        changed = True
        if not changed:
            return


def _resolve_symbol_with_reexports(
    candidate: str,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> str | None:
    """Follow package re-exports up to _MAX_REEXPORT_HOPS to locate an internal IRNode."""
    current = candidate
    for _ in range(_MAX_REEXPORT_HOPS):
        if current in nodes:
            return current
        if "." not in current:
            return None
        mod_part, sym_name = current.rsplit(".", 1)
        mod_idx = mod_indices.get(mod_part)
        if mod_idx is None:
            return None
        redirected = mod_idx.import_bindings.get(sym_name)
        if redirected is None or redirected == current:
            return None
        current = redirected
    return current if current in nodes else None


def _resolve_unqualified_ref(
    name: str,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> str | None:
    """Resolve an unqualified identifier within a module's local and import scope."""
    local = idx.local_defs.get(name)
    if local is not None and local in nodes:
        return local
    bound = idx.import_bindings.get(name)
    if bound is None:
        return None
    return _resolve_symbol_with_reexports(bound, mod_indices, nodes)


def _resolve_dotted_ref(
    dotted: str,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> str | None:
    """Resolve a dotted identifier expression within a module's lexical and import scope."""
    if "." not in dotted:
        return _resolve_unqualified_ref(dotted, idx, mod_indices, nodes)

    direct = _resolve_symbol_with_reexports(dotted, mod_indices, nodes)
    if direct is not None:
        return direct

    head, rest = dotted.split(".", 1)
    bound_head = idx.import_bindings.get(head)
    if bound_head is not None:
        resolved = _resolve_symbol_with_reexports(f"{bound_head}.{rest}", mod_indices, nodes)
        if resolved is not None:
            return resolved

    for prefix, mapped in idx.dotted_imports.items():
        if dotted.startswith(f"{prefix}."):
            suffix = dotted[len(prefix) + 1 :]
            resolved = _resolve_symbol_with_reexports(f"{mapped}.{suffix}", mod_indices, nodes)
            if resolved is not None:
                return resolved
    return None


def _collect_subscript_annotation_refs(expr: ast.Subscript) -> list[str]:
    """Extract type references from subscripted annotations (stripping Annotated/Literal)."""
    base_name = _extract_dotted_name(expr.value)
    leaf = base_name.rsplit(".", 1)[-1] if base_name else ""
    if leaf == "Literal":
        return []
    if leaf == "Annotated":
        first_arg = expr.slice.elts[0] if isinstance(expr.slice, ast.Tuple) else expr.slice
        return _collect_annotation_refs(first_arg)
    refs = [base_name] if base_name is not None else []
    refs.extend(_collect_annotation_refs(expr.slice))
    return refs


def _collect_annotation_refs(expr: ast.expr) -> list[str]:
    """Extract all referenced type symbol dotted names from a Python type annotation AST."""
    result: list[str] = []
    if isinstance(expr, ast.Subscript):
        result = _collect_subscript_annotation_refs(expr)
    elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        result = _collect_annotation_refs(expr.left) + _collect_annotation_refs(expr.right)
    elif isinstance(expr, ast.Tuple):
        for elt in expr.elts:
            result.extend(_collect_annotation_refs(elt))
    elif isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            parsed = ast.parse(expr.value, mode="eval").body
        except SyntaxError:
            parsed = None
        if parsed is not None:
            result = _collect_annotation_refs(parsed)
    else:
        dotted = _extract_dotted_name(expr)
        if dotted is not None:
            result = [dotted]
    return result


def _iter_class_field_annotations(cls_node: ast.ClassDef) -> list[tuple[ast.expr, tuple[int, int]]]:
    """Yield (annotation_expr, line_span) for class body and __init__ self-attribute annotations."""
    found: list[tuple[ast.expr, tuple[int, int]]] = []
    for stmt in cls_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            found.append((stmt.annotation, _node_line_span(stmt)))
        elif isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__":
            for sub in ast.walk(stmt):
                if (
                    isinstance(sub, ast.AnnAssign)
                    and isinstance(sub.target, ast.Attribute)
                    and isinstance(sub.target.value, ast.Name)
                    and sub.target.value.id == "self"
                ):
                    found.append((sub.annotation, _node_line_span(sub)))
    return found


def _return_type_token(signature: str) -> str | None:
    """Extract normalized return type leaf token from a formatted method signature."""
    if "->" not in signature:
        return None
    ret = signature.rsplit("->", 1)[1].strip()
    if not ret or ret in {"object", "Any", "None"}:
        return None
    return ret.split("[", 1)[0].rsplit(".", 1)[-1].strip()


def _public_member_map(node: IRNode) -> dict[str, str | None]:
    """Map public non-dunder member names to optional return type tokens."""
    return {
        m.name: _return_type_token(m.signature)
        for m in node.members
        if m.visibility is Visibility.PUBLIC and m.name != "__init__"
    }


def _conforms_to_interface(
    cls_members: dict[str, str | None],
    iface_members: dict[str, str | None],
) -> bool:
    """Return True if cls_members satisfies all interface member names and return types."""
    if not iface_members:
        return False
    for name, expected_ret in iface_members.items():
        if name not in cls_members:
            return False
        actual_ret = cls_members[name]
        if expected_ret is not None and actual_ret is not None and expected_ret != actual_ret:
            return False
    return True


def _visible_interfaces_for_module(
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> list[IRNode]:
    """Return internal INTERFACE nodes defined in or imported by a module."""
    iface_ids: set[str] = set()
    for sym_id in idx.local_defs.values():
        node = nodes.get(sym_id)
        if node is not None and node.kind is NodeKind.INTERFACE:
            iface_ids.add(sym_id)
    for bound in idx.import_bindings.values():
        resolved = _resolve_symbol_with_reexports(bound, mod_indices, nodes)
        if resolved is not None and nodes[resolved].kind is NodeKind.INTERFACE:
            iface_ids.add(resolved)
    return [nodes[iid] for iid in sorted(iface_ids)]


def _extract_base_edges(
    cls_id: str,
    cls_node: ast.ClassDef,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> list[_EdgeCandidate]:
    """Extract explicit INHERITS / IMPLEMENTS edges from class base expressions."""
    file_path = idx.source_file.rel_posix_path
    cls_span = _node_line_span(cls_node)
    edges: list[_EdgeCandidate] = []
    for base in cls_node.bases:
        dotted = _extract_dotted_name(_unwrap_subscript(base))
        if dotted is None:
            continue
        target_id = _resolve_dotted_ref(dotted, idx, mod_indices, nodes)
        if target_id is None or target_id == cls_id:
            continue
        target_node = nodes[target_id]
        if target_node.kind not in _CLASS_TARGET_KINDS:
            continue
        relation = (
            EdgeRelation.IMPLEMENTS
            if target_node.kind is NodeKind.INTERFACE
            else EdgeRelation.INHERITS
        )
        edges.append(_EdgeCandidate(cls_id, target_id, relation, file_path, cls_span))
    return edges


def _extract_protocol_conformance_edges(
    cls_ir: IRNode,
    cls_span: tuple[int, int],
    file_path: str,
    visible_ifaces: list[IRNode],
) -> list[_EdgeCandidate]:
    """Detect structural Protocol conformance for concrete classes against visible interfaces."""
    if cls_ir.kind is not NodeKind.CLASS:
        return []
    cls_members = _public_member_map(cls_ir)
    edges: list[_EdgeCandidate] = []
    for iface in visible_ifaces:
        if iface.symbol_id != cls_ir.symbol_id and _conforms_to_interface(
            cls_members, _public_member_map(iface)
        ):
            edges.append(
                _EdgeCandidate(
                    cls_ir.symbol_id, iface.symbol_id, EdgeRelation.IMPLEMENTS, file_path, cls_span
                )
            )
    return edges


def _extract_composition_edges(
    cls_id: str,
    cls_node: ast.ClassDef,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> list[_EdgeCandidate]:
    """Extract COMPOSES edges from class field type annotations."""
    file_path = idx.source_file.rel_posix_path
    edges: list[_EdgeCandidate] = []
    for ann_expr, ann_span in _iter_class_field_annotations(cls_node):
        for ref_name in _collect_annotation_refs(ann_expr):
            target_id = _resolve_dotted_ref(ref_name, idx, mod_indices, nodes)
            if (
                target_id is not None
                and target_id != cls_id
                and nodes[target_id].kind in _CLASS_TARGET_KINDS
            ):
                edges.append(
                    _EdgeCandidate(cls_id, target_id, EdgeRelation.COMPOSES, file_path, ann_span)
                )
    return edges


def _extract_class_edges(
    cls_node: ast.ClassDef,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
    visible_ifaces: list[IRNode],
) -> list[_EdgeCandidate]:
    """Extract INHERITS, IMPLEMENTS (explicit & structural Protocol), and COMPOSES edges."""
    cls_id = f"{idx.source_file.module_id}.{cls_node.name}"
    cls_ir = nodes.get(cls_id)
    if cls_ir is None:
        return []
    file_path = idx.source_file.rel_posix_path
    cls_span = _node_line_span(cls_node)
    edges = _extract_base_edges(cls_id, cls_node, idx, mod_indices, nodes)
    edges.extend(_extract_protocol_conformance_edges(cls_ir, cls_span, file_path, visible_ifaces))
    edges.extend(_extract_composition_edges(cls_id, cls_node, idx, mod_indices, nodes))
    return edges


def _resolve_call_target(
    call_node: ast.Call,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> str | None:
    """Resolve an ast.Call target expression to an internal callable or class symbol_id."""
    dotted = _extract_dotted_name(call_node.func)
    if dotted is None:
        return None
    head = dotted.split(".", 1)[0]
    if head in {"self", "cls"}:
        return None
    target_id = _resolve_dotted_ref(dotted, idx, mod_indices, nodes)
    if target_id is not None and nodes[target_id].kind in _CALL_TARGET_KINDS:
        return target_id
    if "." in dotted:
        owner_dotted = dotted.rsplit(".", 1)[0]
        owner_id = _resolve_dotted_ref(owner_dotted, idx, mod_indices, nodes)
        if owner_id is not None and nodes[owner_id].kind in _CLASS_TARGET_KINDS:
            return owner_id
    return None


def _extract_calls_in_subtree(
    src_id: str,
    subtree: ast.AST,
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> list[_EdgeCandidate]:
    """Extract CALLS edges from all ast.Call expressions within a function or class AST."""
    file_path = idx.source_file.rel_posix_path
    edges: list[_EdgeCandidate] = []
    for node in ast.walk(subtree):
        if not isinstance(node, ast.Call):
            continue
        target_id = _resolve_call_target(node, idx, mod_indices, nodes)
        if target_id is not None and target_id != src_id:
            edges.append(
                _EdgeCandidate(
                    src_id, target_id, EdgeRelation.CALLS, file_path, _node_line_span(node)
                )
            )
    return edges


def _extract_module_edges(
    idx: _ModuleIndex,
    mod_indices: dict[str, _ModuleIndex],
    nodes: dict[str, IRNode],
) -> list[_EdgeCandidate]:
    """Extract IMPORTS, INHERITS, IMPLEMENTS, COMPOSES, and CALLS edges for one module."""
    sf = idx.source_file
    edges: list[_EdgeCandidate] = [
        _EdgeCandidate(sf.module_id, target_mod, EdgeRelation.IMPORTS, sf.rel_posix_path, span)
        for target_mod, span in idx.imported_modules
        if target_mod != sf.module_id and target_mod in nodes
    ]
    visible_ifaces = _visible_interfaces_for_module(idx, mod_indices, nodes)
    for stmt in sf.tree.body:
        if isinstance(stmt, ast.ClassDef):
            cls_id = f"{sf.module_id}.{stmt.name}"
            if cls_id in nodes:
                edges.extend(_extract_class_edges(stmt, idx, mod_indices, nodes, visible_ifaces))
                edges.extend(_extract_calls_in_subtree(cls_id, stmt, idx, mod_indices, nodes))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_id = f"{sf.module_id}.{stmt.name}"
            if fn_id in nodes:
                edges.extend(_extract_calls_in_subtree(fn_id, stmt, idx, mod_indices, nodes))
    return edges


def _deduplicate_edges(
    candidates: Iterable[_EdgeCandidate],
    nodes: dict[str, IRNode],
    input_sha256: str,
) -> tuple[IREdge, ...]:
    """Deduplicate, validate endpoints, and deterministically sort extracted edges."""
    best: dict[tuple[str, str, EdgeRelation], _EdgeCandidate] = {}
    for cand in candidates:
        if cand.source == cand.target:
            continue
        if cand.source not in nodes or cand.target not in nodes:
            continue
        key = (cand.source, cand.target, cand.relation)
        prev = best.get(key)
        if prev is None or (cand.file_path, cand.line_span) < (prev.file_path, prev.line_span):
            best[key] = cand

    ordered_keys = sorted(best, key=lambda k: (k[0], k[1], k[2].value))
    return tuple(
        IREdge(
            edge_id=f"e:{src}->{tgt}:{rel.value}",
            source=src,
            target=tgt,
            relation=rel,
            sync_kind=SyncKind.SYNC,
            provenance=_make_provenance(
                input_sha256, best[(src, tgt, rel)].file_path, best[(src, tgt, rel)].line_span
            ),
        )
        for src, tgt, rel in ordered_keys
    )


class PythonAstStructuralIngestor(StructuralIngestorPort):
    """Zero-dependency native Python AST ingestor producing Canonical StructuralGraphIR."""

    def __init__(self, *, include_private: bool = False) -> None:
        self._include_private = include_private

    @property
    def extractor_name(self) -> str:
        return _EXTRACTOR_NAME

    def ingest(self, source: Path | str) -> StructuralGraphIR:
        """Ingest a Python directory, single .py file, or inline Python source string."""
        graph_id, sources = _load_sources(source)
        input_sha256 = _compute_input_sha256(sources)
        module_ids = frozenset(sf.module_id for sf in sources)

        raw_nodes: dict[str, IRNode] = {}
        local_defs_by_mod: dict[str, dict[str, str]] = {}
        for sf in sources:
            file_nodes, local_defs = _extract_file_nodes(
                sf, module_ids, input_sha256, include_private=self._include_private
            )
            local_defs_by_mod[sf.module_id] = local_defs
            for node in file_nodes:
                raw_nodes.setdefault(node.symbol_id, node)

        sorted_nodes = {key: raw_nodes[key] for key in sorted(raw_nodes)}
        mod_indices = {
            sf.module_id: _build_module_index(sf, local_defs_by_mod[sf.module_id], module_ids)
            for sf in sources
        }
        _apply_wildcard_bindings(mod_indices)

        all_candidates: list[_EdgeCandidate] = []
        for mod_id in sorted(mod_indices):
            all_candidates.extend(
                _extract_module_edges(mod_indices[mod_id], mod_indices, sorted_nodes)
            )

        edges = _deduplicate_edges(all_candidates, sorted_nodes, input_sha256)
        return StructuralGraphIR(
            graph_id=graph_id,
            nodes=sorted_nodes,
            edges=edges,
            metadata={
                "extractor": _EXTRACTOR_NAME,
                "input_sha256": input_sha256,
                "file_count": str(len(sources)),
            },
        )
