"""Консоль владельца не должна молча стоить денег (карта, №11 и №18).

№11: «⛔ Скрыть» врача отвечало просто «Врач скрыт». Владелец считает, что
убрал человека из работы, а его будущие записи остаются — пациенты придут
к врачу, которого в клинике уже нет.

№18: «🗑 Удалить совсем» срабатывало с первого тапа, без подтверждения
и без отката.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from conftest import at_tashkent, make_doctor, next_monday
from navbat.telegram.admin_console import AdminConsole

CHAT = 4242


class _Worker:
    """Минимальный дублёр воркера: консоль зовёт _send/_edit и пару хелперов."""

    def __init__(self) -> None:
        self.sent: list = []
        self.edited: list = []

    def _send(self, chat_id, reply) -> None:
        self.sent.append(reply)

    def _edit(self, chat_id, message_id, reply) -> None:
        self.edited.append(reply)

    def _bot_paused(self) -> bool:
        return False


class _Api:
    def answer_callback_query(self, callback_id, text=None) -> None:
        pass


@pytest.fixture
def console(app_session_factory, clinic_a):
    worker = _Worker()
    return AdminConsole(app_session_factory, clinic_a, _Api(), worker), worker


def _callback(data: str) -> dict:
    return {"id": "cb", "data": data, "message": {"message_id": 1}}


def _last(worker):
    return (worker.edited or worker.sent)[-1]


def _actions(reply) -> list[str]:
    return [b.action for row in reply.button_rows for b in row]


def _book(admin_engine, clinic_id, doctor_id, service_id):
    """Будущая запись на врача — то, что владелец обязан увидеть."""
    start = at_tashkent(next_monday(), "10:00")
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO appointment (id, clinic_id, doctor_id, service_id, "
            "time_range, status, tg_chat_id) VALUES (:i, :c, :d, :s, "
            "tstzrange(:start, :finish, '[)'), 'booked', 555)"),
            {"i": uuid.uuid4(), "c": clinic_id, "d": doctor_id, "s": service_id,
             "start": start, "finish": start + __import__("datetime").timedelta(minutes=30)})


def test_hiding_doctor_warns_about_future_bookings(console, admin_engine,
                                                   clinic_a, doctor_a,
                                                   service_cleaning):
    cons, worker = console
    _book(admin_engine, clinic_a, doctor_a, service_cleaning)

    cons.handle_callback(_callback(f"adm:doc:{doctor_a}:deact"), CHAT,
                         f"adm:doc:{doctor_a}:deact")

    text_out = _last(worker).text
    assert "1" in text_out, f"владельцу нужно число записей: {text_out}"
    assert "запис" in text_out.lower(), text_out


def test_hiding_doctor_without_bookings_stays_quiet(console, admin_engine,
                                                    clinic_a, doctor_a):
    """Без записей лишний шум не нужен — предупреждение по факту, не всегда."""
    cons, worker = console

    cons.handle_callback(_callback(f"adm:doc:{doctor_a}:deact"), CHAT,
                         f"adm:doc:{doctor_a}:deact")

    assert "запис" not in _last(worker).text.lower()


def test_delete_asks_for_confirmation_first(console, admin_engine, clinic_a,
                                            doctor_a):
    """Первый тап по «Удалить совсем» обязан спрашивать, а не удалять."""
    cons, worker = console
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": doctor_a})

    cons.handle_callback(_callback(f"adm:doc:{doctor_a}:del"), CHAT,
                         f"adm:doc:{doctor_a}:del")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM doctor WHERE id = :d"),
                             {"d": doctor_a}).scalar_one()
    assert alive == 1, "врач не должен исчезать с первого тапа"
    assert f"adm:doc:{doctor_a}:delyes" in _actions(_last(worker)), \
        "нужна кнопка подтверждения"


def test_confirmed_delete_removes_doctor(console, admin_engine, clinic_a,
                                         doctor_a):
    cons, worker = console
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": doctor_a})

    cons.handle_callback(_callback(f"adm:doc:{doctor_a}:delyes"), CHAT,
                         f"adm:doc:{doctor_a}:delyes")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM doctor WHERE id = :d"),
                             {"d": doctor_a}).scalar_one()
    assert alive == 0


def test_service_delete_asks_for_confirmation_first(console, admin_engine,
                                                    clinic_a, service_cleaning):
    cons, worker = console
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET is_active = false "
                          "WHERE id = :s"), {"s": service_cleaning})

    cons.handle_callback(_callback("adm:svc:cleaning:del"), CHAT,
                         "adm:svc:cleaning:del")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM service WHERE id = :s"),
                             {"s": service_cleaning}).scalar_one()
    assert alive == 1
    assert "adm:svc:cleaning:delyes" in _actions(_last(worker))


def test_confirmed_service_delete_removes_it(console, admin_engine, clinic_a,
                                             service_cleaning):
    cons, worker = console
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET is_active = false "
                          "WHERE id = :s"), {"s": service_cleaning})

    cons.handle_callback(_callback("adm:svc:cleaning:delyes"), CHAT,
                         "adm:svc:cleaning:delyes")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM service WHERE id = :s"),
                             {"s": service_cleaning}).scalar_one()
    assert alive == 0


# ── Ревью волны B, блокер 4: состояние могло измениться между экранами ──────

def test_delyes_refuses_reactivated_doctor(console, admin_engine, clinic_a,
                                           doctor_a):
    """Владелец открыл подтверждение удаления скрытого врача, второй
    администратор вернул его в работу — «Да, удалить» не должно снести
    активного врача (TOCTOU между экраном и нажатием)."""
    cons, worker = console
    # врач активен на момент нажатия «Да»
    cons.handle_callback(_callback(f"adm:doc:{doctor_a}:delyes"), CHAT,
                         f"adm:doc:{doctor_a}:delyes")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM doctor WHERE id = :d"),
                             {"d": doctor_a}).scalar_one()
    assert alive == 1, "активный врач не удаляется"
    assert "скр" in _last(worker).text.lower(), _last(worker).text


def test_delyes_refuses_reactivated_service(console, admin_engine, clinic_a,
                                            service_cleaning):
    cons, worker = console

    cons.handle_callback(_callback("adm:svc:cleaning:delyes"), CHAT,
                         "adm:svc:cleaning:delyes")

    with admin_engine.begin() as conn:
        alive = conn.execute(text("SELECT count(*) FROM service WHERE id = :s"),
                             {"s": service_cleaning}).scalar_one()
    assert alive == 1
    assert "скр" in _last(worker).text.lower()
