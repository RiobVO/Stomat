"""Сетевые ошибки OpenAI SDK → ProviderDownError + сборка fallback-цепочки.

Сеть не дёргается: клиент подменяется стабом. openai — optional-зависимость
[llm], без неё файл скипается целиком.
"""
from __future__ import annotations

import pytest

openai = pytest.importorskip("openai")

import httpx

from navbat.nlu.extractor import ProviderDownError
from navbat.nlu.openai_extractor import OpenAIExtractor

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


class _BoomCompletions:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def parse(self, **kwargs):
        raise self._error


def make_extractor(monkeypatch, error: Exception) -> OpenAIExtractor:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    extractor = OpenAIExtractor()

    class _Chat:
        completions = _BoomCompletions(error)

    class _Client:
        chat = _Chat()

    extractor._client = _Client()
    return extractor


def test_connection_error_becomes_provider_down(monkeypatch):
    error = openai.APIConnectionError(request=_REQUEST)
    with pytest.raises(ProviderDownError):
        make_extractor(monkeypatch, error).extract("чистку завтра")


def test_rate_limit_becomes_provider_down(monkeypatch):
    response = httpx.Response(429, request=_REQUEST)
    error = openai.RateLimitError("429", response=response, body=None)
    with pytest.raises(ProviderDownError):
        make_extractor(monkeypatch, error).extract("чистку завтра")


def test_server_error_becomes_provider_down(monkeypatch):
    response = httpx.Response(500, request=_REQUEST)
    error = openai.InternalServerError("500", response=response, body=None)
    with pytest.raises(ProviderDownError):
        make_extractor(monkeypatch, error).extract("чистку завтра")


# ── Сборка боевой цепочки ────────────────────────────────────────────────────

def test_build_with_gemini_key_inserts_fallback(monkeypatch, app_session_factory,
                                                clinic_a):
    from navbat.nlu.fallback import FallbackExtractor
    from navbat.supervisor import build_real_extractor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    chain = build_real_extractor(app_session_factory, clinic_a, notifier=None)
    # Budgeted → Drift → Deidentifying → Fallback(OpenAI, Gemini)
    assert isinstance(chain._inner._inner._inner, FallbackExtractor)


def test_build_without_gemini_key_keeps_single_provider(monkeypatch,
                                                        app_session_factory,
                                                        clinic_a):
    from navbat.supervisor import build_real_extractor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    chain = build_real_extractor(app_session_factory, clinic_a, notifier=None)
    assert isinstance(chain._inner._inner._inner, OpenAIExtractor)


def test_openai_on_repair_called(monkeypatch):
    from types import SimpleNamespace

    repairs: list[int] = []
    calls = {"n": 0}
    valid = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            refusal=None,
            parsed=__import__("navbat.nlu.schema", fromlist=["Extraction"])
            .Extraction(intent="book", service=None, doctor=None, date_ref=None,
                        time_ref=None, language="ru", is_medical=False)))],
        usage=None,
    )
    broken = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(refusal="мусор",
                                                         parsed=None))],
        usage=None,
    )

    class _Completions:
        def parse(self, **kwargs):
            calls["n"] += 1
            return broken if calls["n"] == 1 else valid

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    extractor = OpenAIExtractor(on_repair=lambda: repairs.append(1))
    extractor._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()))

    got = extractor.extract("чистку завтра")
    assert got.intent == "book"
    assert repairs == [1], "ровно один repair учтён"
