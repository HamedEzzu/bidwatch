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
from tools.fetch import fetch_job_postings, normalize_posting, remember
from tools.llm import usage_snapshot
from tools.notify import (
    flush_notifications,
    format_job_message,
    notify_job,
    send,
    send_notification,
    send_run_summary,
    telegram_configured,
)
from tools.profile import load_profile, read_profile
from tools.scoring import score, score_posting
from tools.store import STATUS_NOTIFIED, STATUS_REJECTED, filter_new, filter_new_postings, set_status

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You are BidWatch, an assistant that monitors freelance and remote job
postings on behalf of one freelancer.

Each run:
1. Fetch the latest postings.
2. Keep only postings not seen before.
3. Load the freelancer's profile once, for your own understanding.
4. Score each new posting 0-100 for fit, with one line of reasoning.
5. Send exactly one notification per qualifying posting, highest score first.
6. Send the closing summary once, after the notifications.

Hard rules:
- NEVER claim skills or experience not present in the profile.
- NEVER submit or send a proposal to a client. You draft only.
- If nothing qualifies, send nothing and end the run quietly.
- Be conservative: a missed marginal job costs less than a wasted bid
  or an inaccurate claim.
- Always include the Remote OK source attribution and the direct job
  link in every notification.

Operating limits for this run:
- The score threshold is {SCORE_THRESHOLD}. Notify for every posting scoring at
  or above it — not just the best one — highest score first.
- Score at most {MAX_POSTINGS_PER_RUN} postings. Stop after that, even if more are new.
- Each notification message must contain: job title, company, the direct
  Remote OK job link, the score, the rationale, the draft proposal, and the
  line "Source: Remote OK".
- The feed mixes software roles with unrelated listings (retail, logistics,
  hospitality). When choosing which postings to score, pick the ones whose
  title and tags look like software development work — do not simply take the
  first few.
- To keep token cost down, after fetching refer to a posting by its id only:
  pass [{{"id": "..."}}, ...] to filter_new_postings, and {{"id": "..."}} to
  score_posting. Never repeat a posting's description back into a tool call. The tools look the full posting up for you.
- When you are done, reply with one short line: how many postings were new,
  how many were scored, and how many notifications you sent."""

TOOLS = [
    fetch_job_postings,
    filter_new_postings,
    load_profile,
    score_posting,
    send_notification,
    send_run_summary,
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
        f"profile, then score up to {MAX_POSTINGS_PER_RUN} of the new postings and "
        "notify me about every one that qualifies, highest score first, then send "
        "the closing summary."
    )
    try:
        summary = str(agent(prompt)).strip()
    except Exception as exc:  # noqa: BLE001 - one bad run must not kill the scheduler
        logger.error("Agent loop failed: %s", exc)
        summary = f"Agent loop failed: {exc}"
    finally:
        # Safety net: deliver anything the model queued but never flushed, so a
        # run that ends early still notifies about the jobs it found.
        stranded = flush_notifications()
        if stranded:
            logger.warning("Flushed %d notification(s) the model left queued.", stranded)
            send(f"Scanned this run — {stranded} above threshold.", repair=False)
    return summary


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


#: Skills worth spending the per-run scoring budget on, derived from the
#: profile's Skills section. Used only to ORDER candidates, never to
#: discard them, and it costs nothing — no model call is involved.
PRIORITY_KEYWORDS = (
    "python", "fastapi", "django", "sqlalchemy", "pytest", "c#", "csharp",
    ".net", "dotnet", "asp.net", "entity framework", "postgres", "postgresql",
    "sql server", "sql", "javascript", "typescript", "node", "express",
    "react", "docker", "linux", "git", "websocket", "backend", "full stack",
    "api", "developer", "engineer", "software",
)


def _relevance(posting: dict[str, Any]) -> int:
    """How many profile keywords a posting mentions. Free — no model call."""
    haystack = " ".join(
        [posting.get("title", ""), " ".join(posting.get("tags", [])), posting.get("description", "")[:600]]
    ).lower()
    return sum(1 for keyword in PRIORITY_KEYWORDS if keyword in haystack)


def prioritize(postings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order postings by keyword overlap with the profile's strong skills.

    The feed is newest-first and mixes software roles with unrelated retail and
    logistics listings, so scoring the newest N would often spend the whole
    per-run model budget on jobs that could never fit. Nothing is discarded —
    only reordered, so the cap is spent on the most plausible candidates first.
    """
    return sorted(postings, key=_relevance, reverse=True)


def run_pipeline(tag: str = TAG, dry_run: bool = False, interactive: bool = False) -> dict[str, int]:
    """Run one cycle deterministically, in Python rather than via the model loop.

    Scores every new posting up to MAX_POSTINGS_PER_RUN, notifies about every
    one that scores at or above the threshold (highest first), and closes with
    a one-line summary. If nothing qualifies, nothing is sent at all.

    Used by --dry-run (no model calls) and available as --mode pipeline.
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
    batch = prioritize(fresh)[:MAX_POSTINGS_PER_RUN]
    qualifying: list[tuple[int, str, dict[str, Any]]] = []

    for posting in batch:
        try:
            if dry_run:
                # No model calls. The stand-in score is derived from the free
                # keyword ranking purely so the ordering and threshold logic
                # are visible offline; it is not a judgement about the job.
                result = {
                    "score": min(99, SCORE_THRESHOLD + 5 + 3 * _relevance(posting)),
                    "rationale": "[dry-run] Scoring skipped; no model call was made.",
                }
            else:
                result = score(posting, profile)
            stats["scored"] += 1
            if result["score"] >= SCORE_THRESHOLD:
                qualifying.append((result["score"], result["rationale"], posting))
            else:
                set_status(posting["id"], STATUS_REJECTED, score=result["score"])
                logger.info("Skipping %r (score %d)", posting["title"], result["score"])
        except Exception as exc:  # noqa: BLE001 - one bad posting must not kill the run
            logger.error("Posting %s failed: %s", posting.get("id"), exc)

    # Highest score first, so the best job is the first thing read.
    qualifying.sort(key=lambda item: item[0], reverse=True)

    for score_value, rationale, posting in qualifying:
        try:
            notify_job(posting, score_value, rationale, console_only=dry_run)
            set_status(posting["id"], STATUS_NOTIFIED, score=score_value)
            stats["notified"] += 1
            if interactive and not telegram_configured():
                import console

                console.handle_job(posting)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not notify about posting %s: %s", posting.get("id"), exc)

    # Nothing qualified means nothing is sent — no summary, no "no jobs" message.
    if stats["notified"]:
        send(
            f"Scanned {stats['new']} new postings — {stats['notified']} above threshold.",
            repair=False,
            console_only=dry_run,
        )

    return stats


def usage() -> dict[str, int]:
    """Approximate token usage for this process (best effort)."""
    return usage_snapshot()
