"""The Ollama request and its failure taxonomy. Protects 949aca8 and de146f6.

None of this touches the GPU: httpx.AsyncClient.post is replaced per test, so
no request is ever built or sent. The taxonomy tests are state 2 ("I was not
looking") against state 3 ("I looked and could not tell") in one file — a
daemon that is down and a daemon that hung must not file the same status.
"""

import json

import httpx
import pytest

from app.services import classifier

_REPLY = json.dumps(
    {
        "event_type": "military",
        "severity": 6,
        "killed_reported": 17,
        "location_name": "Zrariyeh",
        "summary": "An Israeli airstrike on Zrariyeh killed 17 people.",
    }
)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _post_returning(response):
    async def fake_post(self, url, json=None):
        return response

    return fake_post


def _post_raising(exc):
    async def fake_post(self, url, json=None):
        raise exc

    return fake_post


async def test_the_payload_turns_thinking_off(monkeypatch):
    """949aca8. Every qwen3 model on this host reports the thinking
    capability, and with it enabled the reasoning goes to a separate field
    while `response` comes back EMPTY. Measured: qwen3.8-27b produced 3/3
    parse_failed with `got: ''` until this line existed. qwen3:8b answered
    anyway, so the defect was invisible while only the small model ran."""
    captured = {}

    async def fake_post(self, url, json=None):
        captured.update(json)
        return _Response(200, {"response": _REPLY})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await classifier._classify_ollama("Airstrike on Zrariyeh", "", [])

    assert captured["think"] is False, "a thinking model returns an empty body"
    assert captured["format"] == "json"
    assert captured["stream"] is False
    # num_predict is a budget for the ANSWER; a thinking model spends it on
    # reasoning first and then has nothing left to say.
    assert captured["options"]["num_predict"] == 250
    assert captured["options"]["temperature"] == 0


async def test_a_good_reply_classifies(monkeypatch):
    """949aca8. The positive control for the taxonomy below: with a real body
    the same code path produces an "ok" row, so the failure statuses are
    statements about the failure and not about the fake."""
    monkeypatch.setattr(
        httpx.AsyncClient, "post", _post_returning(_Response(200, {"response": _REPLY}))
    )
    out = await classifier._classify_ollama("Airstrike on Zrariyeh", "", [])
    assert out["extraction_status"] == "ok"
    assert out["killed_reported"] == 17
    assert out["extraction_model"] == classifier.settings.ollama_model


@pytest.mark.parametrize(
    "exc,status",
    [
        (httpx.TimeoutException("hung"), "ollama_timeout"),
        (httpx.ConnectError("refused"), "ollama_unreachable"),
    ],
)
async def test_a_hung_daemon_and_a_dead_one_file_different_statuses(
    monkeypatch, exc, status
):
    """de146f6. TimeoutException subclasses RequestError, so it has to be
    caught FIRST. A daemon that took the connection and then hung is not a
    daemon that is down, and an archive filing both as "unreachable" cannot
    tell them apart later."""
    monkeypatch.setattr(httpx.AsyncClient, "post", _post_raising(exc))
    out = await classifier._classify_ollama("x", "", [])
    assert out["extraction_status"] == status
    # Nothing reached a model, so nothing is attributed to one.
    assert out["extraction_model"] is None


async def test_an_unpulled_model_does_not_look_like_a_parse_error(monkeypatch):
    """de146f6. A model name that was never pulled is a configuration fact,
    not unreadable output."""
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        _post_returning(_Response(404, text='{"error":"model \'x\' not found"}')),
    )
    out = await classifier._classify_ollama("x", "", [])
    assert out["extraction_status"] == "ollama_model_missing"


async def test_an_http_error_carries_its_code(monkeypatch):
    """de146f6. The status names the code so a run of 500s is separable from
    a run of 404s without reading the log."""
    monkeypatch.setattr(
        httpx.AsyncClient, "post", _post_returning(_Response(500, text="boom"))
    )
    out = await classifier._classify_ollama("x", "", [])
    assert out["extraction_status"] == "ollama_http_500"


async def test_a_non_string_response_field_does_not_raise_out_of_the_poller(
    monkeypatch,
):
    """949aca8. _THINK_RE.sub on a non-string raises TypeError straight out
    of classify_message, and one raise costs the rest of that feed cycle —
    the poller only guards per feed and the raising article is already marked
    seen, so it is never retried."""
    monkeypatch.setattr(
        httpx.AsyncClient, "post", _post_returning(_Response(200, {"response": 42}))
    )
    out = await classifier._classify_ollama("x", "", [])
    assert out["extraction_status"] == "parse_failed"


async def test_a_leading_think_block_is_stripped(monkeypatch):
    """949aca8. Belt and braces for the line above: if a model ever does emit
    a <think> block with format:json, it precedes the JSON and would
    otherwise read as a parse failure."""
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        _post_returning(_Response(200, {"response": "<think>hmm</think>" + _REPLY})),
    )
    out = await classifier._classify_ollama("x", "", [])
    assert out["extraction_status"] == "ok"
    assert out["severity"] == 6
