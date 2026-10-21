"""The AI assistant must not throw away its context because ONE symbol failed.

WHY
---
`enrich_market_context_for_ai` (api/ai_assistant.py:531) fetches live
screener metrics for the first two tickers it finds in the prompt. The
per-symbol request sat inside one try/except whose handler ended in `break`,
so the FIRST failing symbol aborted the whole context build: the assistant
lost the context for every remaining symbol and the operator saw one quiet
warning line.

Two different failures need two different answers:

* A SYMBOL-level failure (the screener answered, but the body for that
  symbol is unusable) says nothing about the next symbol. The next symbol
  must still be fetched -- that is the whole point of this fix.
* A TRANSPORT-level failure (connection refused, DNS failure, timeout) is a
  property of the SCREENER HOST, not of the symbol. Issuing another request
  would cost another full timeout (2.0s) and cannot succeed, so one outage
  must not become N slow timeouts. That is a PER-CALL short-circuit, chosen
  over a backoff because a backoff still pays the timeout.

Either way the degradation must be VISIBLE: the summary says how many
symbols were skipped and why.

These drive the real function with a fake httpx client -- no network.
"""

import logging

import httpx
import pytest

from api import ai_assistant

LOGGER_NAME = "api.ai_assistant"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


_GOOD_PAYLOAD = {
    "data": {
        "natr": 1.5,
        "macro_trend": "up",
        "oracle_regime": 1,
        "volume_24h": 123456,
    }
}


def _install_fake_screener(monkeypatch, handler):
    """Replace httpx.AsyncClient with a fake that records every URL asked for.

    `handler(url)` returns a `_FakeResponse` or raises.
    """
    requested = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            requested.append(url)
            return handler(url)

    monkeypatch.setattr(ai_assistant.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("SCREENER_API_URL", "https://screener.example.test")
    return requested


def _degraded_messages(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "DEGRADED CONTEXT" in r.getMessage()
    ]


PROMPT = "Trade BTCUSDT and SOLUSDT now"


async def test_one_symbol_failing_does_not_cost_the_others(
    monkeypatch, caplog
):
    """THE CASE: BTCUSDT's body is unusable, SOLUSDT must still be fetched.

    Before the fix the `break` aborted the loop on BTCUSDT and the assistant
    got NO context at all, even though SOLUSDT was perfectly readable.
    """
    requested = _install_fake_screener(
        monkeypatch,
        lambda url: (
            _FakeResponse(200, json_error=ValueError("Expecting value: line 1"))
            if "BTCUSDT" in url
            else _FakeResponse(200, payload=_GOOD_PAYLOAD)
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        context = await ai_assistant.enrich_market_context_for_ai(PROMPT)

    assert any("SOLUSDT" in url for url in requested), (
        "one symbol's failure must not abort the context build for the "
        f"remaining symbols. Only these were requested: {requested!r}"
    )
    assert "LIVE SCREENER DATA for SOLUSDT" in context, (
        "the surviving symbol's context must be returned. Got: "
        f"{context!r}"
    )
    assert "LIVE SCREENER DATA for BTCUSDT" not in context, (
        "the failed symbol must not contribute a context block. Got: "
        f"{context!r}"
    )


async def test_a_degraded_context_is_reported_with_counts_and_reasons(
    monkeypatch, caplog
):
    """The operator must be able to see that the assistant is degraded."""
    _install_fake_screener(
        monkeypatch,
        lambda url: (
            _FakeResponse(200, json_error=ValueError("Expecting value: line 1"))
            if "BTCUSDT" in url
            else _FakeResponse(200, payload=_GOOD_PAYLOAD)
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        await ai_assistant.enrich_market_context_for_ai(PROMPT)

    degraded = _degraded_messages(caplog)
    assert degraded, (
        "a partially failed context build must be reported, not left as one "
        "quiet per-symbol warning. Records: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    message = "\n".join(degraded)
    assert "1 of 2" in message, (
        f"the report must say how many symbols were skipped. Got: {message!r}"
    )
    assert "BTCUSDT" in message, (
        f"the report must name the skipped symbol. Got: {message!r}"
    )
    assert "ValueError" in message, (
        f"the report must say WHY the symbol was skipped. Got: {message!r}"
    )


async def test_a_transport_failure_costs_one_timeout_not_one_per_symbol(
    monkeypatch, caplog
):
    """One outage must not become N slow timeouts.

    A transport-level failure is a property of the screener HOST, so the
    remaining symbols are skipped in this call rather than each paying
    another full 2.0s timeout.
    """
    requested = _install_fake_screener(
        monkeypatch,
        lambda url: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused")
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        context = await ai_assistant.enrich_market_context_for_ai(PROMPT)

    assert len(requested) == 1, (
        "an unreachable screener must cost ONE request, not one per symbol. "
        f"Requested: {requested!r}"
    )
    assert context == "", "no symbol could be read, so there is no context"

    degraded = _degraded_messages(caplog)
    assert degraded, (
        "an unreachable screener must be reported as a degraded context. "
        f"Records: {[r.getMessage() for r in caplog.records]!r}"
    )
    message = "\n".join(degraded)
    assert "2 of 2" in message, (
        f"the report must count BOTH symbols as skipped. Got: {message!r}"
    )
    assert "BTCUSDT" in message and "SOLUSDT" in message, (
        f"the report must name every skipped symbol. Got: {message!r}"
    )


async def test_a_healthy_screener_is_unchanged(monkeypatch, caplog):
    """Behaviour must be unchanged when the screener is healthy."""
    requested = _install_fake_screener(
        monkeypatch, lambda url: _FakeResponse(200, payload=_GOOD_PAYLOAD)
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        context = await ai_assistant.enrich_market_context_for_ai(PROMPT)

    assert len(requested) == 2, f"both symbols must be fetched. Got {requested!r}"
    assert "LIVE SCREENER DATA for BTCUSDT" in context
    assert "LIVE SCREENER DATA for SOLUSDT" in context
    assert not _degraded_messages(caplog), (
        "a healthy screener must not produce a degraded-context report: "
        f"{_degraded_messages(caplog)!r}"
    )


async def test_a_symbol_the_screener_does_not_know_is_not_an_outage(
    monkeypatch, caplog
):
    """A 404 is the screener answering, not the screener failing.

    It must not be reported as a degraded context: nothing broke, the symbol
    simply is not in the screener.
    """
    _install_fake_screener(
        monkeypatch,
        lambda url: (
            _FakeResponse(404, payload={})
            if "BTCUSDT" in url
            else _FakeResponse(200, payload=_GOOD_PAYLOAD)
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        context = await ai_assistant.enrich_market_context_for_ai(PROMPT)

    assert "LIVE SCREENER DATA for SOLUSDT" in context
    assert not _degraded_messages(caplog), (
        "a symbol the screener does not know is not a failure. Got: "
        f"{_degraded_messages(caplog)!r}"
    )


async def test_no_symbols_in_the_prompt_means_no_requests(monkeypatch, caplog):
    """A prompt with no ticker must not touch the screener."""
    requested = _install_fake_screener(
        monkeypatch, lambda url: _FakeResponse(200, payload=_GOOD_PAYLOAD)
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        context = await ai_assistant.enrich_market_context_for_ai("trade ethereum")

    assert requested == [], f"no request expected. Got {requested!r}"
    assert context == ""
    assert not _degraded_messages(caplog)
