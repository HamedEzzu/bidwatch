"""Generates the cover letter for an application, and revises it on request."""

from __future__ import annotations

import json
import logging
from typing import Any

from config import COVER_LETTER_MAX_WORDS
from tools.llm import complete

logger = logging.getLogger(__name__)

LETTER_SYSTEM_PROMPT = f"""You write job application cover letters for one applicant,
in the voice described in their profile.

Hard rules:
- NEVER claim a skill, tool, employer or experience that is not in the profile or
  the applicant details you are given. If the posting asks for something the
  applicant does not have, say plainly what they know and what they do not.
- Open by referencing the employer's actual problem or product, in their words.
- Be specific. No filler, no flattery, no "I am excited to apply".
- Mention one concrete, relevant thing from the profile.
- End with one clarifying question about the role.
- Under {COVER_LETTER_MAX_WORDS} words.

Reply with the letter body ONLY — no subject line, no "Dear Hiring Manager"
salutation block beyond a simple greeting, no signature block, no markdown."""


def _posting_payload(posting: dict[str, Any]) -> str:
    return json.dumps(
        {
            "title": posting.get("title"),
            "company": posting.get("company"),
            "tags": posting.get("tags"),
            "description": posting.get("description"),
        },
        ensure_ascii=False,
    )


def generate_letter(posting: dict[str, Any], profile: str, applicant_text: str) -> str:
    """Write the first draft of the cover letter for this posting."""
    prompt = (
        f"APPLICANT PROFILE (skills, rates, voice):\n{profile}\n\n"
        f"APPLICANT DETAILS:\n{applicant_text}\n\n"
        f"JOB POSTING:\n{_posting_payload(posting)}\n\n"
        "Write the cover letter."
    )
    letter = complete(LETTER_SYSTEM_PROMPT, prompt)
    if not letter:
        return ""
    logger.info("Generated cover letter for posting %s", posting.get("id"))
    return letter.strip()


def revise_letter(
    posting: dict[str, Any], profile: str, applicant_text: str, current: str, instruction: str
) -> str:
    """Rewrite the letter following the user's plain-language instruction.

    The hard rules still apply: an instruction cannot talk the model into
    claiming experience the profile does not contain.
    """
    prompt = (
        f"APPLICANT PROFILE (skills, rates, voice):\n{profile}\n\n"
        f"APPLICANT DETAILS:\n{applicant_text}\n\n"
        f"JOB POSTING:\n{_posting_payload(posting)}\n\n"
        f"CURRENT LETTER:\n{current}\n\n"
        f"The applicant asked for this change:\n{instruction}\n\n"
        "Rewrite the letter applying that change. Keep every hard rule: never "
        "claim experience that is not in the profile or applicant details, even "
        "if the requested change seems to ask for it."
    )
    revised = complete(LETTER_SYSTEM_PROMPT, prompt)
    if not revised:
        logger.error("Revision failed for posting %s; keeping the previous letter", posting.get("id"))
        return current
    logger.info("Revised cover letter for posting %s", posting.get("id"))
    return revised.strip()
