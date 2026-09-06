"""Delivers one notification per qualifying posting.

Primary channel is Telegram; with no credentials configured the message is
printed to the console instead, so the whole pipeline runs offline.
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

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
ATTRIBUTION = "Source: Remote OK"


def format_notification(posting: dict[str, Any], score: int, rationale: str, draft: str) -> str:
    """Build the message body for one qualifying posting."""
    title = posting.get("title", "Untitled")
    company = posting.get("company", "Unknown")
    url = posting.get("url", "")
    salary_min, salary_max = posting.get("salary_min"), posting.get("salary_max")
    if salary_min or salary_max:
        salary = f"${salary_min or '?'} - ${salary_max or '?'}"
    else:
        salary = "not published"
    return (
        f"BidWatch — new match ({score}/100)\n\n"
        f"{title}\n"
        f"{company} · {posting.get('location', 'Remote')}\n"
        f"Salary: {salary}\n"
        f"Link: {url}\n\n"
        f"Why it fits: {rationale}\n\n"
        f"--- DRAFT PROPOSAL (review before sending) ---\n"
        f"{draft}\n\n"
        f"{ATTRIBUTION}"
    )


_LINK_RE = re.compile(r"https?://(?:www\.)?remoteok\.com/\S+", re.I)


def repair_message(message: str) -> str:
    """Fix a message the model composed by hand before it goes out.

    The model sometimes retypes the job URL (wrong capitalisation, a dropped
    character) or forgets the attribution line. Both are obligations under the
    Remote OK API terms, so links are rewritten from the fetched posting
    whenever the trailing id matches, and the attribution is appended if absent.
    """
    def fix(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(").,")
        trailing = url.rstrip("/").rsplit("-", 1)[-1]
        posting = hydrate({"id": trailing})
        canonical = posting.get("url")
        if canonical and canonical.lower() != url.lower():
            logger.info("Repaired job link for posting %s", trailing)
        return canonical or url

    repaired = _LINK_RE.sub(fix, message)
    if ATTRIBUTION.lower() not in repaired.lower():
        repaired = f"{repaired}\n\n{ATTRIBUTION}"
        logger.info("Appended missing Remote OK attribution to notification.")
    return repaired


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(message: str) -> str:
    """Send one message via Telegram, or print it if Telegram is unconfigured."""
    message = repair_message(message)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not (token and chat_id):
        logger.info("Telegram is not configured; printing notification to console.")
        print("\n" + "=" * 72 + f"\n{message}\n" + "=" * 72 + "\n", flush=True)
        return "Notification printed to console (Telegram not configured)."

    try:
        response = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
            timeout=20,
        )
        if response.status_code == 200:
            logger.info("Notification sent to Telegram chat %s", chat_id)
            return "Notification sent via Telegram."
        logger.error("Telegram returned HTTP %s: %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)

    print("\n" + "=" * 72 + f"\n{message}\n" + "=" * 72 + "\n", flush=True)
    return "Telegram send failed; notification printed to console instead."


@tool
def send_notification(message: str) -> str:
    """Send exactly one notification to the freelancer about one qualifying job.

    Call this once per posting that scored above the threshold, never more
    than once for the same posting. The message must contain the job title,
    company, the direct Remote OK link, the score, the rationale, the draft
    proposal, and a "Source: Remote OK" attribution line. This notifies the
    freelancer only — it never contacts the client.

    Args:
        message: The full notification text to deliver.
    """
    return send(message)
