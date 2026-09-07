"""Local posting store: dedupe plus the lifecycle of every posting we see.

A posting moves through:

    new -> notified -> bidding -> applied_email | applied_ats | applied_manual
                    \\-> skipped

Only `new` postings are ever notified about, so nothing that has been sent,
applied to or dismissed can resurface on a later run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from strands import tool

from config import DB_PATH, MAX_SUBMISSIONS_PER_HOUR
from tools.fetch import hydrate

logger = logging.getLogger(__name__)

STATUS_NEW = "new"
STATUS_NOTIFIED = "notified"
STATUS_BIDDING = "bidding"
STATUS_SKIPPED = "skipped"
APPLIED_STATUSES = ("applied_email", "applied_ats", "applied_manual")
VALID_STATUSES = (STATUS_NEW, STATUS_NOTIFIED, STATUS_BIDDING, STATUS_SKIPPED, *APPLIED_STATUSES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open the store, creating or migrating the schema as needed."""
    conn = sqlite3.connect(db_path)
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS submissions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " posting_id TEXT,"
        " method TEXT,"
        " outcome TEXT,"
        " detail TEXT,"
        " attempted_at TEXT)"
    )
    conn.commit()
    return conn


# --- dedupe ----------------------------------------------------------------

def filter_new(postings: list[dict[str, Any]], db_path: str = DB_PATH) -> list[dict[str, Any]]:
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
            known = {row["id"] for row in conn.execute("SELECT id FROM seen_postings")}
            for posting in postings:
                pid = str(posting.get("id", "")).strip()
                if not pid or pid in known:
                    continue
                known.add(pid)
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

    logger.info("%d of %d postings are new", len(fresh), len(postings))
    return fresh


# --- lifecycle -------------------------------------------------------------

def get_status(posting_id: str, db_path: str = DB_PATH) -> str | None:
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
    db_path: str = DB_PATH,
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
        # Retained as the record of what was actually sent.
        fields.append("cover_letter = ?")
        values.append(cover_letter)
    values.append(str(posting_id))
    try:
        conn = _connect(db_path)
        with conn:
            conn.execute(f"UPDATE seen_postings SET {', '.join(fields)} WHERE id = ?", values)
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


def get_posting_record(posting_id: str, db_path: str = DB_PATH) -> dict[str, Any] | None:
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


# --- submission log and rate limit -----------------------------------------

def log_submission(
    posting_id: str, method: str, outcome: str, detail: str = "", db_path: str = DB_PATH
) -> None:
    """Record one submission attempt and how it actually turned out."""
    try:
        conn = _connect(db_path)
        with conn:
            conn.execute(
                "INSERT INTO submissions (posting_id, method, outcome, detail, attempted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (str(posting_id), method, outcome, detail[:2000], _now()),
            )
        logger.info("Submission attempt logged: posting=%s method=%s outcome=%s", posting_id, method, outcome)
    except sqlite3.Error as exc:
        logger.error("Could not log submission for %s: %s", posting_id, exc)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def submissions_last_hour(db_path: str = DB_PATH) -> int:
    """How many submissions actually went out in the last rolling hour."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    try:
        conn = _connect(db_path)
        with conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE outcome = 'sent' AND attempted_at >= ?",
                (cutoff,),
            ).fetchone()
        return int(row["n"])
    except sqlite3.Error as exc:
        logger.error("Could not count recent submissions: %s", exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def submission_allowed(db_path: str = DB_PATH) -> tuple[bool, str]:
    """Check the hourly submission ceiling. Returns (allowed, reason)."""
    used = submissions_last_hour(db_path)
    if used >= MAX_SUBMISSIONS_PER_HOUR:
        return False, (
            f"Rate limit reached: {used} applications already sent in the last hour "
            f"(limit {MAX_SUBMISSIONS_PER_HOUR}). Try again later."
        )
    return True, f"{used}/{MAX_SUBMISSIONS_PER_HOUR} submissions used this hour."


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
