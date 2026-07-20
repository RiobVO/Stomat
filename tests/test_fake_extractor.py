"""FakeExtractor: фикстуры спайка (messages.jsonl) + scripted-очередь.

Все тесты диалога живут на нём — ноль вызовов OpenAI API.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from navbat.nlu.extractor import ExtractionError, FakeExtractor
from navbat.nlu.schema import Extraction

FIXTURES = Path(__file__).parent.parent / "spike_nlu" / "data" / "messages.jsonl"


@pytest.fixture(scope="module")
def fixture_extractor() -> FakeExtractor:
    return FakeExtractor.from_fixtures(FIXTURES)


# ── Схема ────────────────────────────────────────────────────────────────────

def test_schema_rejects_unknown_intent():
    with pytest.raises(ValidationError):
        Extraction(intent="greeting", service=None, doctor=None,
                   date_ref=None, time_ref=None, language="ru", is_medical=False)


def test_schema_rejects_bad_date_ref():
    with pytest.raises(ValidationError):
        Extraction(intent="book", service=None, doctor=None,
                   date_ref="someday", time_ref=None, language="ru", is_medical=False)


def test_schema_rejects_bad_time_ref():
    with pytest.raises(ValidationError):
        Extraction(intent="book", service=None, doctor=None,
                   date_ref=None, time_ref="25:99", language="ru", is_medical=False)


# ── Фикстуры спайка ──────────────────────────────────────────────────────────

def test_fixtures_load_all(fixture_extractor):
    assert len(fixture_extractor) == 410


def test_lookup_real_message(fixture_extractor):
    # u_020: реальное узбекское сообщение, голд после правки конвенции
    got = fixture_extractor.extract("tez yordam kerak tish juda ogriyapti")
    assert got.intent == "book"
    assert got.service == "checkup"
    assert got.date_ref == "today"
    assert got.is_medical is True


def test_lookup_normalizes_case_and_whitespace(fixture_extractor):
    got = fixture_extractor.extract("  TEZ yordam kerak tish juda ogriyapti ")
    assert got.intent == "book"


def test_unknown_text_raises(fixture_extractor):
    with pytest.raises(ExtractionError):
        fixture_extractor.extract("текста нет в фикстурах спайка")


# ── Scripted-очередь ─────────────────────────────────────────────────────────

def test_scripted_queue_in_order():
    first = Extraction(intent="book", service="cleaning", doctor=None,
                       date_ref="tomorrow", time_ref=None, language="ru",
                       is_medical=False)
    second = Extraction(intent="question", service=None, doctor=None,
                        date_ref=None, time_ref=None, language="ru",
                        is_medical=False)
    extractor = FakeExtractor(script=[first, second])
    assert extractor.extract("любой текст").intent == "book"
    assert extractor.extract("любой текст").intent == "question"


def test_scripted_error_raises():
    extractor = FakeExtractor(script=[ExtractionError("кривой JSON")])
    with pytest.raises(ExtractionError):
        extractor.extract("что угодно")


def test_empty_script_and_no_fixtures_raises():
    with pytest.raises(ExtractionError):
        FakeExtractor().extract("что угодно")
