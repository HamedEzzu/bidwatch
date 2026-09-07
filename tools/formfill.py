"""Opens a job application form in a real browser and fills it in.

The hard rule: **this never submits anything.** It fills the fields, drops a
banner on the page, and hands the browser to the user. There is no code path
here that clicks a submit control.

The work is split so it can be tested without a browser:

* `describe_fields` / `plan_fills` are pure functions over field descriptors —
  everything about matching is unit-testable and mockable.
* `open_prefilled_application` is the thin Playwright layer that collects
  descriptors, executes the plan, and keeps the window open.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from typing import Any

from config import BROWSER_PROFILE_DIR, HEADED_BROWSER
from tools.applicant import NEEDS_INPUT
from tools.llm import complete

logger = logging.getLogger(__name__)

# --- canonical fields ------------------------------------------------------

#: Every value BidWatch can put into a form, in the order it prefers to fill.
CANONICAL_FIELDS = (
    "first_name", "last_name", "full_name", "email", "phone", "location",
    "linkedin", "github", "portfolio", "cover_letter", "resume",
    "salary", "availability", "notice", "work_auth", "relocate",
    "languages", "years_experience",
)

#: Exact `name`/`id` attribute matches. Cheapest and most reliable layer.
EXACT_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "first_name": ("first_name", "firstname", "fname", "given_name", "givenname", "first"),
    "last_name": ("last_name", "lastname", "lname", "family_name", "familyname", "surname", "last"),
    "full_name": ("name", "full_name", "fullname", "your_name", "candidate_name", "applicant_name"),
    "email": ("email", "email_address", "emailaddress", "e_mail", "candidate_email"),
    "phone": ("phone", "phone_number", "phonenumber", "telephone", "tel", "mobile", "contact_number"),
    "location": ("location", "city", "address", "candidate_location", "current_location"),
    # Lever names these fields "urls[LinkedIn]"; _attribute_key normalizes the
    # brackets to underscores, so the entries here are in normalized form.
    "linkedin": ("linkedin", "linkedin_url", "linkedin_profile", "urls_linkedin"),
    "github": ("github", "github_url", "github_profile", "urls_github"),
    "portfolio": ("portfolio", "website", "personal_website", "urls_portfolio", "urls_other"),
    "cover_letter": ("cover_letter", "coverletter", "cover_letter_text", "letter", "comments", "message"),
    "resume": ("resume", "cv", "resume_file", "cv_file", "attachment"),
    "salary": ("salary", "salary_expectation", "expected_salary", "desired_salary", "compensation", "rate"),
    "notice": ("notice", "notice_period"),
    "availability": ("availability", "start_date", "available_from"),
}

#: Fuzzy label matching. Each canonical field lists phrases that identify it;
#: `avoid` blocks a near-miss (a "last name" label must not match "name").
LABEL_KEYWORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "first_name": {"match": ("first name", "given name", "forename"), "avoid": ()},
    "last_name": {"match": ("last name", "family name", "surname"), "avoid": ()},
    "full_name": {"match": ("full name", "your name", "name"), "avoid": ("first", "last", "family", "given", "user", "company", "file")},
    "email": {"match": ("email", "e-mail"), "avoid": ()},
    "phone": {"match": ("phone", "mobile", "telephone", "contact number"), "avoid": ()},
    "location": {"match": ("location", "city", "where are you based", "address", "country"), "avoid": ("email",)},
    "linkedin": {"match": ("linkedin",), "avoid": ()},
    "github": {"match": ("github", "gitlab", "code sample"), "avoid": ()},
    "portfolio": {"match": ("portfolio", "website", "personal site", "your site"), "avoid": ("linkedin", "github")},
    "cover_letter": {"match": ("cover letter", "why do you want", "tell us about yourself", "message", "additional information", "anything else"), "avoid": ()},
    "salary": {"match": ("salary", "compensation", "rate expectation", "expected pay", "desired pay"), "avoid": ()},
    "availability": {"match": ("availability", "when can you start", "start date", "available"), "avoid": ()},
    "notice": {"match": ("notice period",), "avoid": ()},
    "work_auth": {"match": ("authorized", "authorisation", "authorization", "work permit", "eligible to work", "visa", "sponsorship"), "avoid": ()},
    "relocate": {"match": ("relocate", "relocation"), "avoid": ()},
    "languages": {"match": ("languages", "which languages", "language proficiency"), "avoid": ("programming",)},
    "years_experience": {"match": ("years of experience", "years experience", "how many years"), "avoid": ()},
}

#: Provider-specific selectors, tried before anything else. These DOMs are
#: stable, and they cover a large share of postings.
ATS_SELECTORS: dict[str, dict[str, str]] = {
    "greenhouse": {
        "first_name": "#first_name", "last_name": "#last_name", "email": "#email",
        "phone": "#phone", "cover_letter": "#cover_letter_text", "resume": "input#resume",
    },
    "lever": {
        "full_name": "input[name='name']", "email": "input[name='email']",
        "phone": "input[name='phone']", "location": "input[name='location']",
        "linkedin": "input[name='urls[LinkedIn]']", "github": "input[name='urls[GitHub]']",
        "portfolio": "input[name='urls[Portfolio]']", "cover_letter": "textarea[name='comments']",
        "resume": "input[name='resume']",
    },
    "ashby": {
        "full_name": "input[name='_systemfield_name']", "email": "input[name='_systemfield_email']",
        "phone": "input[name='_systemfield_phone']", "resume": "input[name='_systemfield_resume']",
    },
    "workable": {
        "first_name": "input[name='firstname']", "last_name": "input[name='lastname']",
        "email": "input[name='email']", "phone": "input[name='phone']",
        "cover_letter": "textarea[name='coverletter']", "resume": "input[name='resume']",
    },
}

#: Inputs that belong to a site's chrome, not to an application form. Job
#: boards are full of these, and filling one puts your phone number into a
#: search box.
JUNK_FIELD_MARKERS = (
    "search", "query", "keyword", "filter", "subscribe", "newsletter",
    "login", "signin", "sign in", "password", "coupon", "promo", "discount",
    "csrf", "captcha", "consent", "cookie", "currency", "sort",
)

_WORD_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: str | None) -> str:
    return _WORD_RE.sub(" ", (value or "").lower()).strip()


def _attribute_key(value: str | None) -> str:
    return _WORD_RE.sub("_", (value or "").lower()).strip("_")


# --- what we have to offer -------------------------------------------------

def applicant_values(applicant: dict[str, Any], letter: str, answers: dict[str, str] | None = None) -> dict[str, str]:
    """Flatten applicant.md plus this application's letter into fillable values."""
    fields = applicant.get("fields", {})
    standard = applicant.get("answers", {})
    name = fields.get("name", "").strip()
    first, _, last = name.partition(" ")

    values = {
        "full_name": name,
        "first_name": first,
        "last_name": last.strip(),
        "email": fields.get("email", ""),
        "phone": fields.get("phone", ""),
        "location": fields.get("location", ""),
        "linkedin": fields.get("linkedin", ""),
        "github": fields.get("github", ""),
        "portfolio": fields.get("portfolio", ""),
        "cover_letter": letter or "",
        "salary": standard.get("salary expectation", ""),
        "availability": standard.get("availability", ""),
        "notice": standard.get("notice period", ""),
        "work_auth": standard.get("work authorization", ""),
        "relocate": standard.get("willing to relocate", ""),
        "languages": standard.get("languages", ""),
        "years_experience": standard.get("years of experience", ""),
    }
    for question, answer in (answers or {}).items():
        if answer and answer != NEEDS_INPUT:
            values.setdefault(f"question::{_normalize(question)}", answer)
    return {key: value for key, value in values.items() if str(value).strip()}


# --- matching --------------------------------------------------------------

def is_junk_field(descriptor: dict[str, Any]) -> bool:
    """True for site chrome — search boxes, newsletter signups, login fields."""
    if descriptor.get("type") in ("search", "password", "checkbox", "radio", "range", "color"):
        return descriptor.get("type") in ("search", "password")
    haystack = " ".join([
        descriptor.get("name", ""), descriptor.get("id", ""), descriptor.get("label", ""),
        descriptor.get("placeholder", ""), descriptor.get("aria", ""),
    ]).lower()
    if any(marker in haystack for marker in JUNK_FIELD_MARKERS):
        return True
    return bool(descriptor.get("in_chrome"))


def describe_fields(raw_fields: list[dict[str, Any]], drop_junk: bool = True) -> list[dict[str, Any]]:
    """Normalize raw DOM field data into descriptors used by the matcher.

    Site chrome (search, login, newsletter) is dropped by default: those are
    not part of the application, and filling them is worse than useless.
    """
    descriptors = []
    for index, field in enumerate(raw_fields):
        descriptor = {
            "index": field.get("index", index),
            "tag": (field.get("tag") or "input").lower(),
            "type": (field.get("type") or "text").lower(),
            "name": field.get("name") or "",
            "id": field.get("id") or "",
            "label": field.get("label") or "",
            "placeholder": field.get("placeholder") or "",
            "aria": field.get("aria") or "",
            "required": bool(field.get("required")),
            "selector": field.get("selector") or "",
            "in_chrome": bool(field.get("in_chrome")),
        }
        if drop_junk and is_junk_field(descriptor):
            logger.debug("Ignoring site-chrome field: %s", descriptor.get("name") or descriptor.get("label"))
            continue
        descriptors.append(descriptor)
    return descriptors


def looks_like_application_form(descriptors: list[dict[str, Any]], form_count: int) -> bool:
    """Is this page actually an application form, or just a page with inputs?

    A job board's listing page has dozens of inputs and no form worth filling.
    Requiring real evidence — an actual <form>, a file upload, or a free-text
    area — stops BidWatch typing into a search box and calling it an application.
    """
    if not descriptors:
        return False
    has_upload = any(d["type"] == "file" for d in descriptors)
    has_textarea = any(d["tag"] == "textarea" for d in descriptors)
    identifiable = sum(1 for d in descriptors if match_by_attribute(d) or match_by_label(d))
    if form_count == 0 and not has_upload and not has_textarea:
        return False
    return has_upload or has_textarea or identifiable >= 3


def field_text(descriptor: dict[str, Any]) -> str:
    """Everything a human would read as this field's name."""
    return _normalize(" ".join([
        descriptor.get("label", ""), descriptor.get("aria", ""),
        descriptor.get("placeholder", ""), descriptor.get("name", ""), descriptor.get("id", ""),
    ]))


def match_by_attribute(descriptor: dict[str, Any]) -> str | None:
    """Layer 1: exact name/id attribute match."""
    for attribute in (descriptor.get("name"), descriptor.get("id")):
        key = _attribute_key(attribute)
        if not key:
            continue
        for canonical, candidates in EXACT_ATTRIBUTES.items():
            if key in candidates:
                return canonical
    return None


def match_by_label(descriptor: dict[str, Any]) -> str | None:
    """Layer 2: fuzzy match on the visible label and related text."""
    text = field_text(descriptor)
    if not text:
        return None
    best: tuple[int, str] | None = None
    for canonical, rules in LABEL_KEYWORDS.items():
        if any(bad in text for bad in rules["avoid"]):
            continue
        for phrase in rules["match"]:
            if phrase in text:
                score = len(phrase)
                if best is None or score > best[0]:
                    best = (score, canonical)
    return best[1] if best else None


def plan_fills(
    descriptors: list[dict[str, Any]],
    values: dict[str, str],
    use_model: bool = True,
) -> dict[str, Any]:
    """Decide what to type where. Pure: no browser, no side effects.

    Returns {"fills": [...], "unmatched": [...], "required_blank": [...]}.
    Every decision records which layer produced it, so a bad fill is traceable.
    """
    fills: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    used: set[str] = set()

    def take(canonical: str, descriptor: dict[str, Any], how: str) -> bool:
        if canonical in used or canonical not in values:
            return False
        fills.append({
            "index": descriptor["index"], "selector": descriptor.get("selector", ""),
            "field": canonical, "value": values[canonical], "how": how,
            "tag": descriptor["tag"], "type": descriptor["type"],
            "label": descriptor.get("label") or descriptor.get("name") or descriptor.get("id"),
        })
        used.add(canonical)
        return True

    deferred: list[dict[str, Any]] = []
    for descriptor in descriptors:
        if descriptor["type"] == "file":
            if "resume" not in used:
                fills.append({
                    "index": descriptor["index"], "selector": descriptor.get("selector", ""),
                    "field": "resume", "value": "", "how": "file input",
                    "tag": descriptor["tag"], "type": "file",
                    "label": descriptor.get("label") or "résumé",
                })
                used.add("resume")
            continue

        canonical = match_by_attribute(descriptor)
        if canonical and take(canonical, descriptor, "attribute"):
            continue
        canonical = match_by_label(descriptor)
        if canonical and take(canonical, descriptor, "label"):
            continue
        deferred.append(descriptor)

    if deferred and use_model:
        for descriptor, canonical in resolve_with_model(deferred, values, used).items():
            target = next((d for d in deferred if str(d["index"]) == str(descriptor)), None)
            if target is not None and canonical:
                take(canonical, target, "model")

    for descriptor in deferred:
        if any(f["index"] == descriptor["index"] for f in fills):
            continue
        unmatched.append({
            "index": descriptor["index"],
            "label": descriptor.get("label") or descriptor.get("placeholder") or descriptor.get("name") or "unnamed field",
            "required": descriptor["required"],
        })

    required_blank = [d for d in unmatched if d["required"]]
    logger.info(
        "Form plan: %d fills (%s), %d unmatched, %d required blank",
        len(fills), ", ".join(sorted({f["field"] for f in fills})), len(unmatched), len(required_blank),
    )
    return {"fills": fills, "unmatched": unmatched, "required_blank": required_blank}


MODEL_MATCH_PROMPT = """You map job-application form fields onto a candidate's stored data.

You are given form fields (by index, with their labels) and the names of the data
values available. For each field, reply with the value name that belongs in it, or
null if none of them fits — a wrong answer on someone's job application is worse
than a blank one, so prefer null when unsure.

Reply with ONLY a JSON object mapping field index to value name or null:
{"3": "phone", "5": null}"""


def resolve_with_model(
    descriptors: list[dict[str, Any]], values: dict[str, str], used: set[str]
) -> dict[str, str | None]:
    """Layer 3: ask the model about fields the first two layers couldn't place."""
    available = [key for key in values if key not in used and not key.startswith("question::")]
    if not available or not descriptors:
        return {}
    payload = [
        {"index": d["index"], "label": d.get("label") or d.get("placeholder") or d.get("name") or "",
         "type": d["type"], "required": d["required"]}
        for d in descriptors
    ][:25]
    raw = complete(
        MODEL_MATCH_PROMPT,
        f"AVAILABLE VALUES: {json.dumps(available)}\n\nFORM FIELDS: {json.dumps(payload, ensure_ascii=False)}",
    )
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return {}
    try:
        mapping = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    # Only accept values that actually exist; the model must not invent a field.
    return {str(k): (v if v in values else None) for k, v in mapping.items() if isinstance(k, (str, int))}


# --- reporting -------------------------------------------------------------

def format_report(posting: dict[str, Any], result: dict[str, Any]) -> str:
    """The message sent after filling: what went in, what didn't."""
    company = posting.get("company", "the employer")
    if result.get("error"):
        return f"Could not fill the form — {company}\n\n{result['error']}"

    lines = [f"Form opened and filled — {company}", ""]
    labels = {"resume": "résumé", "cover_letter": "cover letter"}
    filled = [labels.get(f["field"], f["field"].replace("_", " ")) for f in result.get("fills", [])]
    if filled:
        lines.append("✅ Filled: " + ", ".join(dict.fromkeys(filled)))
    else:
        lines.append("⚠️ Nothing could be filled automatically.")

    # Only fields a human could recognise are worth listing; a wall of
    # "unnamed field" tells the reader nothing.
    named = [
        item for item in result.get("unmatched", [])
        if not item.get("required") and item.get("label") and item["label"] != "unnamed field"
    ]
    seen: set[str] = set()
    for item in named[:6]:
        if item["label"] in seen:
            continue
        seen.add(item["label"])
        lines.append(f'⚠️ Could not match: "{item["label"]}"')
    remaining = len(result.get("unmatched", [])) - len(seen)
    if remaining > 0:
        lines.append(f"⚠️ {remaining} other field(s) left blank.")
    for item in result.get("required_blank", []):
        label = item.get("label") or "an unlabelled field"
        lines.append(f'⚠️ Left blank (looks required): "{label}"')

    lines.append("")
    lines.append("Nothing was submitted. Review every field in the browser window, then submit it yourself.")
    return "\n".join(lines)


# --- the browser layer -----------------------------------------------------

BANNER_TEXT = "BidWatch filled this form — review every field, then submit manually."

_COLLECT_FIELDS_JS = """
() => {
  const out = [];
  const nodes = document.querySelectorAll('input, textarea, select');
  nodes.forEach((el, i) => {
    const type = (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    let label = '';
    if (el.id) {
      const forLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (forLabel) label = forLabel.innerText;
    }
    if (!label) {
      const parent = el.closest('label');
      if (parent) label = parent.innerText;
    }
    if (!label && el.getAttribute('aria-labelledby')) {
      const ref = document.getElementById(el.getAttribute('aria-labelledby'));
      if (ref) label = ref.innerText;
    }
    if (!label) {
      const wrapper = el.closest('div, li, fieldset, section');
      if (wrapper) {
        const lbl = wrapper.querySelector('label');
        if (lbl) label = lbl.innerText;
      }
    }
    el.setAttribute('data-bidwatch-field', String(i));
    const chrome = el.closest('nav, header, footer, [role="search"], [role="navigation"], form[role="search"]');
    out.push({
      index: i,
      tag: el.tagName.toLowerCase(),
      type: type,
      name: el.name || '',
      id: el.id || '',
      label: (label || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
      placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      in_chrome: !!chrome,
      selector: `[data-bidwatch-field="${i}"]`
    });
  });
  return out;
}
"""

_BANNER_JS = """
(text) => {
  const existing = document.getElementById('bidwatch-banner');
  if (existing) existing.remove();
  const bar = document.createElement('div');
  bar.id = 'bidwatch-banner';
  bar.textContent = text;
  bar.style.cssText = [
    'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
    'background:#1f6feb', 'color:#fff', 'padding:14px 18px',
    'font:600 15px/1.4 system-ui, -apple-system, Segoe UI, sans-serif',
    'text-align:center', 'box-shadow:0 2px 10px rgba(0,0,0,.3)'
  ].join(';');
  document.body.appendChild(bar);
  document.body.style.paddingTop = '52px';
  window.scrollTo(0, 0);
}
"""


_APPLY_LINK_JS = """
() => {
  const links = Array.from(document.querySelectorAll('a[href]'));
  const apply = links.find(a => /^\\s*apply\\b/i.test(a.innerText || ''));
  return apply ? apply.href : null;
}
"""

#: A landing page that is asking the user to sign in rather than to apply.
SIGN_IN_MARKERS = ("sign-up", "signup", "sign_up", "/login", "sign-in", "signin", "register")


def looks_like_sign_in_wall(url: str) -> bool:
    return any(marker in (url or "").lower() for marker in SIGN_IN_MARKERS)


#: Boards whose listing page is not an application form.
JOB_BOARD_HOSTS = ("remoteok.com", "remoteok.io", "weworkremotely.com", "indeed.com", "linkedin.com")


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "", re.I)
    return (match.group(1) if match else "").lower().replace("www.", "")


def _click_apply_anchor(page: Any) -> Any:
    """Click the listing's Apply LINK and return the page it opens.

    Boards hand out the employer URL only to a real click (it carries a user
    gesture and a referrer), so navigation alone bounces back. This clicks an
    <a href> and nothing else: the locator is asserted to be an anchor, so it
    can never reach a submit button. Returns the popup page, or None.
    """
    # Listings repeat the Apply link many times, mostly hidden; a hidden one
    # would stall the click until it timed out.
    anchor = page.locator("a[href]:visible").filter(has_text=re.compile(r"^\s*apply\b", re.I)).first
    try:
        if anchor.count() == 0:
            return None
        tag = anchor.evaluate("el => el.tagName.toLowerCase()")
        kind = anchor.evaluate("el => (el.getAttribute('type') || '').toLowerCase()")
        if tag != "a" or kind == "submit":
            logger.warning("Refusing to click a non-anchor apply control (<%s type=%s>)", tag, kind)
            return None
        with page.context.expect_page(timeout=15000) as popup:
            anchor.click(timeout=8000)   # navigation only; asserted to be a link above
        opened = popup.value
        opened.wait_for_load_state("domcontentloaded", timeout=30000)
        opened.wait_for_timeout(2500)
        return opened
    except Exception as exc:  # noqa: BLE001 - no popup is a normal outcome
        logger.info("Apply link did not open a new page: %s", exc)
        return None


def follow_apply_link(page: Any) -> str:
    """From a job-board listing, navigate to the employer's application page.

    Returns the URL landed on. Boards often gate this link — Remote OK's
    /l/<id> bounces straight back to the listing — in which case the caller
    sees the host is unchanged and reports honestly instead of filling the
    board's own search box.
    """
    try:
        href = page.evaluate(_APPLY_LINK_JS)
    except Exception:  # noqa: BLE001
        return page.url
    if not href:
        return page.url
    logger.info("Following the listing's apply link: %s", href)
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
    except Exception as exc:  # noqa: BLE001 - stay on the listing if it fails
        logger.warning("Could not follow the apply link: %s", exc)
    return page.url


def _detect_provider(url: str) -> str | None:
    lowered = (url or "").lower()
    for provider in ATS_SELECTORS:
        if provider in lowered or (provider == "greenhouse" and "greenhouse.io" in lowered):
            return provider
    return None


def open_prefilled_application(
    posting_json: str | dict[str, Any],
    application_data: dict[str, Any],
    resume_path: str = "",
) -> dict[str, Any]:
    """Open the application page in a visible browser and fill it in.

    Fills what it can confidently match, uploads the tailored résumé, injects a
    review banner, and leaves the window open with control handed to the user.
    It NEVER clicks submit.

    Returns a report: {"fills", "unmatched", "required_blank", "error"}. On any
    failure the error is set so the caller can fall back to the manual handoff.

    Args:
        posting_json: The posting, as a dict or a JSON object string.
        application_data: {"apply_url", "applicant", "letter", "answers"}.
        resume_path: Path to the tailored résumé PDF to upload.
    """
    try:
        posting = json.loads(posting_json) if isinstance(posting_json, str) else (posting_json or {})
    except (json.JSONDecodeError, TypeError):
        return {"error": "The posting could not be read.", "fills": [], "unmatched": [], "required_blank": []}

    url = application_data.get("override_url") or application_data.get("apply_url") or posting.get("url", "")
    if not url:
        return {"error": "No application URL is known for this posting.", "fills": [], "unmatched": [], "required_blank": []}

    values = applicant_values(
        application_data.get("applicant", {}),
        application_data.get("letter", ""),
        application_data.get("answers", {}),
    )

    outcome: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    thread = threading.Thread(
        target=_drive_browser, args=(url, values, resume_path, outcome), daemon=True,
        name=f"bidwatch-form-{posting.get('id', '')}",
    )
    thread.start()
    try:
        # The browser stays open after this returns; only the filling is awaited.
        return outcome.get(timeout=180)
    except queue.Empty:
        return {
            "error": "The browser did not finish loading the form in time. The window may still be opening.",
            "fills": [], "unmatched": [], "required_blank": [],
        }


def _drive_browser(url: str, values: dict[str, str], resume_path: str, outcome: queue.Queue) -> None:
    """Runs on its own thread so the browser can outlive the fill."""
    report: dict[str, Any] = {"fills": [], "unmatched": [], "required_blank": [], "error": ""}
    browser = playwright = None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report["error"] = (
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        )
        outcome.put(report)
        return

    try:
        playwright = sync_playwright().start()
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        # A persistent profile keeps cookies between runs, so signing in to a
        # job board once is enough for later applications.
        browser = playwright.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=not HEADED_BROWSER,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001 - a chatty page is not a failure
            pass

        # A board listing is not an application form; follow its Apply link.
        if _host(page.url) in JOB_BOARD_HOSTS:
            opened = _click_apply_anchor(page)
            if opened is not None:
                page = opened
            elif _host(follow_apply_link(page)) in JOB_BOARD_HOSTS:
                pass  # still on the board; handled below

            if looks_like_sign_in_wall(page.url):
                report["error"] = (
                    f"{_host(url)} wants you signed in before it will show the employer's "
                    "application page. The browser window is open on its sign-in page and keeps "
                    "its own profile — sign in once there, then tap Fill form again and it will "
                    "go straight through next time."
                )
                _finish(report, outcome)
                _park(page, browser, playwright)
                return

            if _host(page.url) in JOB_BOARD_HOSTS:
                report["error"] = (
                    f"This is the {_host(page.url)} listing, not the employer's application form. "
                    "The browser is open on it: click Apply yourself, copy the URL of the page that "
                    "opens, and send it to me as: fill <url>"
                )
                _finish(report, outcome)
                _park(page, browser, playwright)
                return

        raw_fields = page.evaluate(_COLLECT_FIELDS_JS)
        form_count = page.evaluate("() => document.querySelectorAll('form').length")
        descriptors = describe_fields(raw_fields)
        if not looks_like_application_form(descriptors, form_count):
            report["error"] = (
                "No application form was found on this page — it may be login-gated, inside an "
                "iframe, or blocking automation. Nothing was filled."
            )
            _finish(report, outcome)
            _park(page, browser, playwright)
            return

        provider = _detect_provider(page.url)
        plan = plan_fills(descriptors, values)
        if provider:
            logger.info("Recognised %s; using provider selectors first.", provider)
            plan = _apply_provider_selectors(page, provider, values, plan)

        for fill in plan["fills"]:
            _execute_fill(page, fill, resume_path)

        page.evaluate(_BANNER_JS, BANNER_TEXT)
        report.update({
            "fills": [f for f in plan["fills"] if not f.get("skipped")],
            "unmatched": plan["unmatched"],
            "required_blank": plan["required_blank"],
        })
        _finish(report, outcome)
        # Hand the window to the user and keep the browser alive until they
        # close it. Nothing is ever submitted from here.
        _park(page, browser, playwright)
        return
    except Exception as exc:  # noqa: BLE001 - any browser failure must degrade, not crash
        logger.error("Form filling failed: %s", exc)
        report["error"] = f"{type(exc).__name__}: {exc}"
        _finish(report, outcome)
        try:
            if browser:
                browser.close()
            if playwright:
                playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def _finish(report: dict[str, Any], outcome: queue.Queue) -> None:
    try:
        outcome.put_nowait(report)
    except queue.Full:
        pass


def _park(page: Any, browser: Any, playwright: Any) -> None:
    """Block until the user closes the window, then clean up."""
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:  # noqa: BLE001 - closed browser, navigation, anything
        pass
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def _apply_provider_selectors(
    page: Any, provider: str, values: dict[str, str], plan: dict[str, Any]
) -> dict[str, Any]:
    """Prefer the provider's known selectors over anything generic matching found."""
    known = ATS_SELECTORS.get(provider, {})
    by_field = {f["field"]: f for f in plan["fills"]}
    for field, selector in known.items():
        if field not in values and field != "resume":
            continue
        try:
            if page.locator(selector).count() == 0:
                continue
        except Exception:  # noqa: BLE001 - a bad selector is not fatal
            continue
        entry = by_field.get(field)
        if entry:
            entry["selector"] = selector
            entry["how"] = f"{provider} selector"
        else:
            plan["fills"].append({
                "index": -1, "selector": selector, "field": field,
                "value": values.get(field, ""), "how": f"{provider} selector",
                "tag": "input", "type": "file" if field == "resume" else "text",
                "label": field.replace("_", " "),
            })
    return plan


def _execute_fill(page: Any, fill: dict[str, Any], resume_path: str) -> None:
    """Type one value. Failures are logged and skipped, never raised."""
    selector = fill.get("selector")
    if not selector:
        fill["skipped"] = True
        return
    try:
        locator = page.locator(selector).first
        if fill["type"] == "file":
            if resume_path and os.path.isfile(resume_path):
                locator.set_input_files(resume_path)
                fill["value"] = os.path.basename(resume_path)
            else:
                fill["skipped"] = True
                logger.warning("No résumé file to upload for %s", selector)
            return
        if fill["tag"] == "select":
            locator.select_option(label=fill["value"])
        else:
            locator.fill(str(fill["value"]))
        logger.info("Filled %s via %s", fill["field"], fill["how"])
    except Exception as exc:  # noqa: BLE001 - one stubborn field must not stop the rest
        fill["skipped"] = True
        logger.warning("Could not fill %s (%s): %s", fill["field"], selector, exc)
