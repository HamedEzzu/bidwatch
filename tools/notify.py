"""Notifications: the short scannable job message, and the Telegram plumbing.

Primary channel is Telegram (with inline buttons); with no credentials
configured the same message is printed to the console and the buttons are
simply omitted, so the whole product runs offline.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests
from strands import tool

from tools.fetch import hydrate

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
ATTRIBUTION = "Source: Remote OK"

_LINK_RE = re.compile(r"https?://(?:www\.)?remoteok\.com/\S+", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Openers that describe the process rather than the company; an "About:" line
# built from one of these would tell the reader nothing.
_BOILERPLATE = (
    "we are looking", "we're looking", "about the role", "about this role",
    "job description", "position summary", "role overview", "who you are",
    "what you'll do", "what you will do", "responsibilities", "requirements",
    "apply now", "join us", "please note", "this is a remote",
)


#: Phrases that mark a sentence as describing the company rather than the task.
_COMPANY_MARKERS = (
    " is a ", " is an ", " is the ", "we are a", "we're a", "we are an",
    "our mission", "our platform", "our product", "we build", "we help",
    "founded in", "company that", "startup", "leading provider", "we make",
)


# --- message building ------------------------------------------------------

def format_salary(posting: dict[str, Any]) -> str | None:
    """Render the salary range, or None when the posting doesn't publish one."""
    low, high = posting.get("salary_min"), posting.get("salary_max")
    if not low and not high:
        return None

    def money(value: int | None) -> str | None:
        if not value:
            return None
        return f"${value // 1000}k" if value >= 10_000 else f"${value:,}"

    low_s, high_s = money(low), money(high)
    if low_s and high_s:
        return f"{low_s}–{high_s}" if low_s != high_s else low_s
    return low_s or high_s


def summarize_company(posting: dict[str, Any], max_chars: int = 140) -> str | None:
    """One line about what the company does, taken from the posting text.

    Extracted, never generated: the first sentence of the description that
    actually describes the business. Returns None when nothing usable is
    found, so the caller can drop the line entirely rather than invent one.
    """
    description = (posting.get("description") or "").strip()
    if not description:
        return None

    def clip(sentence: str) -> str:
        if len(sentence) > max_chars:
            return sentence[:max_chars].rsplit(" ", 1)[0] + "…"
        return sentence

    candidates: list[str] = []
    for sentence in _SENTENCE_RE.split(description.replace("\n", " "))[:8]:
        sentence = " ".join(sentence.split()).strip(" -–—•*")
        if not 30 <= len(sentence) <= 400:
            continue
        if any(sentence.lower().startswith(prefix) for prefix in _BOILERPLATE):
            continue
        if sentence.lower().startswith(("http", "www.")):
            continue
        candidates.append(sentence)

    # Prefer a sentence that describes the business over one describing the
    # task, but never fabricate: if only task text exists, that is what we show.
    for sentence in candidates:
        lowered = sentence.lower()
        if any(marker in lowered for marker in _COMPANY_MARKERS):
            return clip(sentence)
    return clip(candidates[0]) if candidates else None


def format_job_message(posting: dict[str, Any], score: int, rationale: str) -> str:
    """Build the short, scannable notification for one qualifying posting.

    Score first so a glance is enough. Lines that would carry no information
    (unknown salary, no usable company description) are omitted entirely
    rather than padded with "N/A".
    """
    lines = [f"[{score}/100] {posting.get('title', 'Untitled')}"]

    company = posting.get("company", "Unknown")
    location = (posting.get("location") or "").strip()
    lines.append(f"{company} — {location}" if location else company)

    about = summarize_company(posting)
    if about:
        lines.append(f"About: {about}")

    salary = format_salary(posting)
    if salary:
        lines.append(f"Salary: {salary}")

    if rationale:
        lines.append(f"Why it fits: {rationale}")

    lines.append("")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def job_buttons(posting: dict[str, Any]) -> dict[str, Any]:
    """Inline keyboard for a job message: Bid, Open (direct link), Skip."""
    posting_id = posting.get("id", "")
    return {
        "inline_keyboard": [[
            {"text": "✅ Bid", "callback_data": f"bid:{posting_id}"},
            {"text": "🔗 Open", "url": posting.get("url", "https://remoteok.com")},
            {"text": "⏭ Skip", "callback_data": f"skip:{posting_id}"},
        ]]
    }


def repair_message(message: str) -> str:
    """Fix a message composed by the model before it goes out.

    The model sometimes retypes the job URL or forgets the attribution line.
    Both are obligations under the Remote OK API terms, so links are rewritten
    from the posting BidWatch actually fetched, and the attribution appended
    when absent.
    """
    def fix(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(").,")
        trailing = url.rstrip("/").rsplit("-", 1)[-1]
        canonical = hydrate({"id": trailing}).get("url")
        if canonical and canonical.lower() != url.lower():
            logger.info("Repaired job link for posting %s", trailing)
        return canonical or url

    repaired = _LINK_RE.sub(fix, message)
    if ATTRIBUTION.lower() not in repaired.lower():
        repaired = f"{repaired}\n\n{ATTRIBUTION}"
        logger.info("Appended missing Remote OK attribution to notification.")
    return repaired


# --- Telegram transport ----------------------------------------------------

def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _call(method: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any] | None:
    """Call one Telegram Bot API method. Returns the result, or None on failure."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        response = requests.post(TELEGRAM_API.format(token=token, method=method), json=payload, timeout=timeout)
        body = response.json()
        if response.status_code == 200 and body.get("ok"):
            return body.get("result")
        logger.error("Telegram %s failed: HTTP %s %s", method, response.status_code, str(body)[:300])
    except (requests.RequestException, ValueError) as exc:
        logger.error("Telegram %s failed: %s", method, exc)
    return None


def send_message(
    text: str, buttons: dict[str, Any] | None = None, chat_id: str | None = None
) -> dict[str, Any] | None:
    """Send one Telegram message, optionally with an inline keyboard."""
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        return None
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = buttons
    return _call("sendMessage", payload)


def edit_message(chat_id: Any, message_id: Any, text: str, buttons: dict[str, Any] | None = None) -> bool:
    """Rewrite an existing message, e.g. to mark a job applied or skipped."""
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True,
    }
    payload["reply_markup"] = buttons or {"inline_keyboard": []}
    return _call("editMessageText", payload) is not None


def answer_callback(callback_id: str, text: str = "") -> bool:
    """Acknowledge a button tap so the client stops showing a spinner."""
    return _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]}) is not None


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
    """Long-poll for new updates (button taps and text replies)."""
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    result = _call("getUpdates", payload, timeout=timeout + 15)
    return result or []


# --- delivery --------------------------------------------------------------

def _print_console(message: str, buttons: dict[str, Any] | None = None) -> None:
    print("\n" + "=" * 72 + f"\n{message}\n", flush=True)
    if buttons:
        labels = [b["text"] for row in buttons.get("inline_keyboard", []) for b in row]
        if labels:
            print("Actions: " + "  ".join(labels) + "   (console mode — see --interactive)", flush=True)
    print("=" * 72 + "\n", flush=True)


def send(
    message: str,
    buttons: dict[str, Any] | None = None,
    repair: bool = True,
    console_only: bool = False,
) -> str:
    """Deliver one message: Telegram if configured, console otherwise.

    `repair` fixes links and attribution on job messages; it is off for
    non-job text like the run summary, which carries no listing. `console_only`
    forces the console path, so a dry run never posts to the real chat.
    """
    if repair:
        message = repair_message(message)
    if console_only or not telegram_configured():
        logger.info("Telegram is not configured; printing notification to console.")
        _print_console(message, buttons)
        return "Notification printed to console (Telegram not configured)."

    if send_message(message, buttons) is not None:
        logger.info("Notification sent to Telegram.")
        return "Notification sent via Telegram."

    _print_console(message, buttons)
    return "Telegram send failed; notification printed to console instead."


def notify_job(
    posting: dict[str, Any], score: int, rationale: str, console_only: bool = False
) -> str:
    """Send the notification for one qualifying posting, with its buttons."""
    return send(
        format_job_message(posting, score, rationale),
        job_buttons(posting),
        console_only=console_only,
    )


# --- ordered delivery ------------------------------------------------------
# The model may issue several send_notification calls in one parallel batch, so
# arrival order is not something it can guarantee. Notifications are therefore
# queued and flushed highest-score-first by the code, not by the model.
_pending: list[tuple[int, str, dict[str, Any], str]] = []


def queue_job_notification(posting: dict[str, Any], score: int, rationale: str) -> None:
    _pending.append(
        (int(score), format_job_message(posting, int(score), rationale), job_buttons(posting), str(posting.get("id", "")))
    )


def pending_count() -> int:
    return len(_pending)


def flush_notifications(console_only: bool = False) -> int:
    """Send every queued notification, highest score first. Returns the count."""
    from tools.store import STATUS_NOTIFIED, set_status

    ordered = sorted(_pending, key=lambda item: item[0], reverse=True)
    _pending.clear()
    for score, message, buttons, posting_id in ordered:
        send(message, buttons, console_only=console_only)
        if posting_id:
            set_status(posting_id, STATUS_NOTIFIED, score=score)
    return len(ordered)


@tool
def send_notification(posting_id: str, score: int, rationale: str) -> str:
    """Notify the freelancer about ONE qualifying job, by posting id.

    Call this once per posting that scored at or above the threshold, highest
    score first, and never twice for the same posting. Pass only the id, the
    score and your one-line rationale — BidWatch builds the message itself
    (title, company, salary, the direct Remote OK link, the attribution) and
    attaches the Bid / Open / Skip buttons, so do not write the message text.

    This notifies the freelancer only. It never contacts an employer.

    Args:
        posting_id: The id of the posting to notify about.
        score: Its fit score, 0-100.
        rationale: One sentence on why it fits.
    """
    posting = hydrate({"id": str(posting_id)})
    if not posting.get("title"):
        return f"Posting {posting_id} is not in this run's fetched postings; nothing sent."
    queue_job_notification(posting, int(score), rationale)
    return (
        f"Queued the notification for {posting.get('title')} ({int(score)}/100). "
        "It is delivered, highest score first, when you call send_run_summary."
    )


@tool
def send_run_summary(new_count: int, notified_count: int) -> str:
    """Send the one-line closing summary, AFTER all job notifications.

    This also delivers the notifications you queued, in score order, so call it
    once after the last send_notification. If nothing qualified, call nothing
    and end the run quietly.

    Args:
        new_count: How many new postings were scanned this run.
        notified_count: How many scored at or above the threshold.
    """
    delivered = flush_notifications()
    if delivered <= 0:
        return "Nothing qualified; nothing was sent."
    return send(
        f"Scanned {int(new_count)} new postings — {delivered} above threshold.",
        repair=False,
    )
