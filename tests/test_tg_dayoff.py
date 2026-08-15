"""Команды админ-чата /dayoff и /dayopen: клиника сама закрывает дни (Ф1.5).

Решение 06.06.2026: предзаполненный календарь госпраздников отменён — кому
нужен выходной, тот сам закрывает день из админ-чата. Закрытый день уважают
и слоты, и «сейчас закрыто» (таблица holiday, механизм уже покрыт тестами).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from test_dialog_booking import CHAT, extr
from test_tg_worker import make_worker, put_message

ADMIN_CHAT = 777
TASHKENT = ZoneInfo("Asia/Tashkent")


def clinic_today() -> date:
    return datetime.now(TASHKENT).date()


def future_workday(days: int) -> date:
    return clinic_today() + timedelta(days=days)


def holidays_in_db(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT date, reason FROM holiday WHERE clinic_id = :c "
                 "ORDER BY date"),
            {"c": clinic_id},
        ).all()


def admin_text(api) -> str:
    """Последний ответ в админ-чат."""
    return [reply for chat, reply, _ in api.sent if chat == ADMIN_CHAT][-1]


def send_admin(worker, app_session_factory, clinic_id, text_in) -> None:
    put_message(app_session_factory, clinic_id, text_in, chat_id=ADMIN_CHAT)
    worker.process_one()


# ── /dayoff ──────────────────────────────────────────────────────────────────

def test_dayoff_closes_nearest_future_date(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    target = future_workday(7)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               f"/dayoff {target:%d.%m} Навруз")

    rows = holidays_in_db(admin_engine, clinic_a)
    assert [(row.date, row.reason) for row in rows] == [(target, "Навруз")]
    assert "[OK]" in admin_text(api)
    assert f"{target:%d.%m.%Y}" in admin_text(api)
    assert "Навруз" in admin_text(api)


def test_dayoff_past_ddmm_rolls_to_next_year(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    yesterday = clinic_today() - timedelta(days=1)
    if (yesterday.month, yesterday.day) == (2, 29):  # раз в 4 года: нет в +1 году
        yesterday -= timedelta(days=1)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               f"/dayoff {yesterday:%d.%m}")

    expected = date(yesterday.year + 1, yesterday.month, yesterday.day)
    assert [row.date for row in holidays_in_db(admin_engine, clinic_a)] == [expected]


def test_dayoff_duplicate_reports_already_closed(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a,
                                                 service_cleaning):
    target = future_workday(7)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")

    assert len(holidays_in_db(admin_engine, clinic_a)) == 1
    assert "уже выходной" in admin_text(api)


def test_dayoff_usage_lists_upcoming_closed_days(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a,
                                                 service_cleaning):
    target = future_workday(5)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               f"/dayoff {target:%d.%m} Ремонт")
    send_admin(worker, app_session_factory, clinic_a, "/dayoff")

    out = admin_text(api)
    assert "/dayoff DD.MM" in out, "подсказка формата"
    assert f"{target:%d.%m.%Y}" in out and "Ремонт" in out, "список закрытых дней"


def test_dayoff_bad_date_shows_usage(app_session_factory, admin_engine, clinic_a,
                                     doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "/dayoff 31.02")
    send_admin(worker, app_session_factory, clinic_a, "/dayoff abc")

    assert not holidays_in_db(admin_engine, clinic_a)
    replies = [reply for chat, reply, _ in api.sent if chat == ADMIN_CHAT]
    assert len(replies) == 2
    for reply in replies:
        assert "/dayoff DD.MM" in reply


def test_dayoff_from_patient_goes_to_nlu(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a,
                                 [extr(intent="other")],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/dayoff 21.03", chat_id=CHAT)
    worker.process_one()

    assert api.sent[0][0] == CHAT, "пациентский текст ушёл обычным путём"
    assert not holidays_in_db(admin_engine, clinic_a)


# ── №7: закрытие дня видит уже назначенные приёмы ────────────────────────────

def _book_at(admin_engine, clinic_id, doctor_id, service_id, day, hhmm="10:00"):
    """Живая запись на этот день — то, что закрытие дня НЕ отменяет."""
    import uuid

    from conftest import at_tashkent
    start = at_tashkent(day, hhmm)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO appointment (id, clinic_id, doctor_id, service_id, "
            "time_range, status, tg_chat_id) VALUES (:i, :c, :d, :s, "
            "tstzrange(:start, :finish, '[)'), 'booked', 555)"),
            {"i": uuid.uuid4(), "c": clinic_id, "d": doctor_id, "s": service_id,
             "start": start, "finish": start + timedelta(minutes=30)})


def _statuses(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return [r.status for r in conn.execute(
            text("SELECT status FROM appointment WHERE clinic_id = :c"),
            {"c": clinic_id}).all()]


def test_dayoff_warns_about_existing_bookings(app_session_factory, admin_engine,
                                              clinic_a, doctor_a,
                                              service_cleaning):
    """Карта готовности №7: владелец закрывает день, а записи на нём остаются.

    Решение владельца: предупреждать, ничего не отменять."""
    target = future_workday(7)
    _book_at(admin_engine, clinic_a, doctor_a, service_cleaning, target, "10:30")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")

    out = admin_text(api)
    assert "[OK]" in out, "день всё равно закрывается"
    assert "10:30" in out, f"нет времени приёма: {out}"
    assert "1" in out and "запис" in out.lower()
    assert [row.date for row in holidays_in_db(admin_engine, clinic_a)] == [target]
    assert _statuses(admin_engine, clinic_a) == ["booked"], "запись отменена сама"


def test_dayoff_without_bookings_stays_quiet(app_session_factory, admin_engine,
                                             clinic_a, doctor_a,
                                             service_cleaning):
    target = future_workday(7)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")

    assert "запис" not in admin_text(api).lower()


def test_dayoff_ignores_bookings_of_other_days(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Считаем приёмы закрываемого дня, а не все будущие."""
    _book_at(admin_engine, clinic_a, doctor_a, service_cleaning,
             future_workday(3), "09:00")
    target = future_workday(7)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")

    assert "запис" not in admin_text(api).lower()


def test_dayoff_button_warns_about_existing_bookings(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a, service_cleaning):
    """Кнопочный путь консоли предупреждает так же, как слэш-команда."""
    from test_admin_console import click
    target = future_workday(7)
    _book_at(admin_engine, clinic_a, doctor_a, service_cleaning, target, "15:00")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dayoff:add")
    send_admin(worker, app_session_factory, clinic_a, f"{target:%d.%m}")

    out = admin_text(api)
    assert "15:00" in out, f"нет времени приёма: {out}"
    assert "запис" in out.lower()
    assert _statuses(admin_engine, clinic_a) == ["booked"]


# ── /dayopen ─────────────────────────────────────────────────────────────────

def test_dayopen_reopens_closed_day(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    target = future_workday(7)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, f"/dayoff {target:%d.%m}")
    send_admin(worker, app_session_factory, clinic_a, f"/dayopen {target:%d.%m}")

    assert not holidays_in_db(admin_engine, clinic_a)
    assert "снова рабочий" in admin_text(api)


def test_dayopen_not_closed_day(app_session_factory, admin_engine, clinic_a,
                                doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               f"/dayopen {future_workday(3):%d.%m}")

    assert "и так рабочий" in admin_text(api)
