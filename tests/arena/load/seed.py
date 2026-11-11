"""Seed load-test data into a running Arena deployment (idempotent).

Run INSIDE a platform container (it uses the app's own services):
    docker compose ... exec -T ctfd python /opt/CTFd/tests/arena/load/seed.py
or locally:  DATABASE_URL=... python tests/arena/load/seed.py

Creates events <LOAD_PREFIX>-a/-b/-c (default prefix "load") (individual Jeopardy, active), 20
challenges each (flag{<slug>-<n>}), users loaduser001..loaduser{N} with
password LoadPass-123!, and registrations:
  1..100 -> load-a, 1..50 -> load-b, 101..N -> load-c.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../../..")

from CTFd import create_app  # noqa: E402

N_USERS = int(os.environ.get("LOAD_USERS", 150))
PREFIX = os.environ.get("LOAD_PREFIX", "load")
PASSWORD = "LoadPass-123!"


def main():
    app = create_app()
    with app.app_context():
        from CTFd.models import Users, db
        from CTFd.plugins.arena.models import (
            ArenaEvent,
            ArenaEventChallenge,
            ArenaRegistration,
        )
        from CTFd.plugins.arena.services import challenges as chal_service
        from CTFd.plugins.arena.services import events as event_service
        from CTFd.plugins.arena.services import registration as reg_service

        admin = Users.query.filter_by(type="admin").order_by(Users.id).first()
        assert admin, "complete /setup first"
        now = datetime.datetime.utcnow()
        events = {}
        for slug in (f"{PREFIX}-a", f"{PREFIX}-b", f"{PREFIX}-c"):
            ev = ArenaEvent.query.filter_by(slug=slug).first()
            if ev is None:
                ev = event_service.create_event(
                    {
                        "slug": slug,
                        "name": f"{PREFIX.title()} {slug[-1].upper()}",
                        "type": "jeopardy",
                        "visibility": "public",
                        "team_mode": "individual",
                        "registration_open": True,
                        "starts_at": (now - datetime.timedelta(minutes=10)).isoformat()
                        + "Z",
                        "ends_at": (now + datetime.timedelta(days=2)).isoformat() + "Z",
                    },
                    admin,
                )
                event_service.transition(ev, "upcoming")
                event_service.transition(ev, "active")
                db.session.commit()
            events[slug] = ev
            have = ArenaEventChallenge.query.filter_by(event_id=ev.id).count()
            for i in range(have, 20):
                data = {
                    "name": f"{slug}-chal-{i:02d}",
                    "category": ("web", "crypto", "pwn", "forensics")[i % 4],
                    "description": f"Load challenge {i}",
                    "points": 100 + 25 * (i % 8),
                    "visible": True,
                    "flags": [{"type": "static", "content": f"flag{{{slug}-{i}}}"}],
                }
                if i % 5 == 0:
                    data.update(
                        {
                            "type": "dynamic",
                            "minimum_points": 50,
                            "decay": 50,
                            "decay_function": "linear",
                        }
                    )
                chal_service.create(ev, data)
                db.session.commit()
        created = 0
        for n in range(1, N_USERS + 1):
            name = f"loaduser{n:03d}"
            u = Users.query.filter_by(name=name).first()
            if u is None:
                u = Users(
                    name=name,
                    email=f"{name}@load.test",
                    password=PASSWORD,
                    verified=True,
                )
                db.session.add(u)
                db.session.commit()
                created += 1
            targets = []
            if n <= 100:
                targets.append(f"{PREFIX}-a")
            if n <= 50:
                targets.append(f"{PREFIX}-b")
            if n > 100:
                targets.append(f"{PREFIX}-c")
            for slug in targets:
                ev = events[slug]
                if not ArenaRegistration.query.filter_by(
                    event_id=ev.id, user_id=u.id
                ).first():
                    reg_service.register(ev, u)
                    db.session.commit()
        print({s: e.id for s, e in events.items()}, "users created:", created)


if __name__ == "__main__":
    main()
