"""Loads the freelancer profile that everything else is judged against."""

from __future__ import annotations

import logging

from strands import tool

from config import PROFILE_PATH

logger = logging.getLogger(__name__)

MISSING_PROFILE_MESSAGE = (
    "ERROR: profile.md was not found. Create it in the project root with the "
    "freelancer's skills, rates, deal-breakers and proposal voice before scoring."
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


@tool
def load_profile() -> str:
    """Load the freelancer's profile: skills, rates, deal-breakers and voice.

    Call this once per run before scoring. The file is read fresh every time,
    so the freelancer can edit profile.md without restarting the agent.
    """
    return read_profile()
