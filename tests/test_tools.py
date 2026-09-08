"""Tests for the four non-LLM tools. No network calls, no model calls."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import load_fixture_postings  # noqa: E402
from config import FIXTURE_PATH  # noqa: E402
from tools import fetch, notify, profile, store  # noqa: E402


@pytest.fixture
def postings() -> list[dict]:
    return load_fixture_postings(FIXTURE_PATH)


# --- fetch / normalization -------------------------------------------------

def test_fixture_loads_and_skips_legal_notice(postings):
    assert len(postings) == 4
    assert all("legal" not in p for p in postings)
    assert {p["id"] for p in postings} == {"1000001", "1000002", "1000003", "1000004"}


def test_normalized_postings_have_every_field(postings):
    for posting in postings:
        assert set(posting) == set(fetch.POSTING_FIELDS)


def test_normalize_rejects_legal_notice_and_idless_records():
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)
    assert fetch.normalize_posting(raw[0]) is None
    assert fetch.normalize_posting({"position": "No id here"}) is None
    assert fetch.normalize_posting("not a dict") is None


def test_descriptions_are_plain_text_and_truncated(postings):
    first = postings[0]
    assert "<" not in first["description"] and ">" not in first["description"]
    assert "FastAPI" in first["description"]
    long_html = "<p>" + ("word " * 2000) + "</p>"
    assert len(fetch.strip_html(long_html)) <= fetch.MAX_DESCRIPTION_CHARS + 20
    assert fetch.strip_html(None) == ""


def test_missing_salary_is_none(postings):
    wordpress = next(p for p in postings if p["id"] == "1000002")
    assert wordpress["salary_min"] is None and wordpress["salary_max"] is None


def test_urls_are_direct_remoteok_links(postings):
    assert all(p["url"].startswith("https://remoteok.com/") for p in postings)


def test_fetch_returns_empty_list_on_http_error(monkeypatch):
    class FakeResponse:
        status_code = 503
        text = "unavailable"

    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: FakeResponse())
    assert fetch.fetch_job_postings("python", 5) == []


def test_fetch_returns_empty_list_on_network_error(monkeypatch):
    def boom(*args, **kwargs):
        raise fetch.requests.RequestException("no route to host")

    monkeypatch.setattr(fetch.requests, "get", boom)
    assert fetch.fetch_job_postings("python", 5) == []


# --- dedupe store ----------------------------------------------------------

def test_judged_postings_do_not_come_back(tmp_path, postings):
    db = str(tmp_path / "test.db")
    assert len(store.filter_new(postings, db)) == 4
    # Once each has a verdict, the same feed yields nothing.
    for posting in postings:
        store.set_status(posting["id"], "rejected", db_path=db)
    assert store.filter_new(postings, db) == []


def test_unjudged_postings_come_back(tmp_path, postings):
    """A posting recorded but never judged must not be lost.

    A crashed run, or a tool call truncated mid-flight, would otherwise bury a
    good job forever behind the dedupe check.
    """
    db = str(tmp_path / "test.db")
    store.filter_new(postings, db)
    store.set_status(postings[0]["id"], "notified", db_path=db)
    store.set_status(postings[1]["id"], "rejected", db_path=db)

    # The two judged ones stay gone; the two still marked "new" return.
    returning = {p["id"] for p in store.filter_new(postings, db)}
    assert returning == {postings[2]["id"], postings[3]["id"]}


def test_every_terminal_status_counts_as_handled(tmp_path, postings):
    db = str(tmp_path / "test.db")
    for status, posting in zip(("notified", "skipped", "applied_email", "bidding"), postings):
        store.filter_new([posting], db)
        store.set_status(posting["id"], status, db_path=db)
    assert store.filter_new(postings, db) == []


def test_dedupe_returns_only_the_unseen_posting(tmp_path, postings):
    db = str(tmp_path / "test.db")
    store.filter_new(postings[:3], db)
    for posting in postings[:3]:
        store.set_status(posting["id"], "rejected", db_path=db)
    fresh = store.filter_new(postings, db)
    assert [p["id"] for p in fresh] == ["1000004"]


def test_store_creation_is_idempotent(tmp_path, postings):
    db = str(tmp_path / "test.db")
    store._connect(db).close()
    store._connect(db).close()
    assert len(store.filter_new(postings, db)) == 4


def test_dedupe_handles_empty_input(tmp_path):
    assert store.filter_new([], str(tmp_path / "test.db")) == []


# --- profile ---------------------------------------------------------------

def test_profile_loads_real_file():
    text = profile.read_profile()
    # The profile is both the bidding rules and the résumé source material.
    assert "Will not bid on" in text and "Proposal voice" in text
    assert "## Skills" in text and "## Projects" in text and "## Summaries" in text


def test_missing_profile_returns_error_string_not_exception(tmp_path):
    text = profile.read_profile(str(tmp_path / "nope.md"))
    assert text == profile.MISSING_PROFILE_MESSAGE


def test_empty_profile_returns_error_string(tmp_path):
    empty = tmp_path / "profile.md"
    empty.write_text("", encoding="utf-8")
    assert profile.read_profile(str(empty)) == profile.MISSING_PROFILE_MESSAGE


# --- notification ----------------------------------------------------------

def test_job_message_has_the_required_shape(postings):
    posting = postings[0]
    message = notify.format_job_message(posting, 85, "Strong FastAPI and Postgres overlap.")
    lines = message.splitlines()
    assert lines[0] == "[85/100] Senior Python Backend Engineer"
    assert lines[1].startswith("Orbital Data — ")
    assert any(line.startswith("About: ") for line in lines)
    assert "Salary: $40k–$70k" in message
    assert "Why it fits: Strong FastAPI and Postgres overlap." in message
    assert message.rstrip().endswith("Source: Remote OK")
    # The full description and the proposal draft stay out of the alert.
    assert "isSelectedEnd" not in message and len(message) < 700


def test_job_message_omits_salary_line_when_absent(postings):
    wordpress = next(p for p in postings if p["id"] == "1000002")
    message = notify.format_job_message(wordpress, 12, "Deal-breaker.")
    assert "Salary" not in message
    assert "N/A" not in message and "Not specified" not in message


def test_about_line_is_extracted_never_invented():
    assert notify.summarize_company({"description": ""}) is None
    assert notify.summarize_company({"description": "Too short."}) is None
    about = notify.summarize_company(
        {"description": "About the role. Acme is a B2B SaaS company building logistics software."}
    )
    assert about == "Acme is a B2B SaaS company building logistics software."


def test_salary_formatting():
    assert notify.format_salary({"salary_min": 90000, "salary_max": 120000}) == "$90k–$120k"
    assert notify.format_salary({"salary_min": None, "salary_max": 70000}) == "$70k"
    assert notify.format_salary({"salary_min": None, "salary_max": None}) is None


def test_job_buttons_are_bid_open_skip(postings):
    buttons = notify.job_buttons(postings[0])
    row = buttons["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["✅ Bid", "🔗 Open", "⏭ Skip"]
    assert row[0]["callback_data"] == "bid:1000001"
    assert row[2]["callback_data"] == "skip:1000001"
    # Open is a direct link to the listing, as the Remote OK terms require.
    assert row[1]["url"] == postings[0]["url"]


def test_send_falls_back_to_console_without_telegram(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not notify.telegram_configured()
    result = notify.send("hello from bidwatch")
    assert "console" in result.lower()
    assert "hello from bidwatch" in capsys.readouterr().out


def test_send_uses_telegram_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"ok": True, "result": {"message_id": 1}}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hi") == "Notification sent via Telegram."
    assert "token123" in captured["url"]
    assert captured["payload"]["chat_id"] == "42"
    assert captured["payload"]["disable_web_page_preview"] is True
    # Attribution is appended on the way out if the caller omitted it.
    assert captured["payload"]["text"] == "hi\n\nSource: Remote OK"


def test_send_falls_back_to_console_when_telegram_fails(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def boom(*args, **kwargs):
        raise notify.requests.RequestException("timeout")

    monkeypatch.setattr(notify.requests, "post", boom)
    result = notify.send("fallback body")
    assert "console" in result.lower()
    assert "fallback body" in capsys.readouterr().out


# --- defensive score parsing (no model call) -------------------------------

def test_score_parsing_handles_clean_json():
    from tools.scoring import parse_score_response

    parsed = parse_score_response('{"score": 78, "rationale": "Good overlap."}')
    assert parsed == {"score": 78, "rationale": "Good overlap."}


def test_score_parsing_handles_fenced_and_malformed_output():
    from tools.scoring import parse_score_response

    assert parse_score_response('```json\n{"score": 91, "rationale": "Fits."}\n```')["score"] == 91
    assert parse_score_response("score: 44 because the stack matches")["score"] == 44
    assert parse_score_response("total nonsense")["score"] == 0
    assert parse_score_response("")["score"] == 0
    assert parse_score_response('{"score": 900, "rationale": "x"}')["score"] == 100


# --- posting cache (keeps full postings out of the model's context) --------

def test_hydrate_expands_an_id_reference(postings):
    fetch.remember(postings)
    full = fetch.hydrate({"id": "1000001"})
    assert full["title"] == "Senior Python Backend Engineer"
    assert "FastAPI" in full["description"]


def test_hydrate_passes_through_unknown_and_complete_postings(postings):
    fetch.remember(postings)
    assert fetch.hydrate({"id": "does-not-exist"}) == {"id": "does-not-exist"}
    assert fetch.hydrate(postings[0]) == postings[0]
    assert fetch.hydrate("not a dict") == {}


def test_dedupe_accepts_id_references(tmp_path, postings):
    db = str(tmp_path / "test.db")
    fetch.remember(postings)
    fresh = store.filter_new([fetch.hydrate({"id": p["id"]}) for p in postings], db)
    assert len(fresh) == 4
    assert fresh[0]["title"] == "Senior Python Backend Engineer"


# --- prioritization (free, no model call) ----------------------------------

def test_prioritize_puts_software_roles_first(postings):
    from agent import prioritize

    ordered = prioritize(postings)
    assert ordered[0]["id"] in {"1000001", "1000003"}
    assert {p["id"] for p in ordered} == {p["id"] for p in postings}


# --- outbound message repair ----------------------------------------------

def test_repair_fixes_a_retyped_link(postings):
    fetch.remember(postings)
    bad = "Link: https://remoteOK.com/remote-jobs/remote-senior-python-backend-engineer-orbital-data-1000001\n\nSource: Remote OK"
    assert postings[0]["url"] in notify.repair_message(bad)


def test_repair_appends_missing_attribution():
    assert notify.repair_message("A job with no attribution").endswith("Source: Remote OK")


def test_repair_leaves_a_correct_message_alone(postings):
    fetch.remember(postings)
    good = notify.format_job_message(postings[0], 70, "Fits.")
    assert notify.repair_message(good) == good


# --- applicant details -----------------------------------------------------

APPLICANT_SAMPLE = """# Applicant Details

## Identity
- Full name: Test Person
- Email: test@example.com
- Phone: +1 555 0100
- Location: Testville
- Timezone: UTC+2

## Links
- LinkedIn: https://linkedin.com/in/test
- GitHub: TODO — add GitHub profile URL

## Standard answers
- Work authorization: Contractor, remote only
- Salary expectation: $20/hour
- Years of experience: Student with production projects;
  no formal employment history
"""


def test_applicant_parses_fields_answers_and_todos():
    from tools.applicant import parse_applicant

    parsed = parse_applicant(APPLICANT_SAMPLE)
    assert parsed["fields"]["name"] == "Test Person"
    assert parsed["fields"]["email"] == "test@example.com"
    assert parsed["fields"]["linkedin"] == "https://linkedin.com/in/test"
    # A TODO value is reported as outstanding, never passed off as an answer.
    assert "github" in parsed["todo"]
    assert "github" not in parsed["fields"]


def test_applicant_joins_wrapped_values():
    from tools.applicant import parse_applicant

    parsed = parse_applicant(APPLICANT_SAMPLE)
    assert parsed["answers"]["years of experience"].endswith("no formal employment history")


def test_unanswerable_question_is_flagged_not_invented():
    from tools.applicant import NEEDS_INPUT, answer_question, parse_applicant

    parsed = parse_applicant(APPLICANT_SAMPLE)
    assert answer_question("Are you authorized to work remotely?", parsed) == "Contractor, remote only"
    assert answer_question("What is your favourite colour?", parsed) == NEEDS_INPUT


def test_missing_applicant_file_returns_error_string(tmp_path):
    from tools.applicant import MISSING_APPLICANT_MESSAGE, read_applicant

    assert read_applicant(str(tmp_path / "nope.md")) == MISSING_APPLICANT_MESSAGE


# --- application method detection ------------------------------------------

def test_detects_known_ats_providers():
    from tools.apply import detect_ats

    assert detect_ats("apply: https://boards.greenhouse.io/acme/jobs/12345")["provider"] == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/abc-def")["provider"] == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/xyz-1")["provider"] == "ashby"
    assert detect_ats("nothing here") is None


def test_prefers_a_hiring_inbox_over_a_generic_address():
    from tools.apply import find_application_email

    assert find_application_email("write to support@acme.com or careers@acme.com") == "careers@acme.com"
    assert find_application_email("only noreply@acme.com") is None
    assert find_application_email("no addresses here") is None


def test_finds_screening_questions_but_not_prose():
    from tools.apply import find_screening_questions

    found = find_screening_questions(
        "How many years of Python do you have? Isn't the weather nice? Why do you want this role?"
    )
    assert any("years" in q for q in found)
    assert not any("weather" in q for q in found)


# --- store lifecycle -------------------------------------------------------

def test_status_lifecycle_and_no_resurfacing(tmp_path, postings):
    db = str(tmp_path / "t.db")
    store.filter_new(postings, db)
    assert store.get_status("1000001", db) == "new"

    store.set_status("1000001", "notified", score=88, db_path=db)
    assert store.get_status("1000001", db) == "notified"
    record = store.get_posting_record("1000001", db)
    assert record["score"] == 88 and record["notified_at"]

    store.set_status("1000001", "applied_email", cover_letter="Dear team...", db_path=db)
    record = store.get_posting_record("1000001", db)
    assert record["status"] == "applied_email"
    assert record["applied_at"] and record["cover_letter"] == "Dear team..."

    # Nothing already handled comes back on a later run.
    for posting in postings[1:]:
        store.set_status(posting["id"], "rejected", db_path=db)
    assert store.filter_new(postings, db) == []


def test_unknown_status_is_refused(tmp_path, postings):
    db = str(tmp_path / "t.db")
    store.filter_new(postings, db)
    assert store.set_status("1000001", "nonsense", db_path=db) is False
    assert store.get_status("1000001", db) == "new"


def test_old_database_is_migrated_in_place(tmp_path, postings):
    import sqlite3

    db = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE seen_postings (id TEXT PRIMARY KEY, title TEXT, company TEXT,"
        " seen_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.execute("INSERT INTO seen_postings (id, title, company) VALUES ('1000001', 'Old', 'Co')")
    legacy.commit()
    legacy.close()

    # The old row still dedupes, and the new columns are usable.
    assert [p["id"] for p in store.filter_new(postings, db)] != ["1000001"]
    assert store.set_status("1000001", "skipped", db_path=db) is True
    assert store.get_status("1000001", db) == "skipped"


def test_submission_rate_limit(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    for _ in range(5):
        store.log_submission("x", "email", "sent", "ok", db_path=db)
    assert store.submissions_last_hour(db) == 5
    allowed, reason = store.submission_allowed(db)
    assert allowed is False and "Rate limit" in reason


def test_failed_submissions_do_not_consume_the_rate_limit(tmp_path):
    db = str(tmp_path / "t.db")
    for _ in range(9):
        store.log_submission("x", "email", "failed", "smtp down", db_path=db)
    assert store.submissions_last_hour(db) == 0
    assert store.submission_allowed(db)[0] is True


# --- email construction (no network) ---------------------------------------

def test_application_email_has_subject_body_and_no_attachment_when_resume_missing(tmp_path):
    from tools.submit import build_email

    applicant = {"fields": {
        "name": "Test Person", "email": "test@example.com", "phone": "+1 555 0100",
        "location": "Testville", "timezone": "UTC+2", "linkedin": "https://li/x",
        "resume_path": str(tmp_path / "missing.pdf"),
    }}
    posting = {"id": "1", "title": "Backend Engineer", "company": "Acme"}
    message = build_email(posting, applicant, "Letter body here.", "jobs@acme.com")
    assert message["To"] == "jobs@acme.com"
    assert "Backend Engineer" in message["Subject"]
    assert "Letter body here." in message.get_content()
    assert not list(message.iter_attachments())


def test_application_email_attaches_the_resume_when_present(tmp_path):
    from tools.submit import build_email

    resume = tmp_path / "My_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    applicant = {"fields": {"name": "T", "email": "t@example.com", "resume_path": str(resume)}}
    message = build_email({"id": "1", "title": "Role"}, applicant, "Body", "jobs@acme.com")
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "My_Resume.pdf"


# --- ordered delivery ------------------------------------------------------

def test_queued_notifications_flush_highest_score_first(monkeypatch, postings, tmp_path):
    from tools import notify as notify_mod

    notify_mod._pending.clear()
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "order.db"))
    sent: list[str] = []
    monkeypatch.setattr(notify_mod, "send", lambda msg, buttons=None, **kw: sent.append(msg) or "ok")

    notify_mod.queue_job_notification(postings[1], 55, "mid")
    notify_mod.queue_job_notification(postings[0], 91, "high")
    notify_mod.queue_job_notification(postings[2], 70, "low-ish")
    assert notify_mod.pending_count() == 3

    delivered = notify_mod.flush_notifications()
    assert delivered == 3
    assert [line.split("]")[0] for line in sent] == ["[91/100", "[70/100", "[55/100"]
    assert notify_mod.pending_count() == 0


def test_marketing_questions_are_not_treated_as_screening_questions():
    from tools.apply import find_screening_questions

    # Job descriptions are full of rhetorical questions; none of these are
    # things a form is asking the applicant.
    assert find_screening_questions(
        "Are you a talented Senior Developer looking for a remote job with decent compensation?"
    ) == []
    # Scraped pages carry embedded JSON, which must never become a question.
    assert find_screening_questions('"description":" Are you a dev looking for work?') == []


def test_screening_questions_are_deduplicated():
    from tools.apply import find_screening_questions

    found = find_screening_questions(
        "What is your salary expectation? What is your salary expectation?"
    )
    assert len(found) == 1


# --- career database and tailored résumés ----------------------------------

def test_committed_profile_holds_no_contact_details():
    """profile.md is public; phone numbers and addresses belong in applicant.md."""
    import re

    from config import PROFILE_PATH

    with open(PROFILE_PATH, encoding="utf-8") as handle:
        text = handle.read()
    assert not re.search(r"\+\d[\d ()-]{7,}", text), "a phone number is committed in profile.md"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an email address is committed in profile.md"


def test_career_database_parses_every_section():
    from tools.resume import parse_career_database

    db = parse_career_database()
    assert set(db["summaries"]) >= {"dotnet", "python", "node", "backend"}
    assert len(db["projects"]) >= 2
    # The headline is in the committed profile; contact details are merged in
    # from applicant.md, which is gitignored and may be absent on a fresh clone.
    assert db["identity"]["headline"]
    assert db["awards"] and db["education"] and db["certifications"] and db["languages"]
    # Every bullet carries text and tags, and no separator leaked in as a bullet.
    for project in db["projects"]:
        assert project["bullets"]
        for bullet in project["bullets"]:
            assert bullet["text"].strip()
            assert "{tags:" not in bullet["text"]


def test_dotnet_job_selects_the_dotnet_variant_and_leads_with_dotnet_skills():
    from tools.resume import fallback_selection, parse_career_database

    db = parse_career_database()
    selection = fallback_selection(
        {"title": "Senior .NET Developer", "tags": ["c#", "asp.net"],
         "description": "ASP.NET Core, Entity Framework Core and SQL Server."},
        db,
    )
    assert selection["summary"] == "dotnet"
    assert selection["skills"][0] == ".NET backend"


def test_python_job_selects_the_python_variant():
    from tools.resume import fallback_selection, parse_career_database

    selection = fallback_selection(
        {"title": "Python Backend Engineer", "tags": ["python", "fastapi"],
         "description": "FastAPI, async SQLAlchemy and PostgreSQL."},
        parse_career_database(),
    )
    assert selection["summary"] == "python"


def test_selection_can_only_reference_content_that_exists():
    """The invention guard: anything not in the database is discarded."""
    from tools.resume import parse_career_database, validate_selection

    db = parse_career_database()
    result = validate_selection(
        {"summary": "python",
         "projects": [
             {"name": "Restaurant Chain Management System", "bullets": [0, 1]},
             {"name": "Fictional Job at Google", "bullets": [0]},   # never existed
         ],
         "skills": ["Python backend", "Quantum Computing"]},        # second is invented
        db,
    )
    assert [p["name"] for p in result["projects"]] == ["Restaurant Chain Management System"]
    assert "Quantum Computing" not in result["skills"]


def test_out_of_range_bullet_indices_are_dropped():
    from tools.resume import parse_career_database, validate_selection

    db = parse_career_database()
    result = validate_selection(
        {"summary": "python",
         "projects": [{"name": "Restaurant Chain Management System", "bullets": [0, 99, -3]}],
         "skills": []},
        db,
    )
    assert result["projects"][0]["bullets"] == [0]


def test_unusable_selection_returns_none_so_the_caller_falls_back():
    from tools.resume import parse_career_database, validate_selection

    assert validate_selection({"summary": "nope", "projects": [], "skills": []}, parse_career_database()) is None


def test_generated_resume_is_one_page_with_extractable_text(tmp_path):
    from pypdf import PdfReader

    from tools.resume import fallback_selection, parse_career_database, render_pdf

    db = parse_career_database()
    posting = {"title": "Backend Engineer", "company": "Acme", "tags": ["python", "postgres"],
               "description": "FastAPI and PostgreSQL, Docker deployment, WebSockets."}
    path = render_pdf(fallback_selection(posting, db), db, str(tmp_path / "out.pdf"))

    reader = PdfReader(path)
    assert len(reader.pages) == 1, "the résumé must fit one page"
    text = reader.pages[0].extract_text()
    assert "Backend Software Engineer" in text
    assert "SUMMARY" in text and "PROJECTS" in text and "EDUCATION" in text
    # ATS-friendly: no un-extractable glyph junk.
    assert chr(127) not in text
    assert len(text) > 1500


def test_every_rendered_line_exists_in_the_career_database(tmp_path):
    """Nothing reaches the PDF that was not copied from profile.md."""
    import re

    from pypdf import PdfReader

    from tools.resume import fallback_selection, parse_career_database, render_pdf

    db = parse_career_database()
    posting = {"title": "Full Stack Developer", "company": "Acme", "tags": ["react", "node"],
               "description": "React and TypeScript frontend with a Node backend."}
    path = render_pdf(fallback_selection(posting, db), db, str(tmp_path / "out.pdf"))
    text = PdfReader(path).pages[0].extract_text()

    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    source = normalize(" ".join(
        [" ".join(db["summaries"].values())]
        + [f"{s['group']}: {s['text']}" for s in db["skills"]]
        + [b["text"] for p in db["projects"] for b in p["bullets"]]
        + [f"{p['name']} — {p['role']}" for p in db["projects"]]
        + [p["stack"] for p in db["projects"]]
        + db["awards"] + db["education"] + db["certifications"] + db["languages"]
        + list(db["identity"].values())
    ))

    # Every sentence of body text on the page must appear in the source file.
    # Composed lines (the contact strip joins identity fields with "|") are
    # checked field by field.
    for line in text.splitlines():
        for part in line.split("|"):
            candidate = normalize(part.lstrip("- "))
            if len(candidate) < 40:
                continue  # headings, wrapped fragments, single contact fields
            assert candidate in source or candidate[:60] in source, f"invented content: {part[:80]!r}"


def test_output_path_is_company_job_date():
    import datetime as dt

    from tools.resume import output_path

    path = output_path({"company": "FastLane Group", "title": "Full Stack Developer"}, "/tmp/x")
    assert path.endswith(f"fastlane-group_full-stack-developer_{dt.date.today().isoformat()}.pdf")


def test_irrelevant_skill_groups_are_dropped_from_a_tailored_resume():
    from tools.resume import MAX_SKILL_GROUPS, fallback_selection, parse_career_database

    db = parse_career_database()
    selection = fallback_selection(
        {"title": "Python Backend Engineer", "tags": ["python", "fastapi"],
         "description": "FastAPI, async SQLAlchemy, PostgreSQL, Docker."},
        db,
    )
    assert len(selection["skills"]) <= MAX_SKILL_GROUPS
    assert len(selection["skills"]) < len(db["skills"]), "a tailored résumé should not list every group"
    assert "Desktop" not in selection["skills"]
