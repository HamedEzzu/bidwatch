"""The bid flow: prepare a complete application package for one posting.

BidWatch prepares; the person applies. Tapping Bid builds everything an
application needs — a job-tailored résumé, a cover letter in the job seeker's
voice, and a copy-paste field sheet — and hands it over. Nothing is ever sent
to an employer from here, because nothing here can send.

State is held per posting id, so two applications in progress never collide.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from tools.applicant import parse_applicant
from tools.fetch import hydrate
from tools.letter import generate_letter, revise_letter
from tools.package import field_sheet, resume_line
from tools.profile import read_profile
from tools.resume import (
    describe_selection,
    output_path,
    parse_career_database,
    render_pdf,
    select_content,
)
from tools.store import (
    APPLIED_STATUSES,
    STATUS_APPLIED,
    STATUS_BIDDING,
    STATUS_NOTIFIED,
    STATUS_SKIPPED,
    get_posting_record,
    set_status,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: posting_id -> the prepared application package
_drafts: dict[str, dict[str, Any]] = {}


def get_draft(posting_id: str) -> dict[str, Any] | None:
    with _lock:
        return _drafts.get(str(posting_id))


def _store_draft(posting_id: str, draft: dict[str, Any]) -> None:
    with _lock:
        _drafts[str(posting_id)] = draft


def clear_draft(posting_id: str) -> None:
    with _lock:
        _drafts.pop(str(posting_id), None)


def active_draft_ids() -> list[str]:
    with _lock:
        return list(_drafts)


def resolve_posting(posting_id: str) -> dict[str, Any] | None:
    """Find a posting by id: the in-process cache first, then the store.

    The store fallback matters: bot.py runs as a separate process from the
    scheduler, so a button tap has none of the scanner's in-memory postings.
    The full posting — description included — is persisted for exactly this.
    """
    posting = hydrate({"id": str(posting_id)})
    if posting.get("description"):
        return posting
    record = get_posting_record(posting_id)
    if not record:
        return posting if posting.get("title") else None

    try:
        tags = json.loads(record.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    return {
        "id": record["id"],
        "title": record.get("title", ""),
        "company": record.get("company", ""),
        "url": record.get("url", ""),
        "description": record.get("description") or "",
        "tags": tags,
        "location": record.get("location") or "",
        "salary_min": record.get("salary_min"),
        "salary_max": record.get("salary_max"),
        "posted_at": "",
    }


def build_resume(posting: dict[str, Any]) -> tuple[str, str]:
    """Build the tailored résumé for a posting.

    Returns (path, explanation). On any failure the path falls back to the
    static résumé from applicant.md, and the explanation says so — a résumé
    problem must never block preparing the rest of the package.
    """
    static = parse_applicant().get("fields", {}).get("resume_path", "")
    try:
        db = parse_career_database()
        if not db.get("projects"):
            return static, "static résumé (the career database has no projects to select from)"
        selection = select_content(posting, db)
        path = render_pdf(selection, db, output_path(posting))
        note = selection.get("reason") or describe_selection(selection, db)
        logger.info("Tailored résumé for %s: %s", posting.get("id"), path)
        return path, note
    except Exception as exc:  # noqa: BLE001 - fall back rather than block the bid
        logger.error("Résumé generation failed for %s: %s", posting.get("id"), exc)
        if static and os.path.isfile(static):
            return static, f"static résumé (tailored generation failed: {exc})"
        return "", f"no résumé available (generation failed: {exc})"


def start_bid(posting_id: str) -> dict[str, Any]:
    """Prepare the whole application package for one posting.

    Returns the package, or {"error": ...} if the posting or applicant file is
    unusable. Makes two model calls: the résumé selection and the letter.
    """
    posting = resolve_posting(posting_id)
    if not posting:
        return {"error": f"Posting {posting_id} is not in the store — nothing to prepare."}

    record = get_posting_record(posting_id)
    # APPLIED_STATUSES covers the older applied_email/ats/manual values too, so
    # a database written by an earlier version still blocks correctly.
    if record and record.get("status") in APPLIED_STATUSES:
        return {"error": "You already marked this one applied. Nothing to prepare again."}

    applicant = parse_applicant()
    if applicant["raw"].startswith("ERROR:"):
        return {"error": applicant["raw"]}

    letter = generate_letter(posting, read_profile(), applicant["raw"])
    if not letter:
        return {"error": "The model could not be reached to write the cover letter. Try again."}

    resume_path, resume_note = build_resume(posting)

    draft = {
        "posting": posting,
        "applicant": applicant,
        "letter": letter,
        "resume_path": resume_path,
        "resume_note": resume_note,
        "sheet": field_sheet(applicant),
        "apply_url": posting.get("url", ""),
        "awaiting_edit": False,
    }
    _store_draft(posting_id, draft)
    set_status(posting_id, STATUS_BIDDING)
    return draft


def revise(posting_id: str, instruction: str) -> dict[str, Any]:
    """Regenerate the letter following the user's plain-language instruction."""
    draft = get_draft(posting_id)
    if not draft:
        return {"error": "That application is no longer active. Tap Bid again to rebuild it."}
    draft["letter"] = revise_letter(
        draft["posting"], read_profile(), draft["applicant"]["raw"], draft["letter"], instruction
    )
    draft["awaiting_edit"] = False
    _store_draft(posting_id, draft)
    return draft


def set_awaiting_edit(posting_id: str, awaiting: bool = True) -> bool:
    draft = get_draft(posting_id)
    if not draft:
        return False
    draft["awaiting_edit"] = awaiting
    _store_draft(posting_id, draft)
    return True


def awaiting_edit_id() -> str | None:
    """The posting currently waiting for edit instructions, if any."""
    with _lock:
        for posting_id, draft in _drafts.items():
            if draft.get("awaiting_edit"):
                return posting_id
    return None


def render_package(draft: dict[str, Any]) -> str:
    """The package as plain text, used by the console and as a fallback."""
    posting = draft["posting"]
    parts = [
        f"Application package — {posting.get('title', '')} @ {posting.get('company', '')}",
        "",
        resume_line(draft.get("resume_path", ""), draft.get("resume_note", "")),
        "",
        "── Cover letter ──",
        draft["letter"],
        "",
        "── Application details ──",
        draft["sheet"],
        "",
        f"Apply here: {draft.get('apply_url', '')}",
    ]
    return "\n".join(parts)


def mark_applied(posting_id: str) -> str:
    """Record that the user applied, so the posting never resurfaces."""
    draft = get_draft(posting_id) or {}
    set_status(
        posting_id,
        STATUS_APPLIED,
        cover_letter=draft.get("letter"),
        resume_path=draft.get("resume_path"),
    )
    clear_draft(posting_id)
    logger.info("Posting %s marked applied by the user", posting_id)
    return "Marked as applied. This job won't come back."


def cancel(posting_id: str) -> str:
    clear_draft(posting_id)
    set_status(posting_id, STATUS_NOTIFIED)
    return "Closed. Nothing was recorded."


def skip(posting_id: str) -> str:
    clear_draft(posting_id)
    set_status(posting_id, STATUS_SKIPPED)
    return "Skipped. This posting will not come back."
