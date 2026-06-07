"""Метрика NLU-дрифта: доля сбоев/repair в день + алерт админу (Ф1.5, 14.B).

Дрифт = деградация модели/промпта (ExtractionError). Бюджет
(BudgetExceededError) и аутэйдж провайдера (ProviderDownError) — не дрифт.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from navbat.nlu.extractor import ExtractionError, ProviderDownError
from navbat.nlu.gemini_extractor import GeminiExtractor
from navbat.nlu.wrappers import (
    BudgetExceededError,
    DriftTrackingExtractor,
    UsageRecorder,
)
from test_dialog_booking import RecordingNotifier, extr
from test_gemini_extractor import VALID, gemini_response

import httpx
import json


def drift_row(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT requests, failures, repairs FROM llm_usage")).one_or_none()


class StubInner:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    def extract(self, message: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return extr()


# ── Счётчики в llm_usage ─────────────────────────────────────────────────────

def test_failure_and_repair_counters(app_session_factory, admin_engine, clinic_a):
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9)
    recorder.record_failure()
    recorder.record_failure()
    recorder.record_repair()

    row = drift_row(admin_engine)
    assert (row.requests, row.failures, row.repairs) == (0, 2, 1)


def test_drift_wrapper_counts_only_extraction_errors(app_session_factory,
                                                     admin_engine, clinic_a):
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9)

    with pytest.raises(ExtractionError):
        DriftTrackingExtractor(StubInner(ExtractionError("мусор")),
                               recorder).extract("текст")
    assert drift_row(admin_engine).failures == 1

    DriftTrackingExtractor(StubInner(), recorder).extract("текст")  # успех
    with pytest.raises(ProviderDownError):
        DriftTrackingExtractor(StubInner(ProviderDownError("аутэйдж")),
                               recorder).extract("текст")
    with pytest.raises(BudgetExceededError):
        DriftTrackingExtractor(StubInner(BudgetExceededError("cap")),
                               recorder).extract("текст")
    assert drift_row(admin_engine).failures == 1, \
        "успех/аутэйдж/бюджет дрифтом не считаются"


# ── Алерт при росте доли сбоев ───────────────────────────────────────────────

def seed_requests(recorder: UsageRecorder, n: int) -> None:
    for _ in range(n):
        recorder.record(10, 5)


def fail_once(recorder: UsageRecorder) -> None:
    recorder.record_failure()
    recorder.maybe_alert_drift()


def test_drift_alert_fires_once_above_threshold(app_session_factory, clinic_a):
    notifier = RecordingNotifier()
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9,
                             notifier=notifier)
    seed_requests(recorder, 20)

    for _ in range(4):  # 4/20 = 20% — НЕ выше порога 0.2
        fail_once(recorder)
    assert notifier.calls == []

    fail_once(recorder)  # 5/20 = 25% > 20%
    assert len(notifier.calls) == 1
    assert "дрифт" in notifier.calls[0][1].lower()

    fail_once(recorder)  # рост продолжается — второго алерта в тот же день нет
    assert len(notifier.calls) == 1


def test_drift_alert_needs_min_requests(app_session_factory, clinic_a):
    # 5 сбоев из 5 запросов = 100%, но статистика мизерная — не алертим
    notifier = RecordingNotifier()
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9,
                             notifier=notifier)
    seed_requests(recorder, 5)
    for _ in range(5):
        fail_once(recorder)
    assert notifier.calls == []


def test_drift_threshold_is_configurable(app_session_factory, clinic_a):
    notifier = RecordingNotifier()
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9,
                             notifier=notifier, drift_threshold=0.5)
    seed_requests(recorder, 20)
    for _ in range(6):  # 6/20 = 30% < 50%
        fail_once(recorder)
    assert notifier.calls == []


# ── Канал repair из Gemini-экстрактора ───────────────────────────────────────

def test_gemini_on_repair_called(app_session_factory, clinic_a):
    repairs: list[int] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return gemini_response({"intent": "nonsense"})  # вне enum → repair
        return gemini_response(VALID)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = GeminiExtractor(api_key="TESTKEY", client=client,
                                on_repair=lambda: repairs.append(1))
    got = extractor.extract("чистку")

    assert got.intent == "book"
    assert repairs == [1], "ровно один repair учтён"
