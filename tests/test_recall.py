"""Recall: возврат пациентов через N месяцев после визита (инкремент 4).

Пациент, которому не напомнили, не возвращается сам: чистка раз в полгода —
деньги клиники, о которых никто не спрашивает. Рассылка идёт reconciliation'ом
из цикла напоминаний (состояние в БД, переживает рестарт), одна отправка на
приём (UNIQUE в recall_outreach), приглашение несёт исходную запись в сырой
кнопке — фоновый поток не трогает conversation пациента.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.dialog.replies import service_label, t
from navbat.reminders import ReminderService
from test_dialog_booking import CHAT, RecordingNotifier
from test_tg_worker import FakeTelegramAPI

TASHKENT = ZoneInfo("Asia/Tashkent")


def make_recall_service(app_session_factory, clinic_id):
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=RecordingNotifier())
    return service, api


def at_local(hour: int) -> datetime:
    """«Сегодня в HH:00» по часам клиники: окно рассылки живёт по её локали,
    а не по часам сервера."""
    return datetime.now(TASHKENT).replace(hour=hour, minute=0, second=0,
                                          microsecond=0)


def set_recall(admin_engine, service_id, months: int | None) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE service SET recall_months = :m WHERE id = :i"),
            {"m": months, "i": service_id})


def seed_past(admin_engine, clinic_id, doctor_id, service_id, months_ago,
              chat_id=CHAT, lang="ru") -> uuid.UUID:
    """Завершённый приём N месяцев назад прямым INSERT: движок не даёт занять
    прошедшее время, а recall'у нужен именно прошлый визит."""
    appointment_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, time_range, status, tg_chat_id, lang) "
                 "SELECT :i, :c, :d, :s, "
                 "       tstzrange(q.lo, q.lo + interval '30 minutes', '[)'), "
                 "       'booked', :chat, :lang "
                 "FROM (SELECT now() - make_interval(months => :m) AS lo) q"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "chat": chat_id, "lang": lang,
             "m": months_ago})
    return appointment_id


def seed_future(admin_engine, clinic_id, doctor_id, service_id,
                chat_id=CHAT) -> uuid.UUID:
    """Будущая booked-запись того же чата: пациенту, который уже придёт,
    приглашение не нужно."""
    appointment_id = uuid.uuid4()
    start = at_tashkent(next_monday(), "10:00")
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, time_range, status, tg_chat_id) "
                 "VALUES (:i, :c, :d, :s, tstzrange(:lo, :hi, '[)'), "
                 "        'booked', :chat)"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "lo": start,
             "hi": start + timedelta(minutes=30), "chat": chat_id})
    return appointment_id


def outreach_rows(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT appointment_id, tg_chat_id, lang, booked_at "
                 "FROM recall_outreach WHERE clinic_id = :c ORDER BY id"),
            {"c": clinic_id}).all()


def conversation_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM conversation")).scalar()


# ── Отправка приглашения ────────────────────────────────────────────────────

def test_recall_invite_sent_in_window(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    """Приём 7 месяцев назад при интервале 6 → приглашение на языке ПРИЁМА
    с сырой кнопкой записи, отправка отмечена в журнале."""
    set_recall(admin_engine, service_cleaning, 6)
    appointment_id = seed_past(admin_engine, clinic_a, doctor_a,
                               service_cleaning, months_ago=7, lang="uz")
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 1

    chat, body, buttons = api.sent[-1]
    assert chat == CHAT
    # язык берётся из appointment.lang: в conversation фоновый поток не
    # заглядывает и тем более его не переписывает
    assert body == t("recall_invite", "uz",
                     service=service_label("cleaning", "uz"), months=6)
    assert [b.label for b in buttons] == [t("btn_recall_book", "uz")]
    # кнопка сырая и несёт СВОЙ субъект: приглашение живёт дольше одной
    # отправки, номерная a:N потерялась бы при следующей карте кнопок
    assert [b.action for b in buttons] == [f"rcl:{appointment_id}"]
    assert all(len(b.action.encode()) <= 64 for b in buttons), \
        "лимит callback_data Telegram"
    assert [(r.appointment_id, r.tg_chat_id, r.lang, r.booked_at)
            for r in outreach_rows(admin_engine, clinic_a)] == \
        [(appointment_id, CHAT, "uz", None)]
    assert conversation_count(admin_engine) == 0, \
        "правило фона: рассылка не создаёт и не трогает диалог пациента"


def test_second_cycle_does_not_repeat_invite(app_session_factory, admin_engine,
                                             clinic_a, doctor_a,
                                             service_cleaning):
    """Цикл идёт каждые 30 секунд — второе приглашение по тому же приёму
    было бы спамом."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 1
    assert service.send_recalls(now_local=at_local(12)) == 0
    assert len(api.sent) == 1
    assert len(outreach_rows(admin_engine, clinic_a)) == 1


# ── Кого не беспокоим ───────────────────────────────────────────────────────

def test_future_appointment_silences_recall(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """Пациент уже записан — звать его на приём нелепо."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    seed_future(admin_engine, clinic_a, doctor_a, service_cleaning)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_forgotten_patient_is_not_disturbed(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """/forget обнуляет appointment.tg_chat_id — «не беспокоить» бесплатно."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7,
              chat_id=None)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_night_is_silent_and_leaves_nothing_behind(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning):
    """23:00 локали: пациента будить нельзя, а журнал должен остаться пустым —
    иначе утренний цикл сочтёт приглашение отправленным."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(23)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_service_without_interval_never_recalls(app_session_factory,
                                                admin_engine, clinic_a,
                                                doctor_a, service_cleaning):
    """recall_months IS NULL — рассылка по услуге выключена (умолчание)."""
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_interval_not_reached_yet(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    """Три месяца из шести — рано: интервал услуги и есть повод написать."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=3)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []
