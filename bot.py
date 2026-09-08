"""Telegram listener: handles button taps and the edit-letter conversation.

Run this alongside the scheduler:

    python bot.py            # this process listens for taps
    python scheduler.py      # that process finds jobs and notifies

Long-polls getUpdates, answers every callback promptly so buttons never hang,
and rewrites the original job message once an action resolves.

There is no submit button anywhere in here. BidWatch prepares an application
package; the person reviews it and applies.
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
    code_block,
    edit_message,
    get_updates,
    send_document,
    send_message,
    telegram_configured,
)

logger = logging.getLogger("bidwatch.bot")

# Telegram rejects messages over 4096 characters.
TELEGRAM_LIMIT = 4000


def package_buttons(posting_id: str, apply_url: str = "") -> dict[str, Any]:
    """Buttons under a prepared package: edit the letter, or mark it applied.

    No submit button, because BidWatch cannot submit. The apply link is a URL
    button so the employer's page is one tap away.
    """
    rows: list[list[dict[str, Any]]] = []
    if apply_url:
        rows.append([{"text": "🔗 Apply here", "url": apply_url}])
    rows.append([
        {"text": "✏️ Edit letter", "callback_data": f"edit:{posting_id}"},
        {"text": "✅ Mark as applied", "callback_data": f"applied:{posting_id}"},
    ])
    return {"inline_keyboard": rows}


def _split(text: str, limit: int) -> list[str]:
    """Split on line breaks, falling back to a hard cut for one long line."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        split_at = split_at if split_at > 0 else limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)
    return chunks


def send_long(text: str, buttons: dict[str, Any] | None = None) -> None:
    """Send a plain-text message, splitting it if it exceeds Telegram's limit."""
    chunks = _split(text, TELEGRAM_LIMIT)
    for index, chunk in enumerate(chunks):
        send_message(chunk, buttons if index == len(chunks) - 1 else None)


def send_copy_block(heading: str, body: str, buttons: dict[str, Any] | None = None) -> None:
    """Send text in copy blocks, splitting the BODY rather than the markup.

    Splitting the finished HTML would cut a <pre> in half, and Telegram
    rejects unbalanced markup — the message would vanish silently. So the body
    is chunked first and each chunk gets its own complete block.
    """
    room = TELEGRAM_LIMIT - len(heading) - 64   # headroom for tags and escaping
    chunks = _split(body, max(500, room))
    for index, chunk in enumerate(chunks):
        prefix = heading if index == 0 else f"{heading} (continued)"
        send_message(
            f"{prefix}\n{code_block(chunk)}",
            buttons if index == len(chunks) - 1 else None,
            html=True,
        )


def send_package(posting_id: str, draft: dict[str, Any]) -> None:
    """Deliver the prepared application: résumé file, letter, field sheet, link."""
    posting = draft["posting"]
    header = f"Application package — {posting.get('title', '')} @ {posting.get('company', '')}"

    resume_path = draft.get("resume_path", "")
    note = draft.get("resume_note", "")
    if resume_path and os.path.isfile(resume_path):
        caption = f"{header}\n\nRésumé: {note}" if note else header
        if not send_document(resume_path, caption=caption[:1000]):
            send_message(f"{header}\n\nThe résumé is on disk at:\n{resume_path}")
    else:
        send_message(f"{header}\n\n⚠️ No résumé could be generated for this one.")

    # The letter and field sheet go in code blocks: Telegram gives those a copy
    # button, which is the entire point of a package you paste into a form.
    send_copy_block("Cover letter:", draft["letter"])
    send_copy_block(
        "Application details:",
        draft["sheet"],
        buttons=package_buttons(posting_id, draft.get("apply_url", "")),
    )


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
        answer_callback(callback_id, "Preparing your application…")
        send_message("Writing the cover letter and building a résumé for this one…")
        draft = bidflow.start_bid(posting_id)
        if "error" in draft:
            send_message(f"Could not prepare this application: {draft['error']}")
            return
        edit_message(chat_id, message_id, f"📝 Package prepared\n\n{original}")
        send_package(posting_id, draft)
        return

    if action == "edit":
        answer_callback(callback_id, "Send your instructions")
        if not bidflow.set_awaiting_edit(posting_id):
            send_message("That application is no longer active. Tap Bid again to rebuild it.")
            return
        send_message(
            'What should change? Reply in plain language — e.g. "make it shorter", '
            '"less formal", "mention the WebSockets project".'
        )
        return

    if action == "applied":
        answer_callback(callback_id, "Recorded")
        send_message(bidflow.mark_applied(posting_id))
        if chat_id and message_id:
            edit_message(chat_id, message_id, f"✅ Applied\n\n{original}")
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
                "BidWatch is listening. Job alerts arrive here with Bid / Open / Skip buttons.\n\n"
                "Tap Bid and I'll prepare the whole application — a résumé tailored to that job, "
                "a cover letter, and your details ready to paste. You review it and submit it "
                "yourself; BidWatch never sends anything to an employer."
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
    send_copy_block(
        "Revised cover letter:",
        draft["letter"],
        buttons=package_buttons(posting_id, draft.get("apply_url", "")),
    )


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
