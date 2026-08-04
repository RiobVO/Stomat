"""Вопрос о наличии в любой формулировке → выбор дня, не эскалация (П-1).

«а больше слотов нету?», «а ещё?», «другой день?» раньше уходили в
«вопрос вне компетенции» и дёргали админа по пустяку (находка живого
теста 10.06). Теперь: активный контекст предложения слотов ИЛИ словарь
наличия → кнопки выбора дня (тот же путь, что «Другое время»).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import make_service, next_monday
from navbat.dialog.dialog_common import mentions_availability
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, fsm_state, slot_buttons)
from test_dialog_reschedule_cancel import book_directly


def date_actions(reply):
    return [b.action for b in reply.buttons if b.action.startswith("date:")]


def make(app_session_factory, clinic_id, script):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier)
    return engine, notifier


# ── Чистый детектор (без БД) ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "а больше слотов нету?",
    "а ещё?",
    "а еще есть?",
    "другой день можно?",
    "есть свободные окошки?",
    "а другие варианты есть?",
    "можно попозже?",
    "а пораньше?",
    "есть места на неделе?",
    "boshqa kun bormi?",
    "yana bormi?",
    "bo'sh joy bormi?",
    "boʻsh vaqtlar qachon?",   # апостроф-модификатор U+02BB
    "bo’sh joylar?",           # типографский апостроф U+2019
])
def test_detector_positive(text):
    assert mentions_availability(text)


@pytest.mark.parametrize("text", [
    "запишите меня на чистку",
    "сколько стоит имплант?",
    "вы принимаете карты?",
    "болит зуб, что делать?",
    "мой друг посоветовал вас",   # «друг» (человек) ≠ «другой»
    "Гульнора Каримова",
    "salom",
    "ertaga keladi",
])
def test_detector_negative(text):
    assert not mentions_availability(text)


# ── Словарный триггер: idle, никакого контекста ──────────────────────────────

def test_availability_phrase_idle_offers_dates_no_escalation(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "а больше слотов нету?")

    assert len(date_actions(reply)) == 3  # Сегодня/Завтра/Послезавтра
    assert notifier.calls == []
    assert fsm_state(admin_engine) == "booking_collect"


def test_availability_other_intent_also_offers_dates(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="other")])
    reply = engine.handle_text(CHAT, "а ещё?")

    assert len(date_actions(reply)) == 3
    assert notifier.calls == []


def test_picked_date_after_availability_shows_checkup_slots(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # без услуги сетка после выбора дня считается по осмотру — конец пути
    # не тупик: пациент получает реальные слоты
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    engine.handle_text(CHAT, "есть свободные окошки?")
    reply = engine.handle_action(CHAT, f"date:{next_monday().isoformat()}")

    assert slot_buttons(reply)
    assert notifier.calls == []


# ── Контекстный триггер: после показа слотов ЛЮБОЙ вопрос — о наличии ────────

def test_followup_without_markers_after_slots_offers_dates(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
        extr(intent="question"),
    ])
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    assert slot_buttons(offer)  # слоты показаны, контекст активен

    reply = engine.handle_text(CHAT, "а ничего получше нет?")  # ноль маркеров
    assert len(date_actions(reply)) == 3
    assert notifier.calls == []


def test_resched_availability_stays_in_resched(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine, notifier = make(app_session_factory, clinic_a, [
        extr(intent="reschedule", date_ref=explicit(monday + timedelta(days=1))),
        extr(intent="question"),
    ])
    engine.handle_text(CHAT, "перенесите на вторник")
    assert fsm_state(admin_engine) == "resched_offer_slots"

    reply = engine.handle_text(CHAT, "а ещё варианты есть?")
    assert len(date_actions(reply)) == 3
    assert fsm_state(admin_engine) == "resched_offer_slots"  # не выпали из переноса
    assert notifier.calls == []


# ── Не задеваем соседние пути ────────────────────────────────────────────────

def test_price_question_with_marker_still_prices(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=200_000)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question", service="cleaning")])
    reply = engine.handle_text(CHAT, "а чистка ещё делается? сколько стоит?")

    assert "200 000" in reply.text
    assert not date_actions(reply)
    assert notifier.calls == []


def test_unrelated_question_keeps_current_path(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # вне контекста и без маркеров — штатный путь «вне компетенции»
    # (П-2а заменит его на «не понял» + меню, этот тест тогда перепишется)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "вы принимаете карты?")

    assert not date_actions(reply)
    assert notifier.calls  # пока — алерт админу, как раньше
