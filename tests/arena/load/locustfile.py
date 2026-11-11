"""Arena load test (Locust, open source).

Targets a running deployment seeded by tests/arena/load/seed.py.

    locust -f tests/arena/load/locustfile.py --headless -u 100 -r 10 -t 10m \
        --host https://127.0.0.1 --csv out/steady

Environment:
  ARENA_LOAD_HOST_HEADER  Host header to send (default: none), e.g. arena.test
  ARENA_LOAD_VERIFY       "0" to skip TLS verification (self-signed test certs)
  ARENA_LOAD_USERS        number of seeded accounts (default 150)
  ARENA_LOAD_SOLVE_RATE   probability that a submission is correct (default 0.3)

User classes (choose with Locust's class picker or `--class-picker`, or name
them on the command line):
  Participant     realistic player: browse, poll the scoreboard with ETags,
                  view challenges, submit wrong and right flags
  SubmitStorm     everyone hammers flag submissions (challenge release burst)
  ScoreboardWatch projector-style scoreboard polling with ETags
  InstanceBurst   requests a container instance, polls until running, stops it
"""

import itertools
import os
import random
import re
import threading
import warnings

from locust import HttpUser, between, constant, events, task

HOST_HEADER = os.environ.get("ARENA_LOAD_HOST_HEADER")
# Event slugs to use: ARENA_LOAD_PREFIX=load -> load-a, load-b, load-c
PREFIX = os.environ.get("ARENA_LOAD_PREFIX", "load")
VERIFY = os.environ.get("ARENA_LOAD_VERIFY", "1") != "0"
N_ACCOUNTS = int(os.environ.get("ARENA_LOAD_USERS", 150))
SOLVE_RATE = float(os.environ.get("ARENA_LOAD_SOLVE_RATE", 0.3))
PASSWORD = "LoadPass-123!"
API = "/api/v1/arena"
NONCE = re.compile(r"csrfNonce': \"([0-9a-f]+)\"")

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
_ids = itertools.count(1)
_lock = threading.Lock()
EVENTS = {}  # slug -> id, discovered once
COUNTERS = {"attempt_429": 0, "correct": 0, "incorrect": 0, "relogin": 0}


def next_account():
    with _lock:
        n = (next(_ids) - 1) % N_ACCOUNTS + 1
    return n, f"loaduser{n:03d}"


def events_for(n):
    slugs = []
    if n <= 100:
        slugs.append(f"{PREFIX}-a")
    if n <= 50:
        slugs.append(f"{PREFIX}-b")
    if n > 100:
        slugs.append(f"{PREFIX}-c")
    return slugs


@events.test_stop.add_listener
def _report(environment, **_):
    print("ARENA_COUNTERS", COUNTERS)


@events.test_start.add_listener
def _discover(environment, **_):
    import requests

    s = requests.Session()
    s.trust_env = False
    s.verify = VERIFY
    if HOST_HEADER:
        s.headers["Host"] = HOST_HEADER
    r = s.get(environment.host + API + "/events")
    data = r.json()["data"]
    rows = data if isinstance(data, list) else data.get("events", [])
    for e in rows:
        EVENTS[e["slug"]] = e["id"]
    missing = {f"{PREFIX}-a", f"{PREFIX}-b", f"{PREFIX}-c"} - set(EVENTS)
    if missing:
        raise RuntimeError(f"seed first: missing events {missing}")


class _Base(HttpUser):
    abstract = True

    def on_start(self):
        self.client.verify = VERIFY
        self.client.trust_env = False
        if HOST_HEADER:
            self.client.headers["Host"] = HOST_HEADER
        self.n, self.name_ = next_account()
        self.slugs = events_for(self.n)
        self.etags = {}
        self.challenges = {}
        self.solved = set()
        self._relogging = False
        original = self.client.request

        def request(method, url, *args, **kwargs):
            # Like a browser user: if the session is gone (401), log in again.
            # The 401 itself still counts as a failure in the statistics.
            resp = original(method, url, *args, **kwargs)
            if getattr(resp, "status_code", None) == 401 and not self._relogging:
                self._relogging = True
                try:
                    with _lock:
                        COUNTERS["relogin"] += 1
                    self.login()
                finally:
                    self._relogging = False
            return resp

        self.client.request = request
        self.login()

    def login(self):
        r = self.client.get("/login", name="GET /login")
        m = NONCE.search(r.text)
        with self.client.post(
            "/login",
            data={"name": self.name_, "password": PASSWORD, "nonce": m.group(1)},
            name="POST /login",
            allow_redirects=False,
            catch_response=True,
        ) as resp:
            if resp.status_code not in (302, 303):
                resp.failure(f"login {resp.status_code}")
                return
        r = self.client.get("/arena", name="GET /arena (page)")
        m = NONCE.search(r.text)
        self.client.headers["CSRF-Token"] = m.group(1) if m else ""

    def event(self):
        slug = random.choice(self.slugs)
        return slug, EVENTS[slug]

    def load_challenges(self, eid):
        r = self.client.get(f"{API}/events/{eid}/challenges", name="GET challenges")
        if r.ok:
            self.challenges[eid] = r.json()["data"]
            for c in self.challenges[eid]:
                if c.get("solved"):
                    self.solved.add((eid, c["id"]))
        return self.challenges.get(eid, [])

    def scoreboard(self, eid):
        headers = {}
        if eid in self.etags:
            headers["If-None-Match"] = self.etags[eid]
        with self.client.get(
            f"{API}/events/{eid}/scoreboard",
            headers=headers,
            name="GET scoreboard",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 304):
                if r.headers.get("ETag"):
                    self.etags[eid] = r.headers["ETag"]
                r.success()
            else:
                r.failure(f"scoreboard {r.status_code}")

    def submit(self, eid, correct=None):
        chals = self.challenges.get(eid) or self.load_challenges(eid)
        # only seeded "<slug>-chal-NN" challenges have predictable flags
        open_ = [
            c
            for c in chals
            if (eid, c["id"]) not in self.solved and re.search(r"-chal-\d+$", c["name"])
        ]
        if not open_:
            return
        c = random.choice(open_)
        slug = next(s for s, i in EVENTS.items() if i == eid)
        idx = int(c["name"].rsplit("-", 1)[1])
        right = random.random() < SOLVE_RATE if correct is None else correct
        flag = f"flag{{{slug}-{idx}}}" if right else f"flag{{nope-{random.random()}}}"
        with self.client.post(
            f"{API}/events/{eid}/challenges/{c['id']}/attempt",
            json={"submission": flag},
            name="POST attempt",
            catch_response=True,
        ) as r:
            if r.status_code == 200:
                status = r.json()["data"]["status"]
                if status in ("correct", "already_solved"):
                    self.solved.add((eid, c["id"]))
                with _lock:
                    COUNTERS["correct" if status == "correct" else "incorrect"] += 1
                if right and status not in ("correct", "already_solved"):
                    r.failure(f"correct flag rejected: {status}")
                else:
                    r.success()
            elif r.status_code == 429:
                r.success()  # expected per-account throttling, counted separately
                with _lock:
                    COUNTERS["attempt_429"] += 1
            else:
                r.failure(f"attempt {r.status_code}")


class Participant(_Base):
    wait_time = between(2, 6)

    @task(25)
    def poll_scoreboard(self):
        self.scoreboard(self.event()[1])

    @task(20)
    def browse_challenges(self):
        self.load_challenges(self.event()[1])

    @task(12)
    def view_challenge(self):
        _, eid = self.event()
        chals = self.challenges.get(eid) or self.load_challenges(eid)
        if chals:
            c = random.choice(chals)
            self.client.get(
                f"{API}/events/{eid}/challenges/{c['id']}", name="GET challenge"
            )

    @task(12)
    def submit_flag(self):
        self.submit(self.event()[1])

    @task(5)
    def my_score(self):
        self.client.get(f"{API}/events/{self.event()[1]}/my-score", name="GET my-score")

    @task(5)
    def announcements(self):
        self.client.get(
            f"{API}/events/{self.event()[1]}/announcements", name="GET announcements"
        )

    @task(4)
    def page(self):
        slug, _ = self.event()
        self.client.get(f"/arena/e/{slug}/challenges", name="GET event page (HTML)")

    @task(2)
    def history(self):
        self.client.get(
            f"{API}/events/{self.event()[1]}/scoreboard/history", name="GET history"
        )


class SubmitStorm(_Base):
    wait_time = between(0.5, 1.5)

    @task
    def storm(self):
        self.submit(self.event()[1])


class ScoreboardWatch(_Base):
    wait_time = constant(5)

    @task
    def watch(self):
        self.scoreboard(self.event()[1])


class InstanceBurst(_Base):
    """Each user starts one instance, waits for it, then stops it."""

    wait_time = between(5, 10)
    challenge_name = os.environ.get("ARENA_LOAD_INSTANCE_CHALLENGE", "box")

    @task
    def cycle(self):
        import time

        eid = EVENTS[f"{PREFIX}-a"]
        chals = self.load_challenges(eid)
        box = next((c for c in chals if c["name"] == self.challenge_name), None)
        if not box:
            return
        base = f"{API}/events/{eid}/challenges/{box['id']}/instance"
        t0 = time.time()
        r = self.client.post(base, json={}, name="POST instance")
        if r.status_code == 429:
            try:
                code = r.json().get("code", "unknown")
            except ValueError:
                code = "unknown"
            with _lock:
                COUNTERS[f"instance_429_{code}"] = (
                    COUNTERS.get(f"instance_429_{code}", 0) + 1
                )
        if r.status_code not in (200, 202):
            return
        status = None
        for _ in range(60):
            d = self.client.get(base, name="GET instance").json().get("data") or {}
            status = d.get("status")
            if status in ("running", "failed", "error"):
                break
            time.sleep(1)
        events.request.fire(
            request_type="INSTANCE",
            name=f"time to {status}",
            response_time=(time.time() - t0) * 1000,
            response_length=0,
            exception=None if status == "running" else RuntimeError(status),
            context={},
        )
        self.client.delete(base, json={}, name="DELETE instance")
