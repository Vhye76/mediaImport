import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("state")

DETECTED = "DETECTED"
PROBED = "PROBED"
SCREENED = "SCREENED"
IDENTIFIED = "IDENTIFIED"
COMPARED = "COMPARED"
STAGED = "STAGED"
REMUXED = "REMUXED"
TAGGED = "TAGGED"
READY = "READY"
ENCODING = "ENCODING"
ENCODED = "ENCODED"
VERIFIED = "VERIFIED"
PUBLISHED = "PUBLISHED"
RETIRED = "RETIRED"

HELD = "HELD"
QUARANTINED = "QUARANTINED"
FAILED = "FAILED"

PIPELINE = (
    DETECTED, PROBED, SCREENED, IDENTIFIED, COMPARED, STAGED, REMUXED,
    TAGGED, READY, ENCODING, ENCODED, VERIFIED, PUBLISHED, RETIRED,
)
TERMINAL = (RETIRED, QUARANTINED)
STOPPED = (HELD, QUARANTINED, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path   TEXT NOT NULL UNIQUE,
    kind          TEXT,
    stage         TEXT NOT NULL,
    title         TEXT,
    year          INTEGER,
    show          TEXT,
    season        INTEGER,
    episode       INTEGER,
    tmdb          TEXT,
    imdb          TEXT,
    tvdb          TEXT,
    job_id        TEXT,
    work_path     TEXT,
    output_path   TEXT,
    encoder       TEXT,
    grain_ratio   REAL,
    probe_json    TEXT,
    decision_json TEXT,
    compare_json  TEXT,
    reason        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    retry_after   REAL,
    overridden    INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS titles_stage ON titles(stage);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id  INTEGER NOT NULL,
    stage     TEXT NOT NULL,
    detail    TEXT,
    at        REAL NOT NULL,
    FOREIGN KEY (title_id) REFERENCES titles(id)
);
CREATE INDEX IF NOT EXISTS history_title ON history(title_id);
"""


def _json(value):
    if value is None:
        return None
    return json.dumps(value, default=str)


def _unjson(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def _row_to_dict(self, row):
        if row is None:
            return None
        d = dict(row)
        d["probe"] = _unjson(d.pop("probe_json", None))
        d["decision"] = _unjson(d.pop("decision_json", None))
        d["comparison"] = _unjson(d.pop("compare_json", None))
        d["overridden"] = bool(d.get("overridden"))
        return d

    def upsert_source(self, source_path, kind=None):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "SELECT id FROM titles WHERE source_path = ?", (str(source_path),)
            )
            row = cur.fetchone()
            if row:
                return row["id"]
            cur = self._db.execute(
                "INSERT INTO titles (source_path, kind, stage, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(source_path), kind, DETECTED, now, now),
            )
            self._db.commit()
            title_id = cur.lastrowid
        self.record(title_id, DETECTED, "detected in import")
        return title_id

    def get(self, title_id):
        with self._lock:
            cur = self._db.execute("SELECT * FROM titles WHERE id = ?", (title_id,))
            return self._row_to_dict(cur.fetchone())

    def by_source(self, source_path):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE source_path = ?", (str(source_path),)
            )
            return self._row_to_dict(cur.fetchone())

    def all(self, stage=None, limit=500):
        query = "SELECT * FROM titles"
        params = []
        if stage:
            query += " WHERE stage = ?"
            params.append(stage)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cur = self._db.execute(query, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def active(self):
        placeholders = ",".join("?" for _ in STOPPED + TERMINAL)
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE stage NOT IN (%s) ORDER BY created_at" % placeholders,
                STOPPED + TERMINAL,
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def held(self):
        return self.all(stage=HELD)

    def needs_decision(self):
        rows = self.all(stage=HELD) + self.all(stage=FAILED)
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)

    def counts_by_stage(self):
        with self._lock:
            cur = self._db.execute("SELECT stage, COUNT(*) n FROM titles GROUP BY stage")
            return {r["stage"]: r["n"] for r in cur.fetchall()}

    def update(self, title_id, **fields):
        log.debug("title %s fields updated: %s", title_id, ", ".join(sorted(fields)))
        if not fields:
            return
        for key in ("probe", "decision", "comparison"):
            if key in fields:
                column = {"probe": "probe_json", "decision": "decision_json",
                          "comparison": "compare_json"}[key]
                fields[column] = _json(fields.pop(key))
        fields["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % k for k in fields)
        params = list(fields.values()) + [title_id]
        with self._lock:
            self._db.execute("UPDATE titles SET %s WHERE id = ?" % assignments, params)
            self._db.commit()

    def advance(self, title_id, stage, detail=None, **fields):
        log.debug("title %s -> %s%s", title_id, stage, ": " + detail if detail else "")
        fields["stage"] = stage
        self.update(title_id, **fields)
        self.record(title_id, stage, detail)

    def hold(self, title_id, reason):
        self.advance(title_id, HELD, reason, reason=reason)

    def hold_for_retry(self, title_id, reason, delay):
        row = self.get(title_id) or {}
        attempts = (row.get("attempts") or 0) + 1
        self.advance(
            title_id,
            HELD,
            "%s (attempt %d, retrying in %ds)" % (reason, attempts, int(delay)),
            reason=reason,
            attempts=attempts,
            retry_after=time.time() + delay,
        )
        return attempts

    def due_for_retry(self, max_attempts):
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM titles WHERE stage = ? AND retry_after IS NOT NULL"
                " AND retry_after <= ? AND attempts < ?",
                (HELD, now, max_attempts),
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def record(self, title_id, stage, detail=None):
        with self._lock:
            self._db.execute(
                "INSERT INTO history (title_id, stage, detail, at) VALUES (?, ?, ?, ?)",
                (title_id, stage, detail, time.time()),
            )
            self._db.commit()

    def forget(self, title_id):
        with self._lock:
            self._db.execute("DELETE FROM history WHERE title_id = ?", (title_id,))
            self._db.execute("DELETE FROM titles WHERE id = ?", (title_id,))
            self._db.commit()
        log.debug("title %s removed from the store", title_id)

    def history(self, title_id, limit=200):
        with self._lock:
            cur = self._db.execute(
                "SELECT stage, detail, at FROM history WHERE title_id = ?"
                " ORDER BY at ASC LIMIT ?",
                (title_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def resumable(self):
        rows = self.active()
        return [r for r in rows if r["stage"] not in STOPPED]
