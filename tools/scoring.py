"""Scores one posting against the freelancer profile."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from strands import tool

from tools.fetch import hydrate
from tools.llm import complete
from tools.profile import bidding_profile

logger = logging.getLogger(__name__)

SCORING_SYSTEM_PROMPT = """You score remote job postings for one freelancer.

The score answers ONE question: how well does this work match what the
freelancer can actually do, at a rate they would accept?

Weigh, in order:
1. Stack overlap with the profile's "Skills" and "Projects" sections — this
   dominates the score. Overlap with "Willing, but not expert" counts for less
   but still counts.
2. Deal-breakers. If the posting matches anything under "Will not bid on"
   (page-builder/WordPress work, pure visual design, on-site presence, a
   required language other than English or Arabic, trivially small budgets),
   the score MUST be below 20 no matter how good the rest looks.
3. Rate or salary against the profile's target. A missing salary is NEUTRAL —
   most listings omit it — never a reason to mark down.

Calibration, important:
- These listings are all sourced from a board that publishes permanent,
  salaried, full-time roles and does not expose contract type. So "full-time",
  "permanent" or "salaried" is NOT a negative and must not reduce the score.
- A senior title or a "5+ years experience" line is a MILD penalty only (a few
  points). The freelancer decides for themselves whether to apply; your job is
  to judge the work, not to screen them out of it.
- Judge only what the posting says. Do not infer a rejection from the
  freelancer being a student or part-time.

Guide: 80-100 the stack is squarely in the strong skills; 65-79 solid overlap
with some gaps; 40-64 partial overlap or a materially different stack; 20-39
little overlap; 0-19 a deal-breaker or an unrelated field entirely.

Be honest in both directions: do not inflate a poor match, and do not bury a
good one under caveats.

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
def score_posting(posting_json: str, profile: str = "") -> str:
    """Score one job posting 0-100 for fit against the freelancer's profile.

    Call this once per new posting. Returns a compact JSON string:
    {"score": 0-100, "rationale": "one sentence"}. A posting that matches a
    deal-breaker in the profile always scores below 20.

    Args:
        posting_json: One posting as a JSON object string. Passing just
            {"id": "..."} is enough for a posting you already fetched.
        profile: LEAVE THIS EMPTY. BidWatch reads the profile from disk itself.
            Do not paste the profile text here — it is long, and repeating it
            in every call wastes the run's budget and can truncate the call.
    """
    try:
        posting = json.loads(posting_json)
    except (json.JSONDecodeError, TypeError):
        logger.error("score_posting received malformed posting JSON")
        return json.dumps({"score": 0, "rationale": "Posting could not be parsed."})
    if not isinstance(posting, dict):
        return json.dumps({"score": 0, "rationale": "Posting was not a JSON object."})

    posting = hydrate(posting)
    if not posting.get("title"):
        logger.error("score_posting could not resolve posting %r", posting.get("id"))
        return json.dumps({"score": 0, "rationale": "That posting is not in this run's fetched postings."})

    # The profile is read from disk, never taken from the model: a truncated
    # paste would silently score a posting against half a profile.
    result = score(posting, bidding_profile())

    # Record the verdict and act on it here, in code. The threshold is not a
    # judgement call: a run once scored a posting 68 against a threshold of 65
    # and then concluded nothing qualified. Below the line is marked rejected
    # so it is not paid for twice; at or above it is queued for delivery, so a
    # qualifying job reaches the freelancer whatever the model decides next.
    from config import SCORE_THRESHOLD
    from tools.notify import queue_job_notification
    from tools.store import STATUS_REJECTED, set_status

    posting_id = str(posting.get("id", ""))
    if result["score"] < SCORE_THRESHOLD:
        set_status(posting_id, STATUS_REJECTED, score=result["score"])
        result["action"] = "below threshold; recorded as rejected"
    else:
        queue_job_notification(posting, result["score"], result["rationale"])
        result["action"] = "queued for notification; call send_run_summary when finished"
    return json.dumps(result, ensure_ascii=False)
