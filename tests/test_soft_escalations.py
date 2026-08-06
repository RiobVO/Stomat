"""Система минимальных обращений к админу (П-2а).

Эскалация (заморозка + алерт) — ровно два пациентских пути:
прямая просьба позвать человека и двойной сбой подтверждения записи.
«Вопрос вне компетенции» и кривые ответы NLU больше не дёргают админа:
пациент получает «не понял» + меню / повтор шага кнопками.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog.dialog_common import mentions_human_request
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.patients import create_patient
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, fsm_state, slot_buttons)


def make(app_session_factory, clinic_id, script, clock=None, scheduler=None):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier, scheduler=scheduler, clock=clock)
    return engine, notifier


def context_json(admin_engine, chat_id=CHAT) -> dict:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT context FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id}).scalar_one()


# ── Чистый детектор просьбы человека (без БД) ────────────────────────────────

@pytest.mark.parametrize("text_", [
    "позовите администратора",
    "дайте оператора пожалуйста",
    "соедините с менеджером",
    "нужен живой человек",
    "можно поговорить с человеком?",
    "administratorni chaqiring",
    "operator kerak",
    "menejer bilan gaplashmoqchiman",
])
def test_human_request_positive(text_):
    assert mentions_human_request(text_)


@pytest.mark.parametrize("text_", [
    "запишите двух человек на чистку",   # «человек»-количество ≠ просьба
    "ikki odamga yozib qo'ying",
    "запишите на чистку завтра",
    "сколько стоит имплант?",
    "болит зуб",
    "Гульнора Каримова",
])
def test_human_request_negative(text_):
    assert not mentions_human_request(text_)


# ── Эскалация по просьбе: единственный текстовый путь ────────────────────────

def test_human_request_escalates_without_nlu(app_session_factory, admin_engine,
                                             clinic_a, doctor_a):
    # script пуст: вызов экстрактора упал бы — просьба человека ловится ДО NLU
    clock = lambda: at_tashkent(next_monday(), "10:00")  # рабочее окно
    engine, notifier = make(app_session_factory, clinic_a, [], clock=clock)
    reply = engine.handle_text(CHAT, "позовите администратора")

    assert fsm_state(admin_engine) == "escalated"
    assert notifier.calls == [(CHAT, "пациент просит администратора")]
    # первый контакт оборачивается greeting'ом — проверяем вхождение
    assert TEMPLATES["escalated"]["ru"] in reply.text


def test_human_request_out_of_hours_says_morning(app_session_factory,
                                                 admin_engine, clinic_a, doctor_a):
    clock = lambda: at_tashkent(next_monday(), "22:00")  # клиника закрыта
    engine, notifier = make(app_session_factory, clinic_a, [], clock=clock)
    reply = engine.handle_text(CHAT, "позовите администратора")

    assert fsm_state(admin_engine) == "escalated"
    assert "утром" in reply.text


def test_human_request_on_phone_step_releases_hold(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day))],
        clock=lambda: at_tashkent(day, "10:00"))
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)  # hold взят
    engine.handle_text(CHAT, "Гульнора")  # имя → awaiting_phone

    engine.handle_text(CHAT, "не хочу делиться номером, позовите администратора")
    assert fsm_state(admin_engine) == "escalated"
    assert notifier.calls == [(CHAT, "пациент просит администратора")]
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM appointment")).scalar_one()
    assert status == "cancelled", "висящий hold отпущен при эскалации"


def test_human_request_words_on_name_step_become_name(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # на шаге имени детектор выключен: любой текст может быть именем
    day = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day))],
        clock=lambda: at_tashkent(day, "10:00"))
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    engine.handle_text(CHAT, "Оператор Умаров")  # странное имя, но имя
    assert fsm_state(admin_engine) == "awaiting_phone"
    assert notifier.calls == []


# ── «Не понял» + меню вместо «вне компетенции» ───────────────────────────────

def test_unanswerable_question_not_understood_no_alert(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "вы принимаете карты?")

    assert notifier.calls == []
    assert fsm_state(admin_engine) != "escalated"
    # полировка-2: вместо menu — кнопка явного выхода к человеку
    assert [b.action for b in reply.buttons] == ["call_admin"]
    assert "администратора" in reply.text, "подсказка пути к человеку"


# ── Кривые ответы NLU: никогда не эскалируют ─────────────────────────────────

def test_repeated_nlu_failures_never_escalate(app_session_factory, admin_engine,
                                              clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [ExtractionError("1"), ExtractionError("2"),
                             ExtractionError("3")])
    engine.handle_text(CHAT, "абракадабра")
    second = engine.handle_text(CHAT, "опять абракадабра")
    third = engine.handle_text(CHAT, "и снова")

    assert notifier.calls == []
    assert fsm_state(admin_engine) != "escalated"
    # 2-й и 3-й сбой — кнопка «позвать администратора» (полировка-2)
    assert [b.action for b in second.buttons] == ["call_admin"]
    assert [b.action for b in third.buttons] == ["call_admin"]


def test_nlu_failures_mid_booking_repeat_step_buttons(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # LLM лёг посреди оформления: бот повторяет шаг кнопками — кнопочный
    # путь не требует NLU, эскалация не нужна
    day = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day)),
         ExtractionError("1"), ExtractionError("2")],
        clock=lambda: at_tashkent(day, "10:00"))
    engine.handle_text(CHAT, "чистка в понедельник")  # слоты показаны

    engine.handle_text(CHAT, "абракадабра")
    reply = engine.handle_text(CHAT, "опять абракадабра")

    assert notifier.calls == []
    assert fsm_state(admin_engine) == "booking_offer_slots"
    assert slot_buttons(reply), "повтор шага: кнопки слотов в ответе"


def test_menu_press_resets_failure_counter(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    engine, notifier = make(app_session_factory, clinic_a,
                            [ExtractionError("1")])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, "абракадабра")
    assert context_json(admin_engine).get("nlu_failures") == 1

    engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])  # кнопка меню
    assert not context_json(admin_engine).get("nlu_failures"), \
        "меню = пациент сориентировался, счётчик сброшен"


# ── Сбой confirm: retry, затем эскалация ─────────────────────────────────────

class FailingConfirmScheduler:
    """Обёртка над живым SchedulingEngine: confirm падает первые N раз."""

    def __init__(self, inner, failures: int) -> None:
        self._inner = inner
        self.failures = failures

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def confirm(self, appointment_id) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("БД моргнула")
        self._inner.confirm(appointment_id)


def _known_patient(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        create_patient(session, CHAT, "Гульнора", "998901112233")


def booked_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM appointment WHERE status = 'booked'"
        )).scalar_one()


def test_confirm_failure_once_reoffers_slots(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    _known_patient(app_session_factory, clinic_a)
    day = next_monday()
    sched = FailingConfirmScheduler(
        SchedulingEngine(app_session_factory, clinic_a), failures=1)
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day))],
        clock=lambda: at_tashkent(day, "08:00"), scheduler=sched)

    offer = engine.handle_text(CHAT, "чистка в понедельник")
    retry = engine.handle_action(CHAT, slot_buttons(offer)[0].action)  # сбой №1

    assert notifier.calls == []
    assert fsm_state(admin_engine) == "booking_offer_slots"
    assert slot_buttons(retry), "слоты предложены заново"
    assert booked_count(admin_engine) == 0  # hold отпущен, брони нет

    done = engine.handle_action(CHAT, slot_buttons(retry)[0].action)  # успех
    assert booked_count(admin_engine) == 1
    assert fsm_state(admin_engine) == "idle"


def test_confirm_failure_twice_escalates(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    _known_patient(app_session_factory, clinic_a)
    day = next_monday()
    sched = FailingConfirmScheduler(
        SchedulingEngine(app_session_factory, clinic_a), failures=2)
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day))],
        clock=lambda: at_tashkent(day, "08:00"), scheduler=sched)

    offer = engine.handle_text(CHAT, "чистка в понедельник")
    retry = engine.handle_action(CHAT, slot_buttons(offer)[0].action)  # сбой №1
    engine.handle_action(CHAT, slot_buttons(retry)[0].action)          # сбой №2

    assert fsm_state(admin_engine) == "escalated"
    assert notifier.calls == [(CHAT, "сбой подтверждения записи")]
    assert booked_count(admin_engine) == 0
