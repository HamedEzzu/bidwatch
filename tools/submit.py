"""Submits a confirmed application, and reports what actually happened.

Three paths:
  email      SMTP, cover letter as the body, résumé attached
  known_ats  attempt the provider's endpoint, fall back to manual on failure
  manual     hand the finalized letter and fields back for pasting

Nothing here runs without an explicit confirmation from the user; the caller
is responsible for obtaining it, and every attempt is logged with its outcome.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from typing import Any

import requests

from tools.applicant import NEEDS_INPUT
from tools.store import log_submission

logger = logging.getLogger(__name__)

OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"
OUTCOME_MANUAL = "manual_handoff"


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


def _from_address(applicant: dict[str, Any]) -> str:
    return (
        os.getenv("FROM_EMAIL")
        or os.getenv("SMTP_USER")
        or applicant.get("fields", {}).get("email", "")
    )


def build_email(
    posting: dict[str, Any], applicant: dict[str, Any], letter: str, to_address: str
) -> EmailMessage:
    """Build the application email, résumé attached when the file exists."""
    fields = applicant.get("fields", {})
    message = EmailMessage()
    message["Subject"] = f"Application: {posting.get('title', 'Role')} — {fields.get('name', '')}".strip(" —")
    message["From"] = _from_address(applicant)
    message["To"] = to_address
    reply_to = fields.get("email")
    if reply_to and reply_to != message["From"]:
        message["Reply-To"] = reply_to

    contact_lines = [
        f"{fields.get('name', '')}",
        f"{fields.get('email', '')} · {fields.get('phone', '')}".strip(" ·"),
        f"{fields.get('location', '')} ({fields.get('timezone', '')})".strip(" ()"),
    ]
    for label, key in (("LinkedIn", "linkedin"), ("GitHub", "github"), ("Portfolio", "portfolio")):
        if fields.get(key):
            contact_lines.append(f"{label}: {fields[key]}")

    message.set_content(f"{letter}\n\n--\n" + "\n".join(line for line in contact_lines if line.strip()))

    resume_path = fields.get("resume_path", "")
    if resume_path and os.path.isfile(resume_path):
        ctype, _ = mimetypes.guess_type(resume_path)
        maintype, _, subtype = (ctype or "application/pdf").partition("/")
        with open(resume_path, "rb") as handle:
            message.add_attachment(
                handle.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(resume_path)
            )
    return message


def send_email_application(
    posting: dict[str, Any], applicant: dict[str, Any], letter: str, to_address: str
) -> tuple[bool, str]:
    """Send the application by SMTP. Returns (sent, human-readable detail)."""
    posting_id = posting.get("id", "")
    if not smtp_configured():
        detail = "SMTP is not configured (SMTP_HOST / SMTP_USER / SMTP_PASS)."
        log_submission(posting_id, "email", OUTCOME_FAILED, detail)
        return False, detail

    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")

    try:
        message = build_email(posting, applicant, letter, to_address)
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=45)
        else:
            server = smtplib.SMTP(host, port, timeout=45)
            server.starttls()
        with server:
            server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("Email application to %s failed: %s", to_address, detail)
        log_submission(posting_id, "email", OUTCOME_FAILED, detail)
        return False, detail

    attached = os.path.isfile(applicant.get("fields", {}).get("resume_path", ""))
    detail = f"Sent to {to_address}" + ("" if attached else " (no résumé attached — file not found)")
    logger.info("Application emailed for posting %s: %s", posting_id, detail)
    log_submission(posting_id, "email", OUTCOME_SENT, detail)
    return True, detail


def submit_to_ats(
    posting: dict[str, Any], applicant: dict[str, Any], letter: str, ats: dict[str, str]
) -> tuple[bool, str]:
    """Attempt a structured submission to a known ATS.

    Greenhouse and Lever expose public application endpoints, but both require
    a per-board key or token that this project does not hold. When that is the
    case — or the schema does not match — this returns False so the caller can
    fall back to the manual path and say so plainly, rather than pretending an
    application was filed.
    """
    posting_id = posting.get("id", "")
    provider = ats.get("provider", "unknown")
    fields = applicant.get("fields", {})

    if provider == "greenhouse":
        token = os.getenv("GREENHOUSE_API_TOKEN")
        if not token:
            detail = ("Greenhouse submission needs a board API token (GREENHOUSE_API_TOKEN); "
                      "none is configured.")
            log_submission(posting_id, "ats:greenhouse", OUTCOME_FAILED, detail)
            return False, detail
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{ats['org']}/jobs/{ats['job_id']}"
    elif provider == "lever":
        endpoint = f"https://api.lever.co/v0/postings/{ats['org']}/{ats['job_id']}/apply"
    else:
        detail = f"{provider.title()} has no unauthenticated application endpoint."
        log_submission(posting_id, f"ats:{provider}", OUTCOME_FAILED, detail)
        return False, detail

    payload = {
        "name": fields.get("name", ""),
        "email": fields.get("email", ""),
        "phone": fields.get("phone", ""),
        "urls[LinkedIn]": fields.get("linkedin", ""),
        "urls[GitHub]": fields.get("github", ""),
        "comments": letter,
    }
    try:
        response = requests.post(endpoint, data=payload, timeout=45)
        if response.status_code in (200, 201):
            detail = f"Submitted through {provider.title()}."
            log_submission(posting_id, f"ats:{provider}", OUTCOME_SENT, detail)
            return True, detail
        detail = f"{provider.title()} rejected the submission: HTTP {response.status_code} {response.text[:200]}"
    except requests.RequestException as exc:
        detail = f"{provider.title()} submission failed: {exc}"

    logger.warning("ATS submission for %s failed: %s", posting_id, detail)
    log_submission(posting_id, f"ats:{provider}", OUTCOME_FAILED, detail)
    return False, detail


def manual_handoff(
    posting: dict[str, Any], applicant: dict[str, Any], letter: str, requirements: dict[str, Any],
    answers: dict[str, str] | None = None, reason: str = "",
) -> str:
    """Everything needed to apply by hand, ready to paste."""
    fields = applicant.get("fields", {})
    lines = []
    if reason:
        lines.append(f"Not submitted automatically: {reason}")
        lines.append("")
    lines.append(f"Apply here: {requirements.get('apply_url', posting.get('url', ''))}")
    lines.append("")
    lines.append("Fields to paste:")
    for label, key in (
        ("Name", "name"), ("Email", "email"), ("Phone", "phone"),
        ("Location", "location"), ("LinkedIn", "linkedin"),
        ("GitHub", "github"), ("Portfolio", "portfolio"),
    ):
        if fields.get(key):
            lines.append(f"  {label}: {fields[key]}")
    resume_path = fields.get("resume_path", "")
    if resume_path:
        exists = os.path.isfile(resume_path)
        lines.append(f"  Résumé: {resume_path}{'' if exists else '  (FILE NOT FOUND)'}")

    for question, answer in (answers or {}).items():
        marker = "  " if answer != NEEDS_INPUT else "  "
        lines.append(f"{marker}{question}\n    {answer}")

    lines.append("")
    lines.append("── Cover letter ──")
    lines.append(letter)

    log_submission(posting.get("id", ""), requirements.get("method", "manual"), OUTCOME_MANUAL, reason)
    return "\n".join(lines)
