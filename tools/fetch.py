"""Job source adapter.

ALL source-specific knowledge (endpoint shape, HTML in descriptions, the
legal-notice element RemoteOK puts first) lives in this module. Everything
else in BidWatch consumes the normalized posting dict defined by
`normalize_posting`, so a second source can be added here without touching
another file.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import requests
from strands import tool

from config import JOB_SOURCE_URL, MAX_DESCRIPTION_CHARS, USER_AGENT

logger = logging.getLogger(__name__)

#: Postings fetched during this process, keyed by id. Lets downstream tools be
#: handed just {"id": ...} instead of the model re-serializing every full
#: posting back through the context window.
_POSTING_CACHE: dict[str, dict[str, Any]] = {}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

#: Keys every normalized posting carries, whatever the source.
POSTING_FIELDS = (
    "id",
    "title",
    "company",
    "description",
    "tags",
    "url",
    "salary_min",
    "salary_max",
    "location",
    "posted_at",
)


def strip_html(raw: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Turn a fragment of HTML into plain text and truncate it to `limit`."""
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", raw, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …[truncated]"
    return text


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_posting(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one RemoteOK record onto the normalized posting shape.

    Returns None for records that are unusable (no id, or the legal notice).
    """
    if not isinstance(raw, dict) or "legal" in raw or not raw.get("id"):
        return None
    return {
        "id": str(raw.get("id")),
        "title": (raw.get("position") or raw.get("title") or "Untitled").strip(),
        "company": (raw.get("company") or "Unknown").strip(),
        "description": strip_html(raw.get("description")),
        "tags": [str(t) for t in (raw.get("tags") or []) if t],
        # Direct link back to the listing on Remote OK, as their terms require.
        "url": raw.get("url") or f"https://remoteok.com/remote-jobs/{raw.get('slug', raw.get('id'))}",
        "salary_min": _as_int(raw.get("salary_min")),
        "salary_max": _as_int(raw.get("salary_max")),
        "location": (raw.get("location") or "Remote").strip(),
        "posted_at": raw.get("date") or "",
    }


@tool
def fetch_job_postings(tag: str = "dev", limit: int = 20) -> list[dict]:
    """Fetch the most recent remote job postings from the Remote OK public API.

    Use this first on every run to get candidate jobs. Returns a list of
    postings, each with: id, title, company, description, tags, url,
    salary_min, salary_max, location, posted_at. Salary is often null —
    Remote OK only publishes it for some listings. Returns an empty list if
    the source is unreachable; it never raises.

    Args:
        tag: Remote OK tag to filter by, e.g. "python", "dev", "backend".
        limit: Maximum number of postings to return (newest first).
    """
    params = {"tag": tag} if tag else None
    try:
        response = requests.get(
            JOB_SOURCE_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        if response.status_code != 200:
            logger.error("Job source returned HTTP %s", response.status_code)
            return []
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Could not fetch job postings: %s", exc)
        return []

    if not isinstance(payload, list):
        logger.error("Unexpected payload type from job source: %s", type(payload))
        return []

    postings: list[dict[str, Any]] = []
    # The first element is Remote OK's legal/attribution notice, not a job.
    for raw in payload[1:]:
        posting = normalize_posting(raw)
        if posting:
            postings.append(posting)
        if len(postings) >= max(1, limit):
            break

    _POSTING_CACHE.update({p["id"]: p for p in postings})
    logger.info("Fetched %d postings for tag=%r", len(postings), tag)
    return postings


def hydrate(posting: dict[str, Any]) -> dict[str, Any]:
    """Expand a posting reference into the full posting, if it is known.

    A caller (including the model) may pass just ``{"id": "..."}``; anything
    already complete is returned untouched.
    """
    if not isinstance(posting, dict):
        return {}
    cached = _POSTING_CACHE.get(str(posting.get("id", "")))
    if cached and len(posting) < len(cached):
        return cached
    return posting


def remember(postings: list[dict[str, Any]]) -> None:
    """Add postings (e.g. fixtures) to the in-process cache."""
    _POSTING_CACHE.update({p["id"]: p for p in postings if p.get("id")})
