"""Arena background worker process: ``flask arena worker``.

* registers itself in arena_workers and heartbeats;
* the leader (DB lease "scheduler") enqueues periodic jobs with time-bucket
  dedupe keys and recovers stale jobs;
* claims and runs jobs one at a time;
* SIGTERM/SIGINT → graceful stop: finish the current job, release the lease,
  mark the worker stopped.

Run exactly as many worker processes as you need (1 is enough at the target
scale); multiple workers are safe (claims and the leader lease are atomic).
"""

import datetime
import logging
import os
import signal
import socket
import threading
import time
import uuid

from CTFd.models import db
from CTFd.plugins.arena.models import ArenaWorker
from CTFd.plugins.arena.services import jobs

log = logging.getLogger("arena.worker")

# (name, interval_seconds, kind, payload_fn) — later phases append here.
PERIODIC = []
LEADER_LEASE = "scheduler"


def periodic(name, interval, kind, payload_fn=None):
    for item in PERIODIC:
        if item[0] == name:
            return
    PERIODIC.append((name, interval, kind, payload_fn or (lambda: {})))


class Worker:
    def __init__(
        self, app, poll_interval=1.0, lease_seconds=30, kinds=None, schedule=True
    ):
        self.app = app
        # schedule=False: process jobs only (never compete for the leader lease)
        self.schedule = schedule
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.kinds = kinds
        self.id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop_event = threading.Event()
        self.current_job = None

    # ----------------------------------------------------------- lifecycle
    def install_signal_handlers(self):
        def _stop(signum, _frame):
            log.info(
                "worker %s received signal %s; stopping after current job",
                self.id,
                signum,
            )
            self.stop_event.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

    def _register(self):
        from CTFd.plugins.arena import ARENA_VERSION

        db.session.merge(
            ArenaWorker(
                id=self.id,
                hostname=socket.gethostname()[:64],
                pid=os.getpid(),
                status="running",
                started_at=datetime.datetime.utcnow(),
                heartbeat_at=datetime.datetime.utcnow(),
                version=ARENA_VERSION,
            )
        )
        db.session.commit()

    def heartbeat(self, **counters):
        w = db.session.get(ArenaWorker, self.id)
        if w is None:
            self._register()
            w = db.session.get(ArenaWorker, self.id)
        w.heartbeat_at = datetime.datetime.utcnow()
        w.current_job_id = self.current_job.id if self.current_job else None
        for k, inc in counters.items():
            setattr(w, k, (getattr(w, k) or 0) + inc)
        db.session.commit()

    # ------------------------------------------------------------ leader
    def schedule_tick(self, now=None):
        """Leader only: enqueue due periodic jobs and recover stale jobs."""
        now = now or datetime.datetime.utcnow()
        if not jobs.acquire_lease(LEADER_LEASE, self.id, self.lease_seconds, now=now):
            return False
        recovered = jobs.recover_stale(now=now)
        if recovered:
            log.warning("recovered %s stale job(s)", recovered)
        epoch = int(now.timestamp())
        for name, interval, kind, payload_fn in PERIODIC:
            bucket = epoch // interval
            # Advance the schedule's bucket with a CAS; only the winner
            # enqueues, so each bucket runs once even across leader changes.
            if not jobs.advance_schedule(name, bucket):
                continue
            try:
                jobs.enqueue(
                    kind,
                    payload_fn(),
                    dedupe_key=f"periodic:{name}:{bucket}",
                    priority=50,
                )
            except jobs.QueueFull:
                log.error("queue full for periodic job %s", name)
            except Exception:  # noqa: BLE001 - one bad schedule must not stop others
                db.session.rollback()
                log.exception("could not enqueue periodic job %s", name)
        return True

    # -------------------------------------------------------------- loop
    def run_once(self):
        """One iteration: schedule (if leader) and run at most one job."""
        if self.schedule:
            self.schedule_tick()
        job = jobs.claim(self.id, kinds=self.kinds)
        if job is None:
            return None
        self.current_job = job
        self.heartbeat()
        status = jobs.run_job(job, self.id)
        self.current_job = None
        self.heartbeat(
            jobs_done=1 if status == "done" else 0,
            jobs_failed=0 if status == "done" else 1,
        )
        return status

    def run(self, max_iterations=None):
        # Each iteration runs in its OWN app context, and the DB session is
        # removed afterwards. A single long-lived context leaks: e.g. with
        # app.debug or TESTING, Flask-SQLAlchemy appends every query to a list
        # stored on the app context (found in the Phase 16 soak test).
        with self.app.app_context():
            self._register()
        log.info("arena worker %s started", self.id)
        iterations = 0
        last_beat = 0.0
        try:
            while not self.stop_event.is_set():
                with self.app.app_context():
                    try:
                        status = self.run_once()
                    except Exception:  # noqa: BLE001 - DB hiccup: back off, keep running
                        db.session.rollback()
                        log.exception("worker iteration failed")
                        status = None
                        self.stop_event.wait(min(10, self.poll_interval * 5))
                    if status is None and time.monotonic() - last_beat > 10:
                        try:
                            self.heartbeat()
                        except Exception:  # noqa: BLE001
                            db.session.rollback()
                        last_beat = time.monotonic()
                    db.session.remove()
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                if status is None:
                    self.stop_event.wait(self.poll_interval)
        finally:
            with self.app.app_context():
                try:
                    jobs.release_lease(LEADER_LEASE, self.id)
                    w = db.session.get(ArenaWorker, self.id)
                    if w is not None:
                        w.status, w.current_job_id = "stopped", None
                        db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
            log.info("arena worker %s stopped", self.id)


def worker_health(max_age_seconds=60):
    now = datetime.datetime.utcnow()
    rows = ArenaWorker.query.filter(ArenaWorker.status == "running").all()
    alive = [
        w for w in rows if (now - w.heartbeat_at).total_seconds() <= max_age_seconds
    ]
    return {
        "alive": len(alive),
        "workers": [
            {
                "id": w.id,
                "status": w.status,
                "heartbeat_age_s": int((now - w.heartbeat_at).total_seconds()),
                "jobs_done": w.jobs_done,
                "jobs_failed": w.jobs_failed,
                "current_job_id": w.current_job_id,
            }
            for w in rows
        ],
    }
