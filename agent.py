"""The BidWatch agent: a Strands agent with six tools over Amazon Bedrock."""

from __future__ import annotations

import json
import logging
from typing import Any

from strands import Agent
from strands.models import BedrockModel

from config import (
    FIXTURE_PATH,
    MAX_POSTINGS_PER_RUN,
    MODEL_ID,
    REGION,
    SCORE_THRESHOLD,
    TAG,
)
from tools.drafting import draft, draft_proposal
from tools.fetch import fetch_job_postings, normalize_posting, remember
from tools.llm import usage_snapshot
from tools.notify import format_notification, send, send_notification
from tools.profile import load_profile, read_profile
from tools.scoring import score, score_posting
from tools.store import filter_new, filter_new_postings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are BidWatch, an assistant that monitors freelance and remote job
postings on behalf of one freelancer.

Each run:
1. Fetch the latest postings.
2. Keep only postings not seen before.
3. Load the freelancer's profile.
4. Score each new posting 0-100 for fit, with one line of reasoning.
5. For any posting scoring above the threshold, draft a short proposal
   (under 150 words) in the freelancer's voice.
6. Send exactly one notification per qualifying posting.

Hard rules:
- NEVER claim skills or experience not present in the profile.
- NEVER submit or send a proposal to a client. You draft only.
- If nothing qualifies, send nothing and end the run quietly.
- Be conservative: a missed marginal job costs less than a wasted bid
  or an inaccurate claim.
- Always include the Remote OK source attribution and the direct job
  link in every notification.

Operating limits for this run:
- The score threshold is {SCORE_THRESHOLD}. Notify only for scores strictly above it.
- Score at most {MAX_POSTINGS_PER_RUN} postings. Stop after that, even if more are new.
- Each notification message must contain: job title, company, the direct
  Remote OK job link, the score, the rationale, the draft proposal, and the
  line "Source: Remote OK".
- To keep token cost down, after fetching refer to a posting by its id only:
  pass [{{"id": "..."}}, ...] to filter_new_postings, and {{"id": "..."}} to
  score_posting and draft_proposal. Never repeat a posting's description back
  into a tool call. The tools look the full posting up for you.
- When you are done, reply with one short line: how many postings were new,
  how many were scored, and how many notifications you sent."""

TOOLS = [
    fetch_job_postings,
    filter_new_postings,
    load_profile,
    score_posting,
    draft_proposal,
    send_notification,
]


def build_agent() -> Agent:
    """Construct the BidWatch Strands agent with all six tools."""
    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
    )


def run_agent_loop(tag: str = TAG) -> str:
    """Run one cycle by letting the model drive the Strands reasoning loop."""
    agent = build_agent()
    prompt = (
        f"Run one BidWatch cycle now. Fetch postings with tag '{tag}' "
        f"(limit {MAX_POSTINGS_PER_RUN * 4}), drop the ones already seen, load the "
        f"profile, then score at most {MAX_POSTINGS_PER_RUN} of the new postings and "
        "notify me about the ones that qualify."
    )
    try:
        return str(agent(prompt)).strip()
    except Exception as exc:  # noqa: BLE001 - one bad run must not kill the scheduler
        logger.error("Agent loop failed: %s", exc)
        return f"Agent loop failed: {exc}"


def load_fixture_postings(path: str = FIXTURE_PATH) -> list[dict[str, Any]]:
    """Load bundled sample postings for --dry-run (no network, no model)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not load fixture %s: %s", path, exc)
        return []
    postings = [p for p in (normalize_posting(raw) for raw in payload[1:]) if p]
    remember(postings)
    logger.info("Loaded %d fixture postings", len(postings))
    return postings


def run_pipeline(tag: str = TAG, dry_run: bool = False) -> dict[str, int]:
    """Run one cycle deterministically, in Python rather than via the model loop.

    Used by --dry-run (no model calls at all) and available as --mode pipeline
    for a cheaper, fully predictable run. Returns per-run counters.
    """
    stats = {"fetched": 0, "new": 0, "scored": 0, "notified": 0}

    postings = load_fixture_postings() if dry_run else fetch_job_postings(tag=tag, limit=MAX_POSTINGS_PER_RUN * 4)
    stats["fetched"] = len(postings)

    fresh = filter_new(postings)
    stats["new"] = len(fresh)
    if not fresh:
        logger.info("No new postings this cycle; ending quietly.")
        return stats

    profile = read_profile()
    batch = fresh[:MAX_POSTINGS_PER_RUN]

    for posting in batch:
        try:
            if dry_run:
                # No model calls: a transparent, deterministic stand-in so the
                # non-LLM path can be exercised for free.
                result = {
                    "score": SCORE_THRESHOLD + 15,
                    "rationale": "[dry-run] Scoring skipped; no model call was made.",
                }
                proposal = "[dry-run] Proposal drafting skipped; no model call was made."
            else:
                result = score(posting, profile)
                if result["score"] <= SCORE_THRESHOLD:
                    stats["scored"] += 1
                    logger.info("Skipping %r (score %d)", posting["title"], result["score"])
                    continue
                proposal = draft(posting, profile)
            stats["scored"] += 1
            send(format_notification(posting, result["score"], result["rationale"], proposal))
            stats["notified"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad posting must not kill the run
            logger.error("Posting %s failed: %s", posting.get("id"), exc)

    return stats


def usage() -> dict[str, int]:
    """Approximate token usage for this process (best effort)."""
    return usage_snapshot()
