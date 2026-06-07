"""Онбординг: импорт существующих записей из Calendar (E.2).

import_calendar перечисляет врачей с привязанным календарём и прогоняет
готовый sync-механизм; сам sync (импорт ручных событий как gcal_import)
покрыт тестами календаря.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import make_doctor
from navbat.onboard import import_calendar


class RecordingSync:
    def __init__(self) -> None:
        self.doctors: list = []

    def sync_doctor(self, doctor_id) -> None:
        self.doctors.append(doctor_id)


def test_import_calendar_syncs_only_bound_doctors(app_session_factory,
                                                  admin_engine, clinic_a):
    with_cal_1 = make_doctor(admin_engine, clinic_a)
    with_cal_2 = make_doctor(admin_engine, clinic_a)
    make_doctor(admin_engine, clinic_a)  # без календаря — пропускается
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE doctor SET gcal_calendar_id = 'cal@group.calendar.google.com' "
            "WHERE id IN (:a, :b)"), {"a": with_cal_1, "b": with_cal_2})

    sync = RecordingSync()
    synced = import_calendar(app_session_factory, clinic_a, sync)

    assert synced == 2
    assert set(sync.doctors) == {with_cal_1, with_cal_2}
