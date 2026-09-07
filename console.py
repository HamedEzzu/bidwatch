"""Console fallback: the same bid flow, driven by terminal prompts.

Used when Telegram is not configured, or with --interactive for testing the
whole flow locally. Buttons become single-key prompts; nothing else changes.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import bidflow

logger = logging.getLogger(__name__)


def _ask(prompt: str, valid: set[str]) -> str:
    """Read one key from the user. Treats a closed stdin as 'skip'."""
    while True:
        try:
            choice = input(prompt).strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return "s" if "s" in valid else next(iter(valid))
        if choice in valid:
            return choice
        print(f"Please enter one of: {', '.join(sorted(valid))}")


def handle_job(posting: dict[str, Any]) -> None:
    """Offer bid / skip / open for one notified posting."""
    if not sys.stdin.isatty():
        return
    posting_id = str(posting.get("id", ""))
    choice = _ask("[b]id  [s]kip  [o]pen  > ", {"b", "s", "o"})

    if choice == "o":
        print(f"Listing: {posting.get('url', '')}\n")
        choice = _ask("[b]id  [s]kip  > ", {"b", "s"})

    if choice == "s":
        print(bidflow.skip(posting_id) + "\n")
        return

    print("\nReading the application page and filling in your details…\n")
    draft = bidflow.start_bid(posting_id)
    if "error" in draft:
        print(f"Could not prepare this application: {draft['error']}\n")
        return
    review_loop(posting_id, draft)


def review_loop(posting_id: str, draft: dict[str, Any]) -> None:
    """Show the draft and loop on edits until confirmed or cancelled."""
    while True:
        print("\n" + "-" * 72)
        print(bidflow.render_draft(draft))
        print("-" * 72)
        method = (draft.get("requirements") or {}).get("method", "")
        prompt = "[c]onfirm & submit  [e]dit letter  [r]ésumé path  "
        options = {"c", "e", "r", "x"}
        if method in ("manual", "known_ats"):
            prompt += "[f]ill form in browser  "
            options.add("f")
        choice = _ask(prompt + "[x] cancel  > ", options)

        if choice == "f":
            print("\nOpening the application page in a browser…\n")
            _, message = bidflow.fill_form(posting_id)
            print(message + "\n")
            continue

        if choice == "r":
            path = draft.get("resume_path", "")
            print(f"\nTailored résumé: {path or 'none built'}\n")
            continue

        if choice == "x":
            print(bidflow.cancel(posting_id) + "\n")
            return

        if choice == "e":
            try:
                instruction = input("What should change? > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not instruction:
                continue
            print("Rewriting the letter…")
            draft = bidflow.revise(posting_id, instruction)
            if "error" in draft:
                print(draft["error"] + "\n")
                return
            continue

        resume_path = draft.get("resume_path", "")
        status, detail = bidflow.confirm_and_submit(posting_id)
        headers = {
            "applied_email": "✅ Applied by email",
            "applied_ats": "✅ Applied through the ATS",
            "applied_manual": "📋 Manual submission needed — everything is prepared below",
            "failed": "⚠️ Not sent",
        }
        print(f"\n{headers.get(status, status)}\n\n{detail}\n")
        if status == "applied_manual" and resume_path:
            print(f"Upload this résumé with the form: {resume_path}\n")
        return
