"""Демо-история для витрины /stats (карта docs/SALES_READINESS.md, №4).

На чистой демо-базе сводка владельца показывала «записей: 0 · предотвращено
неявок: 0 (≈ 0 сум) · отмен: 0», а секции «Клиенты», «Топ врачей» и
«Хит-услуга» не рендерились вовсе. Это главный денежный аргумент показа —
он не должен быть пустым экраном.

Сидер наполняет прошлое правдоподобной работой бота: записи (часть — вне
рабочих часов), отмены из напоминания с суммой освобождённых слотов,
новые и вернувшиеся пациенты, очередь ожидания.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import make_doctor, make_service
from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.demo_history import seed_demo_history
from navbat.stats import collect_stats, render_stats

TZ = ZoneInfo("Asia/Tashkent")


@pytest.fixture
def priced_clinic(admin_engine, clinic_a, doctor_a):
    """Демо-клиника идёт с прайсом — без цен метрика денег не считается
    (COALESCE(sum(price)) по NULL даёт 0), и тест проверял бы не то."""
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    make_service(admin_engine, clinic_a, "whitening", 60, price=500_000)
    make_doctor(admin_engine, clinic_a, name="Dilnoza opa")
    return clinic_a


@pytest.fixture
def seeded(app_session_factory, admin_engine, priced_clinic):
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today - timedelta(days=6), today, TZ)
    return stats


def test_money_section_is_not_empty(seeded):
    assert seeded.booked > 0, "записи за неделю должны быть"
    assert seeded.prevented_noshows > 0, "нужна демонстрация спасённых слотов"
    assert seeded.saved_revenue > 0, "у спасённых слотов должна быть сумма"


def test_after_hours_bookings_present(seeded):
    """«Бот записывал, пока клиника спала» — сильнейший аргумент показа."""
    assert seeded.after_hours_booked > 0
    assert seeded.after_hours_booked < seeded.booked, \
        "не все записи ночные — иначе выглядит выдумкой"


def test_client_and_doctor_sections_render(seeded):
    assert seeded.new_patients > 0
    assert seeded.returning_patients > 0, "вернувшиеся показывают удержание"
    assert seeded.top_doctors, "топ врачей — часть владельческого рендера"
    assert seeded.hit_service is not None


def test_render_has_no_zero_money_lines(seeded):
    today = datetime.now(TZ).date()
    out = render_stats(seeded, today - timedelta(days=6), today)

    assert "предотвращено неявок: 0" not in out, out
    assert "записей подтверждено: 0" not in out
    assert "👥 Клиенты" in out and "👨‍⚕️ Топ врачей" in out


def test_seed_is_idempotent(app_session_factory, admin_engine, priced_clinic):
    clinic_a = priced_clinic
    """Повторный прогон перед показом не должен удваивать историю."""
    seed_demo_history(app_session_factory, clinic_a, days=14)
    with admin_engine.begin() as conn:
        first = conn.execute(text("SELECT count(*) FROM appointment")).scalar_one()
    seed_demo_history(app_session_factory, clinic_a, days=14)
    with admin_engine.begin() as conn:
        second = conn.execute(text("SELECT count(*) FROM appointment")).scalar_one()

    assert first == second


def test_history_stays_in_the_past(app_session_factory, admin_engine,
                                   priced_clinic):
    clinic_a = priced_clinic
    """Демо-история не должна занимать слоты, которые показываются вживую:
    будущие записи ломают сценарий «выберите время»."""
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        future = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE lower(time_range) > now()"
        )).scalar_one()
    assert future == 0


# ── Правдоподобие витрины: покупатель смотрит глазами владельца ─────────────

def test_recent_week_is_not_weaker_than_previous(app_session_factory,
                                                 priced_clinic):
    """На показе тренд не должен смотреть вниз: «↓19%» под словами о росте
    выручки убивает аргумент вернее пустого экрана."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        cur = collect_stats(session, today - timedelta(days=6), today, TZ)
        prev = collect_stats(session, today - timedelta(days=13),
                             today - timedelta(days=7), TZ)

    assert cur.booked >= prev.booked, \
        f"свежая неделя слабее прошлой: {cur.booked} против {prev.booked}"


def test_hit_service_is_a_mass_one(app_session_factory, priced_clinic):
    """Хит-услуга — то, чем живёт клиника (чистка, осмотр), а не редкий
    премиум: «Брекеты — 3 зап.» читается как выдумка."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today - timedelta(days=6), today, TZ)

    assert stats.hit_service is not None
    assert stats.hit_service[0] in ("cleaning", "checkup", "filling"), \
        f"хитом стала непрофильная услуга: {stats.hit_service}"


def test_doctors_are_not_perfectly_equal(app_session_factory, priced_clinic):
    """Ровно поделённая пополам нагрузка выглядит сгенерированной."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today - timedelta(days=6), today, TZ)

    counts = [cnt for _, cnt, _ in stats.top_doctors]
    assert len(counts) >= 2 and len(set(counts)) > 1, counts


def test_no_overlapping_appointments(app_session_factory, priced_clinic):
    """Сидер обязан жить по правилам живой записи: в БД стоит exclusion
    constraint с буфером, и перекрытие валит весь прогон."""
    clinic_a = priced_clinic
    created = seed_demo_history(app_session_factory, clinic_a, days=14)
    assert created > 0


def test_today_is_filled_once_the_day_started(app_session_factory,
                                              priced_clinic):
    """Живой тык 28.07: владелец жмёт «📊 Статистика», консоль открывает
    сводку ЗА СЕГОДНЯ — а сидер наполнял только прошлые дни, и первый экран
    покупателя снова показывал нули.

    Наполняем сегодня прошедшими часами. Если показ идёт до открытия
    клиники, записей за день нет и быть не может — выдумывать приёмы из
    будущего нельзя, они займут слоты живого сценария; для такого случая
    в DEMO.md сказано переключиться кнопкой на «7 дней».
    """
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today, today, TZ)

    if datetime.now(TZ).hour < 11:
        assert stats.booked == 0, "до открытия клиники приёмов быть не может"
    else:
        assert stats.booked > 0, "днём сводка за сегодня не должна быть пустой"
