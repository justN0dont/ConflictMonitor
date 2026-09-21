"""Classifier contracts: a schema violation is a parse failure, never a
measurement. Protects e5ad5ae and de146f6.

The headline is test_severity_true_is_refused. `severity: true` used to coerce
to 1, trip the `severity <= 1` noise branch in _handle_response, and make the
caller DELETE the event before insert — state 3 ("I looked and couldn't tell")
written into state 1 ("nothing happened"), with no row left behind to audit.
"""

import json

import pytest
from pydantic import ValidationError

from app.config import settings
from app.services import classifier
from app.services.classifier import ClassifierResult

_GOOD = {
    "event_type": "military",
    "severity": 7,
    "killed_reported": None,
    "location_name": "Natanz",
    "summary": "Airstrike on the enrichment halls.",
}


# ── e5ad5ae: reject_boolean covers BOTH fields ───────────────────────────────


def test_severity_true_is_refused_because_it_used_to_erase_the_event():
    """e5ad5ae. pydantic validates bool as int, so true became severity 1 —
    the [NOISE] score — and the event was dropped with extraction_status
    "ok". A model answering the yes/no reading of the question erased the
    row. Refused, not coerced: no row is better than a wrong one."""
    with pytest.raises(ValidationError):
        ClassifierResult(**{**_GOOD, "severity": True})


def test_severity_false_is_refused():
    """e5ad5ae. false coerces to 0, which Field(ge=1) would catch — asserted
    anyway so the bool rule is pinned independently of the range rule."""
    with pytest.raises(ValidationError):
        ClassifierResult(**{**_GOOD, "severity": False})


@pytest.mark.parametrize("value", [True, False])
def test_killed_reported_refuses_a_yes_no_answer(value):
    """e5ad5ae. A model that read "were people killed" instead of "how many"
    answers true; the row was then stamped "ok" and the feed printed
    "1 KILLED" — a guess wearing a measurement's clothes."""
    with pytest.raises(ValidationError):
        ClassifierResult(**{**_GOOD, "killed_reported": value})


@pytest.mark.parametrize("quoted,expected", [("17", 17), ("0", 0)])
def test_a_quoted_number_still_coerces(quoted, expected):
    """e5ad5ae. reject_boolean tests isinstance(v, bool) only. LLMs emit
    quoted numbers constantly and every other value keeps its behaviour."""
    assert ClassifierResult(**{**_GOOD, "killed_reported": quoted}).killed_reported == expected


def test_zero_killed_is_not_null():
    """models.py:43. 0 means the message said nobody died; NULL means it
    stated no count. 88 archive rows really are 0. Neither may be rendered or
    persisted as the other."""
    assert ClassifierResult(**{**_GOOD, "killed_reported": 0}).killed_reported == 0
    assert ClassifierResult(**{**_GOOD, "killed_reported": None}).killed_reported is None


@pytest.mark.parametrize(
    "field,value",
    [("severity", 11), ("severity", 0), ("severity", -3), ("killed_reported", -1)],
)
def test_out_of_range_stays_a_parse_failure_and_is_never_clamped(field, value):
    """e5ad5ae deleted clamp_severity rather than reviving it. A reply of 11
    is not a 10; clamping manufactures a measurement out of a parse
    failure."""
    with pytest.raises(ValidationError):
        ClassifierResult(**{**_GOOD, field: value})


def test_severity_defaults_to_none_not_five():
    """89c6f54 / models.py:22. default=5 wrote the failure fallback to disk
    as though it were a classification — 86.3% of the archive is exactly
    5."""
    assert ClassifierResult().severity is None


# ── _handle_response: a reply that classified nothing is not "ok" ────────────


def test_a_reply_with_no_severity_is_a_parse_failure_not_an_ok_row():
    """e5ad5ae. Recording an empty object as extraction_status "ok" files a
    row claiming a classification that never happened, indistinguishable from
    a real result by its status."""
    with pytest.raises(json.JSONDecodeError):
        classifier._handle_response("{}", "raw", [], "qwen3:8b")


def test_a_json_list_is_not_a_classification():
    """e5ad5ae. Valid JSON, but a list is not a classification — raised as a
    decode error so both backends record it as parse_failed."""
    with pytest.raises(json.JSONDecodeError):
        classifier._handle_response('[{"severity": 5}]', "raw", [], "qwen3:8b")


def test_markdown_fences_are_stripped():
    """de146f6. Local models wrap JSON in ```json fences constantly; an
    unstripped fence is a parse_failed row for a reply that classified
    fine."""
    out = classifier._handle_response(
        "```json\n" + json.dumps(_GOOD) + "\n```", "raw", [], "qwen3:8b"
    )
    assert out["severity"] == 7
    assert out["extraction_status"] == "ok"


def test_noise_summary_sets_is_noise_with_status_ok():
    """e5ad5ae. [NOISE] is a real classification outcome, so it is "ok" —
    the distinction this suite guards is between that and a FAILURE that
    also drops the event."""
    out = classifier._handle_response(
        json.dumps({**_GOOD, "summary": "[NOISE]"}), "raw", [], "qwen3:8b"
    )
    assert out["is_noise"] is True
    assert out["extraction_status"] == "ok"


def test_severity_one_is_noise_but_severity_none_is_not_reachable_as_one():
    """e5ad5ae. An integer 1 is a real answer meaning noise and still takes
    that branch; a MISSING severity must not short-circuit to a drop, which
    is why the branch tests `severity is not None` before comparing."""
    out = classifier._handle_response(
        json.dumps({**_GOOD, "severity": 1}), "raw", [], "qwen3:8b"
    )
    assert out["is_noise"] is True
    with pytest.raises(json.JSONDecodeError):
        classifier._handle_response(
            json.dumps({k: v for k, v in _GOOD.items() if k != "severity"}),
            "raw", [], "qwen3:8b",
        )


def test_the_row_records_which_model_produced_it():
    """de146f6. Without extraction_model an archive mixing Haiku and qwen3
    rows is uninterpretable, and a classifier evaluation cannot tell whose
    output it is scoring."""
    out = classifier._handle_response(json.dumps(_GOOD), "raw", [], "qwen3:8b")
    assert out["extraction_model"] == "qwen3:8b"


# ── _build_fallback: the absent key IS the contract ──────────────────────────


def test_a_fallback_carries_no_severity_key_at_all():
    """89c6f54. Asserted on key ABSENCE, not on .get() being None: a
    fallback is a regex scrape of the raw text, not a threat assessment, and
    writing 5 here is what put a median threat score on 86.3% of the
    archive."""
    out = classifier._build_fallback("Airstrike near Natanz reported", "no_backend")
    assert "severity" not in out
    assert "killed_reported" not in out


def test_a_fallback_says_why_it_is_a_fallback():
    """0051932. State 2 ("I was not looking") and state 3 ("I looked and
    couldn't tell") are different rows, and extraction_status is the only
    thing that tells them apart."""
    assert classifier._build_fallback("x", "no_backend")["extraction_status"] == "no_backend"
    assert classifier._build_fallback("x", "parse_failed", "qwen3:8b")["extraction_model"] == "qwen3:8b"
    assert classifier._build_fallback("x", "no_backend")["extraction_model"] is None


# ── classify_message routing: every branch here returns before any I/O ───────


async def test_backend_none_returns_no_backend(monkeypatch):
    """de146f6. LLM_BACKEND=none is the documented way to run without a
    model; it must be a recorded status, not a silent nothing."""
    monkeypatch.setattr(settings, "llm_backend", "none")
    assert (await classifier.classify_message("x"))["extraction_status"] == "no_backend"


async def test_backend_name_is_normalised(monkeypatch):
    """de146f6. A stray space or capital in .env must not change which
    backend runs."""
    monkeypatch.setattr(settings, "llm_backend", "  NONE  ")
    assert (await classifier.classify_message("x"))["extraction_status"] == "no_backend"


async def test_a_typo_never_silently_bills_the_paid_api(monkeypatch):
    """de146f6. 'olama' used to fall through to Anthropic — an unrecorded
    backend switch that sent traffic and spend to the paid API. The network
    ban in conftest makes an actual call a hard failure, so this asserts both
    the status and that nothing was reached."""
    monkeypatch.setattr(settings, "llm_backend", "olama")
    assert (await classifier.classify_message("x"))["extraction_status"] == "bad_backend"


async def test_anthropic_without_a_key_returns_no_api_key(monkeypatch):
    """de146f6. The key is currently disabled, so this is the live branch;
    it must name itself rather than look like a parse failure."""
    monkeypatch.setattr(settings, "llm_backend", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert (await classifier.classify_message("x"))["extraction_status"] == "no_api_key"
