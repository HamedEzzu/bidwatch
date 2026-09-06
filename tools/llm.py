"""Thin wrapper around the Bedrock model for the two reasoning tools.

Scoring and drafting each need a single, self-contained completion rather
than a tool-calling loop, so they use a tool-less Strands agent here. Token
usage from every call is accumulated so a run can report its cost.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from strands import Agent
from strands.models import BedrockModel

from config import MODEL_ID, REGION

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def get_model() -> BedrockModel:
    """Build the Bedrock model client used across BidWatch."""
    return BedrockModel(model_id=MODEL_ID, region_name=REGION)


def complete(system_prompt: str, user_prompt: str) -> str:
    """Run one completion and return the text. Returns "" on failure."""
    try:
        helper = Agent(model=get_model(), tools=[], system_prompt=system_prompt, callback_handler=None)
        result = helper(user_prompt)
        _record_usage(result)
        return str(result).strip()
    except Exception as exc:  # noqa: BLE001 - a bad model call must not kill the run
        logger.error("Model call failed: %s", exc)
        return ""


def _record_usage(result: Any) -> None:
    try:
        usage = result.metrics.accumulated_usage
        with _lock:
            _usage["input_tokens"] += int(usage.get("inputTokens", 0))
            _usage["output_tokens"] += int(usage.get("outputTokens", 0))
            _usage["calls"] += 1
    except Exception:  # noqa: BLE001 - usage reporting is best-effort
        with _lock:
            _usage["calls"] += 1


def usage_snapshot() -> dict[str, int]:
    """Approximate token usage accumulated so far in this process."""
    with _lock:
        return dict(_usage)


def reset_usage() -> None:
    with _lock:
        _usage.update({"input_tokens": 0, "output_tokens": 0, "calls": 0})
