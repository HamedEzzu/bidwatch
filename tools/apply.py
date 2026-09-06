"""Works out HOW to apply to a posting, and what the application needs.

Classifies each posting into one of three application methods:

    email      an application address is discoverable in the posting or page
    known_ats  Greenhouse, Lever, Ashby or Workable, which expose structured
               application endpoints
    manual     anything else — custom forms, unknown providers, login walls

Nothing here submits anything; it only gathers requirements.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from config import USER_AGENT
from tools.fetch import strip_html

logger = logging.getLogger(__name__)

METHOD_EMAIL = "email"
METHOD_ATS = "known_ats"
METHOD_MANUAL = "manual"

#: ATS providers that publish a documented application endpoint.
ATS_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([\w.-]+)/jobs/(\d+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([\w.-]+)/([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([\w-]+)", re.I),
    "workable": re.compile(r"([\w.-]+)\.workable\.com/j(?:obs)?/([\w-]+)", re.I),
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)

#: Local-parts that indicate an application inbox rather than, say, support.
_APPLY_HINTS = ("job", "career", "hiring", "recruit", "hr", "apply", "talent", "cv", "resume", "work")
#: Addresses that are never an application address.
_EMAIL_BLOCKLIST = ("noreply", "no-reply", "donotreply", "example.com", "sentry", "wixpress",
                    "@remoteok", "privacy", "abuse", "unsubscribe", "@sentry.io")

#: Fields an application typically asks for. Always requested, because even the
#: email path benefits from a complete, consistent set of details.
BASE_FIELDS = ("name", "email", "phone", "location", "cover_letter", "linkedin", "github", "resume")

_QUESTION_RE = re.compile(r"[^.!?\n]{15,180}\?")

#: What application forms actually ask. Deliberately narrow: a job description
#: is full of rhetorical questions ("Are you a talented developer looking for
#: your next challenge?") that are marketing copy, not screening questions.
_QUESTION_HINTS = (
    "years of experience", "years experience", "how many years", "experience do you",
    "authorized", "authorization", "work permit", "eligible to work", "visa",
    "notice period", "when can you start", "availability", "available to start",
    "salary expectation", "expected salary", "desired salary", "rate expectation",
    "willing to relocate", "relocate", "which languages", "what languages",
    "portfolio", "github profile", "linkedin profile", "cover letter",
    "why do you want", "why are you interested", "tell us about a", "describe a time",
)

#: Marketing questions dressed up as screening questions.
_QUESTION_NOISE = (
    "looking for", "do you want to join", "sound like you", "interested in joining",
    "ready to", "tired of", "want to work with us", "is this you",
)


def _fetch_page(url: str) -> tuple[str, str]:
    """Fetch an application page. Returns (final_url, html); ("", "") on failure."""
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=25, allow_redirects=True,
        )
        if response.status_code != 200:
            logger.warning("Application page %s returned HTTP %s", url, response.status_code)
            return response.url, ""
        return response.url, response.text
    except requests.RequestException as exc:
        logger.warning("Could not fetch application page %s: %s", url, exc)
        return url, ""


def detect_ats(*texts: str) -> dict[str, str] | None:
    """Find a known ATS job URL in any of the given texts."""
    for text in texts:
        if not text:
            continue
        for provider, pattern in ATS_PATTERNS.items():
            match = pattern.search(text)
            if match:
                return {
                    "provider": provider,
                    "org": match.group(1),
                    "job_id": match.group(2),
                    "url": match.group(0) if match.group(0).startswith("http") else f"https://{match.group(0)}",
                }
    return None


def find_application_email(*texts: str) -> str | None:
    """Find an address that looks like an application inbox."""
    candidates: list[str] = []
    for text in texts:
        if not text:
            continue
        for address in _EMAIL_RE.findall(text):
            lowered = address.lower()
            if any(bad in lowered for bad in _EMAIL_BLOCKLIST):
                continue
            if lowered.endswith((".png", ".jpg", ".gif", ".webp")):
                continue
            candidates.append(address)
    if not candidates:
        return None
    for address in candidates:
        local = address.split("@", 1)[0].lower()
        if any(hint in local for hint in _APPLY_HINTS):
            return address
    return candidates[0]


def find_screening_questions(text: str, limit: int = 6) -> list[str]:
    """Pull likely screening questions out of the application page text.

    Conservative on purpose. A missed question shows up as a blank the user
    fills in; a false positive puts marketing copy and a mismatched answer into
    an application, which is worse.
    """
    questions: list[str] = []
    seen: set[str] = set()
    for raw in _QUESTION_RE.findall(text or ""):
        question = " ".join(raw.split()).strip(" \"'>-–—•*")
        lowered = question.lower()
        # Scraped pages carry embedded JSON and script fragments; those are
        # never questions a human is being asked.
        if any(artifact in question for artifact in ('":', '{"', '"}', "</", "http")):
            continue
        if not any(hint in lowered for hint in _QUESTION_HINTS):
            continue
        if any(noise in lowered for noise in _QUESTION_NOISE):
            continue
        key = re.sub(r"[^a-z0-9]", "", lowered)
        if key in seen:
            continue
        seen.add(key)
        questions.append(question)
        if len(questions) >= limit:
            break
    return questions


def gather_requirements(posting: dict[str, Any]) -> dict[str, Any]:
    """Determine how to apply to this posting and what the application needs.

    Returns {method, email, ats, apply_url, fields, questions, notes}. Never
    raises: an unreachable page degrades to the manual path.
    """
    posting_url = posting.get("url", "")
    description = posting.get("description", "")
    notes: list[str] = []

    final_url, html = _fetch_page(posting_url) if posting_url else (posting_url, "")
    page_text = strip_html(html, limit=20000) if html else ""
    if posting_url and not html:
        notes.append("The listing page could not be read; classification used the posting text only.")

    # An external "apply" link on the listing usually points at the real form.
    external = ""
    for url in _URL_RE.findall(html or ""):
        if "remoteok" in url.lower() or url.lower().endswith((".css", ".js", ".png", ".jpg", ".svg")):
            continue
        if detect_ats(url):
            external = url
            break

    ats = detect_ats(external, html or "", description, posting_url)
    email = find_application_email(description, page_text)
    questions = find_screening_questions(page_text) or find_screening_questions(description)

    if ats:
        method = METHOD_ATS
        apply_url = ats["url"]
        notes.append(f"Detected {ats['provider'].title()} as the applicant tracking system.")
    elif email:
        method = METHOD_EMAIL
        apply_url = final_url or posting_url
        notes.append(f"Found an application address in the posting: {email}.")
    else:
        method = METHOD_MANUAL
        apply_url = final_url or posting_url
        notes.append("No application email or supported ATS found — this one needs a manual submission.")

    requirements = {
        "posting_id": posting.get("id", ""),
        "method": method,
        "email": email,
        "ats": ats,
        "apply_url": apply_url,
        "fields": list(BASE_FIELDS),
        "questions": questions,
        "notes": notes,
    }
    logger.info(
        "Requirements for %s: method=%s questions=%d", posting.get("id"), method, len(questions)
    )
    return requirements
