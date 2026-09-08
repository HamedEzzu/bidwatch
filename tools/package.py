"""Builds the application package: the field sheet a human pastes into a form.

BidWatch prepares; the person applies. This module owns the copy-paste half of
that package — every value an application form typically asks for, taken
verbatim from applicant.md, one per line.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tools.applicant import NEEDS_INPUT

logger = logging.getLogger(__name__)

#: The lines of the field sheet, in the order forms usually ask for them.
#: (label, where it comes from) — "fields" is applicant.md's Identity/Links
#: section, "answers" its Standard answers section.
SHEET_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Full name", "fields", "name"),
    ("Email", "fields", "email"),
    ("Phone", "fields", "phone"),
    ("Location", "fields", "location"),
    ("LinkedIn", "fields", "linkedin"),
    ("GitHub", "fields", "github"),
    ("Portfolio", "fields", "portfolio"),
    ("Availability", "answers", "availability"),
    ("Salary expectation", "answers", "salary expectation"),
    ("Work authorization", "answers", "work authorization"),
    ("Notice period", "answers", "notice period"),
    ("Languages", "answers", "languages"),
    ("Years of experience", "answers", "years of experience"),
)

#: Rows worth showing even when applicant.md has no answer, because a form will
#: almost certainly ask and the gap is worth knowing about before you sit down.
ALWAYS_SHOWN = ("Full name", "Email", "Phone", "Location")


def _tidy_url(value: str) -> str:
    """Forms and humans both read a URL better without the scheme."""
    trimmed = value.replace("https://", "").replace("http://", "").rstrip("/")
    return trimmed[4:] if trimmed.startswith("www.") else trimmed


def field_sheet(applicant: dict[str, Any]) -> str:
    """One line per value, formatted for copy-paste into an application form."""
    fields = applicant.get("fields", {})
    answers = applicant.get("answers", {})
    timezone = fields.get("timezone", "")

    lines: list[str] = []
    for label, source, key in SHEET_ROWS:
        value = (fields if source == "fields" else answers).get(key, "").strip()
        if label in ("LinkedIn", "GitHub", "Portfolio") and value:
            value = _tidy_url(value)
        if label == "Location" and value and timezone:
            value = f"{value} (remote, {timezone})"
        if not value:
            if label in ALWAYS_SHOWN:
                lines.append(f"{label}: {NEEDS_INPUT} — add it to applicant.md")
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def resume_line(resume_path: str, note: str) -> str:
    """How the résumé is described in the package."""
    if not resume_path or not os.path.isfile(resume_path):
        return f"Résumé: {NEEDS_INPUT} — none could be generated"
    name = os.path.basename(resume_path)
    return f"Résumé: {note} ({name})" if note else f"Résumé: {name}"
