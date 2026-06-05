"""Защита кошелька и PII: маскировка телефонов, дневной token cap.

Имена пациентов в LLM не уходят: шаг имени дёргает NLU только для
вопросоподобного текста (эвристика FSM).
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from navbat.nlu.wrappers import (
    BudgetedExtractor,
    BudgetExceededError,
    DeidentifyingExtractor,
    UsageRecorder,
)
from test_dialog_booking import CHAT, RecordingNotifier, explicit, extr, fsm_state, slot_buttons


class RecordingInner:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def extract(self, message: str):
        self.texts.append(message)
        return extr()


# ── Деидентификация ──────────────────────────────────────────────────────────

def test_phone_numbers_are_masked_before_llm():
    inner = RecordingInner()
    extractor = DeidentifyingExtractor(inner)
    extractor.extract("перезвоните на +998 90 123-45-67 или 901234567")
    sent = inner.texts[0]
    assert "123" not in sent and "901234567" not in sent
    assert sent.count("[phone]") == 2


def test_times_and_dates_are_not_masked():
    inner = RecordingInner()
    DeidentifyingExtractor(inner).extract("запишите на 20.06 в 15:00, кабинет 12")
    assert inner.texts[0] == "запишите на 20.06 в 15:00, кабинет 12"


# ── Учёт токенов и cap ───────────────────────────────────────────────────────

def usage_row(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT requests, in_tokens, out_tokens FROM llm_usage"
        )).one_or_none()


def test_usage_is_accumulated(app_session_factory, admin_engine, clinic_a):
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=1_000_000)
    recorder.record(100, 50)
    recorder.record(200, 80)
    row = usage_row(admin_engine)
    assert (row.requests, row.in_tokens, row.out_tokens) == (2, 300, 130)


def test_cap_blocks_llm_with_single_alert(app_session_factory, admin_engine,
                                          clinic_a):
    notifier = RecordingNotifier()
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=100,
                             notifier=notifier)
    recorder.record(90, 20)  # 110 > cap

    inner = RecordingInner()
    extractor = BudgetedExtractor(inner, recorder)
    for _ in range(3):
        try:
            extractor.extract("hammasi qancha turadi")
        except BudgetExceededError:
            pass
    assert inner.texts == [], "LLM не дёргается после превышения"
    assert len(notifier.calls) == 1, "алерт админу — один раз"


def test_under_cap_passes_through(app_session_factory, clinic_a):
    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=1_000_000)
    inner = RecordingInner()
    assert BudgetedExtractor(inner, recorder).extract("salom").intent == "book"
    assert len(inner.texts) == 1


def test_budget_error_is_extraction_error():
    # FSM-путь: reask → эскалация — бот гаснет мягко, не падает
    assert issubclass(BudgetExceededError, ExtractionError)


# ── Шаг имени: PII не уходит в LLM ───────────────────────────────────────────

def test_plain_name_does_not_hit_nlu(app_session_factory, admin_engine, clinic_a,
                                     doctor_a, service_cleaning):
    # script содержит ТОЛЬКО booking: если бы шаг имени дёргал NLU,
    # FakeExtractor бросил бы ExtractionError и FSM ушёл бы в reask
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(
        script=[extr(service="cleaning", date_ref=explicit(next_monday()))]))
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    engine.handle_text(CHAT, "Алишер")
    assert fsm_state(admin_engine) == "awaiting_phone", "имя принято без NLU"
