"""Bid-flow tests. No network, no model calls, no email actually sent."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bidflow  # noqa: E402
from agent import load_fixture_postings  # noqa: E402
from tools import apply as apply_mod  # noqa: E402
from tools import fetch, letter, store, submit  # noqa: E402


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A posting in a temp store, with the model and the network stubbed out."""
    db = str(tmp_path / "flow.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    for module in (bidflow, submit):
        monkeypatch.setattr(module, "get_posting_record", lambda pid, db_path=db: store.get_posting_record(pid, db), raising=False)

    postings = load_fixture_postings()
    fetch.remember(postings)
    store.filter_new(postings, db)

    # Patch the store helpers bidflow uses so they hit the temp database.
    monkeypatch.setattr(bidflow, "set_status", lambda pid, s, **kw: store.set_status(pid, s, db_path=db, **kw))
    monkeypatch.setattr(bidflow, "get_posting_record", lambda pid: store.get_posting_record(pid, db))
    monkeypatch.setattr(bidflow, "submission_allowed", lambda: store.submission_allowed(db))
    monkeypatch.setattr(submit, "log_submission", lambda *a, **k: store.log_submission(*a, db_path=db, **k))

    monkeypatch.setattr(letter, "complete", lambda system, user: "Generated letter about the actual problem.")
    monkeypatch.setattr(
        apply_mod, "_fetch_page",
        lambda url: (url, "<p>Send your CV to careers@orbital.example</p>"),
    )
    bidflow.clear_draft("1000001")
    yield db
    bidflow.clear_draft("1000001")


def test_bid_produces_a_filled_draft(wired):
    draft = bidflow.start_bid("1000001")
    assert "error" not in draft
    assert draft["requirements"]["method"] == "email"
    assert draft["requirements"]["email"] == "careers@orbital.example"

    rendered = bidflow.render_draft(draft)
    assert "Application draft — Senior Python Backend Engineer @ Orbital Data" in rendered
    assert "Method: Email (careers@orbital.example)" in rendered
    assert "── Cover letter ──" in rendered
    assert "Generated letter about the actual problem." in rendered
    assert store.get_status("1000001", wired) == "bidding"


def test_edit_loop_regenerates_the_letter(wired, monkeypatch):
    bidflow.start_bid("1000001")
    monkeypatch.setattr(letter, "complete", lambda system, user: "Shorter letter.")
    revised = bidflow.revise("1000001", "make it shorter")
    assert revised["letter"] == "Shorter letter."
    assert "Shorter letter." in bidflow.render_draft(revised)


def test_two_bids_do_not_collide(wired):
    first = bidflow.start_bid("1000001")
    second = bidflow.start_bid("1000003")
    assert first["posting"]["id"] == "1000001"
    assert second["posting"]["id"] == "1000003"
    assert bidflow.get_draft("1000001")["posting"]["title"] != bidflow.get_draft("1000003")["posting"]["title"]
    bidflow.clear_draft("1000003")


def test_nothing_is_sent_without_confirmation(wired, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(submit, "send_email_application", lambda *a, **k: sent.append("x") or (True, "ok"))
    bidflow.start_bid("1000001")
    bidflow.cancel("1000001")
    assert sent == []
    assert store.get_status("1000001", wired) == "notified"
    assert bidflow.get_draft("1000001") is None


def test_confirm_submits_by_email_and_records_the_letter(wired, monkeypatch):
    monkeypatch.setattr(bidflow, "send_email_application", lambda p, a, letter_text, to: (True, f"Sent to {to}"))
    bidflow.start_bid("1000001")
    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "applied_email"
    assert "careers@orbital.example" in detail
    record = store.get_posting_record("1000001", wired)
    assert record["status"] == "applied_email"
    assert record["cover_letter"].startswith("Generated letter")


def test_failed_email_is_reported_honestly_not_as_success(wired, monkeypatch):
    monkeypatch.setattr(bidflow, "send_email_application", lambda *a, **k: (False, "SMTP refused"))
    bidflow.start_bid("1000001")
    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "failed"
    assert "nothing was sent" in detail.lower() and "SMTP refused" in detail
    assert store.get_posting_record("1000001", wired)["status"] == "bidding"


def test_ats_failure_falls_back_to_manual_and_says_so(wired, monkeypatch):
    monkeypatch.setattr(
        apply_mod, "_fetch_page",
        lambda url: (url, "<a href='https://boards.greenhouse.io/acme/jobs/999'>Apply</a>"),
    )
    monkeypatch.setattr(bidflow, "submit_to_ats", lambda *a, **k: (False, "needs a board API token"))
    draft = bidflow.start_bid("1000001")
    assert draft["requirements"]["method"] == "known_ats"
    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "applied_manual"
    assert "needs a board API token" in detail
    assert "── Cover letter ──" in detail


def test_manual_path_hands_back_everything_needed(wired, monkeypatch):
    monkeypatch.setattr(apply_mod, "_fetch_page", lambda url: (url, "<p>Apply on our site.</p>"))
    draft = bidflow.start_bid("1000001")
    assert draft["requirements"]["method"] == "manual"
    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "applied_manual"
    assert "Apply here:" in detail and "Fields to paste:" in detail


def test_skip_marks_the_posting_dismissed(wired):
    bidflow.skip("1000002")
    assert store.get_status("1000002", wired) == "skipped"


def test_rate_limit_blocks_further_submissions(wired, monkeypatch):
    monkeypatch.setattr(bidflow, "send_email_application", lambda *a, **k: (True, "ok"))
    for _ in range(5):
        store.log_submission("other", "email", "sent", "ok", db_path=wired)
    bidflow.start_bid("1000001")
    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "failed" and "Rate limit" in detail


def test_already_applied_posting_is_not_sent_twice(wired, monkeypatch):
    monkeypatch.setattr(bidflow, "send_email_application", lambda *a, **k: (True, "ok"))
    bidflow.start_bid("1000001")
    assert bidflow.confirm_and_submit("1000001")[0] == "applied_email"

    # Tapping Bid again must refuse rather than reopen the application.
    again = bidflow.start_bid("1000001")
    assert "already applied" in again["error"].lower()
    assert store.get_posting_record("1000001", wired)["status"] == "applied_email"

    status, detail = bidflow.confirm_and_submit("1000001")
    assert status == "failed"
