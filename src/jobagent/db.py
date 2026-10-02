from __future__ import annotations

import json
from collections.abc import Callable
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from jobagent.models import Job, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    source       TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    apply_url    TEXT NOT NULL,
    ats          TEXT,
    location     TEXT,
    host         TEXT,
    data         TEXT NOT NULL,          -- full Job json
    status       TEXT NOT NULL DEFAULT 'new',
    score        REAL,                   -- triage priority (higher = apply first)
    triage       TEXT,                   -- clef answers json
    result       TEXT,                   -- ApplyResult json
    attempts     INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    applied_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, score DESC);
CREATE INDEX IF NOT EXISTS jobs_fp ON jobs(fingerprint);

-- Inbox requests fulfilled by an external helper (e.g. a Claude session with a Gmail MCP connector).
CREATE TABLE IF NOT EXISTS mail_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,          -- 'otp'
    job_key      TEXT,
    company      TEXT,
    sender_hint  TEXT,
    want         TEXT,                   -- 'code' | 'link'
    requested_at REAL NOT NULL,          -- unix time the code was requested
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    result       TEXT,
    updated_at   TEXT
);

-- Applications sent by email ("send your resume to jobs@...").
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key     TEXT,
    company     TEXT,
    to_addr     TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    attachment  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | sent | failed
    error       TEXT,
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    h = (urlparse(url).hostname or "").lower()
    parts = h.split(".")
    # boards.greenhouse.io -> greenhouse.io, in.linkedin.com -> linkedin.com
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


MAX_PER_COMPANY = 3


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    # ---- discovery ---------------------------------------------------------------------------
    def upsert_jobs(self, jobs: list[Job]) -> int:
        """Insert unseen jobs. Same role already known under another source = SKIPPED duplicate."""
        new = 0
        for j in jobs:
            if self._x("SELECT 1 FROM jobs WHERE key=?", (j.key,)).fetchone():
                continue
            dup = self._x("SELECT key FROM jobs WHERE fingerprint=? LIMIT 1", (j.fingerprint,)).fetchone()
            status = Status.SKIPPED if dup else Status.NEW
            self._x(
                "INSERT INTO jobs(key,fingerprint,source,company,title,url,apply_url,ats,location,host,data,status,"
                "result,discovered_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (j.key, j.fingerprint, j.source, j.company, j.title, j.url, j.apply_url, j.ats, j.location,
                 host_of(j.apply_url), json.dumps(j.to_dict()), status,
                 json.dumps({"summary": f"duplicate of {dup['key']}"}) if dup else None, now(), now()),
            )
            new += status == Status.NEW
        return new

    # ---- triage ------------------------------------------------------------------------------
    def jobs_with_status(self, status: Status, limit: int = 100000) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM jobs WHERE status=? ORDER BY score DESC, discovered_at LIMIT ?",
                       (status, limit)).fetchall()

    def set_triage(self, key: str, status: Status, score: float, answers: dict) -> None:
        self._x("UPDATE jobs SET status=?, score=?, triage=?, updated_at=? WHERE key=?",
                (status, score, json.dumps(answers), now(), key))

    # ---- apply -------------------------------------------------------------------------------
    def claim_next(self, exclude_hosts: set[str], skip_ats: set[str],
                   already_applied: Callable[[sqlite3.Row], bool] = lambda r: False,
                   max_attempts: int = 3) -> sqlite3.Row | None:
        """Atomically take the best queued job whose host isn't saturated and that we haven't applied to.
        Blocked hosts are filtered in SQL (they can be most of the queue, and a LIMIT over them starves workers).
        One application per company at a time: parallel runs on the same employer portal share one account and
        cancel each other's verification codes / password resets."""
        from jobagent.models import company_key as normalize_company

        with self.lock:
            busy = {normalize_company(c) for (c,) in
                    self.conn.execute("SELECT company FROM jobs WHERE status=?", (Status.IN_PROGRESS,))}
            # at most MAX_PER_COMPANY roles per employer: more looks like spray-and-pray to their recruiters
            applied: dict[str, int] = {}
            for (c,) in self.conn.execute("SELECT company FROM jobs WHERE status IN (?,?)", (Status.APPLIED, Status.READY)):
                applied[normalize_company(c or "")] = applied.get(normalize_company(c or ""), 0) + 1
            hosts = sorted(exclude_hosts)
            rows = self.conn.execute(
                f"SELECT * FROM jobs WHERE status=? AND host NOT IN ({','.join('?' * len(hosts))}) "
                "ORDER BY score DESC LIMIT 2000", (Status.QUEUED, *hosts)
            ).fetchall()
            for r in rows:
                if r["attempts"] >= max_attempts:  # restarts/requeues must not loop on the same job forever
                    self.conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE key=?",
                                      (Status.FAILED, now(), r["key"]))
                    continue
                if applied.get(normalize_company(r["company"] or ""), 0) >= MAX_PER_COMPANY:
                    self.conn.execute("UPDATE jobs SET status=?, result=?, updated_at=? WHERE key=?",
                                      (Status.SKIPPED, json.dumps({"summary": f"already applied to {MAX_PER_COMPANY} "
                                       "roles at this company"}), now(), r["key"]))
                    continue
                if (r["ats"] or "") in skip_ats or normalize_company(r["company"] or "") in busy:
                    continue
                # never apply twice to the same role at the same company (DB + APPLIED_JOBS.md)
                if already_applied(r) or self.conn.execute("SELECT 1 FROM jobs WHERE fingerprint=? AND status IN (?,?)",
                                     (r["fingerprint"], Status.APPLIED, Status.IN_PROGRESS)).fetchone():
                    self.conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE key=?",
                                      (Status.SKIPPED, now(), r["key"]))
                    continue
                # several daemon processes share this queue: only the one whose UPDATE flips the row claims it
                if self.conn.execute("UPDATE jobs SET status=?, attempts=attempts+1, updated_at=? WHERE key=? AND status=?",
                                     (Status.IN_PROGRESS, now(), r["key"], Status.QUEUED)).rowcount:
                    return r
        return None

    def skip_company(self, company: str, why: str) -> int:
        from jobagent.models import company_key as normalize_company

        target = normalize_company(company)
        with self.lock:
            keys = [r["key"] for r in self.conn.execute("SELECT key, company FROM jobs WHERE status=?", (Status.QUEUED,))
                    if normalize_company(r["company"] or "") == target]
            for k in keys:
                self.conn.execute("UPDATE jobs SET status=?, result=?, updated_at=? WHERE key=?",
                                  (Status.SKIPPED, json.dumps({"summary": why}), now(), k))
        return len(keys)

    def finish(self, key: str, status: Status, result: dict) -> None:
        self._x("UPDATE jobs SET status=?, result=?, updated_at=?, applied_at=CASE WHEN ?='applied' THEN ? "
                "ELSE applied_at END WHERE key=?", (status, json.dumps(result), now(), status, now(), key))

    def requeue_stale(self) -> None:
        """Jobs left in_progress by a crashed/killed run go back to the queue."""
        # killed mid-run (restart/crash) isn't the job's fault: refund the attempt
        self._x("UPDATE jobs SET status=?, attempts=MAX(attempts-1, 0) WHERE status=?", (Status.QUEUED, Status.IN_PROGRESS))

    def applied_today(self, host: str) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        return self._x("SELECT COUNT(*) FROM jobs WHERE host=? AND status=? AND applied_at >= ?",
                       (host, Status.APPLIED, day)).fetchone()[0]

    def requeue(self, statuses: list[str], max_attempts: int) -> int:
        q = ",".join("?" * len(statuses))
        return self._x(f"UPDATE jobs SET status=? WHERE status IN ({q}) AND attempts < ?",
                       (Status.QUEUED, *statuses, max_attempts)).rowcount

    def mark_email_sent(self, outbox_id: int, ledger) -> None:
        """Outbox email delivered: the job counts as applied and goes into APPLIED_JOBS.md."""
        r = self._x("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        self._x("UPDATE outbox SET status='sent', sent_at=? WHERE id=?", (now(), outbox_id))
        job_row = self._x("SELECT * FROM jobs WHERE key=?", (r["job_key"],)).fetchone()
        if job_row:
            self.finish(r["job_key"], Status.APPLIED, {"summary": f"emailed application to {r['to_addr']}"})
            ledger.record(Job(**json.loads(job_row["data"])), f"emailed {r['to_addr']}")

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._x("SELECT status, COUNT(*) FROM jobs GROUP BY status")}

    def by_status(self, status: str, limit: int = 50) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM jobs WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
