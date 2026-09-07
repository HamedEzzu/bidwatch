"""Builds a job-tailored résumé by SELECTING from the career database.

The one rule that matters: nothing here can invent a claim. The model is never
asked to write résumé text — it returns *indices* into profile.md (which summary
variant, which skill groups, which projects, which bullets within them). Every
line that reaches the PDF is copied verbatim from the file, so an embellishment
is not merely discouraged, it has nowhere to enter.

Selection and ordering only. If the model fails or returns nonsense, a
deterministic tag-overlap fallback picks the content instead.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from typing import Any

from config import BASE_DIR, PROFILE_PATH
from tools.llm import complete

logger = logging.getLogger(__name__)

RESUME_DIR = os.path.join(BASE_DIR, "generated_resumes")

#: Skill groups on one résumé. Beyond this it stops being a tailored document.
MAX_SKILL_GROUPS = 6

_TAGS_RE = re.compile(r"\{tags:([^}]*)\}\s*$", re.I)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_SUB_RE = re.compile(r"^###\s+(\w[\w .()/-]*?):\s*(.+?)\s*$")


def _split_tags(text: str) -> tuple[str, list[str]]:
    """Separate a line's visible text from its {tags: ...} marker."""
    match = _TAGS_RE.search(text)
    if not match:
        return text.strip(), []
    tags = [t.strip().lower() for t in match.group(1).split(",") if t.strip()]
    return _TAGS_RE.sub("", text).strip(), tags


#: applicant.md field names -> the résumé header fields they populate.
_IDENTITY_FROM_APPLICANT = {
    "name": "full name", "email": "email", "phone": "phone", "location": "location",
    "linkedin": "linkedin", "github": "github", "portfolio": "portfolio",
}


def _identity_from_applicant() -> dict[str, str]:
    """Contact details, read from the gitignored applicant.md.

    profile.md is committed, so it holds no phone number or address; the
    résumé header is populated from the private file instead.
    """
    try:
        from tools.applicant import parse_applicant

        fields = parse_applicant().get("fields", {})
    except Exception as exc:  # noqa: BLE001 - a résumé without contact details is still useful
        logger.warning("Could not read applicant details for the résumé header: %s", exc)
        return {}
    identity = {}
    for source, target in _IDENTITY_FROM_APPLICANT.items():
        value = fields.get(source, "")
        if value:
            # URLs read better on a résumé without the scheme.
            identity[target] = re.sub(r"^https?://(www\.)?", "", value).rstrip("/")
    return identity


def parse_career_database(path: str = PROFILE_PATH) -> dict[str, Any]:
    """Parse profile.md into the structured content the generator selects from."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        logger.error("Could not read the career database at %s: %s", path, exc)
        return {}

    db: dict[str, Any] = {
        "identity": {}, "summaries": {}, "skills": [], "projects": [],
        "awards": [], "education": [], "certifications": [], "languages": [],
    }
    section = ""
    sub_kind = sub_name = ""
    project: dict[str, Any] | None = None

    def close_project() -> None:
        nonlocal project
        if project:
            db["projects"].append(project)
            project = None

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("<!--"):
            continue

        heading = _SECTION_RE.match(line)
        if heading:
            close_project()
            section = heading.group(1).strip().lower()
            sub_kind = sub_name = ""
            continue

        sub = _SUB_RE.match(line)
        if sub:
            close_project()
            sub_kind, sub_name = sub.group(1).strip().lower(), sub.group(2).strip()
            if sub_kind == "project":
                project = {"name": sub_name, "role": "", "stack": "", "tags": [], "bullets": []}
            elif sub_kind == "skills":
                db["skills"].append({"group": sub_name, "text": "", "tags": []})
            continue

        stripped = line.strip()
        # Blank lines and horizontal rules carry no content; a rule starts with
        # "-" and would otherwise be read as an empty bullet.
        if not stripped or set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            continue

        if sub_kind == "summary" and section == "summaries":
            db["summaries"].setdefault(sub_name.lower(), "")
            db["summaries"][sub_name.lower()] = (db["summaries"][sub_name.lower()] + " " + stripped).strip()
            continue

        if sub_kind == "skills" and db["skills"]:
            text, tags = _split_tags(stripped)
            entry = db["skills"][-1]
            entry["text"] = (entry["text"] + " " + text).strip()
            entry["tags"] = entry["tags"] + tags
            continue

        if project is not None:
            bullet = stripped.lstrip("-").strip() if stripped.startswith("-") else stripped
            lowered = bullet.lower()
            if lowered.startswith("role:"):
                project["role"] = bullet.split(":", 1)[1].strip()
            elif lowered.startswith("stack line:"):
                project["stack"] = bullet.split(":", 1)[1].strip()
            elif lowered.startswith("tags:"):
                project["tags"] = [t.strip().lower() for t in bullet.split(":", 1)[1].split(",") if t.strip()]
            elif lowered.startswith("bullets:"):
                continue
            elif stripped.startswith("-"):
                text, tags = _split_tags(bullet)
                if text:
                    project["bullets"].append({"text": text, "tags": tags})
            continue

        if stripped.startswith("-"):
            item = stripped.lstrip("-").strip()
            if not item:
                continue
            if section == "identity":
                label, _, value = item.partition(":")
                if label.strip():
                    db["identity"][label.strip().lower()] = value.strip()
            elif section in ("awards", "education", "certifications", "languages"):
                db[section].append(item)

    close_project()
    # Contact details live in applicant.md, not in the committed profile.
    db["identity"] = {**db["identity"], **_identity_from_applicant()}
    return db


# --- selection -------------------------------------------------------------

SELECTION_SYSTEM_PROMPT = """You tailor a résumé by SELECTING from a candidate's career
database. You never write résumé text: you return only indices and names.

Given a job posting and the database, decide:
- which summary variant fits the job's primary stack,
- which skill groups to include and in what order (the job's stack leads),
- which 2-3 projects are most relevant, strongest match first,
- within each chosen project, which bullet indices support THIS application.

Choose bullets that evidence the job's requirements. Leave out bullets about
unrelated technology; a shorter, sharper résumé beats a complete one.

Reply with ONLY this JSON, no markdown fence:
{"summary": "<variant name>",
 "skills": ["<group name>", ...],
 "projects": [{"name": "<project name>", "bullets": [0, 2, 3]}],
 "reason": "<one short line: the variant chosen and what it leads with>"}"""


#: The database tags a skill "dotnet"; a posting writes ".NET". Without this the
#: two never match and a .NET job gets a Python-first résumé.
_TAG_ALIASES = (
    (r"\.net|asp\.net|dot ?net", " dotnet aspnet "),
    (r"\bc#|c-sharp|csharp", " csharp dotnet "),
    (r"entity framework( core)?|\bef core\b", " efcore dotnet database "),
    (r"sql ?server|mssql|t-sql", " sqlserver sql database "),
    (r"postgre ?sql|postgres", " postgres database "),
    (r"node\.?js", " node javascript "),
    (r"type ?script", " typescript javascript "),
    (r"react(\.?js)?", " react frontend "),
    (r"rest(ful)? ?(api)?s?\b", " api rest "),
    (r"web ?sockets?", " websockets realtime "),
    (r"ci/cd|continuous integration", " ci devops "),
    (r"unit test|integration test|pytest|automated test", " testing "),
    (r"authentication|authorization|oauth|\bjwt\b", " auth security jwt "),
)


def _job_text(posting: dict[str, Any]) -> str:
    """The posting as one lowercase haystack, with stack synonyms expanded."""
    text = " ".join([
        posting.get("title", ""),
        " ".join(posting.get("tags", []) or []),
        (posting.get("description", "") or "")[:1500],
    ]).lower()
    for pattern, expansion in _TAG_ALIASES:
        if re.search(pattern, text):
            text += expansion
    return text


def _score_tags(tags: list[str], job_text: str) -> int:
    return sum(1 for tag in tags if tag and tag in job_text)


def fallback_selection(posting: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    """Deterministic tag-overlap selection, used when the model can't be trusted."""
    job_text = _job_text(posting)

    variant_tags = {
        "dotnet": ("c#", ".net", "asp.net", "dotnet", "entity framework", "sql server", "blazor"),
        "python": ("python", "fastapi", "django", "flask", "sqlalchemy", "pandas"),
        "node": ("node", "react", "typescript", "javascript", "express", "frontend", "full stack"),
    }
    variant = "backend"
    best = 0
    for name, needles in variant_tags.items():
        hits = sum(1 for needle in needles if needle in job_text)
        if hits > best:
            variant, best = name, hits

    skills = sorted(db.get("skills", []), key=lambda s: _score_tags(s["tags"], job_text), reverse=True)
    projects = sorted(db.get("projects", []), key=lambda p: _score_tags(p["tags"], job_text), reverse=True)

    chosen = []
    for project in projects[:3]:
        ranked = sorted(
            range(len(project["bullets"])),
            key=lambda i: _score_tags(project["bullets"][i]["tags"], job_text),
            reverse=True,
        )
        keep = sorted(ranked[:4]) if len(ranked) > 4 else sorted(ranked)
        chosen.append({"name": project["name"], "bullets": keep})

    return {
        "summary": variant,
        "skills": [s["group"] for s in skills][:MAX_SKILL_GROUPS],
        "projects": chosen,
        "reason": f"{variant}-focused — matched on the posting's stack keywords.",
    }


def select_content(posting: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    """Ask the model which parts of the database to use; verify every choice.

    Anything the model names that does not exist in the database is discarded,
    and an unusable response falls back to deterministic selection.
    """
    catalogue = {
        "summaries": list(db.get("summaries", {})),
        "skills": [s["group"] for s in db.get("skills", [])],
        "projects": [
            {"name": p["name"], "stack": p["stack"],
             "bullets": {str(i): b["text"][:160] for i, b in enumerate(p["bullets"])}}
            for p in db.get("projects", [])
        ],
    }
    prompt = (
        f"JOB POSTING:\n{json.dumps({k: posting.get(k) for k in ('title', 'company', 'tags', 'description')}, ensure_ascii=False)}\n\n"
        f"CAREER DATABASE:\n{json.dumps(catalogue, ensure_ascii=False)}\n\n"
        "Select the content for this application."
    )

    raw = complete(SELECTION_SYSTEM_PROMPT, prompt)
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        logger.warning("Résumé selection unusable; falling back to tag matching.")
        return fallback_selection(posting, db)
    try:
        choice = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning("Résumé selection was not valid JSON; falling back to tag matching.")
        return fallback_selection(posting, db)

    selection = validate_selection(choice, db)
    if not selection:
        return fallback_selection(posting, db)

    # The model's picks lead; the rest are appended by relevance and the tail is
    # dropped. Listing every group on every résumé is not tailoring — a Python
    # role does not need "Desktop: WinForms".
    job_text = _job_text(posting)
    remainder = sorted(
        (s for s in db.get("skills", []) if s["group"] not in selection["skills"]),
        key=lambda s: _score_tags(s["tags"], job_text),
        reverse=True,
    )
    selection["skills"] = (selection["skills"] + [s["group"] for s in remainder])[:MAX_SKILL_GROUPS]
    return selection


def validate_selection(choice: dict[str, Any], db: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only choices that exist in the database. Returns None if nothing survives."""
    summaries, skills = db.get("summaries", {}), {s["group"] for s in db.get("skills", [])}
    projects = {p["name"]: p for p in db.get("projects", [])}

    variant = str(choice.get("summary", "")).strip().lower()
    if variant not in summaries:
        variant = "backend" if "backend" in summaries else next(iter(summaries), "")

    chosen_skills = [g for g in choice.get("skills", []) if g in skills]

    chosen_projects: list[dict[str, Any]] = []
    for entry in choice.get("projects", []) or []:
        name = str((entry or {}).get("name", "")).strip()
        project = projects.get(name)
        if not project:
            logger.warning("Discarding selected project %r: not in the career database.", name)
            continue
        indices = []
        for value in (entry.get("bullets") or []):
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(project["bullets"]) and index not in indices:
                indices.append(index)
        chosen_projects.append({"name": name, "bullets": indices or list(range(len(project["bullets"])))})

    if not variant or not chosen_projects:
        return None
    return {
        "summary": variant,
        "skills": chosen_skills,
        "projects": chosen_projects[:3],
        "reason": str(choice.get("reason", "")).strip()[:200],
    }


def describe_selection(selection: dict[str, Any], db: dict[str, Any]) -> str:
    """The one-line explanation shown in the draft: variant and what it leads with."""
    variant = selection.get("summary", "backend")
    leads = [p["name"].split("—")[0].strip() for p in selection.get("projects", [])][:2]
    skills = selection.get("skills", [])[:2]
    parts = [f"{variant}-focused"]
    if leads:
        parts.append("leading with " + ", ".join(leads))
    if skills:
        parts.append(", ".join(skills).lower() + " first")
    return " — ".join(parts[:2]) + (f", {parts[2]}" if len(parts) > 2 else "")


# --- rendering -------------------------------------------------------------

def _safe(name: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-")
    return (cleaned[:limit] or "job").lower()


def output_path(posting: dict[str, Any], directory: str = RESUME_DIR) -> str:
    today = dt.date.today().isoformat()
    filename = f"{_safe(posting.get('company', 'company'))}_{_safe(posting.get('title', 'role'))}_{today}.pdf"
    return os.path.join(directory, filename)


#: Progressively tighter budgets, tried in order until the résumé fits one page.
#: (max projects, max bullets per project, max skill groups)
FIT_BUDGETS = (
    (3, 5, 6), (3, 4, 6), (3, 4, 5), (2, 4, 5), (2, 4, 4), (2, 3, 4), (2, 3, 3), (1, 3, 3),
)


def render_pdf(selection: dict[str, Any], db: dict[str, Any], path: str) -> str:
    """Render the selected content to a clean, ATS-friendly ONE-PAGE PDF.

    Deliberately plain: one column, standard Helvetica, no tables, no graphics,
    no text in images — everything extractable by a résumé parser.

    A one-page limit is a hard constraint, so the content is rendered under
    progressively tighter budgets until it fits. Trimming drops the *lowest
    ranked* material first — the selection order already puts the strongest
    match at the top — and never rewrites anything.
    """
    import io

    last_error: Exception | None = None
    for max_projects, max_bullets, max_skills in FIT_BUDGETS:
        trimmed = {
            **selection,
            "skills": selection.get("skills", [])[:max_skills],
            "projects": [
                {"name": p["name"], "bullets": p["bullets"][:max_bullets]}
                for p in selection.get("projects", [])[:max_projects]
            ],
        }
        try:
            probe = io.BytesIO()
            pages = _build(trimmed, db, probe)
        except Exception as exc:  # noqa: BLE001 - try the next budget
            last_error = exc
            continue
        if pages <= 1:
            _build(trimmed, db, path)
            return path

    # Nothing fit; emit the tightest version rather than nothing at all.
    logger.warning("Résumé did not fit one page under any budget; using the tightest layout.")
    if last_error:
        logger.warning("Last layout error: %s", last_error)
    smallest = {
        **selection,
        "skills": selection.get("skills", [])[:4],
        "projects": [{"name": p["name"], "bullets": p["bullets"][:3]}
                     for p in selection.get("projects", [])[:1]],
    }
    _build(smallest, db, path)
    return path


def _build(selection: dict[str, Any], db: dict[str, Any], target: Any) -> int:
    """Lay the résumé out into `target` (a path or stream). Returns the page count."""
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    identity = db.get("identity", {})
    if isinstance(target, str):
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

    name_style = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=15, leading=18, spaceAfter=2)
    role_style = ParagraphStyle("role", fontName="Helvetica", fontSize=9.5, leading=12, spaceAfter=2)
    contact_style = ParagraphStyle("contact", fontName="Helvetica", fontSize=8.5, leading=11, spaceAfter=8)
    heading_style = ParagraphStyle(
        "heading", fontName="Helvetica-Bold", fontSize=9.5, leading=12, spaceBefore=7, spaceAfter=3
    )
    body_style = ParagraphStyle("body", fontName="Helvetica", fontSize=8.5, leading=10.6, alignment=TA_JUSTIFY)
    # Bullets are hanging-indent paragraphs prefixed with a plain hyphen, not
    # list flowables. ReportLab draws list glyphs from ZapfDingbats, and even a
    # literal "•" falls outside the standard-font encoding — both extract as
    # \x7f and would litter an ATS parse. A hyphen is in every base font.
    item_style = ParagraphStyle(
        "item", parent=body_style, spaceAfter=1.5, leftIndent=11, firstLineIndent=-11
    )
    project_style = ParagraphStyle("project", fontName="Helvetica-Bold", fontSize=9, leading=11, spaceBefore=4)
    stack_style = ParagraphStyle(
        "stack", fontName="Helvetica-Oblique", fontSize=8, leading=10, spaceAfter=2
    )

    def escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story: list[Any] = []
    story.append(Paragraph(escape(identity.get("full name", "")).upper(), name_style))
    if identity.get("headline"):
        story.append(Paragraph(escape(identity["headline"]), role_style))
    contact = " | ".join(
        escape(identity[key]) for key in ("location", "phone", "email", "linkedin", "github")
        if identity.get(key)
    )
    story.append(Paragraph(contact, contact_style))

    summary = db.get("summaries", {}).get(selection.get("summary", ""), "")
    if summary:
        story.append(Paragraph("SUMMARY", heading_style))
        story.append(Paragraph(escape(summary), body_style))

    skills_by_group = {s["group"]: s["text"] for s in db.get("skills", [])}
    ordered_skills = [g for g in selection.get("skills", []) if skills_by_group.get(g)]
    if ordered_skills:
        story.append(Paragraph("TECHNICAL SKILLS", heading_style))
        for group in ordered_skills:
            story.append(Paragraph(f"- <b>{escape(group)}:</b> {escape(skills_by_group[group])}", item_style))

    projects = {p["name"]: p for p in db.get("projects", [])}
    selected = [(projects[p["name"]], p["bullets"]) for p in selection.get("projects", []) if p["name"] in projects]
    if selected:
        story.append(Paragraph("PROJECTS", heading_style))
        for project, indices in selected:
            title = escape(project["name"])
            if project.get("role"):
                title += f" — {escape(project['role'])}"
            story.append(Paragraph(title, project_style))
            if project.get("stack"):
                story.append(Paragraph(escape(project["stack"]), stack_style))
            bullets = [project["bullets"][i]["text"] for i in indices if 0 <= i < len(project["bullets"])]
            for bullet in bullets:
                story.append(Paragraph(f"- {escape(bullet)}", item_style))

    for heading, key in (("AWARDS", "awards"), ("EDUCATION", "education"),
                         ("CERTIFICATIONS", "certifications"), ("LANGUAGES", "languages")):
        items = db.get(key, [])
        if not items:
            continue
        story.append(Paragraph(heading, heading_style))
        for item in items:
            story.append(Paragraph(f"- {escape(item)}", item_style))

    doc = SimpleDocTemplate(
        target, pagesize=LETTER,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.45 * inch, bottomMargin=0.4 * inch,
        title=f"{identity.get('full name', 'Resume')} — Résumé", author=identity.get("full name", ""),
    )
    doc.build(story)
    return doc.page


def generate_resume(posting_json: str, profile: str = "") -> str:
    """Build a résumé tailored to one posting and return the PDF path.

    Selects the matching summary, the most relevant skills and projects, and
    only the bullets that support this application — all copied verbatim from
    profile.md. Returns "" if generation fails, so the caller can fall back to
    the static résumé.

    Args:
        posting_json: One posting serialized as a JSON object string.
        profile: Unused; the career database is read from profile.md directly.
    """
    try:
        posting = json.loads(posting_json) if isinstance(posting_json, str) else posting_json
    except (json.JSONDecodeError, TypeError):
        logger.error("generate_resume received malformed posting JSON")
        return ""
    if not isinstance(posting, dict):
        return ""

    try:
        db = parse_career_database()
        if not db.get("projects"):
            logger.error("Career database has no projects; cannot build a résumé.")
            return ""
        selection = select_content(posting, db)
        path = render_pdf(selection, db, output_path(posting))
        logger.info("Generated tailored résumé: %s (%s)", path, selection.get("summary"))
        return path
    except Exception as exc:  # noqa: BLE001 - a failed résumé must not block the bid
        logger.error("Résumé generation failed: %s", exc)
        return ""
