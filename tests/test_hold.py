"""Soft-hold: живой блокирует, протухший освобождает слот без фонового джоба."""
import pytest
from sqlalchemy import text

from navbat.scheduling.errors import SlotTakenError

from conftest import at_tashkent, next_monday


def test_active_hold_blocks_slot(sched, doctor_a, service_cleaning):
    day = next_monday()
    start = at_tashkent(day, "09:00")
    sched.hold(doctor_a, service_cleaning, start)

    assert start not in {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}
    with pytest.raises(SlotTakenError):
        sched.hold(doctor_a, service_cleaning, start)


def test_expired_hold_frees_slot(sched, admin_engine, doctor_a, service_cleaning):
    day = next_monday()
    start = at_tashkent(day, "09:00")
    stale_id = sched.hold(doctor_a, service_cleaning, start)

    # протухание (вместо ожидания 3 минут)
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE appointment SET hold_expires_at = now() - interval '1 minute' "
                 "WHERE id = :id"),
            {"id": stale_id},
        )

    # слот снова в выдаче — прямо в запросе, без фонового джоба
    assert start in {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}

    # и новая запись на него проходит: движок экспирит протухший hold перед вставкой
    new_id = sched.hold(doctor_a, service_cleaning, start)
    assert new_id != stale_id
    with admin_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM appointment WHERE id = :id"), {"id": stale_id}
        ).scalar_one()
    assert status == "expired"
