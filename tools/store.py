"""Local posting store: dedupe plus the lifecycle of every posting we see.

A posting moves through:

    new -> rejected                       (scored below the threshold)
        -> notified -> bidding -> applied (the user prepared and applied)
                    -> skipped

Only postings with a verdict are withheld from later runs: one that was
recorded but never judged — a crashed run, a truncated call — comes back,
so a transient failure cannot cost a good job.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from strands import tool

from config import DB_PATH
from tools.fetch import hydrate

logger = logging.getLogger(__name__)

STATUS_NEW = "new"
STATUS_REJECTED = "rejected"
STATUS_NOTIFIED = "notified"
STATUS_BIDDING = "bidding"
STATUS_SKIPPED = "skipped"
STATUS_APPLIED = "applied"
#: Kept so databases written by earlier versions still read correctly.
LEGACY_APPLIED = ("applied_email", "applied_ats", "applied_manual")
APPLIED_STATUSES = (STATUS_APPLIED, *LEGACY_APPLIED)
VALID_STATUSES = (
    STATUS_NEW, STATUS_REJECTED, STATUS_NOTIFIED, STATUS_BIDDING, STATUS_SKIPPED, *APPLIED_STATUSES
)

#: A posting is "handled" once it has been judged, one way or the other. Only
#: handled postings are withheld from later runs: a posting recorded but never
#: judged — a crashed run, a truncated tool call — must come back, or a good
#: job is lost forever to a transient failure.
HANDLED_STATUSES = (STATUS_REJECTED, STATUS_NOTIFIED, STATUS_BIDDING, STATUS_SKIPPED, *APPLIED_STATUSES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve(db_path: str | None) -> str:
    """Which database to use, decided per call.

    Binding DB_PATH as a default argument would freeze it at import time, so a
    test (or a dry run) could not point the store somewhere else — and would
    silently write to the real store instead.
    """
    return db_path or DB_PATH


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open the store, creating or migrating the schema as needed."""
    conn = sqlite3.connect(_resolve(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_postings ("
        " id TEXT PRIMARY KEY,"
        " title TEXT,"
        " company TEXT,"
        " seen_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    # Migration: earlier versions stored only id/title/company/seen_at.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(seen_postings)")}
    for column, ddl in (
        ("status", f"ALTER TABLE seen_postings ADD COLUMN status TEXT DEFAULT '{STATUS_NEW}'"),
        ("url", "ALTER TABLE seen_postings ADD COLUMN url TEXT"),
        ("score", "ALTER TABLE seen_postings ADD COLUMN score INTEGER"),
        ("notified_at", "ALTER TABLE seen_postings ADD COLUMN notified_at TEXT"),
        ("applied_at", "ALTER TABLE seen_postings ADD COLUMN applied_at TEXT"),
        ("cover_letter", "ALTER TABLE seen_postings ADD COLUMN cover_letter TEXT"),
        ("resume_path", "ALTER TABLE seen_postings ADD COLUMN resume_path TEXT"),
        # The full posting is persisted so a separate process (bot.py) can
        # write a letter against the real description, not just the title.
        ("description", "ALTER TABLE seen_postings ADD COLUMN description TEXT"),
        ("tags", "ALTER TABLE seen_postings ADD COLUMN tags TEXT"),
        ("location", "ALTER TABLE seen_postings ADD COLUMN location TEXT"),
        ("salary_min", "ALTER TABLE seen_postings ADD COLUMN salary_min INTEGER"),
        ("salary_max", "ALTER TABLE seen_postings ADD COLUMN salary_max INTEGER"),
    ):
        if column not in existing:
            conn.execute(ddl)
    conn.commit()
    return conn


# --- dedupe ----------------------------------------------------------------

def filter_new(postings: list[dict[str, Any]], db_path: str | None = None) -> list[dict[str, Any]]:
    """Plain-Python core of `filter_new_postings` (also used by tests)."""
    if not postings:
        return []
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.error("Posting store unavailable (%s); treating all postings as new", exc)
        return list(postings)

    fresh: list[dict[str, Any]] = []
    try:
        with conn:
            placeholders = ", ".join("?" * len(HANDLED_STATUSES))
            handled = {
                row["id"] for row in conn.execute(
                    f"SELECT id FROM seen_postings WHERE status IN ({placeholders})", HANDLED_STATUSES
                )
            }
            for posting in postings:
                pid = str(posting.get("id", "")).strip()
                if not pid or pid in handled:
                    continue
                handled.add(pid)
                fresh.append(posting)
                conn.execute(
                    "INSERT OR IGNORE INTO seen_postings"
                    " (id, title, company, url, status, seen_at, description, tags,"
                    "  location, salary_min, salary_max)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pid, posting.get("title", ""), posting.get("company", ""),
                     posting.get("url", ""), STATUS_NEW, _now(),
                     posting.get("description", ""), json.dumps(posting.get("tags", [])),
                     posting.get("location", ""), posting.get("salary_min"),
                     posting.get("salary_max")),
                )
    except sqlite3.Error as exc:
        logger.error("Posting store write failed: %s", exc)
    finally:
        conn.close()

    logger.info("%d of %d postings still need judging", len(fresh), len(postings))
    return fresh


# --- lifecycle -------------------------------------------------------------

def get_status(posting_id: str, db_path: str | None = None) -> str | None:
    """Current status of a posting, or None if it was never seen."""
    try:
        conn = _connect(db_path)
        with conn:
            row = conn.execute(
                "SELECT status FROM seen_postings WHERE id = ?", (str(posting_id),)
            ).fetchone()
        return row["status"] if row else None
    except sqlite3.Error as exc:
        logger.error("Could not read status for %s: %s", posting_id, exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def set_status(
    posting_id: str,
    status: str,
    score: int | None = None,
    cover_letter: str | None = None,
    resume_path: str | None = None,
    db_path: str | None = None,
) -> bool:
    """Move a posting to `status`, stamping the matching timestamp."""
    if status not in VALID_STATUSES:
        logger.error("Refusing to set unknown status %r", status)
        return False
    fields = ["status = ?"]
    values: list[Any] = [status]
    if status == STATUS_NOTIFIED:
        fields.append("notified_at = ?")
        values.append(_now())
    if status in APPLIED_STATUSES:
        fields.append("applied_at = ?")
        values.append(_now())
    if score is not None:
        fields.append("score = ?")
        values.append(int(score))
    if cover_letter is not None:
        # Retained as the record of what was prepared for this job.
        fields.append("cover_letter = ?")
        values.append(cover_letter)
    if resume_path is not None:
        fields.append("resume_path = ?")
        values.append(resume_path)
    values.append(str(posting_id))
    try:
        conn = _connect(db_path)
        with conn:
            cursor = conn.execute(
                f"UPDATE seen_postings SET {', '.join(fields)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                # The posting was never recorded — a bot restarted against a
                # fresh database, say. Create the row rather than reporting a
                # success that changed nothing and letting the job resurface.
                conn.execute(
                    "INSERT OR IGNORE INTO seen_postings (id, status, seen_at) VALUES (?, ?, ?)",
                    (str(posting_id), status, _now()),
                )
                conn.execute(
                    f"UPDATE seen_postings SET {', '.join(fields)} WHERE id = ?", values
                )
                logger.info("Posting %s was not in the store; recorded it as %s", posting_id, status)
        logger.info("Posting %s -> %s", posting_id, status)
        return True
    except sqlite3.Error as exc:
        logger.error("Could not update posting %s: %s", posting_id, exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def get_posting_record(posting_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """Everything the store knows about one posting."""
    try:
        conn = _connect(db_path)
        with conn:
            row = conn.execute(
                "SELECT * FROM seen_postings WHERE id = ?", (str(posting_id),)
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        logger.error("Could not read posting %s: %s", posting_id, exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@tool
def filter_new_postings(postings: list[dict]) -> list[dict]:
    """Drop postings that were already seen on a previous run.

    Call this immediately after fetching. Returns only the postings whose ids
    are not yet in the local store, and records those ids so the same posting
    is never notified about twice. Running it twice with the same input
    returns an empty list the second time.

    To save tokens, pass ONLY the ids of the postings you just fetched, like
    [{"id": "1137062"}, {"id": "1136990"}] — do not repeat the titles or
    descriptions. The full postings are returned to you.

    Args:
        postings: The fetched postings, or just [{"id": ...}, ...] references.
    """
    return filter_new([hydrate(p) for p in postings])
