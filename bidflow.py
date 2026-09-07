"""The bid flow: gather requirements, fill the application, submit on confirmation.

Shared by the Telegram bot and the console fallback so both behave identically.
State is held per posting id, so two bids in progress never collide.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from tools.applicant import NEEDS_INPUT, answer_question, parse_applicant
from tools.apply import METHOD_ATS, METHOD_EMAIL, METHOD_MANUAL, gather_requirements
from tools.fetch import hydrate
from tools.letter import generate_letter, revise_letter
from tools.resume import (
    describe_selection,
    output_path,
    parse_career_database,
    render_pdf,
    select_content,
)
from tools.profile import read_profile
from tools.store import (
    APPLIED_STATUSES,
    STATUS_BIDDING,
    STATUS_SKIPPED,
    get_posting_record,
    set_status,
    submission_allowed,
)
from tools.submit import manual_handoff, send_email_application, submit_to_ats

logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: posting_id -> in-progress application draft
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
    The full posting — description included — is persisted for exactly this,
    otherwise the cover letter would be written from the job title alone.
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


def start_bid(posting_id: str) -> dict[str, Any]:
    """Steps 1 and 2: work out how to apply, then fill everything in.

    Returns the draft dict, or {"error": ...} if the posting or the applicant
    file is unusable. Makes one model call for the cover letter.
    """
    posting = resolve_posting(posting_id)
    if not posting:
        return {"error": f"Posting {posting_id} is not in the store — nothing to bid on."}

    # Tapping Bid again on a job already applied to must not reopen it: that
    # would clear the applied status and allow a duplicate submission.
    record = get_posting_record(posting_id)
    if record and record.get("status") in APPLIED_STATUSES:
        return {"error": f"You already applied to this posting ({record['status']}). Not starting again."}

    applicant = parse_applicant()
    if applicant["raw"].startswith("ERROR:"):
        return {"error": applicant["raw"]}

    requirements = gather_requirements(posting)
    answers = {q: answer_question(q, applicant) for q in requirements.get("questions", [])}
    letter = generate_letter(posting, read_profile(), applicant["raw"])
    if not letter:
        return {"error": "The model could not be reached to write the cover letter. Try again."}

    resume_path, resume_note = build_resume(posting)

    draft = {
        "posting": posting,
        "requirements": requirements,
        "applicant": applicant,
        "answers": answers,
        "letter": letter,
        "resume_path": resume_path,
        "resume_note": resume_note,
        "awaiting_edit": False,
    }
    _store_draft(posting_id, draft)
    set_status(posting_id, STATUS_BIDDING)
    return draft


def build_resume(posting: dict[str, Any]) -> tuple[str, str]:
    """Build the tailored résumé for a posting.

    Returns (path, explanation). On any failure the path falls back to the
    static résumé from applicant.md, and the explanation says so — a résumé
    problem must never block an application.
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


def revise(posting_id: str, instruction: str) -> dict[str, Any]:
    """Step 4: regenerate the letter from a plain-language instruction."""
    draft = get_draft(posting_id)
    if not draft:
        return {"error": "That application draft is no longer active. Tap Bid again to restart."}
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


def render_draft(draft: dict[str, Any]) -> str:
    """Step 3: the filled application, shown for review before anything is sent."""
    posting, requirements = draft["posting"], draft["requirements"]
    fields = draft["applicant"]["fields"]
    method = requirements["method"]

    if method == METHOD_EMAIL:
        method_line = f"Email ({requirements['email']})"
    elif method == METHOD_ATS:
        method_line = f"{requirements['ats']['provider'].title()} (ATS)"
    else:
        method_line = "Manual — no automatic submission available"

    lines = [
        f"Application draft — {posting.get('title', '')} @ {posting.get('company', '')}",
        f"Method: {method_line}",
        "",
    ]
    for label, key in (
        ("Name", "name"), ("Email", "email"), ("Phone", "phone"), ("Location", "location"),
        ("LinkedIn", "linkedin"), ("GitHub", "github"), ("Portfolio", "portfolio"),
    ):
        if fields.get(key):
            value = fields[key]
            if key == "location" and fields.get("timezone"):
                value = f"{value} ({fields['timezone']}, remote)"
            lines.append(f"{label}: {value}")
        elif key in ("github", "portfolio"):
            lines.append(f"{label}: {NEEDS_INPUT} (add it to applicant.md)")

    resume_path = draft.get("resume_path") or fields.get("resume_path", "")
    if resume_path and os.path.isfile(resume_path):
        note = draft.get("resume_note", "")
        lines.append(f"Résumé: {note}" if note else f"Résumé: attached ({os.path.basename(resume_path)})")
        lines.append(f"         {os.path.basename(resume_path)}")
    elif resume_path:
        lines.append(f"Résumé: {NEEDS_INPUT} — {resume_path} not found, nothing will be attached")
    else:
        lines.append(f"Résumé: {NEEDS_INPUT} — no résumé available")

    if draft["answers"]:
        lines.append("")
        lines.append("── Screening questions ──")
        for question, answer in draft["answers"].items():
            lines.append(f"Q: {question}")
            lines.append(f"A: {answer}")

    lines.append("")
    lines.append("── Cover letter ──")
    lines.append(draft["letter"])
    return "\n".join(lines)


def confirm_and_submit(posting_id: str) -> tuple[str, str]:
    """Step 5: submit, only ever called after an explicit confirmation.

    Returns (status, message) where status is one of the applied_* statuses or
    "failed". The message always states the path actually taken.
    """
    draft = get_draft(posting_id)
    if not draft:
        return "failed", "That application draft is no longer active. Tap Bid again to restart."

    record = get_posting_record(posting_id)
    if record and record.get("status") in APPLIED_STATUSES:
        return "failed", f"Already applied to this posting ({record['status']}). Not sending again."

    allowed, reason = submission_allowed()
    if not allowed:
        return "failed", reason

    posting, requirements = draft["posting"], draft["requirements"]
    applicant, letter = draft["applicant"], draft["letter"]
    method = requirements["method"]

    # The tailored résumé is built during the draft so it can be reviewed; if it
    # is missing by now, rebuild it rather than falling back silently.
    resume_path = draft.get("resume_path", "")
    if not resume_path or not os.path.isfile(resume_path):
        resume_path, _ = build_resume(posting)
    applicant = {**applicant, "fields": {**applicant.get("fields", {})}}
    if resume_path:
        applicant["fields"]["resume_path"] = resume_path

    if method == METHOD_EMAIL:
        sent, detail = send_email_application(posting, applicant, letter, requirements["email"])
        if sent:
            set_status(posting_id, "applied_email", cover_letter=letter)
            clear_draft(posting_id)
            return "applied_email", f"Applied by email. {detail}"
        return "failed", f"Email submission failed — nothing was sent. {detail}"

    if method == METHOD_ATS:
        sent, detail = submit_to_ats(posting, applicant, letter, requirements["ats"])
        if sent:
            set_status(posting_id, "applied_ats", cover_letter=letter)
            clear_draft(posting_id)
            return "applied_ats", f"Applied through the ATS. {detail}"
        handoff = manual_handoff(posting, applicant, letter, requirements, draft["answers"], reason=detail)
        set_status(posting_id, "applied_manual", cover_letter=letter)
        clear_draft(posting_id)
        return "applied_manual", handoff

    handoff = manual_handoff(
        posting, applicant, letter, requirements, draft["answers"],
        reason="no application email or supported ATS was found for this posting",
    )
    set_status(posting_id, "applied_manual", cover_letter=letter)
    clear_draft(posting_id)
    return "applied_manual", handoff


def cancel(posting_id: str) -> str:
    clear_draft(posting_id)
    set_status(posting_id, "notified")
    return "Cancelled. Nothing was sent."


def skip(posting_id: str) -> str:
    clear_draft(posting_id)
    set_status(posting_id, STATUS_SKIPPED)
    return "Skipped. This posting will not come back."
