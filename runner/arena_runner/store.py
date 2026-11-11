"""Runner-local persistent state (SQLite, stdlib only).

Survives runner restarts so that expired/orphaned containers are cleaned up.
Also stores request nonces for replay protection.
"""

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  image TEXT NOT NULL,
  status TEXT NOT NULL,
  backend_ref TEXT,
  host TEXT,
  ports TEXT,
  labels TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_instances_status_exp ON instances(status, expires_at);
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, expires_at REAL NOT NULL);
"""

ACTIVE = ("starting", "running", "stopped")


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None

    # nonces ------------------------------------------------------------
    def seen(self, nonce, ttl):
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM nonces WHERE expires_at < ?", (now,))
            try:
                self._conn.execute(
                    "INSERT INTO nonces(nonce, expires_at) VALUES (?, ?)",
                    (nonce, now + ttl),
                )
                return False
            except sqlite3.IntegrityError:
                return True

    # instances -----------------------------------------------------------
    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        d["ports"] = json.loads(d["ports"] or "{}")
        d["labels"] = json.loads(d["labels"] or "{}")
        return d

    def get(self, instance_id):
        with self._lock:
            return self._row(
                self._conn.execute(
                    "SELECT * FROM instances WHERE id=?", (instance_id,)
                ).fetchone()
            )

    def insert(self, instance_id, image, labels, ttl):
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO instances(id, image, status, labels, created_at, expires_at, updated_at) "
                "VALUES (?, ?, 'starting', ?, ?, ?, ?)",
                (instance_id, image, json.dumps(labels), now, now + ttl, now),
            )
        return self.get(instance_id)

    UPDATABLE = frozenset(
        {"status", "backend_ref", "host", "ports", "labels", "error", "expires_at"}
    )

    def update(self, instance_id, **fields):
        if not fields:
            return self.get(instance_id)
        unknown = set(fields) - self.UPDATABLE
        if unknown:  # column names are interpolated below; never accept others
            raise ValueError(f"not updatable: {sorted(unknown)}")
        fields["updated_at"] = time.time()
        for k in ("ports", "labels"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE instances SET {cols} WHERE id=?",  # noqa: S608 (fixed keys)
                (*fields.values(), instance_id),
            )
        return self.get(instance_id)

    def count_active(self):
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM instances WHERE status IN ('starting','running','stopped')"
            ).fetchone()[0]

    def expired(self, now=None):
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM instances WHERE status IN ('starting','running','stopped') AND expires_at < ?",
                (now,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def active(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM instances WHERE status IN ('starting','running','stopped')"
            ).fetchall()
        return [self._row(r) for r in rows]

    def used_ports(self):
        ports = set()
        for r in self.active():
            ports.update(int(p) for p in r["ports"].values())
        return ports
