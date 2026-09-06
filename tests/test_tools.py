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

def test_dedupe_returns_nothing_the_second_time(tmp_path, postings):
    db = str(tmp_path / "test.db")
    assert len(store.filter_new(postings, db)) == 4
    assert store.filter_new(postings, db) == []


def test_dedupe_returns_only_the_unseen_posting(tmp_path, postings):
    db = str(tmp_path / "test.db")
    store.filter_new(postings[:3], db)
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
    assert "Strong skills" in text and "Will not bid on" in text


def test_missing_profile_returns_error_string_not_exception(tmp_path):
    text = profile.read_profile(str(tmp_path / "nope.md"))
    assert text == profile.MISSING_PROFILE_MESSAGE


def test_empty_profile_returns_error_string(tmp_path):
    empty = tmp_path / "profile.md"
    empty.write_text("", encoding="utf-8")
    assert profile.read_profile(str(empty)) == profile.MISSING_PROFILE_MESSAGE


# --- notification ----------------------------------------------------------

def test_notification_contains_every_required_element(postings):
    posting = postings[0]
    message = notify.format_notification(posting, 82, "Strong FastAPI and Postgres overlap.", "Draft body here.")
    assert posting["title"] in message
    assert posting["company"] in message
    assert posting["url"] in message
    assert "82" in message
    assert "Strong FastAPI and Postgres overlap." in message
    assert "Draft body here." in message
    assert "Source: Remote OK" in message


def test_notification_reports_missing_salary_gracefully(postings):
    wordpress = next(p for p in postings if p["id"] == "1000002")
    assert "Salary: not published" in notify.format_notification(wordpress, 10, "Deal-breaker.", "n/a")


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

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send("hi") == "Notification sent via Telegram."
    assert "token123" in captured["url"]
    assert captured["payload"]["chat_id"] == "42"
    assert captured["payload"]["disable_web_page_preview"] is False
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
    good = notify.format_notification(postings[0], 70, "Fits.", "Draft.")
    assert notify.repair_message(good) == good
