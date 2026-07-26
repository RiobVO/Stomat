"""Тонкий Gemini-клиент: structured output, repair, классификация ошибок.

Сеть мокается httpx.MockTransport (образец test_gcal_api.py) — реальный
Google не дёргается: ДЕНЬГИ, платные вызовы только по явной команде.
"""
from __future__ import annotations

import json

import httpx
import pytest

from navbat.nlu.extractor import ExtractionError, ProviderDownError
from navbat.nlu.gemini_extractor import GeminiExtractor

VALID = {
    "intent": "book", "service": "cleaning", "doctor": None,
    "date_ref": "today", "time_ref": None, "language": "ru",
    "is_medical": False,
}


def gemini_response(payload: dict | str, prompt_tokens: int = 11,
                    out_tokens: int = 7) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": prompt_tokens,
                          "candidatesTokenCount": out_tokens},
    })


def make_extractor(handler, on_usage=None):
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = handler(request, requests)
        if isinstance(result, Exception):
            raise result
        return result

    client = httpx.Client(transport=httpx.MockTransport(recording))
    extractor = GeminiExtractor(api_key="TESTKEY", client=client,
                                on_usage=on_usage)
    return extractor, requests


def test_extract_parses_structured_response():
    usage: list[tuple[int, int]] = []
    extractor, requests = make_extractor(
        lambda req, reqs: gemini_response(VALID),
        on_usage=lambda i, o: usage.append((i, o)))

    got = extractor.extract("хочу на чистку сегодня")

    assert got.intent == "book" and got.service == "cleaning"
    request = requests[0]
    assert request.headers["x-goog-api-key"] == "TESTKEY", \
        "ключ в заголовке, не в URL — не светить в логах"
    assert ":generateContent" in str(request.url)
    body = json.loads(request.content)
    assert body["system_instruction"]["parts"][0]["text"], "промпт уходит"
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["properties"]["intent"]["enum"]
    assert usage == [(11, 7)], "токены дошли до учёта бюджета"


def test_repair_after_invalid_schema():
    def handler(request, requests):
        if len(requests) == 1:
            return gemini_response({"intent": "nonsense"})  # вне enum интентов
        return gemini_response(VALID)

    extractor, requests = make_extractor(handler)
    got = extractor.extract("чистку")

    assert got.intent == "book"
    assert len(requests) == 2
    second = json.dumps(json.loads(requests[1].content), ensure_ascii=False)
    assert "валидацию" in second, "repair-hint уходит модели"


def test_extraction_error_after_repair_exhausted():
    extractor, requests = make_extractor(
        lambda req, reqs: gemini_response("это вообще не json"))

    with pytest.raises(ExtractionError):
        extractor.extract("чистку")
    assert len(requests) == 2, "основная попытка + один repair"


def test_timeout_raises_provider_down():
    extractor, _ = make_extractor(
        lambda req, reqs: httpx.ConnectTimeout("эмуляция таймаута"))

    with pytest.raises(ProviderDownError):
        extractor.extract("чистку")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_errors_raise_provider_down(status):
    extractor, _ = make_extractor(
        lambda req, reqs: httpx.Response(status,
                                         json={"error": {"message": "boom"}}))

    with pytest.raises(ProviderDownError):
        extractor.extract("чистку")


def test_empty_candidates_raise_extraction_error():
    # safety-блок: модель жива, ответа нет — repair бессмыслен, сразу сбой NLU
    extractor, requests = make_extractor(
        lambda req, reqs: httpx.Response(200, json={"candidates": []}))

    with pytest.raises(ExtractionError):
        extractor.extract("чистку")
    assert len(requests) == 1
