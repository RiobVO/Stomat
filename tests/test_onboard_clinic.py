"""Онбординг реальной клиники: создание клиники (с криптослучайной солью),
врачей, услуг и правка графика/цены — без ручного SQL (B1+B2+B3)."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from navbat.crypto import decrypt_text
from navbat.onboard import (
    _validate_intervals,
    add_doctor,
    add_service,
    create_clinic,
    set_doctor_schedule,
    set_service_price,
)


def _clinic_row(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT name, salt, timezone FROM clinic WHERE id = :id"),
            {"id": clinic_id},
        ).one()


def test_create_clinic_generates_unique_random_salt(app_session_factory, admin_engine):
    a = create_clinic(app_session_factory, "Клиника А", "Asia/Tashkent")
    b = create_clinic(app_session_factory, "Клиника Б")  # tz по умолчанию
    ra, rb = _clinic_row(admin_engine, a), _clinic_row(admin_engine, b)
    assert ra.name == "Клиника А"
    assert ra.timezone == "Asia/Tashkent"
    assert rb.timezone == "Asia/Tashkent"  # дефолт
    # соль — обязательна, длинная и РАЗНАЯ у разных клиник (иначе хэши
    # телефонов обратимы перебором, B3)
    assert ra.salt and len(ra.salt) >= 32
    assert ra.salt != rb.salt


def test_add_doctor_default_and_custom(app_session_factory, admin_engine, clinic_a):
    did = add_doctor(app_session_factory, clinic_a, "Доктор Хаус", buffer_min=15)
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT name_encrypted, working_intervals, buffer_min "
                 "FROM doctor WHERE id = :d"), {"d": did}).one()
    assert decrypt_text(row.name_encrypted) == "Доктор Хаус"
    assert row.buffer_min == 15
    assert "mon" in row.working_intervals  # стандартный график по умолчанию


def test_add_service_validates_catalog_and_duplicates(app_session_factory, clinic_a):
    sid = add_service(app_session_factory, clinic_a, "cleaning", 30, 350_000)
    assert sid is not None
    with pytest.raises(ValueError):
        add_service(app_session_factory, clinic_a, "botox", 30)  # не из SERVICE_KEYS
    with pytest.raises(ValueError):
        add_service(app_session_factory, clinic_a, "cleaning", 45)  # дубль ключа


def test_set_service_price(app_session_factory, admin_engine, clinic_a):
    add_service(app_session_factory, clinic_a, "xray", 15, 80_000)
    set_service_price(app_session_factory, clinic_a, "xray", 95_000)
    with admin_engine.begin() as conn:
        price = conn.execute(
            text("SELECT price FROM service WHERE name = 'xray'")).scalar_one()
    assert int(price) == 95_000
    with pytest.raises(ValueError):
        set_service_price(app_session_factory, clinic_a, "implant", 100)  # нет такой


def test_set_doctor_schedule(app_session_factory, admin_engine, clinic_a):
    did = add_doctor(app_session_factory, clinic_a, "Доктор")
    new_sched = {"mon": [["10:00", "14:00"]], "tue": [["10:00", "14:00"]]}
    set_doctor_schedule(app_session_factory, clinic_a, did, new_sched)
    with admin_engine.begin() as conn:
        wi = conn.execute(
            text("SELECT working_intervals FROM doctor WHERE id = :d"),
            {"d": did}).scalar_one()
    assert wi == new_sched


def test_validate_intervals_rejects_malformed():
    assert _validate_intervals({"mon": [["09:00", "13:00"], ["14:00", "18:00"]]})
    with pytest.raises(ValueError):
        _validate_intervals({})  # пусто
    with pytest.raises(ValueError):
        _validate_intervals({"funday": [["09:00", "13:00"]]})  # неизвестный день
    with pytest.raises(ValueError):
        _validate_intervals({"mon": [["13:00", "09:00"]]})  # начало >= конца
    with pytest.raises(ValueError):
        _validate_intervals({"mon": [["25:00", "26:00"]]})  # некорректное время
    with pytest.raises(ValueError):
        _validate_intervals({"mon": [["09:00"]]})  # не пара
