"""Regenerate the reference tables in docs/API_DOCUMENTATION.md and
docs/DATABASE_SCHEMA.md from the running application.

    python tests/arena/tools/gen_reference_docs.py ROUTES.md SCHEMA.md

ROUTES.md receives one markdown table row per Arena route and method;
SCHEMA.md receives one section per Arena table. Paste them into the docs.
"""

import inspect
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PREFIXES = ("/api/v1/arena", "/arena", "/plugins/arena")


def _auth(decos):
    for pattern, label in (
        (r"global_permission_required\(([^)]*)\)", "global: {}"),
        (r"event_permission_required\(([^)]*)\)", "event: {}"),
    ):
        m = re.search(pattern, decos)
        if m:
            return label.format(m.group(1))
    for needle, label in (
        ("event_participant_required", "participant"),
        ("authed_only", "login"),
        ("admins_only", "superadmin"),
        ("event_view", "event visible"),
    ):
        if needle in decos:
            return label
    return "public"


def route_rows(app):
    rows = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith(PREFIXES):
            continue
        view = inspect.unwrap(app.view_functions[rule.endpoint])
        methods = ",".join(
            sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        )
        decos = ""
        try:
            mod_src = inspect.getsource(sys.modules[view.__module__])
            m = re.search(r"((?:@[^\n]+\n)+)def " + view.__name__ + r"\(", mod_src)
            decos = m.group(1) if m else ""
        except (OSError, TypeError):
            pass
        rl = re.search(r"ratelimit\(([^)]*)\)", decos)
        limit = ""
        if rl:
            limit = "limit " + re.sub(r'method=|key_prefix="[^"]*"', "", rl.group(1))
            limit = limit.strip(" ,").replace(", ,", ",")
        doc = (inspect.getdoc(view) or "").split("\n")[0]
        rows.append((rule.rule, methods, _auth(decos), limit, doc))
    return sorted(rows)


def schema_sections(db, table_names):
    out = []
    for name in sorted(table_names):
        t = db.metadata.tables[name]
        out.append(
            f"### `{t.name}`\n\n| Column | Type | Null | Notes |\n|---|---|---|---|\n"
        )
        for c in t.columns:
            notes = []
            if c.primary_key:
                notes.append("PK")
            for fk in c.foreign_keys:
                notes.append(
                    f"FK → `{fk.target_fullname}` ({fk.ondelete or 'NO ACTION'})"
                )
            if c.index:
                notes.append("indexed")
            if c.unique:
                notes.append("unique")
            null = "yes" if c.nullable else "no"
            out.append(f"| `{c.name}` | {c.type} | {null} | {', '.join(notes)} |\n")
        cons = []
        for con in t.constraints:
            kind = type(con).__name__
            if kind == "UniqueConstraint":
                cols = ", ".join(col.name for col in con.columns)
                cons.append(f"UNIQUE `{con.name}` ({cols})")
            elif kind == "CheckConstraint":
                cons.append(f"CHECK `{con.name}`")
        for ix in t.indexes:
            if len(ix.columns) > 1 or ix.unique:
                cols = ", ".join(c.name for c in ix.columns)
                cons.append(
                    f"{'UNIQUE ' if ix.unique else ''}INDEX `{ix.name}` ({cols})"
                )
        if cons:
            out.append("\nConstraints: " + "; ".join(sorted(cons)) + "\n")
        out.append("\n")
    return "".join(out)


def main(routes_path, schema_path):
    sys.path.insert(0, ROOT)
    os.environ.setdefault("ARENA_CERTIFICATES_ENABLED", "true")
    from CTFd.models import db
    from CTFd.plugins.arena import dbmigrate
    from tests.arena.helpers import create_arena_ctfd

    app = create_arena_ctfd()
    rows = route_rows(app)
    with open(routes_path, "w") as f:
        for path, methods, auth, limit, doc in rows:
            path = path.replace("|", "\\|")
            f.write(
                f"| `{methods}` | `{path}` | {auth} | {limit} | {doc.replace('|', '/')} |\n"
            )
    with app.app_context():
        text = schema_sections(db, dbmigrate.arena_tables())
    with open(schema_path, "w") as f:
        f.write(text)
    print(f"{len(rows)} routes, {text.count('### ')} tables")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
