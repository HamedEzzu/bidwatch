"""Form-filling tests. Playwright is mocked; nothing here opens a browser
or touches a real site."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import formfill  # noqa: E402


@pytest.fixture
def applicant() -> dict:
    return {
        "fields": {
            "name": "Hamed Ezzu", "email": "h@example.com", "phone": "+218 91 000 0000",
            "location": "Tripoli, Libya", "linkedin": "https://linkedin.com/in/h",
            "github": "https://github.com/h", "portfolio": "https://h.example",
        },
        "answers": {
            "salary expectation": "$20/hour", "work authorization": "Contractor, remote only",
            "notice period": "Immediate",
        },
    }


def field(**kwargs) -> dict:
    base = {"index": 0, "tag": "input", "type": "text", "name": "", "id": "",
            "label": "", "placeholder": "", "aria": "", "required": False, "selector": "#x"}
    base.update(kwargs)
    return base


# --- values ----------------------------------------------------------------

def test_applicant_values_split_the_name_and_drop_blanks(applicant):
    values = formfill.applicant_values(applicant, "My letter.")
    assert values["full_name"] == "Hamed Ezzu"
    assert values["first_name"] == "Hamed" and values["last_name"] == "Ezzu"
    assert values["cover_letter"] == "My letter."
    assert values["salary"] == "$20/hour"
    assert "relocate" not in values  # not in applicant.md, so not offered


def test_flagged_screening_answers_are_never_offered(applicant):
    from tools.applicant import NEEDS_INPUT

    values = formfill.applicant_values(applicant, "L", {"Favourite colour?": NEEDS_INPUT})
    assert not any(v == NEEDS_INPUT for v in values.values())


# --- layer 1: attributes ---------------------------------------------------

def test_exact_attribute_matching():
    assert formfill.match_by_attribute(field(name="first_name")) == "first_name"
    assert formfill.match_by_attribute(field(id="email_address")) == "email"
    assert formfill.match_by_attribute(field(name="urls[LinkedIn]")) == "linkedin"
    assert formfill.match_by_attribute(field(name="mystery_box")) is None


# --- layer 2: labels -------------------------------------------------------

def test_fuzzy_label_matching():
    assert formfill.match_by_label(field(label="Phone number *")) == "phone"
    assert formfill.match_by_label(field(label="LinkedIn Profile URL")) == "linkedin"
    assert formfill.match_by_label(field(placeholder="Where are you based?")) == "location"


def test_last_name_is_not_matched_as_full_name():
    """The avoid-list stops the classic first/last vs full name collision."""
    assert formfill.match_by_label(field(label="Last name")) == "last_name"
    assert formfill.match_by_label(field(label="First name")) == "first_name"
    assert formfill.match_by_label(field(label="Full name")) == "full_name"


def test_unrelated_label_matches_nothing():
    assert formfill.match_by_label(field(label="How did you hear about us?")) is None


# --- planning --------------------------------------------------------------

def test_plan_fills_maps_a_typical_form(applicant):
    values = formfill.applicant_values(applicant, "Letter body.")
    descriptors = formfill.describe_fields([
        {"index": 0, "name": "first_name", "selector": "#a"},
        {"index": 1, "name": "last_name", "selector": "#b"},
        {"index": 2, "label": "Email address", "selector": "#c", "required": True},
        {"index": 3, "label": "Phone", "selector": "#d"},
        {"index": 4, "tag": "textarea", "label": "Cover letter", "selector": "#e"},
        {"index": 5, "type": "file", "label": "Resume", "selector": "#f"},
    ])
    plan = formfill.plan_fills(descriptors, values, use_model=False)
    mapped = {f["field"]: f["value"] for f in plan["fills"]}
    assert mapped["first_name"] == "Hamed"
    assert mapped["email"] == "h@example.com"
    assert mapped["cover_letter"] == "Letter body."
    assert "resume" in mapped
    assert plan["unmatched"] == []


def test_unmatched_and_required_blank_are_reported(applicant):
    values = formfill.applicant_values(applicant, "L")
    descriptors = formfill.describe_fields([
        {"index": 0, "label": "Email", "selector": "#a"},
        {"index": 1, "label": "Why do you want to work here?", "tag": "textarea", "selector": "#b"},
        {"index": 2, "label": "What is your favourite colour?", "selector": "#c", "required": True},
    ])
    plan = formfill.plan_fills(descriptors, values, use_model=False)
    labels = [u["label"] for u in plan["unmatched"]]
    assert "What is your favourite colour?" in labels
    assert [r["label"] for r in plan["required_blank"]] == ["What is your favourite colour?"]


def test_a_value_is_never_used_twice(applicant):
    values = formfill.applicant_values(applicant, "L")
    descriptors = formfill.describe_fields([
        {"index": 0, "label": "Email", "selector": "#a"},
        {"index": 1, "label": "Confirm email", "selector": "#b"},
    ])
    plan = formfill.plan_fills(descriptors, values, use_model=False)
    assert sum(1 for f in plan["fills"] if f["field"] == "email") == 1


def test_every_fill_records_which_layer_decided_it(applicant):
    values = formfill.applicant_values(applicant, "L")
    descriptors = formfill.describe_fields([
        {"index": 0, "name": "email", "selector": "#a"},
        {"index": 1, "label": "Phone number", "selector": "#b"},
    ])
    plan = formfill.plan_fills(descriptors, values, use_model=False)
    assert {f["how"] for f in plan["fills"]} == {"attribute", "label"}


# --- layer 3: the model ----------------------------------------------------

def test_model_layer_places_an_ambiguous_field(monkeypatch, applicant):
    values = formfill.applicant_values(applicant, "L")
    monkeypatch.setattr(formfill, "complete", lambda s, u: '{"0": "salary"}')
    descriptors = formfill.describe_fields([{"index": 0, "label": "Your expectations", "selector": "#a"}])
    plan = formfill.plan_fills(descriptors, values, use_model=True)
    assert plan["fills"][0]["field"] == "salary"
    assert plan["fills"][0]["how"] == "model"


def test_model_cannot_invent_a_value(monkeypatch, applicant):
    values = formfill.applicant_values(applicant, "L")
    monkeypatch.setattr(formfill, "complete", lambda s, u: '{"0": "blood_type"}')
    descriptors = formfill.describe_fields([{"index": 0, "label": "Mystery", "selector": "#a"}])
    plan = formfill.plan_fills(descriptors, values, use_model=True)
    assert plan["fills"] == []
    assert plan["unmatched"][0]["label"] == "Mystery"


def test_unusable_model_output_leaves_the_field_blank(monkeypatch, applicant):
    values = formfill.applicant_values(applicant, "L")
    monkeypatch.setattr(formfill, "complete", lambda s, u: "I could not say")
    descriptors = formfill.describe_fields([{"index": 0, "label": "Mystery", "selector": "#a"}])
    assert formfill.plan_fills(descriptors, values, use_model=True)["fills"] == []


# --- provider detection ----------------------------------------------------

def test_known_ats_providers_are_recognised():
    assert formfill._detect_provider("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert formfill._detect_provider("https://jobs.lever.co/acme/x") == "lever"
    assert formfill._detect_provider("https://acme.com/careers") is None


def test_every_provider_has_selectors_for_the_core_fields():
    for provider, selectors in formfill.ATS_SELECTORS.items():
        assert "email" in selectors, provider
        assert selectors.get("full_name") or selectors.get("first_name"), provider


# --- reporting -------------------------------------------------------------

def test_report_lists_filled_unmatched_and_required_blanks():
    report = formfill.format_report(
        {"company": "Acme Corp"},
        {"fills": [{"field": "full_name"}, {"field": "email"}, {"field": "cover_letter"}, {"field": "resume"}],
         "unmatched": [{"label": "Why do you want to work here?", "required": False}],
         "required_blank": [{"label": "Salary expectation", "required": True}]},
    )
    assert "Form opened and filled — Acme Corp" in report
    assert "✅ Filled: full name, email, cover letter, résumé" in report
    assert '⚠️ Could not match: "Why do you want to work here?"' in report
    assert '⚠️ Left blank (looks required): "Salary expectation"' in report
    assert "Nothing was submitted" in report


def test_report_explains_a_failure():
    report = formfill.format_report({"company": "Acme"}, {"error": "the page blocked automation"})
    assert "Could not fill the form — Acme" in report
    assert "blocked automation" in report


# --- failure handling ------------------------------------------------------

def test_missing_playwright_is_reported_not_raised(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    result = formfill.open_prefilled_application(
        {"id": "1", "url": "https://example.com/apply"}, {"apply_url": "https://example.com/apply"}, ""
    )
    assert "Playwright is not installed" in result["error"]


def test_no_apply_url_is_reported():
    result = formfill.open_prefilled_application({"id": "1"}, {}, "")
    assert "No application URL" in result["error"]


def test_malformed_posting_is_reported():
    assert "could not be read" in formfill.open_prefilled_application("{not json", {}, "")["error"]


# --- the one rule that matters ---------------------------------------------

def test_the_module_contains_no_way_to_submit_a_form():
    """The rule that matters: nothing here may submit an application.

    One click is permitted — the listing's "Apply" anchor, which is navigation,
    not submission — and only inside `_click_apply_anchor`, which proves the
    element is an <a> and refuses anything of type=submit. Everything else that
    could submit a form is banned outright.
    """
    import re

    with open(formfill.__file__, encoding="utf-8") as handle:
        source = handle.read()

    lines = source.splitlines()
    allowed_start = next(i for i, l in enumerate(lines) if l.startswith("def _click_apply_anchor"))
    allowed_end = next(i for i, l in enumerate(lines[allowed_start + 1:], allowed_start + 1)
                       if l.startswith("def "))

    for number, line in enumerate(lines):
        if line.strip().startswith("#") or '"""' in line:
            continue
        assert not re.search(r"\.submit\(\)|press\(['\"]Enter|keyboard\.press", line), \
            f"submit path found on line {number + 1}: {line!r}"
        if re.search(r"\.click\(", line):
            assert allowed_start < number < allowed_end, \
                f"click outside the apply-anchor helper on line {number + 1}: {line!r}"


def test_the_apply_click_refuses_anything_that_is_not_a_link():
    """A submit button dressed up as "Apply" must never be clicked."""
    source = open(formfill.__file__, encoding="utf-8").read()
    body = source.split("def _click_apply_anchor")[1].split("\ndef ")[0]
    assert 'tag != "a"' in body and 'kind == "submit"' in body
    assert "a[href]" in body


def test_sign_in_walls_are_recognised():
    assert formfill.looks_like_sign_in_wall("https://remoteok.com/sign-up?user_type=worker")
    assert formfill.looks_like_sign_in_wall("https://x.com/login?next=/apply")
    assert not formfill.looks_like_sign_in_wall("https://boards.greenhouse.io/acme/jobs/1")


def test_job_board_listings_are_not_treated_as_application_forms():
    """The bug this fixes: a Remote OK listing has 50+ search and nav inputs."""
    listing_fields = [{"index": i, "name": f"filter_{i}", "in_chrome": i % 2 == 0} for i in range(50)]
    listing_fields.append({"index": 99, "type": "search", "placeholder": "🔍 Search"})
    descriptors = formfill.describe_fields(listing_fields)
    assert formfill.looks_like_application_form(descriptors, form_count=0) is False


def test_search_and_chrome_inputs_are_never_filled():
    kept = formfill.describe_fields([
        {"index": 0, "type": "search", "placeholder": "🔍 Search"},
        {"index": 1, "name": "newsletter_email", "label": "Subscribe"},
        {"index": 2, "name": "nav_query", "in_chrome": True},
        {"index": 3, "name": "email", "label": "Email address"},
    ])
    assert [d["name"] for d in kept] == ["email"]


def test_report_does_not_list_walls_of_unnamed_fields():
    report = formfill.format_report(
        {"company": "Acme"},
        {"fills": [{"field": "email"}],
         "unmatched": [{"label": "unnamed field", "required": False} for _ in range(40)]
                      + [{"label": "Portfolio URL", "required": False}],
         "required_blank": []},
    )
    assert report.count("unnamed field") == 0
    assert '⚠️ Could not match: "Portfolio URL"' in report
    assert "other field(s) left blank" in report
