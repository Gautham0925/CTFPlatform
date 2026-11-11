"""Phase 6: DB-backed jobs and worker."""

import datetime
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from CTFd.models import db
from CTFd.plugins.arena.models import ArenaJob, ArenaLease, ArenaWorker
from CTFd.plugins.arena.services import jobs
from CTFd.plugins.arena.worker import Worker
from tests.arena.helpers import create_arena_ctfd
from tests.helpers import destroy_ctfd

CALLS = []


@jobs.handler("test.ok", max_attempts=3, lease_seconds=60)
def _ok(payload, job):
    CALLS.append(("ok", payload.get("n")))
    return {"n": payload.get("n")}


@jobs.handler("test.boom", max_attempts=2, lease_seconds=60)
def _boom(payload, job):
    CALLS.append(("boom", job.attempts))
    raise RuntimeError("kaboom")


@jobs.handler("test.slow", max_attempts=3, lease_seconds=60)
def _slow(payload, job):
    time.sleep(float(payload.get("sleep", 0.5)))
    CALLS.append(("slow", payload.get("n")))
    return {}


@jobs.handler("test.probe", max_attempts=1, lease_seconds=60)
def _probe(payload, job):
    from flask_sqlalchemy import get_debug_queries

    CALLS.append(("probe", len(get_debug_queries())))
    return {}


@pytest.fixture
def app():
    CALLS.clear()
    app = create_arena_ctfd()
    yield app
    destroy_ctfd(app)


def _server(app):
    return not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")


def test_enqueue_dedupe_and_unknown(app):
    with app.app_context():
        a = jobs.enqueue("test.ok", {"n": 1}, dedupe_key="k1")
        b = jobs.enqueue("test.ok", {"n": 2}, dedupe_key="k1")
        assert a.id == b.id and ArenaJob.query.count() == 1
        with pytest.raises(jobs.UnknownJob):
            jobs.enqueue("nope")


def test_queue_is_bounded(app):
    app.config["ARENA_JOBS_MAX_QUEUED"] = 3
    with app.app_context():
        for i in range(3):
            jobs.enqueue("test.ok", {"n": i})
        with pytest.raises(jobs.QueueFull):
            jobs.enqueue("test.ok", {"n": 9})


def test_claim_run_complete_and_dedupe_released(app):
    with app.app_context():
        jobs.enqueue("test.ok", {"n": 1}, dedupe_key="k")
        j = jobs.claim("w1")
        assert j.status == "running" and j.attempts == 1 and j.locked_by == "w1"
        assert jobs.claim("w2") is None  # nothing else runnable
        assert jobs.run_job(j, "w1") == "done"
        db.session.refresh(j)
        assert j.status == "done" and j.result == {"n": 1} and j.dedupe_key is None
        # the same key can be scheduled again after completion
        assert jobs.enqueue("test.ok", {"n": 2}, dedupe_key="k").id != j.id


def test_retry_backoff_then_dead_letter_and_manual_retry(app):
    with app.app_context():
        job = jobs.enqueue("test.boom")
        j = jobs.claim("w")
        assert jobs.run_job(j, "w") == "queued"
        db.session.refresh(job)
        assert job.attempts == 1 and "kaboom" in job.last_error
        assert job.run_after > datetime.datetime.utcnow()  # backoff
        assert jobs.claim("w") is None  # not yet runnable
        later = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
        j = jobs.claim("w", now=later)
        assert jobs.run_job(j, "w") == "dead"
        db.session.refresh(job)
        assert job.status == "dead" and job.finished_at is not None
        assert jobs.retry_dead(job.id) is True
        db.session.refresh(job)
        assert job.status == "queued" and job.attempts == 0


def test_backoff_grows_and_is_capped():
    delays = [jobs.backoff_seconds(n) for n in (1, 2, 3, 10, 20)]
    assert delays[0] < delays[1] < delays[2]
    assert all(d <= jobs.BACKOFF_CAP * 1.25 for d in delays)


def test_stale_job_recovery_and_lost_lease_cannot_complete(app):
    with app.app_context():
        jobs.enqueue("test.ok", {"n": 1})
        j = jobs.claim("crashed-worker")
        future = datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
        assert jobs.recover_stale(now=future) == 1
        db.session.refresh(j)
        assert j.status == "queued" and j.locked_by is None
        j2 = jobs.claim("w2", now=future)
        assert j2.id == j.id
        # The crashed worker comes back and tries to complete: rejected.
        assert jobs.complete(j, "crashed-worker") is False
        assert jobs.complete(j2, "w2") is True


def test_unknown_handler_goes_dead(app):
    with app.app_context():
        db.session.add(
            ArenaJob(
                kind="ghost",
                payload={},
                status="queued",
                attempts=0,
                max_attempts=3,
                priority=100,
            )
        )
        db.session.commit()
        j = jobs.claim("w")
        assert jobs.run_job(j, "w") == "dead"


def test_leader_lease_exclusive_and_expires(app):
    with app.app_context():
        now = datetime.datetime.utcnow()
        assert jobs.acquire_lease("scheduler", "a", 30, now=now) is True
        assert jobs.acquire_lease("scheduler", "b", 30, now=now) is False
        assert jobs.acquire_lease("scheduler", "a", 30, now=now) is True  # renew
        assert (
            jobs.acquire_lease(
                "scheduler", "b", 30, now=now + datetime.timedelta(seconds=31)
            )
            is True
        )
        jobs.release_lease("scheduler", "b")
        assert db.session.get(ArenaLease, "scheduler").holder is None


def test_scheduler_ticks_are_deduplicated(app):
    with app.app_context():
        w1, w2 = Worker(app), Worker(app)
        now = datetime.datetime.utcnow()
        assert w1.schedule_tick(now=now) is True
        assert w2.schedule_tick(now=now) is False  # not the leader
        assert w1.schedule_tick(now=now) is True  # same buckets → no duplicates
        from CTFd.plugins.arena.worker import PERIODIC

        kinds = sorted(k for (k,) in db.session.query(ArenaJob.kind))
        assert kinds == sorted(
            kind for _n, _i, kind, _p in PERIODIC
        )  # one per schedule


def test_worker_loop_processes_and_heartbeats(app):
    with app.app_context():
        for i in range(3):
            jobs.enqueue("test.ok", {"n": i})
    w = Worker(app, poll_interval=0.01, schedule=False)
    w.run(max_iterations=10)
    with app.app_context():
        assert sorted(n for k, n in CALLS if k == "ok") == [0, 1, 2]
        row = db.session.get(ArenaWorker, w.id)
        assert row.status == "stopped" and row.jobs_done >= 3


def test_jobs_survive_worker_restart(app):
    with app.app_context():
        for i in range(4):
            jobs.enqueue("test.ok", {"n": i})
    Worker(app, poll_interval=0.01, schedule=False).run(
        max_iterations=2
    )  # "crash" after 2
    Worker(app, poll_interval=0.01, schedule=False).run(max_iterations=10)  # new worker
    with app.app_context():
        assert ArenaJob.query.filter_by(kind="test.ok", status="done").count() == 4
        assert sorted(n for k, n in CALLS if k == "ok") == [0, 1, 2, 3]


def test_graceful_stop_finishes_current_job(app):
    with app.app_context():
        jobs.enqueue("test.slow", {"sleep": 0.5, "n": 1})
        jobs.enqueue("test.slow", {"sleep": 0.5, "n": 2})
    w = Worker(app, poll_interval=0.01, schedule=False)
    t = threading.Thread(target=w.run)
    t.start()
    time.sleep(0.2)  # first job is running
    w.stop_event.set()
    t.join(timeout=10)
    with app.app_context():
        statuses = sorted(j.status for j in ArenaJob.query.filter_by(kind="test.slow"))
        assert statuses == ["done", "queued"]  # current finished, next untouched
        assert db.session.get(ArenaWorker, w.id).status == "stopped"


def test_concurrent_workers_never_run_a_job_twice(app):
    if not _server(app):
        pytest.skip("needs MariaDB/MySQL")
    with app.app_context():
        for i in range(60):
            jobs.enqueue("test.ok", {"n": i})
    workers = [Worker(app, poll_interval=0.01, schedule=False) for _ in range(8)]
    threads = [
        threading.Thread(target=w.run, kwargs={"max_iterations": 40}) for w in workers
    ]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]
    ran = [n for k, n in CALLS if k == "ok"]
    assert sorted(ran) == list(range(60)), (len(ran), len(set(ran)))
    with app.app_context():
        assert ArenaJob.query.filter_by(kind="test.ok", status="done").count() == 60


def test_cli_worker_sigterm_graceful(app):
    """Real process: `flask arena worker` exits cleanly on SIGTERM."""
    if not _server(app):
        pytest.skip("needs a shared MariaDB/MySQL database")
    env = dict(
        os.environ,
        DATABASE_URL=app.config["SQLALCHEMY_DATABASE_URI"],
        SECRET_KEY="x",
        FLASK_APP="CTFd:create_app()",
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "flask", "arena", "worker", "--poll", "0.2"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        with app.app_context():
            while (
                time.time() < deadline
                and not ArenaWorker.query.filter_by(status="running").count()
            ):
                time.sleep(0.5)
                db.session.rollback()
            assert ArenaWorker.query.filter_by(status="running").count() == 1
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
        with app.app_context():
            db.session.rollback()
            assert ArenaWorker.query.filter_by(status="stopped").count() == 1
    finally:
        if p.poll() is None:
            p.kill()


def test_periodic_bucket_runs_once_even_after_completion(app):
    """Regression: finished periodic jobs clear dedupe_key; the scheduler must
    still not re-enqueue the same time bucket (that starved normal jobs)."""
    with app.app_context():
        w = Worker(app, poll_interval=0.01)
        # Pin to 1 s after an hour boundary so +31 s crosses only the 30 s
        # schedule's bucket (not the 60 s or hourly ones).
        hour = (int(time.time()) // 3600) * 3600
        now = datetime.datetime.utcfromtimestamp(hour + 1)
        w.schedule_tick(now=now)
        while (
            j := jobs.claim(w.id, now=now + datetime.timedelta(seconds=1))
        ) is not None:
            jobs.run_job(j, w.id)
        before = ArenaJob.query.count()
        w.schedule_tick(now=now)
        assert ArenaJob.query.count() == before
        from CTFd.plugins.arena.worker import PERIODIC

        later = now + datetime.timedelta(seconds=31)
        changed = sum(
            1
            for _n, interval, _k, _p in PERIODIC
            if int(later.timestamp()) // interval != int(now.timestamp()) // interval
        )
        w.schedule_tick(now=later)  # only schedules whose bucket changed run again
        assert ArenaJob.query.count() == before + changed


def test_scheduler_worker_still_processes_jobs_eventually(app):
    """A scheduling worker runs periodic jobs first, then normal work."""
    with app.app_context():
        jobs.enqueue("test.ok", {"n": 99})
    Worker(app, poll_interval=0.01).run(max_iterations=60)
    assert ("ok", 99) in CALLS


def test_worker_memory_is_bounded_in_debug_mode(app):
    """Regression (Phase 16 soak): the worker ran its whole loop in one app
    context; with app.debug, Flask-SQLAlchemy appended every query to a list on
    that context, so the scheduler leader grew ~60 MB per 30 min. Each
    iteration now gets a fresh context, so the recorded-query list resets."""
    app.debug = True
    try:
        with app.app_context():
            for _ in range(15):
                jobs.enqueue("test.probe", {})
        w = Worker(app, poll_interval=0.01, schedule=True)
        w.run(max_iterations=40)
    finally:
        app.debug = False
    seen = [n for k, n in CALLS if k == "probe"]
    assert len(seen) == 15
    # per-iteration context: what one iteration recorded, never a running total
    assert max(seen) < 60, seen
    assert seen[-1] <= seen[0] + 10, seen
