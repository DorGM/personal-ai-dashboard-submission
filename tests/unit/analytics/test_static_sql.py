"""Static guarantees for SQL in app/analytics/ (ADR-005 §3.3 "Fixed SQL only").

- No SQL-shaped string is built with an f-string, `%`, `+` or `.format`.
- Every `execute`/`executemany` call's first argument is a module-level string constant, or a
  subscript of a module-level mapping whose values are all string constants (chosen by enum);
  `executescript` is never used.
- The deferred career tables are never named anywhere in the package.
- Every T6 query is user-scoped and excludes soft-deleted rows.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.analytics import tools

PACKAGE = Path(__file__).resolve().parents[3] / "app" / "analytics"
DEFERRED_TABLES = ("job_search_profile", "job_reports", "job_recommendations", "industry_trends")
SQL_SHAPE = re.compile(r"\bselect\b[\s\S]*\bfrom\b|\binsert\s+into\b|\bupdate\b[\s\S]*\bset\b|\bdelete\s+from\b"
                       r"|\bpragma\b|\battach\b", re.IGNORECASE)
EXECUTE_METHODS = {"execute", "executemany"}


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _text(node: ast.AST) -> str:
    """Literal text of a string node (f-string constant parts included)."""
    if _is_str(node):
        return str(node.value)  # type: ignore[attr-defined]
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) for v in node.values if isinstance(v, ast.Constant))
    return ""


def _constant_kind(value: ast.AST) -> str | None:
    """'str' for a string literal, 'map' for a dict (optionally in MappingProxyType) of string literals."""
    if _is_str(value):
        return "str"
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "MappingProxyType" \
            and len(value.args) == 1:
        value = value.args[0]
    if isinstance(value, ast.Dict) and value.values and all(_is_str(v) for v in value.values):
        return "map"
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and value is not None and (kind := _constant_kind(value)):
            constants[target.id] = kind
    return constants


def violations(source: str, name: str = "<src>") -> list[str]:
    """All fixed-SQL rule violations in one module's source."""
    tree = ast.parse(source)
    constants = _module_constants(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        line = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.JoinedStr) and SQL_SHAPE.search(_text(node)):
            found.append(f"{line}: f-string with SQL")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add) and any(
                SQL_SHAPE.search(_text(side)) for side in (node.left, node.right)):
            found.append(f"{line}: SQL built with % or +")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method == "format" and SQL_SHAPE.search(_text(node.func.value)):
                found.append(f"{line}: SQL built with .format")
            if method == "executescript":
                found.append(f"{line}: executescript is not allowed")
            if method in EXECUTE_METHODS:
                first = node.args[0] if node.args else None
                fixed = (isinstance(first, ast.Name) and constants.get(first.id) == "str") or (
                    isinstance(first, ast.Subscript) and isinstance(first.value, ast.Name)
                    and constants.get(first.value.id) == "map")
                if not fixed:
                    found.append(f"{line}: {method}() argument is not a module-level SQL constant")
    return found


# B-05 modules belong to the main workstream and carry their own SQL tests (parallel-work protocol §2).
MAIN_OWNED = {"metrics.py", "periods.py"}


def _package_files() -> list[Path]:
    files = sorted(p for p in PACKAGE.glob("*.py") if p.name not in MAIN_OWNED)
    assert files, "app/analytics has no modules"
    return files


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: p.name)
def test_package_uses_fixed_sql_only(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8"), path.name) == []


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: p.name)
def test_deferred_tables_are_never_named(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert [t for t in DEFERRED_TABLES if t in text] == []


@pytest.mark.parametrize("snippet", [
    'def f(c, t):\n    c.execute(f"SELECT * FROM {t}")\n',
    'def f(c, t):\n    c.execute("SELECT * FROM %s" % t)\n',
    'def f(c, t):\n    c.execute("SELECT * FROM x WHERE a = " + t)\n',
    'def f(c, t):\n    c.execute("SELECT * FROM {}".format(t))\n',
    'def f(c):\n    c.execute("SELECT 1 FROM x")\n',                       # inline literal, not a constant
    'def f(c, q):\n    c.execute(q)\n',
    'Q = make()\ndef f(c):\n    c.execute(Q)\n',
    'M = {"a": "SELECT 1 FROM x", "b": other}\ndef f(c, k):\n    c.execute(M[k])\n',
    'def f(c):\n    c.executescript("x")\n',
    'X = 1\ndef f(t):\n    return f"delete from {t}"\n',
])
def test_checker_catches_violations(snippet: str) -> None:
    assert violations(snippet)


def test_checker_accepts_constants_and_enum_keyed_maps() -> None:
    ok = ('Q = "SELECT 1 FROM x WHERE user_id = ?"\n'
          'M: Final = MappingProxyType({"a": "SELECT a FROM t", "b": ("SELECT b" " FROM t")})\n'
          'def f(c, k, uid):\n    c.execute(Q, (uid,))\n    c.execute(M[k], (uid,))\n'
          '    return f"Filter {k} is not allowed"\n')
    assert violations(ok) == []


def test_list_queries_are_user_scoped_and_skip_soft_deleted() -> None:
    from app.analytics.ports import DOMAINS
    for mapping in (tools._LIST_SQL, tools._COUNT_SQL):
        assert set(mapping) == set(DOMAINS)
        for sql in mapping.values():
            assert "r.user_id = ?" in sql and "r.deleted_at IS NULL" in sql and "BETWEEN ? AND ?" in sql
            assert "url" not in sql.lower()
    for domain, sql in tools._LIST_SQL.items():
        assert sql.rstrip().endswith("LIMIT ?")
        assert sql.count("?") == tools._COUNT_SQL[domain].count("?") + (4 if domain in tools._VALUE_SORT else 2) + 1


def test_earliest_queries_are_user_scoped_and_skip_soft_deleted() -> None:
    from app.analytics.ports import DOMAINS
    assert set(tools._EARLIEST_SQL) == set(DOMAINS)
    for sql in tools._EARLIEST_SQL.values():
        assert sql.startswith("SELECT MIN(") and sql.endswith("WHERE user_id = ? AND deleted_at IS NULL")
        assert sql.count("?") == 1
