"""``flask arena …`` operator commands.

flask arena migrate [--revision REV]
flask arena db-downgrade REV|base --yes
flask arena db-current
flask arena check-integrity
"""

import json
import sys

import click
from flask.cli import AppGroup

from CTFd.plugins.arena import dbmigrate, integrity

arena_cli = AppGroup("arena", help="Arena (multi-event) operator commands.")


@arena_cli.command("migrate")
@click.option("--revision", default=None, help="Target revision (default: head).")
def migrate(revision):
    """Apply Arena schema migrations. Back up the database first."""
    before = dbmigrate.current_revision()
    after = dbmigrate.upgrade(revision=revision)
    click.echo(f"arena schema: {before} -> {after} (head {dbmigrate.head_revision()})")


@arena_cli.command("db-downgrade")
@click.argument("revision")
@click.option("--yes", is_flag=True, help="Confirm. 'base' drops all Arena tables.")
def db_downgrade(revision, yes):
    """Downgrade the Arena schema to REVISION ('base' removes Arena tables)."""
    if not yes:
        click.echo("Refusing to downgrade without --yes (back up first).", err=True)
        sys.exit(2)
    before = dbmigrate.current_revision()
    after = dbmigrate.downgrade(revision)
    click.echo(f"arena schema: {before} -> {after}")


@arena_cli.command("db-current")
def db_current():
    """Show current and head Arena schema revisions."""
    ok, detail = dbmigrate.schema_state()
    click.echo(json.dumps({"up_to_date": ok, **detail}))


@arena_cli.command("check-integrity")
def check_integrity():
    """Run read-only integrity checks. Exit code 1 if any problem is found."""
    results = integrity.run_all()
    failed = False
    for name, problems in results.items():
        status = "OK" if not problems else "FAIL"
        click.echo(f"[{status}] {name}")
        for p in problems:
            click.echo(f"    - {p}")
        failed = failed or bool(problems)
    sys.exit(1 if failed else 0)
