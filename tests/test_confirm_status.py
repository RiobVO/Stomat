"""Статусы подтверждения визита (пакет показа, инкремент 3).

«Приду» из напоминания было пустым «ок»: пациент нажимал, а клиника об этом
не узнавала — кому звонить утром, владелец выяснял по телефону. Тап пишет
`appointment.confirm_status`, и день на экране показывает, кто подтвердил.

Кнопка `attend:<uuid>` живёт в чате до самого приёма и несёт субъект сама
(инвариант долгоживущих кнопок) — значит, субъект обязан проверяться на
месте: чужая или отменённая запись отметку не получает.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import CHAT, fsm_state
from test_gcal_export import book

OTHER_CHAT = CHAT + 1


def make_engine(app_session_factory, clinic_id) -> DialogEngine:
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=[]))


def confirmation(admin_engine, appointment_id):
    """(confirm_status, confirmed_at) записи прямым SQL — мимо приложения."""
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT confirm_status, confirmed_at FROM appointment "
                 "WHERE id = :id"), {"id": appointment_id}).one()


def escalate(admin_engine, clinic_id, chat_id=CHAT):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO conversation (clinic_id, tg_chat_id, fsm_state) "
            "VALUES (:cl, :c, 'escalated')"), {"cl": clinic_id, "c": chat_id})


def test_attend_tap_records_confirmation(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    """Тап по «Приду» — состояние записи, а не вежливый ответ в пустоту."""
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00",
                             chat_id=CHAT)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    row = confirmation(admin_engine, appointment_id)
    assert row.confirm_status == "confirmed", "подтверждение никуда не записалось"
    assert row.confirmed_at is not None, "время подтверждения не проставлено"
    assert reply.text == TEMPLATES["attend_ok"]["ru"]


def test_second_tap_keeps_the_confirmation(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    """Кнопка висит в чате до приёма — по ней жмут и дважды.

    Повторный тап не должен ни отменять отметку, ни отвечать пациенту
    «кнопка устарела»: он подтвердил то же самое, что и в первый раз."""
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00",
                             chat_id=CHAT)
    engine = make_engine(app_session_factory, clinic_a)

    engine.handle_action(CHAT, f"attend:{appointment_id}")
    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    assert confirmation(admin_engine, appointment_id).confirm_status == "confirmed"
    assert reply.text == TEMPLATES["attend_ok"]["ru"]


def test_attend_touches_only_own_appointment(app_session_factory, admin_engine,
                                             clinic_a, doctor_a,
                                             service_cleaning):
    """callback_data приходит от клиента: чужой id внутри той же клиники
    отмечал бы чужую запись «придёт» — RLS изолирует клиники, но не
    пациентов между собой. Владелец получил бы ложное «подтвердил»."""
    victim_id, _ = book(app_session_factory, clinic_a, doctor_a,
                        service_cleaning, next_monday(), "11:00",
                        chat_id=OTHER_CHAT)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"attend:{victim_id}")

    assert confirmation(admin_engine, victim_id).confirm_status is None, \
        "подтверждена чужая запись"
    assert reply.text == TEMPLATES["stale_button"]["ru"]


def test_attend_on_cancelled_appointment_confirms_nothing(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Пациент отменил запись, а старое напоминание с «Приду» осталось в
    чате: отменённому приёму подтверждать нечего, иначе он всплывёт в дне
    владельца как живой."""
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, next_monday(), "09:00",
                                 chat_id=CHAT)
    sched.cancel(appointment_id)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    row = confirmation(admin_engine, appointment_id)
    assert row.confirm_status is None and row.confirmed_at is None
    assert reply.text == TEMPLATES["stale_button"]["ru"]


def test_broken_appointment_id_does_not_crash(app_session_factory,
                                              admin_engine, clinic_a,
                                              doctor_a, service_cleaning):
    """id записи приходит из callback_data: мусор падал бы в приведении типа
    и уводил сообщение пациента в dead letter (тот же урок, что у
    remind_cancel)."""
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, "attend:не-uuid")

    assert reply.text == TEMPLATES["stale_button"]["ru"]
    assert fsm_state(admin_engine) == "idle"


def test_attend_in_escalated_still_records(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    """Пациент позвал администратора, потом пришло напоминание (живой тест
    12.06). Для пациента ничего не меняется — «ждём вас» и заморозка на
    месте, — но подтверждение владельцу нужно и отсюда."""
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, next_monday(), "09:00",
                             chat_id=CHAT)
    escalate(admin_engine, clinic_a)
    engine = make_engine(app_session_factory, clinic_a)

    reply = engine.handle_action(CHAT, f"attend:{appointment_id}")

    assert reply.text == TEMPLATES["attend_ok"]["ru"]
    assert confirmation(admin_engine, appointment_id).confirm_status == "confirmed"
    assert fsm_state(admin_engine) == "escalated", \
        "заморозка не снимается — диалог по-прежнему у администратора"
