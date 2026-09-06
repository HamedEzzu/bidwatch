"""Reads applicant.md — the fixed paperwork used to fill in applications.

Kept separate from profile.md on purpose: profile.md decides which jobs are
worth bidding on (skills, rates, deal-breakers, voice), while this file is the
identity and the standard answers. Read fresh on every bid so it can be edited
without restarting anything.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from config import APPLICANT_PATH, BASE_DIR

logger = logging.getLogger(__name__)

MISSING_APPLICANT_MESSAGE = (
    "ERROR: applicant.md was not found. Copy applicant.example.md to applicant.md "
    "and fill in your details before bidding."
)

NEEDS_INPUT = "⚠️ NEEDS YOUR INPUT"

_BULLET_RE = re.compile(r"^\s*-\s*([^:]{2,40}):\s*(.*)$")

#: Maps the labels in applicant.md onto the field names an application asks for.
_FIELD_ALIASES = {
    "full name": "name",
    "name": "name",
    "email": "email",
    "phone": "phone",
    "location": "location",
    "timezone": "timezone",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "file path": "resume_path",
}


def read_applicant(path: str = APPLICANT_PATH) -> str:
    """Raw contents of applicant.md, or a clear error string if it is missing."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read().strip()
    except OSError as exc:
        logger.error("Could not read applicant file at %s: %s", path, exc)
        return MISSING_APPLICANT_MESSAGE
    return content or MISSING_APPLICANT_MESSAGE


def _is_placeholder(value: str) -> bool:
    """A TODO or empty value is not an answer — it needs the user's input."""
    stripped = value.strip()
    return not stripped or stripped.upper().startswith("TODO")


def parse_applicant(text: str | None = None) -> dict[str, Any]:
    """Parse applicant.md into fields, standard answers and unresolved TODOs.

    Values still marked TODO are reported in "todo" rather than being passed
    off as answers.
    """
    text = read_applicant() if text is None else text
    fields: dict[str, str] = {}
    answers: dict[str, str] = {}
    todo: list[str] = []
    section = ""

    last: tuple[str, str] | None = None  # (bucket, key) of the bullet in progress

    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip().lower()
            last = None
            continue
        match = _BULLET_RE.match(line)
        if not match:
            # An indented continuation line belongs to the bullet above it.
            if last and line.startswith((" ", "\t")) and line.strip():
                bucket, key = last
                target = answers if bucket == "answers" else fields
                target[key] = f"{target[key]} {line.strip()}".strip()
            elif not line.strip():
                last = None
            continue
        label, value = match.group(1).strip().lower(), match.group(2).strip()
        if _is_placeholder(value):
            todo.append(label)
            last = None
            continue
        if section.startswith("standard answers"):
            answers[label] = value
            last = ("answers", label)
        elif label in _FIELD_ALIASES:
            fields[_FIELD_ALIASES[label]] = value
            last = ("fields", _FIELD_ALIASES[label])
        else:
            answers[label] = value
            last = ("answers", label)

    resume_path = fields.get("resume_path", "")
    if resume_path:
        resolved = resume_path if os.path.isabs(resume_path) else os.path.join(BASE_DIR, resume_path)
        fields["resume_path"] = os.path.normpath(resolved)
        fields["resume_exists"] = str(os.path.isfile(fields["resume_path"]))

    return {"fields": fields, "answers": answers, "todo": todo, "raw": text}


def answer_question(question: str, applicant: dict[str, Any]) -> str:
    """Answer one screening question from applicant.md, or flag it.

    Matching is deliberately literal: if the file doesn't already contain the
    answer, the question is marked as needing the user rather than guessed at.
    """
    lowered = question.lower()
    keyword_map = {
        ("authorized", "authorization", "work permit", "eligible to work", "visa"): "work authorization",
        ("available", "availability", "start"): "availability",
        ("notice period",): "notice period",
        ("salary", "compensation", "rate", "pay expectation"): "salary expectation",
        ("remote", "onsite", "on-site", "in office"): "remote preference",
        ("relocate", "relocation"): "willing to relocate",
        ("language", "speak", "english", "arabic"): "languages",
        ("years of experience", "years experience", "how many years", "experience do you"): "years of experience",
    }
    for keywords, key in keyword_map.items():
        if any(keyword in lowered for keyword in keywords):
            value = applicant.get("answers", {}).get(key)
            if value:
                return value
    return NEEDS_INPUT
