"""FallbackExtractor: аутэйдж основного LLM → запасной (Ф1.5, BRIEF разд. 14.B).

ExtractionError (кривой JSON) — НЕ повод для failover: модель жива, запасной
не спасёт, а латентность удвоится. Оба провайдера легли — ProviderDownError
наружу, апдейт ретраит очередь сообщений (текущее поведение dead letter).
"""
from __future__ import annotations

import pytest

from navbat.nlu.extractor import ExtractionError, ProviderDownError
from navbat.nlu.fallback import FallbackExtractor
from navbat.nlu.schema import Extraction


def extraction(**kwargs) -> Extraction:
    base = dict(intent="book", service=None, doctor=None, date_ref=None,
                time_ref=None, language="ru", is_medical=False)
    return Extraction(**{**base, **kwargs})


class StubExtractor:
    def __init__(self, result: Extraction | None = None,
                 error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def extract(self, text: str) -> Extraction:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return self._result


def test_primary_ok_secondary_untouched():
    primary = StubExtractor(result=extraction(service="cleaning"))
    secondary = StubExtractor(result=extraction())

    got = FallbackExtractor(primary, secondary).extract("хочу на чистку")

    assert got.service == "cleaning"
    assert secondary.calls == []


def test_provider_down_switches_to_secondary():
    primary = StubExtractor(error=ProviderDownError("аутэйдж"))
    secondary = StubExtractor(result=extraction(service="filling"))

    got = FallbackExtractor(primary, secondary).extract("пломбу поставить")

    assert got.service == "filling"
    assert secondary.calls == ["пломбу поставить"], \
        "запасной получил исходный текст"


def test_extraction_error_does_not_failover():
    primary = StubExtractor(error=ExtractionError("кривой JSON"))
    secondary = StubExtractor(result=extraction())

    with pytest.raises(ExtractionError):
        FallbackExtractor(primary, secondary).extract("абракадабра")
    assert secondary.calls == []


def test_both_down_raises_provider_down():
    primary = StubExtractor(error=ProviderDownError("раз"))
    secondary = StubExtractor(error=ProviderDownError("два"))

    with pytest.raises(ProviderDownError):
        FallbackExtractor(primary, secondary).extract("текст")
