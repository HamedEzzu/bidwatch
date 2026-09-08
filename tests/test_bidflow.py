"""Bid-flow tests: preparing an application package.

No network, no model calls, no browser, and nothing that could reach an
employer — there is no longer any code that can.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bidflow  # noqa: E402
from agent import load_fixture_postings  # noqa: E402
from tools import fetch, letter, resume, store  # noqa: E402


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A posting in a temp store, with the model stubbed out."""
    db = str(tmp_path / "flow.db")
    monkeypatch.setattr(store, "DB_PATH", db)

    postings = load_fixture_postings()
    fetch.remember(postings)
    store.filter_new(postings, db)

    monkeypatch.setattr(bidflow, "set_status", lambda pid, s, **kw: store.set_status(pid, s, db_path=db, **kw))
    monkeypatch.setattr(bidflow, "get_posting_record", lambda pid: store.get_posting_record(pid, db))
    monkeypatch.setattr(letter, "complete", lambda system, user: "Generated letter about the actual problem.")
    monkeypatch.setattr(resume, "complete", lambda system, user: "")
    monkeypatch.setattr(bidflow, "output_path", lambda posting: resume.output_path(posting, str(tmp_path / "cv")))

    bidflow.clear_draft("1000001")
    yield db
    bidflow.clear_draft("1000001")


def test_bid_prepares_a_complete_package(wired):
    draft = bidflow.start_bid("1000001")
    assert "error" not in draft

    assert draft["letter"] == "Generated letter about the actual problem."
    assert draft["resume_path"].endswith(".pdf") and os.path.isfile(draft["resume_path"])
    assert draft["apply_url"].startswith("https://remoteok.com/")
    assert store.get_status("1000001", wired) == "bidding"

    # The field sheet carries what a form asks for, one value per line.
    sheet = draft["sheet"]
    for label in ("Full name:", "Email:", "Phone:", "Location:", "LinkedIn:", "Salary expectation:"):
        assert label in sheet


def test_rendered_package_has_every_part(wired):
    draft = bidflow.start_bid("1000001")
    text = bidflow.render_package(draft)
    assert "Application package — Senior Python Backend Engineer @ Orbital Data" in text
    assert "── Cover letter ──" in text
    assert "── Application details ──" in text
    assert "Apply here: https://remoteok.com/" in text
    assert "Résumé:" in text


def test_edit_loop_regenerates_the_letter(wired, monkeypatch):
    bidflow.start_bid("1000001")
    monkeypatch.setattr(letter, "complete", lambda system, user: "Shorter letter.")
    revised = bidflow.revise("1000001", "make it shorter")
    assert revised["letter"] == "Shorter letter."
    assert "Shorter letter." in bidflow.render_package(revised)


def test_two_applications_do_not_collide(wired):
    first = bidflow.start_bid("1000001")
    second = bidflow.start_bid("1000003")
    assert first["posting"]["id"] == "1000001"
    assert second["posting"]["id"] == "1000003"
    assert bidflow.get_draft("1000001")["posting"]["title"] != bidflow.get_draft("1000003")["posting"]["title"]
    bidflow.clear_draft("1000003")


def test_mark_applied_records_the_letter_and_resume(wired):
    draft = bidflow.start_bid("1000001")
    resume_path = draft["resume_path"]
    assert "Marked as applied" in bidflow.mark_applied("1000001")

    record = store.get_posting_record("1000001", wired)
    assert record["status"] == "applied"
    assert record["applied_at"]
    assert record["cover_letter"].startswith("Generated letter")
    assert record["resume_path"] == resume_path
    assert bidflow.get_draft("1000001") is None


def test_an_applied_posting_never_comes_back(wired, postings=None):
    bidflow.start_bid("1000001")
    bidflow.mark_applied("1000001")
    assert store.filter_new(load_fixture_postings(), wired) != []
    assert all(p["id"] != "1000001" for p in store.filter_new(load_fixture_postings(), wired))


def test_preparing_an_applied_posting_is_refused(wired):
    bidflow.start_bid("1000001")
    bidflow.mark_applied("1000001")
    assert "already marked this one applied" in bidflow.start_bid("1000001")["error"]


def test_a_legacy_applied_status_still_blocks_re_preparing(wired):
    """Databases from earlier versions hold applied_email / _ats / _manual."""
    store.set_status("1000003", "applied_manual", db_path=wired)
    assert "already marked this one applied" in bidflow.start_bid("1000003")["error"]


def test_skip_marks_the_posting_dismissed(wired):
    bidflow.skip("1000002")
    assert store.get_status("1000002", wired) == "skipped"


def test_closing_leaves_no_record_of_applying(wired):
    bidflow.start_bid("1000001")
    assert "Nothing was recorded" in bidflow.cancel("1000001")
    assert store.get_status("1000001", wired) == "notified"
    assert bidflow.get_draft("1000001") is None


def test_resume_failure_does_not_block_the_package(wired, monkeypatch):
    monkeypatch.setattr(bidflow, "select_content", lambda p, db: (_ for _ in ()).throw(RuntimeError("model down")))
    draft = bidflow.start_bid("1000001")
    assert "error" not in draft, "a résumé failure must not stop the letter and field sheet"
    assert "failed" in draft["resume_note"]
    assert draft["letter"]


def test_nothing_in_the_flow_can_reach_an_employer():
    """The design guarantee: no submission path exists any more."""
    import re

    with open(bidflow.__file__, encoding="utf-8") as handle:
        source = handle.read()
    banned = ("smtplib", "send_email", "sendmail", "submit_to_ats", "requests.post", "playwright")
    for term in banned:
        assert term not in source, f"{term} is back in the bid flow"
    assert not re.search(r"\.click\(|\.submit\(", source)
