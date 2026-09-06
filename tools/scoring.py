"""Scores one posting against the freelancer profile."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from strands import tool

from tools.fetch import hydrate
from tools.llm import complete

logger = logging.getLogger(__name__)

SCORING_SYSTEM_PROMPT = """You score freelance job postings for one freelancer.

Weigh, in order:
1. Skill overlap with the profile's "Strong skills" (highest weight). Overlap
   with "Willing, but not expert" counts for less.
2. Budget or salary against the profile's target rate. Missing salary is
   neutral, not negative.
3. Deal-breakers. If the posting matches anything under "Will not bid on",
   the score MUST be below 20, whatever else it offers.

Be conservative: a missed marginal job costs less than a wasted bid.

Reply with ONLY a JSON object, no markdown fence and no other text:
{"score": <integer 0-100>, "rationale": "<one sentence>"}"""

_JSON_RE = re.compile(r"\{.*?\}", re.S)
_SCORE_RE = re.compile(r'"?score"?\s*[:=]\s*(\d{1,3})')


def parse_score_response(text: str) -> dict[str, Any]:
    """Parse the model's reply defensively; never raise, never over-score."""
    fallback = {"score": 0, "rationale": "Could not parse model response; scored 0 to stay conservative."}
    if not text:
        return fallback

    candidate = None
    match = _JSON_RE.search(text)
    if match:
        try:
            candidate = json.loads(match.group(0))
        except json.JSONDecodeError:
            candidate = None

    if isinstance(candidate, dict) and "score" in candidate:
        score = candidate.get("score")
        rationale = str(candidate.get("rationale") or "No rationale supplied.").strip()
    else:
        loose = _SCORE_RE.search(text)
        if not loose:
            return fallback
        score = loose.group(1)
        rationale = text.strip().splitlines()[0][:300]

    try:
        score_int = int(float(score))
    except (TypeError, ValueError):
        return fallback
    return {"score": max(0, min(100, score_int)), "rationale": rationale[:300]}


def score(posting: dict[str, Any], profile: str) -> dict[str, Any]:
    """Score a normalized posting dict. Returns {"score": int, "rationale": str}."""
    payload = {
        "title": posting.get("title"),
        "company": posting.get("company"),
        "tags": posting.get("tags"),
        "location": posting.get("location"),
        "salary_min": posting.get("salary_min"),
        "salary_max": posting.get("salary_max"),
        "description": posting.get("description"),
    }
    user_prompt = (
        f"FREELANCER PROFILE:\n{profile}\n\n"
        f"JOB POSTING:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Score this posting."
    )
    result = parse_score_response(complete(SCORING_SYSTEM_PROMPT, user_prompt))
    logger.info("Scored %r -> %d", posting.get("title"), result["score"])
    return result


@tool
def score_posting(posting_json: str, profile: str) -> str:
    """Score one job posting 0-100 for fit against the freelancer's profile.

    Call this once per new posting. Returns a compact JSON string:
    {"score": 0-100, "rationale": "one sentence"}. A posting that matches a
    deal-breaker in the profile always scores below 20.

    Args:
        posting_json: One posting as a JSON object string. Passing just
            {"id": "..."} is enough for a posting you already fetched.
        profile: The freelancer's profile text from load_profile.
    """
    try:
        posting = json.loads(posting_json)
    except (json.JSONDecodeError, TypeError):
        logger.error("score_posting received malformed posting JSON")
        return json.dumps({"score": 0, "rationale": "Posting could not be parsed."})
    if not isinstance(posting, dict):
        return json.dumps({"score": 0, "rationale": "Posting was not a JSON object."})
    return json.dumps(score(hydrate(posting), profile), ensure_ascii=False)
