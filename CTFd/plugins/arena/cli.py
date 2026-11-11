"""``flask arena …`` operator commands.

flask arena migrate [--revision REV]
flask arena db-downgrade REV|base --yes
flask arena db-current
flask arena check-integrity
flask arena worker [--once] [--no-schedule]
flask arena worker-health [--max-age 60] [--host]
flask arena jobs stats|retry|purge|recover
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


@arena_cli.command("worker")
@click.option("--once", is_flag=True, help="Run a single iteration and exit.")
@click.option(
    "--poll", default=1.0, show_default=True, help="Idle poll interval (seconds)."
)
@click.option("--kinds", default=None, help="Comma-separated job kinds to process.")
@click.option(
    "--no-schedule", is_flag=True, help="Process jobs only; never run the scheduler."
)
def worker_cmd(once, poll, kinds, no_schedule):
    """Run the Arena background worker (graceful stop on SIGTERM/SIGINT)."""
    from flask import current_app

    from CTFd.plugins.arena.worker import Worker

    w = Worker(
        current_app._get_current_object(),
        poll_interval=poll,
        kinds=kinds.split(",") if kinds else None,
        schedule=not no_schedule,
    )
    w.install_signal_handlers()
    w.run(max_iterations=1 if once else None)


@arena_cli.command("worker-health")
@click.option(
    "--max-age", default=60, show_default=True, help="Heartbeat age limit (s)."
)
@click.option(
    "--host", "this_host", is_flag=True, help="Only count workers on this hostname."
)
def worker_health_cmd(max_age, this_host):
    """Exit 0 if at least one worker heartbeat is fresh (container healthcheck)."""
    import socket

    from CTFd.plugins.arena.worker import worker_health

    report = worker_health(max_age_seconds=max_age)
    workers = report["workers"]
    if this_host:
        prefix = socket.gethostname()[:40] + ":"
        workers = [w for w in workers if w["id"].startswith(prefix)]
    alive = [w for w in workers if w["heartbeat_age_s"] <= max_age]
    click.echo(json.dumps({"alive": len(alive), "workers": workers}, default=str))
    sys.exit(0 if alive else 1)


@arena_cli.command("jobs")
@click.argument("action", type=click.Choice(["stats", "retry", "purge", "recover"]))
@click.argument("job_id", required=False, type=int)
def jobs_cmd(action, job_id):
    """Inspect and manage background jobs."""
    from CTFd.plugins.arena.services import jobs

    if action == "stats":
        click.echo(json.dumps(jobs.stats(), indent=2))
    elif action == "retry":
        if job_id is None:
            raise click.UsageError("job_id required")
        click.echo("requeued" if jobs.retry_dead(job_id) else "not a dead job")
    elif action == "purge":
        click.echo(f"purged {jobs.purge_finished()}")
    elif action == "recover":
        click.echo(f"recovered {jobs.recover_stale()}")
