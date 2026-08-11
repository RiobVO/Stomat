# Self-service инкремент 2 — врачи, услуги, расписание, выходные (план реализации)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Владелец клиники управляет врачами, услугами, расписанием и выходными кнопками прямо в админ-чате Telegram, без CLI и без захода на сервер.

**Architecture:** Расширяем существующий `AdminConsole` (`telegram/admin_console.py`) — те же паттерны: reply-меню верхнего уровня (точный матч label'ов до NLU), inline-разделы с сырым префиксом `adm:` (мимо tg_actions-map), pending-ввод в `conversation.context.extras`. Мутации — через функции `onboard.py`. «Удаление» врача/услуги = деактивация (`is_active`), физическое удаление только для никогда-не-использованных (FK RESTRICT). Контракт `is_active` живёт в слое данных (репозитории), чтобы деактивированный врач/услуга гарантированно исчезали из записи/слотов.

**Tech Stack:** Python, PostgreSQL (RLS, exclusion constraint), SQLAlchemy Core (`text()`), Alembic, pytest против реального postgres (:5434).

**Спека:** `docs/superpowers/specs/2026-06-14-self-service-increment-2-design.md`

**Порядок:** P-1 (фундамент) обязателен первым — P-2/P-3 зависят от `is_active` и onboard-функций. P-4 независим, но идёт последним по простоте.

**Соглашение по коммитам (правило проекта):** автор/коммиттер — `dejavuu <95645082+RiobVO@users.noreply.github.com>`; никаких упоминаний ассистента/AI/Co-Authored-By; префиксы `feat`/`fix`/`refactor`/`chore`/`docs`.

**Перед «готово» каждого P:** полный `python -m pytest` (серия не нужна — конкурентных тестов в этом инкременте нет, но прогон обязателен) + `python -m navbat --check`.

---

## P-1 — Фундамент: миграция 0021 + контракт is_active + onboard-функции

**Цель P-1:** в БД появляется `is_active`; репозитории по умолчанию отдают только активных; деактивированный врач/услуга исчезают из записи и слотов; есть `onboard`-функции для всех мутаций. UI ещё нет — всё проверяется через репозитории/движок.

**File Structure:**
- Create: `migrations/versions/0021_doctor_service_active.py`
- Modify: `src/navbat/dialog/doctors_repo.py` (фильтр + `doctor_list_all`)
- Modify: `src/navbat/dialog/services_repo.py` (фильтр + `service_list_all`)
- Modify: `src/navbat/onboard.py` (новые функции)
- Modify: `src/navbat/stats.py:218`, `src/navbat/supervisor.py:178-179` (сырой `FROM doctor`/`FROM service` → `WHERE is_active`)
- Test: `tests/test_self_service_repos.py` (новый), `tests/test_onboard_self_service.py` (новый)

### Task 1.1: Миграция 0021 — колонки is_active

**Files:**
- Create: `migrations/versions/0021_doctor_service_active.py`

- [ ] **Step 1: Написать миграцию** (образец — `0017_faq_topics.py`)

```python
"""Self-service инкремент 2: деактивация врачей и услуг.

«Удалить врача/услугу» при наличии записей невозможно (FK RESTRICT на
appointment/waitlist), поэтому «удаление» = деактивация. is_active=false:
скрыт из записи и слотов, прошлые/будущие записи и /stats целы.
DEFAULT true — существующие строки остаются активными (бэкфилл не нужен).

Revision ID: 0021
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE doctor  ADD COLUMN is_active boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE service ADD COLUMN is_active boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE service DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE doctor  DROP COLUMN IF EXISTS is_active")
```

- [ ] **Step 2: Применить миграцию и проверить идемпотентность/реверс**

Run:
```bash
python -m alembic -c alembic.ini upgrade head
python -m alembic -c alembic.ini downgrade -1
python -m alembic -c alembic.ini upgrade head
```
Expected: все три проходят без ошибок; после финального upgrade колонки `doctor.is_active`/`service.is_active` существуют.

- [ ] **Step 3: Подтвердить колонку SQL-запросом**

Run:
```bash
python -c "from sqlalchemy import create_engine, text; e=create_engine('postgresql+psycopg://postgres:navbat_dev@localhost:5434/navbat'); import sys; c=e.connect(); ok=c.execute(text(\"SELECT count(*) FROM information_schema.columns WHERE table_name IN ('doctor','service') AND column_name='is_active'\")).scalar_one(); print('[OK]' if ok==2 else '[FAIL]', ok); sys.exit(0 if ok==2 else 1)"
```
Expected: `[OK] 2`

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0021_doctor_service_active.py
git commit -m "feat(db): is_active on doctor and service (migration 0021)"
```

### Task 1.2: Фильтр is_active в doctors_repo + doctor_list_all

**Files:**
- Modify: `src/navbat/dialog/doctors_repo.py`
- Test: `tests/test_self_service_repos.py`

- [ ] **Step 1: Написать падающий тест**

```python
"""Контракт is_active: деактивированный врач/услуга исчезают из записи и
слотов; *_all-варианты видят всех. Репозитории — единственная точка фильтра."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from conftest import WORKING_INTERVALS, make_doctor, make_service
from navbat.db.base import tenant_transaction
from navbat.dialog import doctors_repo, services_repo


def _deactivate_doctor(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": doctor_id})


def test_doctor_list_hides_inactive(app_session_factory, admin_engine, clinic_a):
    active = make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        ids = [row[0] for row in doctors_repo.doctor_list(session)]
    assert active in ids and hidden not in ids


def test_working_intervals_hides_inactive(app_session_factory, admin_engine,
                                          clinic_a):
    make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        schedules = doctors_repo.working_intervals(session)
    assert len(schedules) == 1  # только активный врач формирует окно клиники


def test_doctor_list_all_shows_inactive(app_session_factory, admin_engine,
                                        clinic_a):
    active = make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        rows = {r.id: r for r in doctors_repo.doctor_list_all(session)}
    assert rows[active].is_active is True and rows[hidden].is_active is False
    assert rows[active].name == "Akmal"  # имя расшифровано
```

- [ ] **Step 2: Запустить — убедиться что падает**

Run: `python -m pytest tests/test_self_service_repos.py -q`
Expected: FAIL (`doctor_list` отдаёт обоих; `doctor_list_all` не существует).

- [ ] **Step 3: Реализовать фильтр и doctor_list_all**

В `src/navbat/dialog/doctors_repo.py` — добавить `WHERE is_active` в существующие и новую функцию:

```python
def working_intervals(session: Session) -> list:
    """working_intervals активных врачей клиники (для open_bounds/слотов)."""
    return list(session.execute(
        text("SELECT working_intervals FROM doctor WHERE is_active")
    ).scalars().all())


def doctor_list(session: Session) -> list[tuple[uuid.UUID, str | None]]:
    """(id, имя) активных врачей клиники, по id — для записи и слотов."""
    rows = session.execute(
        text("SELECT id, name_encrypted FROM doctor WHERE is_active ORDER BY id")
    ).all()
    return [
        (row.id, decrypt_text(row.name_encrypted) if row.name_encrypted else None)
        for row in rows
    ]


def doctor_list_all(session: Session) -> list:
    """ВСЕ врачи (вкл. деактивированных) для админ-консоли: namedtuple-строки
    с расшифрованным именем (id, name, working_intervals, buffer_min,
    gcal_calendar_id, is_active)."""
    from collections import namedtuple

    Doc = namedtuple(
        "Doc", "id name working_intervals buffer_min gcal_calendar_id is_active")
    rows = session.execute(
        text("SELECT id, name_encrypted, working_intervals, buffer_min, "
             "gcal_calendar_id, is_active FROM doctor ORDER BY is_active DESC, id")
    ).all()
    return [
        Doc(r.id,
            decrypt_text(r.name_encrypted) if r.name_encrypted else None,
            r.working_intervals, r.buffer_min, r.gcal_calendar_id, r.is_active)
        for r in rows
    ]
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_self_service_repos.py -q`
Expected: 3 теста про врачей PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/dialog/doctors_repo.py tests/test_self_service_repos.py
git commit -m "feat(dialog): doctors_repo filters is_active, adds doctor_list_all"
```

### Task 1.3: Фильтр is_active в services_repo + service_list_all

**Files:**
- Modify: `src/navbat/dialog/services_repo.py`
- Test: `tests/test_self_service_repos.py`

- [ ] **Step 1: Дописать падающие тесты**

```python
def _deactivate_service(admin_engine, clinic_id, name):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE service SET is_active = false "
                 "WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name})


def test_service_keys_and_resolve_hide_inactive(app_session_factory,
                                                admin_engine, clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30)
    make_service(admin_engine, clinic_a, "braces", 60)
    _deactivate_service(admin_engine, clinic_a, "braces")

    with tenant_transaction(app_session_factory, clinic_a) as session:
        keys = services_repo.service_keys(session)
        braces_id = services_repo.service_id(session, "braces")
        prices = {r.name for r in services_repo.price_list(session)}
    assert keys == ["cleaning"]
    assert braces_id is None  # деактивированную услугу не записать
    assert prices == {"cleaning"}


def test_service_list_all_shows_inactive(app_session_factory, admin_engine,
                                         clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350000)
    make_service(admin_engine, clinic_a, "braces", 60)
    _deactivate_service(admin_engine, clinic_a, "braces")

    with tenant_transaction(app_session_factory, clinic_a) as session:
        rows = {r.name: r for r in services_repo.service_list_all(session)}
    assert rows["cleaning"].is_active is True
    assert rows["braces"].is_active is False
    assert rows["cleaning"].duration_min == 30
    assert rows["cleaning"].price == 350000
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_self_service_repos.py -q`
Expected: FAIL на новых тестах (`service_keys` отдаёт braces; `service_list_all` нет).

- [ ] **Step 3: Реализовать фильтр и service_list_all**

В `src/navbat/dialog/services_repo.py`:

```python
def service_id(session: Session, key: str) -> uuid.UUID | None:
    return session.execute(
        text("SELECT id FROM service WHERE name = :name AND is_active "
             "ORDER BY name LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()


def service_keys(session: Session) -> list[str]:
    """Ключи активных услуг клиники, по алфавиту (для кнопок выбора)."""
    return list(session.execute(
        text("SELECT name FROM service WHERE is_active ORDER BY name")
    ).scalars().all())


def price_list(session: Session) -> list[Row]:
    """(name, price) активных услуг по алфавиту; price может быть NULL."""
    return list(session.execute(
        text("SELECT name, price FROM service WHERE is_active ORDER BY name")
    ).all())


def service_price(session: Session, key: str) -> int | None:
    return session.execute(
        text("SELECT price FROM service WHERE name = :name AND is_active LIMIT 1"),
        {"name": key},
    ).scalar_one_or_none()


def service_list_all(session: Session) -> list[Row]:
    """ВСЕ услуги (вкл. деактивированные) для админ-консоли:
    (name, duration_min, price, is_active), по алфавиту."""
    return list(session.execute(
        text("SELECT name, duration_min, price, is_active FROM service "
             "ORDER BY name")
    ).all())
```

Примечание: `service_name(session, sid)` НЕ фильтруем — резолв id→имя для уже существующей записи должен работать и для деактивированной услуги (история приёмов).

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_self_service_repos.py -q`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/dialog/services_repo.py tests/test_self_service_repos.py
git commit -m "feat(dialog): services_repo filters is_active, adds service_list_all"
```

### Task 1.4: Деактивированный врач не отдаёт слотов (интеграция движка)

**Files:**
- Test: `tests/test_self_service_repos.py`

Это проверка сквозного контракта: фильтр в `doctor_list`/`working_intervals` уже стоит, тест фиксирует, что движок записи (`SchedulingEngine.find_free_slots` через `shared_helpers._collect_slots`) деактивированного врача не предлагает. `find_free_slots` принимает `doctor_id`, поэтому ключ — что деактивированного нет в списке-кандидатов.

- [ ] **Step 1: Написать тест на отсутствие слотов деактивированного**

```python
from datetime import timedelta

from conftest import next_monday
from navbat.dialog.shared_helpers import _BookingMixinSlots  # см. примечание


def test_deactivated_doctor_offers_no_slots(app_session_factory, admin_engine,
                                            clinic_a):
    from navbat.scheduling.engine import SchedulingEngine

    only = make_doctor(admin_engine, clinic_a, name="Akmal")
    svc = make_service(admin_engine, clinic_a, "cleaning", 30)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": only})

    engine = SchedulingEngine(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        candidates = doctors_repo.doctor_list(session)
    assert candidates == []  # кандидатов нет → слотов не будет

    day = next_monday()
    slots = engine.find_free_slots(only, svc, day)  # сам врач ещё «жив» по id
    assert slots, "движок по id видит слоты — фильтрация на уровне списка врачей"
```

Примечание исполнителю: `_BookingMixinSlots` импортировать НЕ нужно — строка-импорт удалена в реализации; этот тест проверяет ровно две вещи: (1) деактивированный не попадает в `doctor_list` (список-кандидатов записи), (2) сам движок по явному id слот считает — значит контракт держится именно фильтром списка, а не движком. Убери неиспользуемый импорт перед запуском.

- [ ] **Step 2: Запустить — зелено сразу** (фильтр уже в 1.2)

Run: `python -m pytest tests/test_self_service_repos.py::test_deactivated_doctor_offers_no_slots -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_self_service_repos.py
git commit -m "test(dialog): deactivated doctor drops out of booking candidates"
```

### Task 1.5: onboard-функции — длительность, переименование, буфер

**Files:**
- Modify: `src/navbat/onboard.py`
- Test: `tests/test_onboard_self_service.py`

- [ ] **Step 1: Написать падающие тесты**

```python
"""onboard-мутации для self-service инкремента 2 (вызываются из консоли)."""
from __future__ import annotations

from sqlalchemy import text

from conftest import make_doctor, make_service
from navbat import onboard
from navbat.crypto import decrypt_text


def _doctor_row(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT name_encrypted, buffer_min, is_active "
                 "FROM doctor WHERE id = :d"), {"d": doctor_id}).one()


def _service_row(admin_engine, clinic_id, name):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT duration_min, is_active FROM service "
                 "WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name}).one()


def test_set_service_duration(app_session_factory, admin_engine, clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30)
    onboard.set_service_duration(app_session_factory, clinic_a, "cleaning", 45)
    assert _service_row(admin_engine, clinic_a, "cleaning").duration_min == 45


def test_rename_doctor_encrypts(app_session_factory, admin_engine, clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    onboard.rename_doctor(app_session_factory, clinic_a, did, "Akmal Karimov")
    row = _doctor_row(admin_engine, did)
    assert decrypt_text(row.name_encrypted) == "Akmal Karimov"


def test_set_doctor_buffer(app_session_factory, admin_engine, clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal", buffer_min=10)
    onboard.set_doctor_buffer(app_session_factory, clinic_a, did, 20)
    assert _doctor_row(admin_engine, did).buffer_min == 20
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_onboard_self_service.py -q`
Expected: FAIL (функций нет).

- [ ] **Step 3: Реализовать** (добавить в `onboard.py` рядом с `set_doctor_schedule`)

```python
def set_service_duration(session_factory, clinic_id: uuid.UUID, name: str,
                         duration_min: int) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE service SET duration_min = :d WHERE name = :n "
                 "RETURNING id"),
            {"d": duration_min, "n": name},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"услуга {name!r} не найдена в клинике {clinic_id}")


def rename_doctor(session_factory, clinic_id: uuid.UUID, doctor_id: uuid.UUID,
                  name: str) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE doctor SET name_encrypted = :name WHERE id = :d "
                 "RETURNING id"),
            {"name": encrypt_text(name), "d": doctor_id},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"врач {doctor_id} не найден в клинике {clinic_id}")


def set_doctor_buffer(session_factory, clinic_id: uuid.UUID,
                      doctor_id: uuid.UUID, buffer_min: int) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE doctor SET buffer_min = :b WHERE id = :d RETURNING id"),
            {"b": buffer_min, "d": doctor_id},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"врач {doctor_id} не найден в клинике {clinic_id}")
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_onboard_self_service.py -q`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/onboard.py tests/test_onboard_self_service.py
git commit -m "feat(onboard): set_service_duration, rename_doctor, set_doctor_buffer"
```

### Task 1.6: onboard — активация/деактивация и удаление

**Files:**
- Modify: `src/navbat/onboard.py`
- Test: `tests/test_onboard_self_service.py`

Деактивация услуги гасит активные waitlist-записи по ней (иначе матчер шлёт слоты по снятой услуге). Удаление — только при нуле FK-ссылок.

- [ ] **Step 1: Написать падающие тесты**

```python
import uuid

from conftest import next_monday, at_tashkent


def _waitlist_status(admin_engine, wid):
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT status FROM waitlist WHERE id = :i"),
                            {"i": wid}).scalar_one()


def _add_waitlist(admin_engine, clinic_id, service_id, chat=555):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO waitlist (clinic_id, service_id, tg_chat_id) "
                 "VALUES (:c, :s, :chat) RETURNING id"),
            {"c": clinic_id, "s": service_id, "chat": chat}).scalar_one()


def test_deactivate_then_activate_doctor(app_session_factory, admin_engine,
                                         clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    onboard.deactivate_doctor(app_session_factory, clinic_a, did)
    assert _doctor_row(admin_engine, did).is_active is False
    onboard.activate_doctor(app_session_factory, clinic_a, did)
    assert _doctor_row(admin_engine, did).is_active is True


def test_deactivate_service_cancels_waitlist(app_session_factory, admin_engine,
                                             clinic_a):
    sid = make_service(admin_engine, clinic_a, "cleaning", 30)
    wid = _add_waitlist(admin_engine, clinic_a, sid)
    onboard.deactivate_service(app_session_factory, clinic_a, "cleaning")
    assert _service_row(admin_engine, clinic_a, "cleaning").is_active is False
    assert _waitlist_status(admin_engine, wid) == "cancelled"


def test_delete_unused_doctor_ok(app_session_factory, admin_engine, clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Botir")
    onboard.delete_doctor(app_session_factory, clinic_a, did)
    with admin_engine.begin() as conn:
        gone = conn.execute(text("SELECT 1 FROM doctor WHERE id = :d"),
                            {"d": did}).scalar_one_or_none()
    assert gone is None


def test_delete_referenced_doctor_blocked(app_session_factory, admin_engine,
                                          clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    sid = make_service(admin_engine, clinic_a, "cleaning", 30)
    day = next_monday()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (clinic_id, doctor_id, service_id, "
                 "status, time_range) VALUES (:c, :d, :s, 'booked', "
                 "tstzrange(:lo, :hi))"),
            {"c": clinic_a, "d": did, "s": sid,
             "lo": at_tashkent(day, "09:00"), "hi": at_tashkent(day, "09:30")})
    try:
        onboard.delete_doctor(app_session_factory, clinic_a, did)
        assert False, "удаление врача с записью должно быть запрещено"
    except ValueError as exc:
        assert "запис" in str(exc).lower()
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_onboard_self_service.py -q`
Expected: FAIL (функций нет).

- [ ] **Step 3: Реализовать** (добавить в `onboard.py`)

```python
def deactivate_doctor(session_factory, clinic_id: uuid.UUID,
                      doctor_id: uuid.UUID) -> None:
    _set_doctor_active(session_factory, clinic_id, doctor_id, False)


def activate_doctor(session_factory, clinic_id: uuid.UUID,
                    doctor_id: uuid.UUID) -> None:
    _set_doctor_active(session_factory, clinic_id, doctor_id, True)


def _set_doctor_active(session_factory, clinic_id, doctor_id, active) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE doctor SET is_active = :a WHERE id = :d RETURNING id"),
            {"a": active, "d": doctor_id},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"врач {doctor_id} не найден в клинике {clinic_id}")


def deactivate_service(session_factory, clinic_id: uuid.UUID, name: str) -> None:
    """Снять услугу с продажи: is_active=false + погасить активные записи
    листа ожидания по ней (иначе матчер шлёт слоты по снятой услуге)."""
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE service SET is_active = false WHERE name = :n "
                 "RETURNING id"),
            {"n": name},
        ).scalar_one_or_none()
        if updated is None:
            raise ValueError(f"услуга {name!r} не найдена в клинике {clinic_id}")
        session.execute(
            text("UPDATE waitlist SET status = 'cancelled' "
                 "WHERE service_id = :s AND status IN ('waiting', 'notified')"),
            {"s": updated},
        )


def activate_service(session_factory, clinic_id: uuid.UUID, name: str) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE service SET is_active = true WHERE name = :n "
                 "RETURNING id"),
            {"n": name},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"услуга {name!r} не найдена в клинике {clinic_id}")


def delete_doctor(session_factory, clinic_id: uuid.UUID,
                  doctor_id: uuid.UUID) -> None:
    """Физически удалить врача — ТОЛЬКО если на него нет ни одной записи
    (FK RESTRICT). Иначе ValueError — нужно деактивировать."""
    with tenant_transaction(session_factory, clinic_id) as session:
        refs = session.execute(
            text("SELECT count(*) FROM appointment WHERE doctor_id = :d"),
            {"d": doctor_id},
        ).scalar_one()
        if refs:
            raise ValueError(
                f"у врача есть записи ({refs}) — удалить нельзя, деактивируйте")
        deleted = session.execute(
            text("DELETE FROM doctor WHERE id = :d RETURNING id"),
            {"d": doctor_id},
        ).scalar_one_or_none()
    if deleted is None:
        raise ValueError(f"врач {doctor_id} не найден в клинике {clinic_id}")


def delete_service(session_factory, clinic_id: uuid.UUID, name: str) -> None:
    """Физически удалить услугу — ТОЛЬКО если нет ссылок из appointment и
    waitlist. Иначе ValueError."""
    with tenant_transaction(session_factory, clinic_id) as session:
        sid = session.execute(
            text("SELECT id FROM service WHERE name = :n"), {"n": name},
        ).scalar_one_or_none()
        if sid is None:
            raise ValueError(f"услуга {name!r} не найдена в клинике {clinic_id}")
        refs = session.execute(
            text("SELECT (SELECT count(*) FROM appointment WHERE service_id = :s) "
                 "+ (SELECT count(*) FROM waitlist WHERE service_id = :s)"),
            {"s": sid},
        ).scalar_one()
        if refs:
            raise ValueError(
                f"на услугу есть записи ({refs}) — удалить нельзя, деактивируйте")
        session.execute(text("DELETE FROM service WHERE id = :s"), {"s": sid})
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_onboard_self_service.py -q`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/onboard.py tests/test_onboard_self_service.py
git commit -m "feat(onboard): activate/deactivate and delete doctor/service"
```

### Task 1.7: Сырые FROM doctor/service вне репозиториев → WHERE is_active

**Files:**
- Modify: `src/navbat/stats.py` (строка ~218)
- Modify: `src/navbat/supervisor.py` (строки ~178-179)

Консистентность: метрика «вне рабочих часов» и счётчики `--check` считают по активным.

- [ ] **Step 1: Правка stats.py**

В `src/navbat/stats.py` найти `text("SELECT working_intervals FROM doctor")` (~218) и заменить на:
```python
        text("SELECT working_intervals FROM doctor WHERE is_active")
```

- [ ] **Step 2: Правка supervisor.py**

В `src/navbat/supervisor.py` (~178-179) заменить два запроса:
```python
            doctors = session.execute(
                text("SELECT count(*) FROM doctor WHERE is_active")).scalar_one()
            services = session.execute(
                text("SELECT count(*) FROM service WHERE is_active")).scalar_one()
```

- [ ] **Step 3: Прогон затронутых тестов stats**

Run: `python -m pytest tests/test_stats.py -q`
Expected: PASS (демо/фикстуры активны по умолчанию — поведение не меняется).

- [ ] **Step 4: Commit**

```bash
git add src/navbat/stats.py src/navbat/supervisor.py
git commit -m "refactor(stats): count only active doctors/services"
```

### Task 1.8: Полный прогон P-1

- [ ] **Step 1: Весь сьют**

Run: `python -m pytest -q`
Expected: всё зелёное (включая старые тесты — фильтр is_active на демо/фикстурах прозрачен).

- [ ] **Step 2: Преддемо-чек**

Run: `python -m navbat --check`
Expected: все `[OK]`, ревизия миграций = head (0021).

---

## P-2 — Раздел «Услуги» (заменяет «Цены»)

**Цель P-2:** reply-кнопка `💰 Цены` → `💊 Услуги`; раздел показывает все услуги (активные + ⚪ скрытые), карточка услуги правит цену и длительность, деактивирует/активирует/удаляет, «➕ Добавить услугу» из каталога `SERVICE_KEYS`.

**File Structure:**
- Modify: `src/navbat/telegram/admin_console.py` (раздел услуг, новые callback-ветки)
- Test: `tests/test_admin_console.py` (обновить тесты «Цены» → «Услуги», добавить новые)

**Соглашение callback'ов раздела:** все `adm:`-префиксные (роутинг воркера уже их ловит — `worker.py:209`, менять воркер не нужно). Текущая услуга хранится в `extras["adm_svc"]` (ключ короткий). Схема:
- `adm:services` — список услуг
- `adm:svc:<key>` — карточка услуги (ставит `adm_svc`)
- `adm:price:<key>` — правка цены (существующая ветка, переиспуск)
- `adm:dur:<key>` — правка длительности (pending `dur:<key>`)
- `adm:svc:<key>:deact` / `adm:svc:<key>:act` — деактивация/активация
- `adm:svc:<key>:del` — удаление (если нет ссылок)
- `adm:svcadd` — список добавляемых канонических услуг
- `adm:svcadd:<key>` — начать добавление (pending `svcadd:<key>` для длительности)

### Task 2.1: Переименовать кнопку «Цены» → «Услуги», новый список

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Обновить существующие тесты + новый тест списка**

В `tests/test_admin_console.py` заменить упоминания `ac.BTN_PRICES` на `ac.BTN_SERVICES` в `test_admin_start_shows_admin_console` (строка 79). Добавить тест:

```python
def test_services_menu_lists_active_and_inactive(app_session_factory,
                                                 admin_engine, clinic_a):
    from conftest import make_service
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350000)
    braces = make_service(admin_engine, clinic_a, "braces", 60)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET is_active = false WHERE id = :s"),
                     {"s": braces})

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_SERVICES)

    rows = api.row_keyboards[-1]
    acts = actions(rows)
    assert "adm:svc:cleaning" in acts and "adm:svc:braces" in acts
    assert "adm:svcadd" in acts
    body = last_to(api, ADMIN_CHAT)
    assert "Услуги" in body
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -q`
Expected: FAIL (`BTN_SERVICES` нет; меню «Цены» не реагирует на `BTN_SERVICES`).

- [ ] **Step 3: Реализовать константу, меню и список**

В `admin_console.py`:
- Переименовать константу: `BTN_PRICES = "💰 Цены"` → `BTN_SERVICES = "💊 Услуги"`. Обновить `_MENU_LABELS` и `main_menu()` rows: первая строка `(BTN_SERVICES, BTN_ABOUT)`.
- В `_menu_action`: `if label == BTN_SERVICES: return self._services_menu()` (вместо `BTN_PRICES → _prices_menu`).
- Заменить `_prices_menu` на `_services_menu`:

```python
    def _services_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
        rows = []
        for row in rows_data:
            emoji = SERVICE_EMOJI.get(row.name, "🦷")
            label = SERVICE_LABELS.get(row.name, {}).get("ru", row.name)
            if row.is_active:
                price = f"{_fmt_sum(row.price)}" if row.price is not None else "—"
                text_btn = f"{emoji} {label} · {row.duration_min} мин · {price}"
            else:
                text_btn = f"⚪ {label} (скрыта)"
            rows.append((Button(text_btn, f"adm:svc:{row.name}"),))
        rows.append((Button("➕ Добавить услугу", "adm:svcadd"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}💊 <b>Услуги</b>\nВыберите услугу:",
                     button_rows=tuple(rows))
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -q`
Expected: PASS (новый тест + обновлённые старые; некоторые price-тесты ещё используют `adm:price:` — они продолжат работать, см. 2.2).

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): services section replaces prices, lists active+hidden"
```

### Task 2.2: Карточка услуги — цена, длительность, переходы

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тест карточки и длительности**

```python
def _service_field(admin_engine, clinic_id, name, field):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {field} FROM service WHERE clinic_id=:c AND name=:n"),
            {"c": clinic_id, "n": name}).scalar_one()


def test_service_card_shows_price_and_duration_buttons(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")
    acts = actions(api.row_keyboards[-1]) + actions(api.edited[-1][3] if api.edited else ())
    # карточка либо отправлена, либо отредактирована — берём последнюю поверхность
    rendered = api.edited[-1][3] if api.edited else api.row_keyboards[-1]
    acts = actions(rendered)
    assert "adm:price:cleaning" in acts
    assert "adm:dur:cleaning" in acts
    assert "adm:svc:cleaning:deact" in acts


def test_duration_edit_via_button_and_number(app_session_factory, admin_engine,
                                             clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dur:cleaning")
    send_admin(worker, app_session_factory, clinic_a, "45")
    assert _service_field(admin_engine, clinic_a, "cleaning", "duration_min") == 45
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_invalid_duration_rejected(app_session_factory, admin_engine, clinic_a,
                                   service_cleaning):
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dur:cleaning")
    for bad in ("0", "4", "500", "abc"):
        send_admin(worker, app_session_factory, clinic_a, bad)
        assert _service_field(admin_engine, clinic_a, "cleaning",
                              "duration_min") == 30
        assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "dur:cleaning"
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "service_card or duration" -q`
Expected: FAIL.

- [ ] **Step 3: Реализовать карточку, длительность, диспетч handle_callback/handle_text**

Добавить константу и методы; расширить `handle_callback` и `handle_text`.

Константа сверху файла:
```python
DURATION_MIN, DURATION_MAX = 5, 480
```

Карточка и правка длительности:
```python
    def _service_card(self, key: str, notice: str = "",
                      message_id: int | None = None,
                      chat_id: int | None = None) -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            row = next((r for r in services_repo.service_list_all(session)
                        if r.name == key), None)
        if row is None:
            return self._services_menu(notice="услуга не найдена")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        emoji = SERVICE_EMOJI.get(key, "🦷")
        price = f"{_fmt_sum(row.price)} сум" if row.price is not None else "не задана"
        toggle = (Button("✅ Активировать", f"adm:svc:{key}:act") if not row.is_active
                  else Button("⛔ Деактивировать", f"adm:svc:{key}:deact"))
        rows = [
            (Button("💰 Цена", f"adm:price:{key}"),
             Button("⏱ Длительность", f"adm:dur:{key}")),
            (toggle,),
        ]
        with tenant_transaction(self._sf, self._cid) as session:
            refs = self._service_refs(session, key)
        if not row.is_active and refs == 0:
            rows.append((Button("🗑 Удалить совсем", f"adm:svc:{key}:del"),))
        rows.append((Button("◀ Назад", "adm:services"),))
        head = f"{notice}\n\n" if notice else ""
        state = "" if row.is_active else " ⚪ <i>(скрыта)</i>"
        body = (f"{head}{emoji} <b>{_esc(label)}</b>{state}\n"
                f"Длительность: {row.duration_min} мин · Цена: {price}")
        reply = Reply(body, button_rows=tuple(rows))
        if message_id is not None and chat_id is not None:
            self._worker._edit(chat_id, message_id, reply)
        return reply

    @staticmethod
    def _service_refs(session, key: str) -> int:
        return session.execute(
            text("SELECT (SELECT count(*) FROM appointment a JOIN service s "
                 "ON s.id = a.service_id WHERE s.name = :n) "
                 "+ (SELECT count(*) FROM waitlist w JOIN service s "
                 "ON s.id = w.service_id WHERE s.name = :n)"),
            {"n": key},
        ).scalar_one()

    def _begin_duration_edit(self, chat_id, key, message_id) -> None:
        self._set_pending(chat_id, f"dur:{key}")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        reply = Reply(
            f"⏱ <b>{_esc(label)}</b>\nВведите длительность в минутах "
            f"({DURATION_MIN}–{DURATION_MAX}), например 30.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_duration(self, chat_id, key, raw) -> Reply:
        value = raw.strip()
        if not value.isdigit() or not DURATION_MIN <= int(value) <= DURATION_MAX:
            return Reply(
                f"⚠️ Длительность — целое {DURATION_MIN}–{DURATION_MAX} минут.\n"
                f"Введите ещё раз или нажмите «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        onboard.set_service_duration(self._sf, self._cid, key, int(value))
        self._clear_pending(chat_id)
        return self._service_card(key, notice=f"✅ Длительность: {value} мин")
```

Изменить `_apply_price` чтобы возвращал карточку услуги:
```python
        return self._service_card(key, notice=f"✅ Цена «{_esc(label)}»: "
                                              f"{_fmt_sum(price)} сум")
```

Расширить `handle_callback` — заменить тело на диспетчер:
```python
    def handle_callback(self, callback: dict, chat_id: int, data: str) -> None:
        self._api.answer_callback_query(callback["id"])
        message_id = callback["message"].get("message_id")
        body = data[len("adm:"):]
        if body in ("home", "cancel"):
            self._clear_pending(chat_id)
            self._worker._send(chat_id, self.main_menu())
            return
        if body == "services":
            self._worker._send(chat_id, self._services_menu())
            return
        if body == "svcadd":
            self._worker._send(chat_id, self._service_add_menu())
            return
        kind, _, arg = body.partition(":")
        if kind == "price":
            self._begin_price_edit(chat_id, arg, message_id)
            return
        if kind == "dur":
            self._begin_duration_edit(chat_id, arg, message_id)
            return
        if kind == "faq":
            self._begin_faq_edit(chat_id, arg, message_id)
            return
        if kind == "svc":
            self._handle_svc_callback(chat_id, arg, message_id)
            return
        if kind == "svcadd":
            self._begin_service_add(chat_id, arg, message_id)
            return

    def _handle_svc_callback(self, chat_id, arg, message_id) -> None:
        key, _, action = arg.partition(":")
        if action == "":
            self._service_card(key, message_id=message_id, chat_id=chat_id)
            return
        if action == "deact":
            onboard.deactivate_service(self._sf, self._cid, key)
            self._service_card(key, notice="⛔ Услуга скрыта",
                               message_id=message_id, chat_id=chat_id)
            return
        if action == "act":
            onboard.activate_service(self._sf, self._cid, key)
            self._service_card(key, notice="✅ Услуга снова доступна",
                               message_id=message_id, chat_id=chat_id)
            return
        if action == "del":
            try:
                onboard.delete_service(self._sf, self._cid, key)
            except ValueError as exc:
                self._service_card(key, notice=f"⚠️ {_esc(str(exc))}",
                                   message_id=message_id, chat_id=chat_id)
                return
            self._worker._send(chat_id, self._services_menu(
                notice="🗑 Услуга удалена"))
```

Расширить `handle_text` pending-ветки (после блока faq):
```python
            if kind == "dur":
                return self._apply_duration(chat_id, arg, stripped)
            if kind == "svcadd":
                return self._apply_service_add(chat_id, arg, stripped)
```

Примечание: `_service_add_menu`/`_begin_service_add`/`_apply_service_add` реализуются в 2.3 — пока добавь временные заглушки, возвращающие `self._services_menu()`, чтобы файл импортировался; в 2.3 заменишь.

Заглушки на время:
```python
    def _service_add_menu(self, notice: str = "") -> Reply:
        return self._services_menu()

    def _begin_service_add(self, chat_id, key, message_id) -> None:
        self._worker._send(chat_id, self._services_menu())

    def _apply_service_add(self, chat_id, key, raw) -> Reply:
        return self._services_menu()
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "service_card or duration" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): service card edits price/duration, deactivate/delete"
```

### Task 2.3: Добавление услуги из каталога

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тест добавления**

```python
def test_service_add_lists_only_missing_catalog(app_session_factory,
                                                admin_engine, clinic_a,
                                                service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svcadd")
    acts = actions(api.row_keyboards[-1] or (api.edited[-1][3] if api.edited else ()))
    assert "adm:svcadd:cleaning" not in acts   # уже есть
    assert "adm:svcadd:braces" in acts          # из каталога, ещё нет


def test_service_add_creates_with_duration(app_session_factory, admin_engine,
                                           clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svcadd:braces")
    send_admin(worker, app_session_factory, clinic_a, "60")
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT duration_min, is_active FROM service "
                 "WHERE clinic_id = :c AND name = 'braces'"),
            {"c": clinic_a}).one()
    assert row.duration_min == 60 and row.is_active is True
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "service_add" -q`
Expected: FAIL (заглушки возвращают список услуг, не каталог добавления).

- [ ] **Step 3: Заменить заглушки реализацией**

Импорт каталога сверху файла: `from navbat.nlu.schema import SERVICE_KEYS`.

```python
    def _service_add_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            existing = {r.name for r in services_repo.service_list_all(session)}
        missing = [k for k in SERVICE_KEYS if k not in existing]
        rows = [
            (Button(f"{SERVICE_EMOJI.get(k, '🦷')} "
                    f"{SERVICE_LABELS.get(k, {}).get('ru', k)}", f"adm:svcadd:{k}"),)
            for k in missing
        ]
        rows.append((Button("◀ Назад", "adm:services"),))
        head = f"{notice}\n\n" if notice else ""
        body = (f"{head}➕ <b>Добавить услугу</b>\nВыберите услугу из каталога:"
                if missing else f"{head}Все услуги каталога уже добавлены.")
        return Reply(body, button_rows=tuple(rows))

    def _begin_service_add(self, chat_id, key, message_id) -> None:
        if key not in SERVICE_KEYS:
            self._worker._send(chat_id, self._service_add_menu(
                notice="услуга не из каталога"))
            return
        self._set_pending(chat_id, f"svcadd:{key}")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        reply = Reply(
            f"➕ <b>{_esc(label)}</b>\nВведите длительность приёма в минутах "
            f"({DURATION_MIN}–{DURATION_MAX}), например 30.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_service_add(self, chat_id, key, raw) -> Reply:
        value = raw.strip()
        if not value.isdigit() or not DURATION_MIN <= int(value) <= DURATION_MAX:
            return Reply(
                f"⚠️ Длительность — целое {DURATION_MIN}–{DURATION_MAX} минут.\n"
                f"Введите ещё раз или нажмите «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        try:
            onboard.add_service(self._sf, self._cid, key, int(value))
        except ValueError as exc:
            self._clear_pending(chat_id)
            return self._services_menu(notice=f"⚠️ {_esc(str(exc))}")
        self._clear_pending(chat_id)
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        return self._service_card(key, notice=f"✅ Услуга «{_esc(label)}» добавлена")
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "service_add" -q`
Expected: PASS.

- [ ] **Step 5: Полный прогон test_admin_console + commit**

Run: `python -m pytest tests/test_admin_console.py -q`
Expected: всё PASS.
```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): add service from canonical catalog"
```

---

## P-3 — Раздел «Врачи» (CRUD + расписание)

**Цель P-3:** reply-кнопка `👨‍⚕️ Врачи` → список (активные + ⚪) + «➕ Добавить»; карточка врача (имя, буфер, график, деактивация/удаление); поток расписания (шаблоны + «Свой график» текстом).

**File Structure:**
- Modify: `src/navbat/telegram/admin_console.py` (раздел врачей, рендер расписания, парсер смен)
- Test: `tests/test_admin_console.py`

**Callback-схема:** текущий врач — в `extras["adm_doc"]` (uuid-строка). Выбор дней «Своего графика» — в `extras["adm_sch_days"]` (список ключей).
- `adm:doctors` — список
- `adm:doc:<uuid>` — карточка (ставит `adm_doc`)
- `adm:doc:name` / `adm:doc:buffer` — правка (pending `dname`/`dbuf`, врач из `adm_doc`)
- `adm:doc:deact` / `adm:doc:act` / `adm:doc:del`
- `adm:docadd` — начать добавление (pending `dadd:name`)
- `adm:sched` — меню расписания (шаблоны)
- `adm:sched:tpl:<n>` — применить шаблон n
- `adm:sched:custom` — выбор дней
- `adm:sched:day:<key>` — toggle дня
- `adm:sched:next` — перейти к вводу смен (pending `sched`)

### Task 3.1: Рендер расписания и парсер смен (чистые функции)

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тесты чистых функций**

```python
def test_format_schedule_groups_consecutive_days():
    wi = {"mon": [["09:00", "13:00"], ["14:00", "18:00"]],
          "tue": [["09:00", "13:00"], ["14:00", "18:00"]],
          "wed": [["09:00", "13:00"], ["14:00", "18:00"]],
          "thu": [["09:00", "13:00"], ["14:00", "18:00"]],
          "fri": [["09:00", "13:00"], ["14:00", "18:00"]],
          "sat": [["09:00", "13:00"]]}
    out = ac._format_schedule(wi)
    assert "Пн–Пт 09:00–13:00, 14:00–18:00" in out
    assert "Сб 09:00–13:00" in out
    assert "Вс" not in out  # выходной не показываем


def test_format_schedule_empty():
    assert ac._format_schedule({}) == "выходной всю неделю"


def test_parse_shifts_ok():
    assert ac._parse_shifts("09:00-13:00, 14:00-18:00") == \
        [["09:00", "13:00"], ["14:00", "18:00"]]


def test_parse_shifts_rejects_bad():
    for bad in ("9-18", "25:00-26:00", "13:00-09:00", "", "abc"):
        assert ac._parse_shifts(bad) is None
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "format_schedule or parse_shifts" -q`
Expected: FAIL.

- [ ] **Step 3: Реализовать чистые функции** (модульный уровень в `admin_console.py`)

```python
from navbat.scheduling.calendar_rules import WEEKDAY_KEYS

_DAY_RU = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
           "fri": "Пт", "sat": "Сб", "sun": "Вс"}


def _format_schedule(wi: dict) -> str:
    """working_intervals → «Пн–Пт 09:00–13:00, 14:00–18:00; Сб 09:00–13:00».
    Последовательные дни с одинаковыми сменами схлопываются; выходные опущены."""
    groups: list[tuple[list[str], tuple]] = []
    for key in WEEKDAY_KEYS:
        spans = (wi or {}).get(key) or []
        if not spans:
            continue
        sig = tuple(tuple(s) for s in spans)
        prev_idx = WEEKDAY_KEYS.index(groups[-1][0][-1]) if groups else -2
        if groups and groups[-1][1] == sig and WEEKDAY_KEYS.index(key) == prev_idx + 1:
            groups[-1][0].append(key)
        else:
            groups.append(([key], sig))
    if not groups:
        return "выходной всю неделю"
    parts = []
    for days, sig in groups:
        label = (_DAY_RU[days[0]] if len(days) == 1
                 else f"{_DAY_RU[days[0]]}–{_DAY_RU[days[-1]]}")
        hours = ", ".join(f"{a}–{b}" for a, b in sig)
        parts.append(f"{label} {hours}")
    return "; ".join(parts)


def _parse_shifts(raw: str) -> list[list[str]] | None:
    """«09:00-13:00, 14:00-18:00» → [["09:00","13:00"],["14:00","18:00"]].
    Требует HH:MM-HH:MM, начало < конца. None — кривой ввод."""
    spans: list[list[str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, sep, end = chunk.partition("-")
        if not sep:
            return None
        start, end = start.strip(), end.strip()
        try:
            if onboard._parse_hhmm(start) >= onboard._parse_hhmm(end):
                return None
        except ValueError:
            return None
        spans.append([start, end])
    return spans or None
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "format_schedule or parse_shifts" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): schedule render and shift-text parser"
```

### Task 3.2: Список и карточка врача, top-меню кнопка «Врачи»

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тесты списка и карточки**

```python
def test_doctors_menu_lists_active_and_hidden(app_session_factory, admin_engine,
                                              clinic_a):
    from conftest import make_doctor
    make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active=false WHERE id=:d"),
                     {"d": hidden})

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_DOCTORS)

    acts = actions(api.row_keyboards[-1])
    assert f"adm:doc:{make_doctor.__name__ and ''}" or True  # см. ниже
    assert any(a == f"adm:doc:{hidden}" for a in acts)
    assert "adm:docadd" in acts
    body = last_to(api, ADMIN_CHAT)
    assert "Врачи" in body


def test_doctor_card_shows_actions(app_session_factory, admin_engine, clinic_a):
    from conftest import make_doctor
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{did}")

    rendered = api.edited[-1][3] if api.edited else api.row_keyboards[-1]
    acts = actions(rendered)
    assert "adm:doc:name" in acts and "adm:doc:buffer" in acts
    assert "adm:sched" in acts and "adm:doc:deact" in acts
    body = (api.edited[-1][2] if api.edited else last_to(api, ADMIN_CHAT))
    assert "Akmal" in body
    # текущий врач запомнен в extras
    assert context_of(admin_engine, ADMIN_CHAT)["adm_doc"] == str(did)
```

Примечание исполнителю: в первом тесте используй реальный uuid возвращаемого `make_doctor` (присвой переменной `active = make_doctor(...)`) и проверяй `f"adm:doc:{active}"` в `acts`. Псевдо-строку из примера замени.

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "doctors_menu or doctor_card" -q`
Expected: FAIL (`BTN_DOCTORS` нет, разделов нет).

- [ ] **Step 3: Реализовать кнопку, список, карточку, extras-хелперы**

Константа: `BTN_DOCTORS = "👨‍⚕️ Врачи"`; добавить в `_MENU_LABELS`; `main_menu()` rows вторая строка `(BTN_SERVICES, BTN_DOCTORS)` — пересобрать раскладку:
```python
        rows = ((BTN_SERVICES, BTN_DOCTORS), (BTN_ABOUT,), (BTN_STATS,),
                (pause_btn,))
```
В `_menu_action`: `if label == BTN_DOCTORS: return self._doctors_menu()`.

extras-хелперы (рядом с `_set_pending`):
```python
    def _set_extra(self, chat_id: int, key: str, value) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            conv.context.extras[key] = value
            save_conversation(session, conv)

    def _get_extra(self, chat_id: int, key: str):
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
        return conv.context.extras.get(key)
```

Список и карточка:
```python
    def _doctors_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            docs = doctors_repo.doctor_list_all(session)
        rows = []
        for d in docs:
            name = d.name or "(без имени)"
            if d.is_active:
                sched = _format_schedule(d.working_intervals)
                short = sched if len(sched) <= 32 else sched[:31] + "…"
                rows.append((Button(f"{name} · {short}", f"adm:doc:{d.id}"),))
            else:
                rows.append((Button(f"⚪ {name} (скрыт)", f"adm:doc:{d.id}"),))
        rows.append((Button("➕ Добавить врача", "adm:docadd"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}👨‍⚕️ <b>Врачи</b>\nВыберите врача:",
                     button_rows=tuple(rows))

    def _doctor_card(self, chat_id: int, doctor_id: str, notice: str = "",
                     message_id: int | None = None) -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            d = next((x for x in doctors_repo.doctor_list_all(session)
                      if str(x.id) == doctor_id), None)
            refs = self._doctor_refs(session, doctor_id) if d else 0
        if d is None:
            return self._doctors_menu(notice="врач не найден")
        self._set_extra(chat_id, "adm_doc", doctor_id)
        name = d.name or "(без имени)"
        cal = "привязан" if d.gcal_calendar_id else "не привязан"
        toggle = (Button("✅ Активировать", "adm:doc:act") if not d.is_active
                  else Button("⛔ Деактивировать", "adm:doc:deact"))
        rows = [
            (Button("✏️ Имя", "adm:doc:name"), Button("🕐 График", "adm:sched")),
            (Button("⏱ Буфер", "adm:doc:buffer"), toggle),
        ]
        if not d.is_active and refs == 0:
            rows.append((Button("🗑 Удалить совсем", "adm:doc:del"),))
        rows.append((Button("◀ Назад", "adm:doctors"),))
        state = "" if d.is_active else " ⚪ <i>(скрыт)</i>"
        head = f"{notice}\n\n" if notice else ""
        body = (f"{head}👨‍⚕️ <b>{_esc(name)}</b>{state}\n"
                f"График: {_esc(_format_schedule(d.working_intervals))}\n"
                f"Буфер: {d.buffer_min} мин · Календарь: {cal}")
        reply = Reply(body, button_rows=tuple(rows))
        if message_id is not None:
            self._worker._edit(chat_id, message_id, reply)
        return reply

    @staticmethod
    def _doctor_refs(session, doctor_id: str) -> int:
        return session.execute(
            text("SELECT count(*) FROM appointment WHERE doctor_id = :d"),
            {"d": doctor_id},
        ).scalar_one()
```

Расширить `handle_callback` диспетчер ветками (перед финальным `return`):
```python
        if body == "doctors":
            self._worker._send(chat_id, self._doctors_menu())
            return
        if body == "docadd":
            self._begin_doctor_add(chat_id, message_id)
            return
        if kind == "doc":
            self._handle_doc_callback(chat_id, arg, message_id)
            return
        if kind == "sched":
            self._handle_sched_callback(chat_id, arg, message_id)
            return
```

`_handle_doc_callback`:
```python
    def _handle_doc_callback(self, chat_id, arg, message_id) -> None:
        # arg: "<uuid>" | "name" | "buffer" | "deact" | "act" | "del"
        if arg in ("name", "buffer", "deact", "act", "del"):
            doctor_id = self._get_extra(chat_id, "adm_doc")
            if not doctor_id:
                self._worker._send(chat_id, self._doctors_menu())
                return
            if arg == "name":
                self._set_pending(chat_id, "dname")
                self._edit_or_send(chat_id, message_id, Reply(
                    "✏️ Введите имя врача.",
                    button_rows=((Button("✖ Отмена", "adm:cancel"),),)))
                return
            if arg == "buffer":
                self._set_pending(chat_id, "dbuf")
                self._edit_or_send(chat_id, message_id, Reply(
                    "⏱ Введите буфер между приёмами в минутах (0–120).",
                    button_rows=((Button("✖ Отмена", "adm:cancel"),),)))
                return
            if arg == "deact":
                onboard.deactivate_doctor(self._sf, self._cid, doctor_id)
                self._doctor_card(chat_id, doctor_id, notice="⛔ Врач скрыт",
                                  message_id=message_id)
                return
            if arg == "act":
                onboard.activate_doctor(self._sf, self._cid, doctor_id)
                self._doctor_card(chat_id, doctor_id, notice="✅ Врач снова в записи",
                                  message_id=message_id)
                return
            if arg == "del":
                try:
                    onboard.delete_doctor(self._sf, self._cid, doctor_id)
                except ValueError as exc:
                    self._doctor_card(chat_id, doctor_id,
                                      notice=f"⚠️ {_esc(str(exc))}",
                                      message_id=message_id)
                    return
                self._worker._send(chat_id, self._doctors_menu(
                    notice="🗑 Врач удалён"))
                return
        # arg = uuid → карточка
        self._doctor_card(chat_id, arg, message_id=message_id)
```

Расширить `handle_text` pending-ветки:
```python
            if kind == "dname":
                return self._apply_doctor_name(chat_id, stripped)
            if kind == "dbuf":
                return self._apply_doctor_buffer(chat_id, stripped)
```

```python
    def _apply_doctor_name(self, chat_id, raw) -> Reply:
        doctor_id = self._get_extra(chat_id, "adm_doc")
        value = raw.strip()
        if not value or len(value) > 100:
            return Reply("⚠️ Имя — непустой текст до 100 символов.",
                         button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        onboard.rename_doctor(self._sf, self._cid, doctor_id, value)
        self._clear_pending(chat_id)
        return self._doctor_card(chat_id, doctor_id, notice="✅ Имя обновлено")

    def _apply_doctor_buffer(self, chat_id, raw) -> Reply:
        doctor_id = self._get_extra(chat_id, "adm_doc")
        value = raw.strip()
        if not value.isdigit() or not 0 <= int(value) <= 120:
            return Reply("⚠️ Буфер — целое 0–120 минут.",
                         button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        onboard.set_doctor_buffer(self._sf, self._cid, doctor_id, int(value))
        self._clear_pending(chat_id)
        return self._doctor_card(chat_id, doctor_id, notice="✅ Буфер обновлён")
```

Примечание: `_begin_doctor_add` и `_handle_sched_callback` реализуются в 3.3/3.4 — добавь временные заглушки (`self._worker._send(chat_id, self._doctors_menu())`), чтобы файл импортировался.

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "doctors_menu or doctor_card" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): doctors section — list, card, rename, buffer, toggle"
```

### Task 3.3: Поток расписания — шаблоны и свой график

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тесты шаблона и своего графика**

```python
def _doctor_wi(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT working_intervals FROM doctor "
                                 "WHERE id = :d"), {"d": doctor_id}).scalar_one()


def test_schedule_template_applies(app_session_factory, admin_engine, clinic_a):
    from conftest import make_doctor
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{did}")  # ставит adm_doc
    click(worker, app_session_factory, clinic_a, "adm:sched:tpl:0")

    wi = _doctor_wi(admin_engine, did)
    assert wi["mon"] == [["09:00", "18:00"]]
    assert "sat" not in wi  # шаблон 0 — Пн–Пт


def test_custom_schedule_days_then_shifts(app_session_factory, admin_engine,
                                          clinic_a):
    from conftest import make_doctor
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{did}")
    click(worker, app_session_factory, clinic_a, "adm:sched:custom")
    click(worker, app_session_factory, clinic_a, "adm:sched:day:mon")
    click(worker, app_session_factory, clinic_a, "adm:sched:day:tue")
    click(worker, app_session_factory, clinic_a, "adm:sched:next")
    send_admin(worker, app_session_factory, clinic_a, "10:00-14:00")

    wi = _doctor_wi(admin_engine, did)
    assert wi == {"mon": [["10:00", "14:00"]], "tue": [["10:00", "14:00"]]}
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_custom_schedule_bad_shifts_repeats(app_session_factory, admin_engine,
                                            clinic_a):
    from conftest import make_doctor
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{did}")
    click(worker, app_session_factory, clinic_a, "adm:sched:custom")
    click(worker, app_session_factory, clinic_a, "adm:sched:day:mon")
    click(worker, app_session_factory, clinic_a, "adm:sched:next")
    send_admin(worker, app_session_factory, clinic_a, "ерунда")

    assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "sched"
    body = last_to(api, ADMIN_CHAT)
    assert "⚠️" in body
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "schedule_template or custom_schedule" -q`
Expected: FAIL.

- [ ] **Step 3: Реализовать поток расписания**

Шаблоны (модульный уровень):
```python
_SCHEDULE_TEMPLATES = (
    ("Пн–Пт 09:00–18:00",
     {d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}),
    ("Пн–Сб 09–13 / 14–18",
     {d: [["09:00", "13:00"], ["14:00", "18:00"]]
      for d in ("mon", "tue", "wed", "thu", "fri", "sat")}),
    ("Пн–Пт 10:00–19:00",
     {d: [["10:00", "19:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}),
)
```

Заменить заглушку `_handle_sched_callback`:
```python
    def _handle_sched_callback(self, chat_id, arg, message_id) -> None:
        doctor_id = self._get_extra(chat_id, "adm_doc")
        if not doctor_id:
            self._worker._send(chat_id, self._doctors_menu())
            return
        if arg == "":
            self._sched_menu(chat_id, message_id)
            return
        kind, _, rest = arg.partition(":")
        if kind == "tpl":
            label, schedule = _SCHEDULE_TEMPLATES[int(rest)]
            onboard.set_doctor_schedule(self._sf, self._cid, doctor_id, schedule)
            self._doctor_card(chat_id, doctor_id,
                              notice=f"✅ График: {label}", message_id=message_id)
            return
        if kind == "custom":
            self._set_extra(chat_id, "adm_sch_days", [])
            self._sched_days(chat_id, message_id)
            return
        if kind == "day":
            days = list(self._get_extra(chat_id, "adm_sch_days") or [])
            if rest in days:
                days.remove(rest)
            else:
                days.append(rest)
            self._set_extra(chat_id, "adm_sch_days", days)
            self._sched_days(chat_id, message_id)
            return
        if kind == "next":
            days = self._get_extra(chat_id, "adm_sch_days") or []
            if not days:
                self._sched_days(chat_id, message_id, notice="Выберите хотя бы один день")
                return
            self._set_pending(chat_id, "sched")
            self._edit_or_send(chat_id, message_id, Reply(
                "🕐 Введите часы работы для выбранных дней, например:\n"
                "<code>09:00-13:00, 14:00-18:00</code>",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),)))

    def _sched_menu(self, chat_id, message_id) -> None:
        rows = [(Button(label, f"adm:sched:tpl:{i}"),)
                for i, (label, _) in enumerate(_SCHEDULE_TEMPLATES)]
        rows.append((Button("✏️ Свой график", "adm:sched:custom"),))
        rows.append((Button("◀ Назад", f"adm:doc:{self._get_extra(chat_id, 'adm_doc')}"),))
        self._edit_or_send(chat_id, message_id, Reply(
            "🕐 <b>График</b>\nВыберите шаблон или задайте свой:",
            button_rows=tuple(rows)))

    def _sched_days(self, chat_id, message_id, notice: str = "") -> None:
        selected = set(self._get_extra(chat_id, "adm_sch_days") or [])
        day_buttons = [
            Button(("✓" if k in selected else "") + _DAY_RU[k], f"adm:sched:day:{k}")
            for k in WEEKDAY_KEYS
        ]
        rows = [tuple(day_buttons[:4]), tuple(day_buttons[4:]),
                (Button("Далее ▶", "adm:sched:next"),),
                (Button("✖ Отмена", "adm:cancel"),)]
        head = f"{notice}\n\n" if notice else ""
        self._edit_or_send(chat_id, message_id, Reply(
            f"{head}🕐 Отметьте рабочие дни (тап переключает):",
            button_rows=tuple(rows)))
```

`handle_text` pending-ветка `sched`:
```python
            if kind == "sched":
                return self._apply_custom_schedule(chat_id, stripped)
```

```python
    def _apply_custom_schedule(self, chat_id, raw) -> Reply:
        spans = _parse_shifts(raw)
        if spans is None:
            return Reply(
                "⚠️ Формат: <code>09:00-13:00, 14:00-18:00</code> "
                "(время HH:MM, начало раньше конца). Повторите или «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        doctor_id = self._get_extra(chat_id, "adm_doc")
        days = self._get_extra(chat_id, "adm_sch_days") or []
        schedule = {day: spans for day in days}
        onboard.set_doctor_schedule(self._sf, self._cid, doctor_id, schedule)
        self._clear_pending(chat_id)
        return self._doctor_card(chat_id, doctor_id, notice="✅ График обновлён")
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "schedule_template or custom_schedule" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): doctor schedule via templates and custom shift text"
```

### Task 3.4: Добавление врача

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

Поток: «➕ Добавить врача» → имя (текст) → создаётся врач с дефолтным графиком (`onboard.WORKING_INTERVALS`) и буфером 10 → открывается карточка (там владелец сразу правит график). Это держит поток коротким; график настраивается готовым разделом.

- [ ] **Step 1: Тест добавления**

```python
def test_doctor_add_creates_with_name(app_session_factory, admin_engine, clinic_a):
    from navbat.crypto import decrypt_text
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:docadd")
    send_admin(worker, app_session_factory, clinic_a, "Dilnoza opa")

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT name_encrypted, buffer_min, is_active FROM doctor "
                 "WHERE clinic_id = :c"), {"c": clinic_a}).one()
    assert decrypt_text(row.name_encrypted) == "Dilnoza opa"
    assert row.buffer_min == 10 and row.is_active is True
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "doctor_add" -q`
Expected: FAIL (заглушка).

- [ ] **Step 3: Заменить заглушку `_begin_doctor_add` + ветка применения**

```python
    def _begin_doctor_add(self, chat_id, message_id) -> None:
        self._set_pending(chat_id, "dadd")
        self._edit_or_send(chat_id, message_id, Reply(
            "➕ <b>Новый врач</b>\nВведите имя врача.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),)))
```

`handle_text` pending-ветка `dadd` (до общих):
```python
            if kind == "dadd":
                return self._apply_doctor_add(chat_id, stripped)
```

```python
    def _apply_doctor_add(self, chat_id, raw) -> Reply:
        value = raw.strip()
        if not value or len(value) > 100:
            return Reply("⚠️ Имя — непустой текст до 100 символов.",
                         button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        doctor_id = onboard.add_doctor(self._sf, self._cid, value)
        self._clear_pending(chat_id)
        return self._doctor_card(chat_id, str(doctor_id),
                                 notice="✅ Врач добавлен. Настройте график.")
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "doctor_add" -q`
Expected: PASS.

- [ ] **Step 5: Полный test_admin_console + commit**

Run: `python -m pytest tests/test_admin_console.py -q`
Expected: всё PASS.
```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): add doctor with name, opens card for schedule setup"
```

---

## P-4 — Раздел «Выходные»

**Цель P-4:** reply-кнопка `📅 Выходные` → список ближайших закрытых дней (тап = снова открыть) + «➕ Закрыть день» (ввод `ДД.ММ [причина]`). Логика переиспускает `holiday`-таблицу и `_parse_ddmm` из воркера.

**File Structure:**
- Modify: `src/navbat/telegram/admin_console.py` (раздел выходных)
- Test: `tests/test_admin_console.py`

**Доступ к парсеру даты:** `UpdateWorker._parse_ddmm` — `@staticmethod`. Консоль зовёт `self._worker._parse_ddmm(raw, today)`; `today` — через `self._worker._clinic_today()`.

**Callback-схема:**
- `adm:dayoff` — список
- `adm:dayoff:open:<iso>` — снова открыть дату (YYYY-MM-DD)
- `adm:dayoff:add` — начать ввод (pending `dayoff`)

### Task 4.1: Список выходных + повторное открытие

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тесты**

```python
def _add_holiday(admin_engine, clinic_id, iso, reason=None):
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO holiday (clinic_id, date, reason) "
                 "VALUES (:c, :d, :r)"),
            {"c": clinic_id, "d": iso, "r": reason})


def _holiday_count(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM holiday WHERE clinic_id=:c"),
                            {"c": clinic_id}).scalar_one()


def test_dayoff_menu_lists_and_reopen(app_session_factory, admin_engine, clinic_a):
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=10)).isoformat()
    _add_holiday(admin_engine, clinic_a, future, "Праздник")

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_DAYOFF)
    acts = actions(api.row_keyboards[-1])
    assert f"adm:dayoff:open:{future}" in acts
    assert "adm:dayoff:add" in acts

    click(worker, app_session_factory, clinic_a, f"adm:dayoff:open:{future}")
    assert _holiday_count(admin_engine, clinic_a) == 0
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "dayoff_menu" -q`
Expected: FAIL (`BTN_DAYOFF` нет).

- [ ] **Step 3: Реализовать кнопку, список, открытие**

Константа: `BTN_DAYOFF = "📅 Выходные"`; в `_MENU_LABELS`; `main_menu()` rows вторая строка `(BTN_SERVICES, BTN_DOCTORS)`, новая строка `(BTN_ABOUT, BTN_DAYOFF)`:
```python
        rows = ((BTN_SERVICES, BTN_DOCTORS), (BTN_ABOUT, BTN_DAYOFF),
                (BTN_STATS,), (pause_btn,))
```
В `_menu_action`: `if label == BTN_DAYOFF: return self._dayoff_menu()`.

```python
    def _dayoff_menu(self, notice: str = "") -> Reply:
        today = self._worker._clinic_today()
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = session.execute(
                text("SELECT date, reason FROM holiday WHERE date >= :t "
                     "ORDER BY date LIMIT 20"), {"t": today},
            ).all()
        rows = []
        for r in rows_data:
            label = f"{r.date:%d.%m.%Y}" + (f" ({r.reason})" if r.reason else "")
            rows.append((Button(f"{label} ✖", f"adm:dayoff:open:{r.date.isoformat()}"),))
        rows.append((Button("➕ Закрыть день", "adm:dayoff:add"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        intro = ("Ближайшие закрытые дни (тап — снова открыть):"
                 if rows_data else "Закрытых дней впереди нет.")
        return Reply(f"{head}📅 <b>Выходные</b>\n{intro}",
                     button_rows=tuple(rows))
```

В `handle_callback` диспетчере:
```python
        if body == "dayoff":
            self._worker._send(chat_id, self._dayoff_menu())
            return
        if kind == "dayoff":
            self._handle_dayoff_callback(chat_id, arg, message_id)
            return
```

```python
    def _handle_dayoff_callback(self, chat_id, arg, message_id) -> None:
        sub, _, rest = arg.partition(":")
        if sub == "add":
            self._set_pending(chat_id, "dayoff")
            self._edit_or_send(chat_id, message_id, Reply(
                "📅 Введите дату и (по желанию) причину:\n"
                "<code>21.03 Навруз</code>",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),)))
            return
        if sub == "open":
            with tenant_transaction(self._sf, self._cid) as session:
                session.execute(text("DELETE FROM holiday WHERE date = :d"),
                                {"d": rest})
            self._worker._send(chat_id, self._dayoff_menu(
                notice="✅ День снова рабочий"))
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "dayoff_menu" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): day-off section lists closed days and reopens"
```

### Task 4.2: Закрытие дня вводом даты

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Тесты**

```python
def test_dayoff_add_closes_day(app_session_factory, admin_engine, clinic_a):
    from datetime import date, timedelta
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dayoff:add")
    target = date.today() + timedelta(days=5)
    send_admin(worker, app_session_factory, clinic_a,
               f"{target.day:02d}.{target.month:02d} Учёт")

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT date, reason FROM holiday WHERE clinic_id = :c"),
            {"c": clinic_a}).one()
    assert row.reason == "Учёт"
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_dayoff_add_bad_date_repeats(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dayoff:add")
    send_admin(worker, app_session_factory, clinic_a, "ерунда")

    assert _holiday_count(admin_engine, clinic_a) == 0
    assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "dayoff"
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "dayoff_add" -q`
Expected: FAIL (pending-ветки `dayoff` нет).

- [ ] **Step 3: Реализовать ветку применения**

`handle_text` pending-ветка `dayoff`:
```python
            if kind == "dayoff":
                return self._apply_dayoff(chat_id, stripped)
```

```python
    def _apply_dayoff(self, chat_id, raw) -> Reply:
        parts = raw.split(maxsplit=1)
        today = self._worker._clinic_today()
        target = self._worker._parse_ddmm(parts[0], today) if parts else None
        if target is None:
            return Reply(
                "⚠️ Формат: <code>21.03 причина</code> (день.месяц). "
                "Повторите или «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        reason = parts[1].strip() if len(parts) > 1 else None
        with tenant_transaction(self._sf, self._cid) as session:
            exists = session.execute(
                text("SELECT 1 FROM holiday WHERE date = :d"), {"d": target},
            ).scalar_one_or_none()
            if not exists:
                session.execute(
                    text("INSERT INTO holiday (clinic_id, date, reason) VALUES "
                         "(current_setting('app.clinic_id')::uuid, :d, :r)"),
                    {"d": target, "r": reason})
        self._clear_pending(chat_id)
        return self._dayoff_menu(notice=f"✅ {target:%d.%m.%Y} — выходной")
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py -k "dayoff_add" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): close day via date text in day-off section"
```

---

## Финал: сквозная верификация

### Task F.1: Полный сьют + чек + демо

- [ ] **Step 1: Весь сьют**

Run: `python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 2: Преддемо-чек**

Run: `python -m navbat --check`
Expected: все `[OK]`, ревизия миграций = head (0021).

- [ ] **Step 3: Восстановить демо-клинику** (pytest TRUNCATE'ит базу)

Run: `python -m navbat.onboard --demo`
Expected: `[OK] демо-клиника`.

### Task F.2: Верификационный драйвер ([OK]/[FAIL], exit-code)

**Files:**
- Create: `scripts/verify_self_service.py` (если каталога `scripts/` нет — положить в корень как `verify_self_service.py`, не коммитить рабочий артефакт по желанию; для приёмки достаточно прогона)

- [ ] **Step 1: Написать драйвер**

```python
"""Верификация self-service инкремента 2 на фейковой сессии воркера:
деактивация врача убирает его из записи; правка графика доезжает в БД;
услуга добавляется и удаляется. Печатает [OK]/[FAIL], код возврата 0/≠0."""
import sys

# переиспускаем тест-инфраструктуру
sys.path.insert(0, "tests")
import uuid
from sqlalchemy import text

from conftest import make_doctor, make_service  # noqa: E402
from navbat.db.base import (make_app_engine, make_session_factory,  # noqa: E402
                            tenant_transaction)
from navbat.dialog import doctors_repo  # noqa: E402
from navbat import onboard  # noqa: E402

ADMIN_DSN = "postgresql+psycopg://postgres:navbat_dev@localhost:5434/navbat"


def main() -> int:
    from sqlalchemy import create_engine
    admin = create_engine(ADMIN_DSN)
    cid = uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(text("INSERT INTO clinic (id, name, salt, timezone) VALUES "
                          "(:id, 'verify', 'salt', 'Asia/Tashkent')"), {"id": cid})
    did = make_doctor(admin, cid, name="Verify")
    sf = make_session_factory(make_app_engine())
    try:
        onboard.deactivate_doctor(sf, cid, did)
        with tenant_transaction(sf, cid) as s:
            if doctors_repo.doctor_list(s):
                print("[FAIL] деактивированный врач остался в записи"); return 1
        onboard.activate_doctor(sf, cid, did)
        onboard.set_doctor_schedule(sf, cid, did, {"mon": [["10:00", "14:00"]]})
        with admin.begin() as conn:
            wi = conn.execute(text("SELECT working_intervals FROM doctor "
                                   "WHERE id=:d"), {"d": did}).scalar_one()
        if wi != {"mon": [["10:00", "14:00"]]}:
            print(f"[FAIL] график не доехал: {wi}"); return 1
        onboard.add_service(sf, cid, "braces", 60)
        onboard.delete_service(sf, cid, "braces")
        print("[OK] self-service: деактивация, график, услуга — проверены")
        return 0
    finally:
        with admin.begin() as conn:
            conn.execute(text("DELETE FROM appointment WHERE clinic_id=:c"), {"c": cid})
            conn.execute(text("DELETE FROM service WHERE clinic_id=:c"), {"c": cid})
            conn.execute(text("DELETE FROM doctor WHERE clinic_id=:c"), {"c": cid})
            conn.execute(text("DELETE FROM clinic WHERE id=:c"), {"c": cid})
        admin.dispose()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Прогнать драйвер**

Run: `python verify_self_service.py`
Expected: `[OK] self-service: деактивация, график, услуга — проверены`, код 0.

Примечание: запускать ПОСЛЕ восстановления демо (драйвер сам чистит свою клинику; на демо не влияет). Если положил файл в корень и не коммитишь — добавь в `.gitignore` или просто удали после приёмки.

---

## Self-Review (выполнено при написании плана)

**Покрытие спеки:**
- Миграция 0021 + is_active → Task 1.1.
- Контракт is_active в репозиториях + `*_all` → Tasks 1.2, 1.3; интеграция «нет слотов» → 1.4; сырые запросы stats/supervisor → 1.7.
- onboard-функции (duration/rename/buffer/activate/deactivate/delete + waitlist-гашение) → 1.5, 1.6.
- Рестрактур меню «Цены»→«Услуги» + новые кнопки → 2.1 (Услуги), 3.2 (Врачи), 4.1 (Выходные).
- Раздел «Услуги» (цена/длительность/деактивация/удаление/добавление из каталога) → 2.1–2.3.
- Раздел «Врачи» (список/карточка/имя/буфер/деактивация/удаление/добавление) → 3.2, 3.4; расписание (шаблоны + свой график текстом, парсер, рендер) → 3.1, 3.3.
- Раздел «Выходные» (список/повторное открытие/закрытие вводом даты) → 4.1, 4.2.
- Границы (GCal-OAuth вне скоупа, русский, разные часы по дням вне скоупа, услуги только из каталога) — отражены: добавление врача без календаря (3.4), один набор смен на выбранные дни (3.3), добавление услуги только из `SERVICE_KEYS` (2.3).

**Плейсхолдеры:** временные заглушки в 2.2/3.2 помечены как заменяемые в 2.3/3.3/3.4 — это осознанный приём для импортируемости файла между задачами, не «TODO без кода».

**Согласованность типов/имён:** callback-префиксы (`adm:svc`, `adm:doc`, `adm:sched`, `adm:dayoff`, `adm:svcadd`), extras-ключи (`adm_doc`, `adm_sch_days`, `adm_pending`), pending-виды (`price`/`dur`/`faq`/`svcadd`/`dname`/`dbuf`/`dadd`/`sched`/`dayoff`) — сквозно одинаковы в callback-диспетчере и в `handle_text`. Воркер уже роутит весь `adm:` в консоль (`worker.py:209`) — изменений воркера не требуется.
