"""Вопрос о наличии → выбор дня, но только по явным сигналам (П-1).

«а больше слотов нету?», «а ещё?», «другой день?» раньше уходили в
«вопрос вне компетенции» и дёргали админа по пустяку (находка живого
теста 10.06). Пересмотр 11.06 (живой тест пользователя): контекст-правило
«любой текст посреди сценария = про наличие» сужено — мусор («уыкп») и
«привет» прыгали на выбор дня с дефолтным осмотром. Теперь посреди
сценария — ТОЛЬКО словарный маркер (даты/время прикрывает бэкстоп
booking_like); прочий непонятый текст повторяет ТЕКУЩИЙ шаг машинерией
сбоев NLU. Вне сценария словарь и ветка ctx.service/ctx.date — как раньше.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import make_service, next_monday
from navbat.dialog.conversation import Conversation, DialogContext
from navbat.dialog.dialog_common import mentions_availability
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, fsm_state, slot_buttons)
from test_dialog_reschedule_cancel import book_directly
from test_faq_layer import saved_questions


def date_actions(reply):
    return [b.action for b in reply.buttons if b.action.startswith("date:")]


def service_actions(reply):
    return [b.action for b in reply.buttons if b.action.startswith("service:")]


def ctx_service(admin_engine, chat_id=CHAT) -> str | None:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT context ->> 'service' FROM conversation "
                 "WHERE tg_chat_id = :c"), {"c": chat_id}).scalar_one()


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


# ── Посреди сценария: ТОЛЬКО словарный маркер (пересмотр 11.06) ──────────────

def test_followup_without_markers_mid_slots_repeats_step(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # пересмотр 11.06: раньше ЛЮБОЙ текст после показа слотов прыгал на выбор
    # дня; теперь без словарного маркера — повтор текущего шага (те же слоты)
    day = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
        extr(intent="question"),
    ])
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    assert slot_buttons(offer)  # слоты показаны, контекст активен

    reply = engine.handle_text(CHAT, "а ничего получше нет?")  # ноль маркеров
    assert not date_actions(reply), "прыжка на выбор дня больше нет"
    assert slot_buttons(reply), "повтор текущего шага: слоты снова в ответе"
    assert fsm_state(admin_engine) == "booking_offer_slots"
    assert notifier.calls == []


def test_garbage_on_service_step_repeats_service_question(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # живой баг 11.06: «уыкп» на шаге УСЛУГИ прыгал на выбор дня с дефолтным
    # осмотром — теперь повтор вопроса услуги, услуга не подменяется
    make_service(admin_engine, clinic_a, "checkup", 30)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="other")])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])  # шаг услуги

    reply = engine.handle_text(CHAT, "уыкп")
    assert not date_actions(reply), "мусор — не вопрос о наличии"
    assert service_actions(reply), "повтор шага: кнопки услуг"
    assert ctx_service(admin_engine) is None, "дефолтный осмотр не подставлен"
    assert fsm_state(admin_engine) == "booking_collect"
    assert notifier.calls == []
    assert saved_questions(admin_engine) == [], \
        "мусор посреди сценария — не вопрос владельцу, копилка пуста"


def test_second_garbage_mid_scenario_adds_call_admin_button(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # 2-й непонятый текст подряд — «не понял» + явный выход к человеку,
    # шаг услуги повторяется (машинерия nlu_failures, как в фоллбэках)
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="other"), extr(intent="other")])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])

    engine.handle_text(CHAT, "уыкп")
    reply = engine.handle_text(CHAT, "ыфвап")
    actions = [b.action for b in reply.buttons]
    assert "call_admin" in actions, "2-й сбой — кнопка к человеку"
    assert service_actions(reply), "шаг услуги повторён"
    assert fsm_state(admin_engine) == "booking_collect", "не эскалация"
    assert notifier.calls == []


def test_greeting_with_service_set_repeats_day_step(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # живой баг 11.06: «ПРИВЕТ» в зависшем booking_collect получал «на какой
    # день?» как availability-прыжок; теперь это честный повтор текущего шага
    # (шаг — дата) через reask, услуга не трогается
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(service="cleaning"), extr(intent="other")])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, "хочу чистку")  # услуга есть, шаг — день

    reply = engine.handle_text(CHAT, "ПРИВЕТ")
    assert TEMPLATES["reask"]["ru"] in reply.text, "непонятое = сбой, не вопрос"
    assert TEMPLATES["ask_date"]["ru"] in reply.text, "текущий шаг повторён"
    assert ctx_service(admin_engine) == "cleaning", "услуга не подменена"
    assert notifier.calls == []


def test_dictionary_marker_mid_slots_still_offers_dates(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # П-1 жив для явных маркеров: словарное «а ещё?» посреди показа слотов —
    # по-прежнему выбор дня
    day = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
        extr(intent="question"),
    ])
    engine.handle_text(CHAT, "чистка в понедельник")

    reply = engine.handle_text(CHAT, "а ещё?")
    assert len(date_actions(reply)) == 3
    assert notifier.calls == []


def test_context_branch_outside_scenario_still_availability(
        app_session_factory, clinic_a):
    # ветка ctx.service/ctx.date ВНЕ сценария жива: доуточнение, когда услуга
    # ещё в контексте, а state уже не сценарный — трактуем как «а ещё?»;
    # посреди сценария тот же текст без маркера — уже НЕ наличие
    engine, _ = make(app_session_factory, clinic_a, [])
    no_markers = "ну а ближе?"
    idle = Conversation(chat_id=CHAT, state="idle",
                        context=DialogContext(service="cleaning"))
    assert engine._asks_availability(idle, no_markers)
    mid = Conversation(chat_id=CHAT, state="booking_collect",
                       context=DialogContext(service="cleaning"))
    assert not engine._asks_availability(mid, no_markers)


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


def test_unrelated_question_not_availability(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # вне контекста и без маркеров — это НЕ вопрос о наличии: «не понял»
    # + кнопка к человеку (полировка-2), кнопок дат нет, админа не дёргаем
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "вы принимаете карты?")

    assert not date_actions(reply)
    assert [b.action for b in reply.buttons] == ["call_admin"]
    assert notifier.calls == []
