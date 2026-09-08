"""Console fallback: the same prepared package, printed to the terminal.

Used when Telegram is not configured, or with --interactive for testing the
whole flow locally. The résumé path is shown rather than attached.
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

    print("\nWriting the cover letter and building a tailored résumé…\n")
    draft = bidflow.start_bid(posting_id)
    if "error" in draft:
        print(f"Could not prepare this application: {draft['error']}\n")
        return
    review_loop(posting_id, draft)


def review_loop(posting_id: str, draft: dict[str, Any]) -> None:
    """Show the package and loop on letter edits until the user is done."""
    while True:
        print("\n" + "-" * 72)
        print(bidflow.render_package(draft))
        print("-" * 72)
        choice = _ask("[e]dit letter  [a] mark as applied  [x] close  > ", {"e", "a", "x"})

        if choice == "x":
            print(bidflow.cancel(posting_id) + "\n")
            return

        if choice == "a":
            print("\n" + bidflow.mark_applied(posting_id) + "\n")
            return

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
