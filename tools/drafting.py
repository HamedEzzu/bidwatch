"""Drafts a proposal for a posting that cleared the score threshold."""

from __future__ import annotations

import json
import logging
from typing import Any

from strands import tool

from tools.fetch import hydrate
from tools.llm import complete

logger = logging.getLogger(__name__)

DRAFTING_SYSTEM_PROMPT = """You write freelance proposal drafts for one freelancer,
in the voice described in their profile.

Hard rules:
- NEVER claim a skill, tool or experience that is not in the profile. If the
  posting needs something the profile does not list, say plainly what the
  freelancer knows and what they do not.
- Open by referencing the client's actual problem, in their own words.
- Be specific. No filler, no flattery, no "I am excited to apply".
- Mention one concrete, relevant thing from the profile.
- End with one clarifying question about the project.
- Under 150 words.

Reply with the proposal text only. No subject line, no preamble, no markdown."""

MAX_WORDS = 150


def _trim(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;:") + " …"


def draft(posting: dict[str, Any], profile: str) -> str:
    """Draft a proposal for a normalized posting dict."""
    payload = {
        "title": posting.get("title"),
        "company": posting.get("company"),
        "tags": posting.get("tags"),
        "description": posting.get("description"),
    }
    user_prompt = (
        f"FREELANCER PROFILE:\n{profile}\n\n"
        f"JOB POSTING:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Write the proposal draft."
    )
    text = complete(DRAFTING_SYSTEM_PROMPT, user_prompt)
    if not text:
        return "[Draft unavailable: the model could not be reached for this posting.]"
    return _trim(text)


@tool
def draft_proposal(posting_json: str, profile: str) -> str:
    """Draft a proposal under 150 words for one posting, in the freelancer's voice.

    Call this only for postings that scored above the threshold. The draft
    opens on the client's actual problem and never claims a skill that is not
    in the profile. It is a DRAFT for the freelancer to review — it is never
    sent to the client.

    Args:
        posting_json: One posting as a JSON object string. Passing just
            {"id": "..."} is enough for a posting you already fetched.
        profile: The freelancer's profile text from load_profile.
    """
    try:
        posting = json.loads(posting_json)
    except (json.JSONDecodeError, TypeError):
        return "[Draft unavailable: posting could not be parsed.]"
    if not isinstance(posting, dict):
        return "[Draft unavailable: posting was not a JSON object.]"
    return draft(hydrate(posting), profile)
