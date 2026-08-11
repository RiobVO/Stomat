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

from datetime import datetime, time, timedelta
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


def _seeded_counts(admin_engine) -> tuple[int, int, int]:
    """(записей, приглашений, просьб об оценке) — всё, что налил сид."""
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT (SELECT count(*) FROM appointment), "
            "       (SELECT count(*) FROM recall_outreach), "
            "       (SELECT count(*) FROM review)")).one()


def test_seed_is_idempotent(app_session_factory, admin_engine, priced_clinic):
    clinic_a = priced_clinic
    """Повторный прогон перед показом не должен удваивать историю.

    Журналы возвратов и оценок сеются отдельной фазой по ВСЕМ прошедшим
    приёмам базы, а не только по созданным этим прогоном, — значит второй
    запуск проходит по тем же приёмам, и защита от дублей у них своя
    (UNIQUE(appointment_id) + ON CONFLICT), а не пропуск наполненного дня."""
    seed_demo_history(app_session_factory, clinic_a, days=14)
    first = _seeded_counts(admin_engine)
    seed_demo_history(app_session_factory, clinic_a, days=14)
    second = _seeded_counts(admin_engine)

    assert first[1] and first[2], \
        "журналы пусты — идемпотентность держалась бы на пустоте"
    assert first == second


def test_history_stays_in_the_past(app_session_factory, admin_engine,
                                   priced_clinic):
    """Демо-история не должна занимать слоты, которые показываются вживую:
    будущие записи ломают сценарий «выберите время».

    Сравниваем с ИНЖЕКТИРОВАННЫМ «сейчас», а не с серверным now(), и берём
    момент ВНУТРИ первого приёма смены — 09:15. Иначе регрессия «проверяю
    начало приёма вместо конца» не ловится вовсе: буфер врача и округление до
    получасовой сетки уносят следующий слот далеко за «сейчас», и тест
    остаётся зелёным на сломанном коде (проверено откатом, ре-ревью).

    День берём рабочий: в воскресенье у фикстурных врачей смен нет, сегодняшний
    день пропускается целиком и проверять было бы нечего."""
    clinic_a = priced_clinic
    day = datetime.now(TZ).date()
    while day.weekday() == 6:
        day -= timedelta(days=1)
    now = datetime.combine(day, time(9, 15), TZ)
    seed_demo_history(app_session_factory, clinic_a, days=14, now=now)

    with admin_engine.begin() as conn:
        # именно upper: приём, ИДУЩИЙ сейчас, занимает слот живого сценария
        # так же, как будущий — проверка по lower пропустила бы регрессию
        future = conn.execute(
            text("SELECT count(*) FROM appointment WHERE upper(time_range) > :now"),
            {"now": now}).scalar_one()
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

    То же и в воскресенье: у демо-врачей это выходной, сидер пропускает день
    целиком, и пустая витрина «за сегодня» — правильное поведение.

    Ожидание зависит от часа прогона, поэтому веток три: до 09:30 и в выходной
    записей нет, с 11:00 они обязаны быть, а между — серое окно, где ответ
    решает длительность первой посеянной услуги.
    """
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    today = datetime.now(TZ).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today, today, TZ)

    now = datetime.now(TZ)
    # 09:30 — самый ранний момент, когда сегодняшний приём мог ЗАВЕРШИТЬСЯ:
    # смена начинается в 09:00, короткая услуга фикстуры длится 30 минут, а
    # сидер берёт только прошедшие часы (demo_history: not_after). Оформление
    # такого приёма датируется задним числом (max(start − 3ч, 06:00)), поэтому
    # в сводку за сегодня он попадает сразу, как приём прошёл
    if now.weekday() == 6 or now.time() < time(9, 30):
        assert stats.booked == 0, \
            "выходной или клиника ещё не наработала на первый завершённый приём"
    elif now.hour >= 11:
        assert stats.booked > 0, "днём сводка за сегодня не должна быть пустой"
    else:
        pytest.skip("09:30–11:00: результат зависит от длительности первой "
                    "посеянной услуги (30 или 60 минут) — недетерминирован")


# ── Ревью сидера: радиус поражения, чистота данных, доменные правила ────────

def test_cli_refuses_non_demo_clinic(monkeypatch, capsys):
    """Сидер синтетический: запуск на боевом арендаторе — порча данных
    живой клиники. CLI обязан отказать (ревью, блокирующая находка)."""
    import sys as _sys

    from navbat import onboard

    monkeypatch.setattr(_sys, "argv",
                        ["onboard", "--demo-history",
                         "--clinic", "11111111-1111-4111-8111-111111111111"])
    with pytest.raises(SystemExit):
        onboard.main()


def test_count_window_is_measured_in_local_days(app_session_factory, admin_engine,
                                                priced_clinic, doctor_a):
    """Счётчик существующей истории обязан мерить окно теми же локальными
    ДАТАМИ, что ведёт сид.

    Абсолютное «now() минус N суток» отрезает утро самого старого дня окна: при
    `--days 0` не видно ни одной прошедшей записи вовсе, а после наполнения в
    субботу повтор в воскресенье вечером терял субботнее утро. CLI объявлял
    исправную историю несуществующей и печатал [FAIL] перед показом."""
    from navbat.demo_history import summary_stats

    clinic_a = priced_clinic
    now = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    start = now.replace(hour=0, minute=30)  # приём в начале сегодняшних суток
    with admin_engine.begin() as conn:
        appointment_id = conn.execute(
            text("INSERT INTO appointment (clinic_id, doctor_id, time_range, "
                 "buffer_min, status, source) VALUES (:c, :d, "
                 "tstzrange(:lo, :hi, '[)'), 10, 'booked', 'demo_history') "
                 "RETURNING id"),
            {"c": clinic_a, "d": doctor_a, "lo": start,
             "hi": start + timedelta(minutes=30)}).scalar_one()
        conn.execute(
            text("INSERT INTO appointment_audit (clinic_id, appointment_id, actor, "
                 "action, at) VALUES (:c, :a, 'bot', 'confirm', :at)"),
            {"c": clinic_a, "a": appointment_id, "at": start.replace(minute=5)})

    assert summary_stats(app_session_factory, clinic_a, days=0, now=now).booked == 1


def test_cli_fails_when_the_summary_stays_empty(monkeypatch, admin_engine,
                                                app_session_factory):
    """Сид может создать записи, которых сводка НЕ УВИДИТ: оформление прошлых
    приёмов датируется накануне, поэтому приёмы самого старого дня окна
    выпадают из периода. CLI печатал «создано N» над пустой витриной, и
    владелец шёл на показ, доверяя сообщению (ревью, блокер)."""
    import sys as _sys

    from navbat import onboard

    onboard.seed_demo_clinic(app_session_factory)
    today = datetime.now(TZ).date()
    depth = 14
    while (today - timedelta(days=depth)).weekday() == 6:
        depth -= 1  # старейший день окна обязан быть рабочим, иначе сеять негде
    with admin_engine.begin() as conn:  # рабочим остаётся только старейший день
        for shift in range(depth):
            conn.execute(
                text("INSERT INTO holiday (clinic_id, date, reason) "
                     "VALUES (:c, :d, 'Праздник')"),
                {"c": onboard.DEMO_CLINIC_ID, "d": today - timedelta(days=shift)})
    monkeypatch.setattr(_sys, "argv",
                        ["onboard", "--demo-history", "--days", str(depth)])

    with pytest.raises(SystemExit) as exc:
        onboard.main()

    assert "[FAIL]" in str(exc.value), str(exc.value)
    # именно ветка «создано, но мимо окна»: отказ «наливать некуда» означал бы,
    # что сид вообще ничего не создал, и блокер остался бы непокрытым
    assert "создано" in str(exc.value), str(exc.value)


def test_cli_counts_money_lines_as_a_living_summary(monkeypatch, admin_engine,
                                                    app_session_factory):
    """Витрина не пуста и без подтверждений в окне: отмены заранее живут на
    cancel-аудитах в день приёма и держат денежные строки («предотвращено
    неявок» с суммой), тогда как confirm датируется накануне и из окна
    выпадает. Критерий по одним подтверждениям объявлял такую сводку пустой и
    пугал владельца [FAIL] на исправной клинике (ревью)."""
    import sys as _sys

    from navbat import onboard

    onboard.seed_demo_clinic(app_session_factory)
    today = datetime.now(TZ).date()
    # день с шестью приёмами (DAILY_PLAN), чтобы среди них была отмена;
    # и он обязан быть рабочим — в воскресенье сеять негде
    depth = 10 if (today - timedelta(days=10)).weekday() != 6 else 12
    with admin_engine.begin() as conn:
        for shift in range(depth):
            conn.execute(
                text("INSERT INTO holiday (clinic_id, date, reason) "
                     "VALUES (:c, :d, 'Праздник')"),
                {"c": onboard.DEMO_CLINIC_ID, "d": today - timedelta(days=shift)})
    monkeypatch.setattr(_sys, "argv",
                        ["onboard", "--demo-history", "--days", str(depth)])

    assert onboard.main() == 0

    with tenant_transaction(app_session_factory, onboard.DEMO_CLINIC_ID) as session:
        stats = collect_stats(session, today - timedelta(days=depth), today, TZ)
    assert stats.booked == 0, "предпосылка: подтверждения ушли за границу окна"
    assert stats.prevented_noshows > 0, "денежные строки и держат витрину"


def test_summary_count_sees_non_demo_bookings(app_session_factory, admin_engine,
                                              priced_clinic, doctor_a):
    """Витрина считает ВСЕ подтверждения, не только синтетику. Счётчик, знающий
    лишь про `demo_history`, объявлял бы «наливать некуда» над непустой сводкой:
    слоты могли занять живые записи показа."""
    from navbat.demo_history import summary_stats

    clinic_a = priced_clinic
    now = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    start = now.replace(hour=9)
    with admin_engine.begin() as conn:
        appointment_id = conn.execute(
            text("INSERT INTO appointment (clinic_id, doctor_id, time_range, "
                 "buffer_min, status, source) VALUES (:c, :d, "
                 "tstzrange(:lo, :hi, '[)'), 10, 'booked', 'bot') RETURNING id"),
            {"c": clinic_a, "d": doctor_a, "lo": start,
             "hi": start + timedelta(minutes=30)}).scalar_one()
        conn.execute(
            text("INSERT INTO appointment_audit (clinic_id, appointment_id, actor, "
                 "action, at) VALUES (:c, :a, 'bot', 'confirm', :at)"),
            {"c": clinic_a, "a": appointment_id, "at": start})

    assert summary_stats(app_session_factory, clinic_a, days=0, now=now).booked == 1


def test_cli_rejects_negative_window(monkeypatch):
    """`--days -1` — не окно: цикл сида пуст, а счётчик искал бы записи с
    завтрашнего дня, и исправная клиника получала «наливать некуда». Проверяем
    именно причину отказа: любой другой преждевременный выход не считается."""
    import sys as _sys

    from navbat import onboard

    monkeypatch.setattr(_sys, "argv", ["onboard", "--demo-history", "--days", "-1"])
    with pytest.raises(SystemExit) as exc:
        onboard.main()
    assert "--days" in str(exc.value), str(exc.value)


def test_clear_ignores_window_argument(monkeypatch):
    """Очистка окном не пользуется — отказ из-за `--days` был бы ложной стеной
    перед единственной командой отката."""
    import sys as _sys

    from navbat import onboard

    monkeypatch.setattr(_sys, "argv",
                        ["onboard", "--demo-history-clear", "--days", "-1"])
    assert onboard.main() == 0


def test_cli_says_when_there_is_nothing_to_seed(monkeypatch, admin_engine):
    """Ноль созданных записей значит одно из двух: окно уже наполнено или
    наливать некуда (врачи и услуги скрыты, слоты заняты чужими событиями).
    Второе молчаливое «демо-история уже есть» скрывало проблему: владелец шёл
    на показ с пустой витриной, считая, что история на месте (ре-ревью)."""
    import sys as _sys

    from navbat import onboard

    with admin_engine.begin() as conn:  # клиника есть, врачей и услуг нет
        conn.execute(
            text("INSERT INTO clinic (id, name, salt, timezone) VALUES "
                 "(:id, 'Navbat Demo', 'test-salt', 'Asia/Tashkent')"),
            {"id": onboard.DEMO_CLINIC_ID})
    monkeypatch.setattr(_sys, "argv", ["onboard", "--demo-history"])

    with pytest.raises(SystemExit) as exc:
        onboard.main()

    assert "уже есть" not in str(exc.value), str(exc.value)


def test_cli_survives_missing_demo_clinic(monkeypatch):
    """Сеять историю до `--demo` — типовой промах порядка команд. Ответ должен
    быть понятной строкой, а не трейсбеком NoResultFound из чтения таймзоны."""
    import sys as _sys

    from navbat import onboard

    monkeypatch.setattr(_sys, "argv", ["onboard", "--demo-history"])
    with pytest.raises(SystemExit) as exc:
        onboard.main()
    assert "[FAIL]" in str(exc.value), str(exc.value)


def test_no_active_waitlist_rows(app_session_factory, admin_engine,
                                 priced_clinic):
    """Активная очередь с синтетическими чатами заставляет матчер слать
    пуши в никуда каждые 30 секунд, а при ошибке доставки гасит строки —
    метрика «в очереди» пропадает посреди показа."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        active = conn.execute(text(
            "SELECT count(*) FROM waitlist "
            "WHERE status IN ('waiting', 'notified')")).scalar_one()
    assert active == 0


def test_appointments_carry_doctor_buffer(app_session_factory, admin_engine,
                                          priced_clinic):
    """buffer_min участвует в exclusion constraint: без него синтетика
    живёт по более мягким правилам, чем живая запись."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        zero = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'demo_history' "
            "AND buffer_min = 0")).scalar_one()
    assert zero == 0, "буфер врача должен попадать в запись"


def test_history_respects_working_days(app_session_factory, admin_engine,
                                       priced_clinic):
    """Записи в день, когда врач не работает, — вопрос от владельца на
    первом же экране статистики."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        sundays = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'demo_history' "
            "AND extract(isodow FROM lower(time_range) AT TIME ZONE "
            "'Asia/Tashkent') = 7")).scalar_one()
        lunch = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'demo_history' "
            "AND extract(hour FROM lower(time_range) AT TIME ZONE "
            "'Asia/Tashkent') = 13")).scalar_one()
    assert sundays == 0, "воскресенье у демо-врачей выходной"
    assert lunch == 0, "в обед приёмов нет"


# ── Повторное ревью сидера: что нашёл независимый проход ───────────────────

def _seed_at(app_session_factory, clinic_id, hour: int = 15, days: int = 14):
    """Сид с зафиксированным «сейчас» последнего РАБОЧЕГО дня.

    Возвращает (создано, день сида). Час фиксируем, иначе тест дня зависит от
    часа прогона (ночью приёмов за этот день не бывает вовсе). День откатываем
    с воскресенья: у фикстурных врачей смен в этот день нет, сидер пропускает
    его целиком — в воскресном прогоне проверять было бы нечего."""
    day = datetime.now(TZ).date()
    while day.weekday() == 6:
        day -= timedelta(days=1)
    now = datetime.combine(day, time(hour, 0), TZ)
    created = seed_demo_history(app_session_factory, clinic_id, days=days, now=now)
    return created, day


def test_todays_summary_is_not_empty(app_session_factory, priced_clinic):
    """Сводка за ДЕНЬ считает не приёмы, а работу бота: confirm-аудиты
    (stats.py) и created_at. Оформление всей истории «накануне» оставляло
    первый экран покупателя пустым даже с приёмами в этом дне.

    Судим по последнему рабочему дню, а не по календарному «сегодня»:
    демо-клиника по воскресеньям закрыта, витрина за такой день пуста
    законно — иначе тест проверял бы день недели, а не наполнение сводки."""
    clinic_a = priced_clinic
    _, day = _seed_at(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, day, day, TZ)

    assert stats.booked > 0, "за рабочий день бот не оформил ни одной записи"
    assert stats.new_patients + stats.returning_patients > 0, "секция клиентов пуста"
    assert stats.top_doctors, "топ врачей за день пуст"


def test_todays_appointments_are_booked_today(app_session_factory, admin_engine,
                                              priced_clinic):
    clinic_a = priced_clinic
    _, day = _seed_at(app_session_factory, clinic_a)

    with admin_engine.begin() as conn:
        # сверяем с днём сида, а не с now(): хелпер сеет последний рабочий
        # день, и в воскресном прогоне сравнение с календарным «сегодня» не
        # проверяло бы ничего — приёмов в воскресенье сидер не создаёт вовсе
        mismatched = conn.execute(text(
            "SELECT count(*) FROM appointment "
            "WHERE source = 'demo_history' "
            "AND (lower(time_range) AT TIME ZONE 'Asia/Tashkent')::date = :day "
            "AND (created_at AT TIME ZONE 'Asia/Tashkent')::date <> :day"),
            {"day": day}).scalar_one()
    assert mismatched == 0, "приёмы дня сида оформлены не в этот день"


def test_history_skips_closed_days(app_session_factory, admin_engine,
                                   priced_clinic):
    """Приёмы в день, закрытый через /dayoff, — та же ложь, что приём в
    воскресенье: живая запись туда не пустила бы (engine смотрит holiday)."""
    clinic_a = priced_clinic
    closed = datetime.now(TZ).date() - timedelta(days=3)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO holiday (clinic_id, date, reason) "
            "VALUES (:c, :d, 'Праздник')"), {"c": clinic_a, "d": closed})
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        on_holiday = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'demo_history' "
            "AND (lower(time_range) AT TIME ZONE 'Asia/Tashkent')::date = :d"),
            {"d": closed}).scalar_one()
    assert on_holiday == 0, "сидер записал приёмы в закрытый день"


def test_appointments_sit_on_the_slot_grid(app_session_factory, admin_engine,
                                           priced_clinic):
    """Слоты живой записи идут по сетке 30 минут (calendar_rules). Приём
    в 09:40 в календаре врача — первый вопрос владельца на показе."""
    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        off_grid = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'demo_history' "
            "AND extract(minute FROM lower(time_range) AT TIME ZONE "
            "'Asia/Tashkent')::int % 30 <> 0")).scalar_one()
    assert off_grid == 0, "приёмы стоят мимо получасовой сетки"


def test_seed_survives_occupied_past(app_session_factory, admin_engine,
                                     clinic_a, doctor_a):
    """В прошлом врача уже стоит приём: ручное событие из его личного
    календаря (первый импорт идёт без timeMin — calendar/sync.py) или запись,
    оставшаяся после предыдущего показа.

    Сидер существующие записи не смотрит — занятость в проекте гарантирует БД,
    а не код. Значит пересечение обязано стоить ОДНОГО слота: сид идёт одной
    транзакцией, и exclusion constraint откатывал бы её целиком — команда
    падала бы трейсбеком перед показом, оставляя витрину пустой.
    """
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    day = datetime.now(TZ).date() - timedelta(days=1)
    while day.weekday() == 6:  # воскресенье у фикстурного врача выходной
        day -= timedelta(days=1)
    occupied = datetime.combine(day, time(9, 0), TZ)  # первый слот смены
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (clinic_id, doctor_id, time_range, "
                 "buffer_min, status, source) VALUES (:c, :d, "
                 "tstzrange(:lo, :hi, '[)'), 0, 'booked', 'gcal_import')"),
            {"c": clinic_a, "d": doctor_a, "lo": occupied,
             "hi": occupied + timedelta(hours=1)})

    created = seed_demo_history(app_session_factory, clinic_a, days=14)

    assert created > 0, "сид упал на чужой записи"
    with admin_engine.begin() as conn:
        same_day = conn.execute(
            text("SELECT count(*) FROM appointment WHERE source = 'demo_history' "
                 "AND (lower(time_range) AT TIME ZONE 'Asia/Tashkent')::date = :d"),
            {"d": day}).scalar_one()
        foreign = conn.execute(text(
            "SELECT count(*) FROM appointment WHERE source = 'gcal_import' "
            "AND status = 'booked'")).scalar_one()
    assert same_day > 0, "занятый первый слот стоил всего дня"
    assert foreign == 1, "чужая запись должна остаться нетронутой"


def test_stale_history_is_extended(app_session_factory, priced_clinic):
    """Сид, сделанный неделю назад, обязан догоняться до сегодня.

    Гейт «история уже есть» смотрел на факт наличия строк, поэтому второй
    показ шёл с пустой свежей неделей: `/stats 7` — нули, тренд вниз, а
    команда отвечала «демо-история уже есть» и ничего не делала."""
    clinic_a = priced_clinic
    now = datetime.now(TZ)
    seed_demo_history(app_session_factory, clinic_a, days=14,
                      now=now - timedelta(days=8))

    created = seed_demo_history(app_session_factory, clinic_a, days=14, now=now)

    assert created > 0, "свежие дни не наполнились"
    today = now.date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today - timedelta(days=6), today, TZ)
    assert stats.booked > 0, "сводка за свежую неделю осталась пустой"


def test_every_active_doctor_gets_appointments(app_session_factory, admin_engine,
                                               clinic_a, doctor_a):
    """Каждый активный врач обязан попадать в раскладку: невыбранный исчезает
    из «Топ врачей» на витрине, а день, когда работает только он, остаётся
    пустым. Паттерн нагрузки давал лишь двух врачей независимо от их числа."""
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    make_doctor(admin_engine, clinic_a, name="Dilnoza opa")
    make_doctor(admin_engine, clinic_a, name="Rustam aka")

    seed_demo_history(app_session_factory, clinic_a, days=14)

    with admin_engine.begin() as conn:
        used = conn.execute(text(
            "SELECT count(DISTINCT doctor_id) FROM appointment "
            "WHERE source = 'demo_history'")).scalar_one()
    assert used == 3, f"записи достались {used} врачам из 3"


def test_clear_removes_everything_it_created(app_session_factory, admin_engine,
                                             priced_clinic):
    """Откат обязателен: без него следы демо остаются в базе навсегда."""
    from navbat.demo_history import clear_demo_history

    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    clear_demo_history(app_session_factory, clinic_a)

    with admin_engine.begin() as conn:
        left = conn.execute(text(
            "SELECT (SELECT count(*) FROM appointment WHERE source = 'demo_history') "
            "+ (SELECT count(*) FROM appointment_audit aa WHERE NOT EXISTS "
            "   (SELECT 1 FROM appointment a WHERE a.id = aa.appointment_id))"
        )).scalar_one()
    assert left == 0, "не должно остаться ни записей, ни осиротевшего аудита"


def test_no_synthetic_patient_books_twice_in_a_row(app_session_factory,
                                                   admin_engine, priced_clinic):
    """Витрина не должна показывать «нового пациента», который записался
    дважды подряд.

    Формула чата брала остаток счётчика, и соседние записи 3k+1 и 3k+2
    получали ОДИН чат: треть визитов оказывалась повтором вплотную к
    оригиналу, а разных лиц — и, значит, «новых» в сводке — заметно меньше
    задуманного."""
    seed_demo_history(app_session_factory, priced_clinic, days=14)
    with admin_engine.begin() as conn:
        chats = conn.execute(text(
            "SELECT tg_chat_id FROM appointment WHERE source = 'demo_history' "
            "ORDER BY lower(time_range), id")).scalars().all()
    glued = [i for i in range(1, len(chats)) if chats[i] == chats[i - 1]]

    assert len(chats) > 20, "сидер вообще ничего не налил"
    assert not glued, f"подряд идущие визиты одного лица: {len(glued)}"


# ── Журналы витрины: возвраты (recall) и оценки приёма ─────────────────────

def test_recall_and_review_lines_are_alive(seeded):
    """Две продающие фичи пакета живут в собственных журналах: пустые
    recall_outreach и review — это пустые строки «🔁 Возврат пациентов» и
    «⭐ Оценки пациентов», которые на показе приходится пересказывать
    словами (render_stats их при нуле вовсе не печатает)."""
    assert seeded.recalls_sent > 0, "приглашений на повторный визит нет"
    assert seeded.recalls_returned > 0, "без конверсии строка теряет смысл"
    assert seeded.recalls_saved > 0, "у возвратов должна быть сумма"
    assert seeded.reviews_count > 0, "оценок нет — строка ⭐ не отрендерится"
    assert 3.5 <= seeded.reviews_avg < 5.0, \
        f"средняя {seeded.reviews_avg}: ровная пятёрка читается как выдумка"


def test_render_shows_recall_and_review_sections(seeded):
    today = datetime.now(TZ).date()
    out = render_stats(seeded, today - timedelta(days=6), today)

    assert "🔁" in out and "⭐" in out, out


def test_journals_stay_in_the_past(app_session_factory, admin_engine,
                                   priced_clinic):
    """Строка журнала из будущего — такая же ложь, как будущий приём:
    «спросили оценку завтра» владелец увидит в первой же выгрузке.

    Сравниваем с ИНЖЕКТИРОВАННЫМ «сейчас», как тест записей: сид считает
    моменты журналов от времени приёма, и на плавающем now() разница в
    секунды ничего не доказывала бы. День берём рабочий — в воскресенье у
    фикстурных врачей смен нет."""
    clinic_a = priced_clinic
    day = datetime.now(TZ).date()
    while day.weekday() == 6:
        day -= timedelta(days=1)
    now = datetime.combine(day, time(15, 0), TZ)
    seed_demo_history(app_session_factory, clinic_a, days=14, now=now)

    with admin_engine.begin() as conn:
        rows, ahead = conn.execute(text(
            "SELECT (SELECT count(*) FROM recall_outreach) "
            "     + (SELECT count(*) FROM review), "
            "       (SELECT count(*) FROM recall_outreach "
            "         WHERE sent_at > :now OR booked_at > :now) "
            "     + (SELECT count(*) FROM review "
            "         WHERE requested_at > :now OR rated_at > :now)"),
            {"now": now}).one()

    assert rows > 0, "журналы вообще не посеяны — проверять нечего"
    assert ahead == 0, "момент журнала уехал в будущее"


def test_some_review_requests_stay_unanswered(app_session_factory, admin_engine,
                                              priced_clinic):
    """Молчание — не двойка: часть просьб остаётся без оценки, и в среднюю
    такие строки не входят вовсе (stats.collect_stats). Журнал, где ответили
    поголовно все, читается как выдумка ровно так же, как средняя 5.0."""
    seed_demo_history(app_session_factory, priced_clinic, days=14)

    with admin_engine.begin() as conn:
        asked, silent = conn.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE rating IS NULL) "
            "FROM review")).one()

    assert asked > 0, "просьб об оценке нет вовсе"
    assert 0 < silent < asked, f"промолчали {silent} из {asked}"


def test_clear_removes_the_journals_too(app_session_factory, admin_engine,
                                        priced_clinic):
    """FK на запись у журналов намеренно нет (0025, 0026): удали откат
    сначала приёмы — найти их строки было бы уже нечем, и возвраты с оценками
    остались бы в базе навсегда, а витрина считала бы приглашения к
    несуществующим приёмам."""
    from navbat.demo_history import clear_demo_history

    clinic_a = priced_clinic
    seed_demo_history(app_session_factory, clinic_a, days=14)
    before = _seeded_counts(admin_engine)

    clear_demo_history(app_session_factory, clinic_a)

    left = _seeded_counts(admin_engine)
    assert before[1] and before[2], "чистить нечего — журналы не посеяны"
    assert left[1] == 0 and left[2] == 0, f"в журналах осталось: {left}"


def test_repeat_visits_are_apart_by_formula():
    """Инвариант «сосед по времени ≠ то же лицо» обязан держаться формулой,
    а не удачной раскладкой дня.

    Тест выше судит по реальной раскладке, а она зависит от календаря прогона:
    какие дни окна выпали на воскресенье, сколько записей влезло в сегодня,
    как легли приёмы разной длительности. С прежней формулой (один чат у
    записей created и created+3) он краснел примерно через прогон, а в иные
    дни недели оставался зелёным на сломанном коде — то есть один он инвариант
    не стережёт. Здесь проверяем сам запас расстояния, детерминированно."""
    from navbat.demo_history import NEIGHBOUR_SPAN, _chat_index

    visits: dict[int, list[int]] = {}
    for created in range(300):  # с запасом: две недели дают под сотню записей
        visits.setdefault(_chat_index(created), []).append(created)
    repeated = [when for when in visits.values() if len(when) > 1]

    assert repeated, "вернувшихся нет — витрина показывает только новых"
    assert len(repeated) < len(visits), "новых лиц нет — все записи повторные"
    for when in repeated:
        assert len(when) == 2, f"одно лицо ходит больше двух раз: {when}"
        first, second = when
        assert second - first > NEIGHBOUR_SPAN, \
            f"повтор {second} может встать вплотную к оригиналу {first}"
        assert _chat_index(first) == first, \
            f"донор {first} сам повтор — живого первого визита за ним нет"
