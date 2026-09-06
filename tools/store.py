"""Local dedupe store: remembers which postings have already been handled."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from strands import tool

from config import DB_PATH
from tools.fetch import hydrate

logger = logging.getLogger(__name__)


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open the store, creating the schema if it does not exist yet."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_postings ("
        " id TEXT PRIMARY KEY,"
        " title TEXT,"
        " company TEXT,"
        " seen_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return conn


def filter_new(postings: list[dict[str, Any]], db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Plain-Python core of `filter_new_postings` (also used by tests)."""
    if not postings:
        return []
    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        logger.error("Dedupe store unavailable (%s); treating all postings as new", exc)
        return list(postings)

    fresh: list[dict[str, Any]] = []
    try:
        with conn:
            known = {row[0] for row in conn.execute("SELECT id FROM seen_postings")}
            for posting in postings:
                pid = str(posting.get("id", "")).strip()
                if not pid or pid in known:
                    continue
                known.add(pid)
                fresh.append(posting)
                conn.execute(
                    "INSERT OR IGNORE INTO seen_postings (id, title, company) VALUES (?, ?, ?)",
                    (pid, posting.get("title", ""), posting.get("company", "")),
                )
    except sqlite3.Error as exc:
        logger.error("Dedupe store write failed: %s", exc)
    finally:
        conn.close()

    logger.info("%d of %d postings are new", len(fresh), len(postings))
    return fresh


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
