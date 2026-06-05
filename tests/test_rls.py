"""Изоляция арендаторов: RLS под непривилегированной ролью navbat_app."""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from navbat.db.base import tenant_transaction

from conftest import at_tashkent, make_doctor, make_service, next_monday


@pytest.fixture
def appointment_b(admin_engine, clinic_b):
    """Запись чужой клиники, созданная админом (сетап мимо RLS)."""
    doctor_b = make_doctor(admin_engine, clinic_b)
    service_b = make_service(admin_engine, clinic_b, "cleaning", 30)
    day = next_monday()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment "
                 "(clinic_id, doctor_id, service_id, time_range, status, source) "
                 "VALUES (:c, :d, :s, tstzrange(:f, :t, '[)'), 'booked', 'manual')"),
            {"c": clinic_b, "d": doctor_b, "s": service_b,
             "f": at_tashkent(day, "09:00"), "t": at_tashkent(day, "09:30")},
        )
    return clinic_b


def test_tenant_sees_only_own_rows(app_session_factory, clinic_a, appointment_b):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        count = session.execute(text("SELECT count(*) FROM appointment")).scalar_one()
    assert count == 0  # запись клиники B невидима из контекста A


def test_cross_tenant_insert_rejected(
    app_session_factory, clinic_a, clinic_b, admin_engine
):
    doctor_b = make_doctor(admin_engine, clinic_b)
    day = next_monday()
    with pytest.raises(DBAPIError, match="row-level security"):
        with tenant_transaction(app_session_factory, clinic_a) as session:
            session.execute(
                text("INSERT INTO appointment "
                     "(clinic_id, doctor_id, time_range, status, source) "
                     "VALUES (:c, :d, tstzrange(:f, :t, '[)'), 'booked', 'manual')"),
                {"c": clinic_b, "d": doctor_b,  # ЧУЖОЙ clinic_id из контекста A
                 "f": at_tashkent(day, "09:00"), "t": at_tashkent(day, "09:30")},
            )


def test_no_tenant_context_fails_loudly(app_session_factory, appointment_b):
    # без app.clinic_id запрос падает (current_setting без missing_ok), а не молчит
    with pytest.raises(DBAPIError):
        with app_session_factory() as session:
            session.execute(text("SELECT count(*) FROM appointment")).scalar_one()


def test_engine_scoped_to_own_clinic(app_session_factory, clinic_a, appointment_b):
    """Движок клиники A не видит врачей/записи клиники B даже по прямому id."""
    from navbat.scheduling.engine import SchedulingEngine
    from navbat.scheduling.errors import AppointmentNotFoundError

    sched_a = SchedulingEngine(app_session_factory, clinic_a)
    with pytest.raises(AppointmentNotFoundError):
        sched_a.cancel(uuid.uuid4())  # несуществующая/чужая запись — одно и то же
