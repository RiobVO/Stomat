"""Кнопка «👤 Позвать администратора» в фоллбэках (полировка-2, задача А).

Нажатие = ровно путь mentions_human_request: заморозка + алерт админу.
Кнопка появляется ТОЛЬКО в фоллбэк-ответах («не понял» после 2-го сбоя NLU
и вопрос вне компетенции), НЕ в постоянном меню: осознанное отступление
от «0 эскалаций» — потерянному пациенту нужен явный выход к человеку.
"""
from __future__ import annotations

from conftest import at_tashkent, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, fsm_state, slot_buttons)
from test_faq_layer import saved_questions


def make(app_session_factory, clinic_id, script, clock=None):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier, clock=clock)
    return engine, notifier


def call_admin_buttons(reply):
    return [b for b in reply.buttons if b.action == "call_admin"]


# ── Нажатие кнопки = путь «позовите администратора» ──────────────────────────

def test_call_admin_action_escalates_with_alert(app_session_factory,
                                                admin_engine, clinic_a,
                                                doctor_a):
    # script пуст: NLU не дёргается — callback идёт мимо экстрактора
    clock = lambda: at_tashkent(next_monday(), "10:00")  # рабочее окно
    engine, notifier = make(app_session_factory, clinic_a, [], clock=clock)
    reply = engine.handle_action(CHAT, "call_admin")

    assert fsm_state(admin_engine) == "escalated"
    assert notifier.calls == [(CHAT, "пациент просит администратора")]
    assert reply.text == TEMPLATES["escalated"]["ru"]


def test_call_admin_out_of_hours_says_morning(app_session_factory,
                                              admin_engine, clinic_a,
                                              doctor_a):
    clock = lambda: at_tashkent(next_monday(), "22:00")  # клиника закрыта
    engine, notifier = make(app_session_factory, clinic_a, [], clock=clock)
    reply = engine.handle_action(CHAT, "call_admin")

    assert fsm_state(admin_engine) == "escalated"
    assert reply.text == TEMPLATES["escalated_closed"]["ru"]


def test_second_click_does_not_repeat_alert(app_session_factory, admin_engine,
                                            clinic_a, doctor_a):
    clock = lambda: at_tashkent(next_monday(), "10:00")
    engine, notifier = make(app_session_factory, clinic_a, [], clock=clock)
    engine.handle_action(CHAT, "call_admin")
    second = engine.handle_action(CHAT, "call_admin")

    assert len(notifier.calls) == 1, "повторный клик алерт не дублирует"
    assert second.text == TEMPLATES["escalated"]["ru"]
    assert fsm_state(admin_engine) == "escalated"


# ── Где кнопка появляется ────────────────────────────────────────────────────

def test_second_nlu_failure_offers_call_admin_button(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [ExtractionError("1"), ExtractionError("2")])
    engine.handle_action(CHAT, "lang:ru")  # greeting показан

    first = engine.handle_text(CHAT, "абракадабра")
    assert not call_admin_buttons(first), "1-й сбой — мягкий reask без кнопки"
    assert first.menu, "reask предлагает меню самообслуживания"

    second = engine.handle_text(CHAT, "опять абракадабра")
    assert call_admin_buttons(second), "2-й сбой — явный выход к человеку"
    assert not second.menu, "кнопка заменяет menu: reply_markup один"
    assert notifier.calls == []
    assert fsm_state(admin_engine) != "escalated"


def test_unanswerable_question_offers_button_and_saves_question(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    engine.handle_action(CHAT, "lang:ru")
    reply = engine.handle_text(CHAT, "вы принимаете карты?")

    assert call_admin_buttons(reply)
    assert not reply.menu
    assert notifier.calls == []
    assert saved_questions(admin_engine) == ["вы принимаете карты?"], \
        "копилка вопросов для дайджеста работает по-прежнему"


def test_mid_booking_second_failure_repeats_step_buttons(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    # посреди оформления шаг важнее кнопки: _with_reprompt отдаёт кнопки
    # шага, выживание call_admin не требуется
    day = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day)),
         ExtractionError("1"), ExtractionError("2")],
        clock=lambda: at_tashkent(day, "10:00"))
    engine.handle_text(CHAT, "чистка в понедельник")  # слоты показаны

    engine.handle_text(CHAT, "абракадабра")
    reply = engine.handle_text(CHAT, "опять абракадабра")

    assert slot_buttons(reply), "повтор шага: кнопки слотов в ответе"
    assert notifier.calls == []
    assert fsm_state(admin_engine) == "booking_offer_slots"
