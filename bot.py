"""Telegram listener: handles button taps and the edit-letter conversation.

Run this alongside the scheduler:

    python bot.py            # this process listens for taps
    python scheduler.py      # that process finds jobs and notifies

Long-polls getUpdates, answers every callback promptly so buttons never hang,
and rewrites the original job message once an action resolves.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

import bidflow
from tools.notify import (
    answer_callback,
    edit_message,
    get_updates,
    send_document,
    send_message,
    telegram_configured,
)

logger = logging.getLogger("bidwatch.bot")

CONFIRM_BUTTONS = {
    "inline_keyboard": [
        [
            {"text": "📤 Confirm & Submit", "callback_data": "confirm:{id}"},
            {"text": "✏️ Edit letter", "callback_data": "edit:{id}"},
        ],
        [
            {"text": "📄 View résumé", "callback_data": "resume:{id}"},
            {"text": "✖️ Cancel", "callback_data": "cancel:{id}"},
        ],
    ]
}

# Telegram rejects messages over 4096 characters.
TELEGRAM_LIMIT = 4000


def confirm_buttons(posting_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": button["text"], "callback_data": button["callback_data"].format(id=posting_id)}
             for button in row]
            for row in CONFIRM_BUTTONS["inline_keyboard"]
        ]
    }


def send_long(text: str, buttons: dict[str, Any] | None = None) -> None:
    """Send a message, splitting it if it exceeds Telegram's length limit."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_LIMIT:
        split_at = remaining.rfind("\n", 0, TELEGRAM_LIMIT)
        split_at = split_at if split_at > 0 else TELEGRAM_LIMIT
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)
    for index, chunk in enumerate(chunks):
        send_message(chunk, buttons if index == len(chunks) - 1 else None)


def handle_callback(callback: dict[str, Any]) -> None:
    """Handle one button tap."""
    data = callback.get("data", "")
    callback_id = callback.get("id", "")
    message = callback.get("message", {}) or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original = message.get("text", "")

    action, _, posting_id = data.partition(":")
    logger.info("Button tap: action=%s posting=%s", action, posting_id)

    if action == "skip":
        answer_callback(callback_id, "Skipped")
        bidflow.skip(posting_id)
        edit_message(chat_id, message_id, f"⏭ Skipped\n\n{original}")
        return

    if action == "bid":
        answer_callback(callback_id, "Reading the application page…")
        send_message("Reading the application page and filling in your details…")
        draft = bidflow.start_bid(posting_id)
        if "error" in draft:
            send_message(f"Could not prepare this application: {draft['error']}")
            return
        edit_message(chat_id, message_id, f"📝 Bidding…\n\n{original}")
        send_long(bidflow.render_draft(draft), confirm_buttons(posting_id))
        return

    if action == "edit":
        answer_callback(callback_id, "Send your instructions")
        if not bidflow.set_awaiting_edit(posting_id):
            send_message("That draft is no longer active. Tap Bid again to restart.")
            return
        send_message(
            "What should change? Reply in plain language — e.g. \"make it shorter\", "
            "\"less formal\", \"mention the WebSockets project\"."
        )
        return

    if action == "resume":
        answer_callback(callback_id, "Sending the résumé…")
        draft = bidflow.get_draft(posting_id)
        path = (draft or {}).get("resume_path", "")
        if not path or not os.path.isfile(path):
            send_message("No résumé has been built for this application. Tap Bid again to rebuild it.")
            return
        note = (draft or {}).get("resume_note", "")
        if not send_document(path, caption=f"Tailored résumé — {note}" if note else "Tailored résumé"):
            send_message(f"Could not upload the résumé. It is on disk at:\n{path}")
        return

    if action == "cancel":
        answer_callback(callback_id, "Cancelled")
        send_message(bidflow.cancel(posting_id))
        return

    if action == "confirm":
        answer_callback(callback_id, "Submitting…")
        # Captured before submission clears the draft.
        resume_before = (bidflow.get_draft(posting_id) or {}).get("resume_path", "")
        status, detail = bidflow.confirm_and_submit(posting_id)
        headers = {
            "applied_email": "✅ Applied by email",
            "applied_ats": "✅ Applied through the ATS",
            "applied_manual": "📋 Manual submission needed — everything is prepared below",
            "failed": "⚠️ Not sent",
        }
        send_long(f"{headers.get(status, status)}\n\n{detail}")
        if status == "applied_manual" and resume_before:
            # Manual submission means the user uploads it themselves, so put the
            # file in the chat rather than only naming its path.
            send_document(resume_before, caption="Tailored résumé for this application")
        return

    answer_callback(callback_id, "Unknown action")


def handle_message(message: dict[str, Any]) -> None:
    """Handle a plain text reply — the edit-letter loop."""
    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        if text.startswith("/start") or text.startswith("/help"):
            send_message(
                "BidWatch is listening. Job alerts arrive here with Bid / Open / Skip buttons.\n"
                "Tap Bid to prepare an application; nothing is ever sent without your "
                "explicit Confirm & Submit."
            )
        return

    posting_id = bidflow.awaiting_edit_id()
    if not posting_id:
        return

    send_message("Rewriting the letter…")
    draft = bidflow.revise(posting_id, text)
    if "error" in draft:
        send_message(draft["error"])
        return
    send_long(bidflow.render_draft(draft), confirm_buttons(posting_id))


def poll(poll_timeout: int = 30) -> None:
    """Long-poll for updates until interrupted."""
    offset: int | None = None
    logger.info("Listening for button taps and replies. Ctrl-C to stop.")
    while True:
        try:
            updates = get_updates(offset=offset, timeout=poll_timeout)
        except Exception as exc:  # noqa: BLE001 - the listener must stay up
            logger.error("getUpdates failed: %s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update.get("update_id", 0) + 1
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                logger.exception("Failed to handle update %s: %s", update.get("update_id"), exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="BidWatch Telegram listener.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("strands").setLevel(logging.WARNING)

    if not telegram_configured():
        logger.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set, so there are no buttons to "
            "listen for. Use 'python scheduler.py --once --interactive' for the console flow."
        )
        return 1

    try:
        poll()
    except KeyboardInterrupt:
        logger.info("Listener stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
