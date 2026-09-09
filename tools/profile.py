"""Loads the job seeker profile that everything else is judged against."""

from __future__ import annotations

import logging

from strands import tool

from config import PROFILE_PATH

logger = logging.getLogger(__name__)

MISSING_PROFILE_MESSAGE = (
    "ERROR: profile.md was not found. Create it in the project root with the "
    "job seeker's skills, rates, deal-breakers and proposal voice before scoring."
)


def read_profile(path: str = PROFILE_PATH) -> str:
    """Read profile.md from disk. Returns an error string if it is missing."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read().strip()
    except OSError as exc:
        logger.error("Could not read profile at %s: %s", path, exc)
        return MISSING_PROFILE_MESSAGE
    if not content:
        logger.warning("Profile at %s is empty", path)
        return MISSING_PROFILE_MESSAGE
    return content


#: Sections that decide whether a job is worth bidding on. The career-database
#: half of profile.md (summaries, project bullets) is for résumé building and
#: only dilutes a scoring prompt.
BIDDING_SECTIONS = (
    "skills", "willing, but not expert", "rates", "will not bid on", "context",
)


def bidding_profile(path: str = PROFILE_PATH) -> str:
    """The parts of the profile that matter for scoring a job.

    profile.md doubles as a career database for résumé generation; feeding all
    of it into every scoring call is expensive and blunts the signal. Falls
    back to the whole file if the expected sections are not found.
    """
    text = read_profile(path)
    if text.startswith("ERROR:"):
        return text

    kept: list[str] = []
    keeping = False
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            keeping = any(heading.startswith(section) for section in BIDDING_SECTIONS)
        elif line.startswith("# "):
            keeping = False
        if keeping:
            kept.append(line)

    extracted = "\n".join(kept).strip()
    if len(extracted) < 200:
        logger.warning("Could not extract bidding sections from the profile; using the whole file.")
        return text
    return extracted


@tool
def load_profile() -> str:
    """Load the job seeker's profile: skills, rates, deal-breakers and voice.

    Call this once per run before scoring. The file is read fresh every time,
    so the job seeker can edit profile.md without restarting the agent.
    """
    return read_profile()
