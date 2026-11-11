"""Static security rules for the Arena package (see docs/SECURITY_PLAN.md)."""

import pathlib
import re

ARENA = pathlib.Path(__file__).resolve().parents[2] / "CTFd" / "plugins" / "arena"
SOURCES = [p for p in ARENA.rglob("*.py") if "migrations" not in p.parts]


def _offenders(pattern):
    rx = re.compile(pattern)
    hits = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if rx.search(line):
                hits.append(f"{path.relative_to(ARENA)}:{n}: {line.strip()}")
    return hits


def test_sources_found():
    assert len(SOURCES) >= 8


def test_no_unscoped_primary_key_lookups():
    """`.query.get(id)` bypasses event scoping; use scoped_get_or_404."""
    assert _offenders(r"\.query\.get\(") == []


def test_no_string_built_sql():
    assert _offenders(r"text\(\s*f[\"']") == []
    assert _offenders(r"execute\(\s*f[\"']") == []
    assert _offenders(r"execute\([^)]*%\s*\(") == []


def test_no_csrf_bypass():
    assert _offenders(r"bypass_csrf_protection") == []


def test_no_request_supplied_event_id_in_guards():
    """Guards must not read event ids from query/body/form."""
    for name in ("authz.py", "context.py"):
        src = (ARENA / "core" / name).read_text()
        assert "request.args" not in src
        assert "request.form" not in src
        assert "get_json" not in src
        assert "request.values" not in src


def test_load_never_runs_migrations_on_servers():
    src = (ARENA / "__init__.py").read_text()
    body = src.split("def load(app):", 1)[1]
    assert "dbmigrate.upgrade" not in body
    assert "native_migrations" not in body
    assert "migrations.upgrade" not in body
