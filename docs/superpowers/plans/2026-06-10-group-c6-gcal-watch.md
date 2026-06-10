# C-6 GCal watch + мгновенный алерт OAuth-refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push-уведомления Google Calendar будят синк мгновенно (вместо ожидания интервала), протухший OAuth-токен алертится сразу (не после 3 циклов). Шестой инкремент по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md` (секция 6).

**Architecture:** `events.watch` per врач-календарь: канал открывает `GcalWatchManager` из календарного цикла супервизора (продление — новый канал заранее до expiration, Google продлевать не умеет). Push прилетает на существующий `WebhookServer` по пути `/gcal/push/<channel_id>` (nginx уже проксирует), валидируется по `doctor.gcal_channel_id` и взводит `threading.Event` — календарный цикл просыпается немедленно. Сбой watch (нет HTTPS, квота, неверифицированное приложение) — warning в лог, поллинг продолжает работать; watch включается только в webhook-режиме (`NAVBAT_WEBHOOK_URL` задан). `CalendarAuthError` в sync_loop — алерт сразу (admin + владелец через существующий `system_alert`), раз в день; порог 3 циклов остаётся для сети/5xx.

**Tech Stack:** stdlib (threading, uuid) + существующие httpx-клиент и паттерны тестов (MockTransport, фейковые api-объекты, живой HTTP на эфемерном порту).

**Контекст для исполнителя:**
- `src/navbat/calendar/api.py` — тонкий httpx-клиент; `_call()` строит URL от `BASE_URL`, `missing_ok=True` глотает 404. Тесты — `tests/test_gcal_api.py` (хелпер `make_api`).
- `src/navbat/calendar/sync_loop.py` — `CalendarSyncLoop.run_once()` ловит `Exception` per врач; `_record()` ведёт счётчик сбоев и `_alerted`. Тесты — `tests/test_calendar_sync_loop.py` (хелпер `_loop_with_calendar_doctor`, `RecordingNotifier` пишет `(chat_id, reason)` в `.calls`).
- `src/navbat/telegram/transport.py` — `WebhookServer` (stdlib ThreadingHTTPServer): тело вычитывается ДО ответа (иначе WinError 10053). Тесты — `tests/test_tg_transport.py` (живой HTTP, порт 0).
- `src/navbat/supervisor.py:270-281` — календарный поток (`calendar_loop`), `:296-312` — webhook-режим.
- Миграции — сырой SQL через `op.execute` (образец `migrations/versions/0011_gcal_last_sync.py`); последняя — 0014. Conftest сам делает `alembic upgrade head`.
- `deploy/nginx/templates/default.conf.template` уже проксирует `/gcal/push/` → app:8443 — НЕ трогать.
- Запуск тестов: `python -m pytest tests/<file> -q` (полный сьют — без параллелизма, общая БД :5434; Docker Desktop должен быть запущен).

---

### Task 1: API-методы `watch_events` / `stop_channel`

**Files:**
- Modify: `src/navbat/calendar/api.py` (после `free_busy`, строка ~98)
- Test: `tests/test_gcal_api.py`

- [x] **Step 1: падающие тесты** — в конец `tests/test_gcal_api.py`:

```python
# ── watch-каналы (C-6) ───────────────────────────────────────────────────────

def test_watch_events_posts_channel():
    api, requests = make_api(
        lambda req, reqs: httpx.Response(200, json={
            "resourceId": "RES1", "expiration": "1760000000000"}))
    result = api.watch_events(CAL, "CH-1", "https://x.uz/gcal/push/CH-1")

    call = api_calls(requests)[0]
    assert call.url.path.endswith("/events/watch")
    body = json.loads(call.content)
    assert body == {"id": "CH-1", "type": "web_hook",
                    "address": "https://x.uz/gcal/push/CH-1"}
    assert result["resourceId"] == "RES1"


def test_stop_channel_missing_is_ok():
    # канал уже умер у Google (404) — идемпотентность важнее строгости
    api, requests = make_api(lambda req, reqs: httpx.Response(404, json={}))
    api.stop_channel("CH-1", "RES1")
    call = api_calls(requests)[0]
    assert call.url.path.endswith("/channels/stop")
    assert json.loads(call.content) == {"id": "CH-1", "resourceId": "RES1"}
```

- [x] **Step 2: убедиться, что падают**

Run: `python -m pytest tests/test_gcal_api.py -q -k watch_events or stop_channel`
Expected: 2 failed, `AttributeError: ... has no attribute 'watch_events'`

- [x] **Step 3: реализация** — в `GoogleCalendarAPI` после `free_busy`:

```python
    def watch_events(self, calendar_id: str, channel_id: str, address: str) -> dict:
        """Push-канал на события календаря; Google вернёт resourceId+expiration."""
        return self._call("POST", f"/calendars/{calendar_id}/events/watch", json={
            "id": channel_id, "type": "web_hook", "address": address,
        })

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        # 404 — канал уже истёк: идемпотентность важнее строгости
        self._call("POST", "/channels/stop",
                   json={"id": channel_id, "resourceId": resource_id},
                   missing_ok=True)
```

- [x] **Step 4: зелёный прогон файла**

Run: `python -m pytest tests/test_gcal_api.py -q`
Expected: all passed

- [x] **Step 5: Commit** `feat(calendar): events.watch + channels.stop API methods`

---

### Task 2: миграция 0015 + `GcalWatchManager`

**Files:**
- Create: `migrations/versions/0015_gcal_watch.py`, `src/navbat/calendar/watch.py`
- Test: `tests/test_gcal_watch.py` (новый)

- [x] **Step 1: миграция** `migrations/versions/0015_gcal_watch.py`:

```python
"""C-6: push-каналы Google Calendar (events.watch) per врач.

Канал у Google не продлевается — открывается новый заранее до expiration;
id/resource/expiration храним, чтобы продлевать и валидировать входящие
push-уведомления по channel_id в URL.

Revision ID: 0015
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_channel_id text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_resource_id text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_channel_expires_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_channel_id")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_resource_id")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_channel_expires_at")
```

- [x] **Step 2: падающие тесты** — `tests/test_gcal_watch.py` целиком:

```python
"""Watch-каналы Google Calendar (C-6): открытие, продление, деградация.

Сбой watch НЕ критичен (поллинг прикрывает) — менеджер не бросает и не алертит.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from conftest import make_doctor
from navbat.calendar.api import CalendarAPIError
from navbat.calendar.watch import RENEW_LEAD, GcalWatchManager

BASE = "https://clinic.example.uz"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


class FakeWatchAPI:
    def __init__(self, fail: bool = False, expiration_ms: int | None = None) -> None:
        self.fail = fail
        self.expiration_ms = expiration_ms
        self.watch_calls: list[tuple[str, str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []

    def watch_events(self, calendar_id, channel_id, address):
        self.watch_calls.append((calendar_id, channel_id, address))
        if self.fail:
            raise CalendarAPIError("push недоступен (домен не верифицирован)")
        result = {"resourceId": f"RES-{len(self.watch_calls)}"}
        if self.expiration_ms is not None:
            result["expiration"] = str(self.expiration_ms)
        return result

    def stop_channel(self, channel_id, resource_id):
        self.stop_calls.append((channel_id, resource_id))


def _calendar_doctor(admin_engine, clinic_id, **fields):
    doctor_id = make_doctor(admin_engine, clinic_id)
    sets = ", ".join(f"{name} = :{name}" for name in fields)
    with admin_engine.begin() as conn:
        conn.execute(text(
            f"UPDATE doctor SET gcal_calendar_id = 'cal@x'"
            + (f", {sets}" if sets else "") + " WHERE id = :doctor_id"),
            {"doctor_id": doctor_id, **fields})
    return doctor_id


def _watch_row(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT gcal_channel_id, gcal_resource_id, gcal_channel_expires_at "
            "FROM doctor WHERE id = :d"), {"d": doctor_id}).one()


def _manager(app_session_factory, clinic_id, api):
    return GcalWatchManager(app_session_factory, clinic_id, api, BASE + "/",
                            clock=lambda: NOW)


def test_opens_channel_and_stores_fields(app_session_factory, admin_engine, clinic_a):
    doctor_id = _calendar_doctor(admin_engine, clinic_a)
    expiration = NOW + timedelta(days=7)
    api = FakeWatchAPI(expiration_ms=int(expiration.timestamp() * 1000))
    _manager(app_session_factory, clinic_a, api).ensure_channels()

    assert len(api.watch_calls) == 1
    calendar_id, channel_id, address = api.watch_calls[0]
    assert calendar_id == "cal@x"
    assert address == f"{BASE}/gcal/push/{channel_id}"
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id == channel_id
    assert row.gcal_resource_id == "RES-1"
    assert row.gcal_channel_expires_at == expiration


def test_fresh_channel_not_reopened(app_session_factory, admin_engine, clinic_a):
    _calendar_doctor(admin_engine, clinic_a,
                     gcal_channel_id="CH-OLD", gcal_resource_id="RES-OLD",
                     gcal_channel_expires_at=NOW + RENEW_LEAD + timedelta(hours=1))
    api = FakeWatchAPI()
    _manager(app_session_factory, clinic_a, api).ensure_channels()
    assert api.watch_calls == []
    assert api.stop_calls == []


def test_expiring_channel_renewed_and_old_stopped(app_session_factory, admin_engine,
                                                  clinic_a):
    doctor_id = _calendar_doctor(
        admin_engine, clinic_a,
        gcal_channel_id="CH-OLD", gcal_resource_id="RES-OLD",
        gcal_channel_expires_at=NOW + timedelta(hours=1))  # < RENEW_LEAD
    api = FakeWatchAPI(expiration_ms=int((NOW + timedelta(days=7)).timestamp() * 1000))
    _manager(app_session_factory, clinic_a, api).ensure_channels()

    assert len(api.watch_calls) == 1
    assert api.stop_calls == [("CH-OLD", "RES-OLD")]
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id != "CH-OLD"


def test_watch_failure_degrades_to_polling(app_session_factory, admin_engine,
                                           clinic_a):
    doctor_id = _calendar_doctor(admin_engine, clinic_a)
    api = FakeWatchAPI(fail=True)
    _manager(app_session_factory, clinic_a, api).ensure_channels()  # не бросает
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id is None  # ничего не записали — поллинг как раньше


def test_doctor_without_calendar_ignored(app_session_factory, admin_engine, clinic_a):
    make_doctor(admin_engine, clinic_a)  # gcal_calendar_id IS NULL
    api = FakeWatchAPI()
    _manager(app_session_factory, clinic_a, api).ensure_channels()
    assert api.watch_calls == []
```

- [x] **Step 3: убедиться, что падают**

Run: `python -m pytest tests/test_gcal_watch.py -q`
Expected: ошибка импорта `No module named 'navbat.calendar.watch'`

- [x] **Step 4: реализация** — `src/navbat/calendar/watch.py` целиком:

```python
"""Push-каналы Google Calendar (events.watch) per врач-календарь (C-6).

Google канал не продлевает — открываем новый заранее (RENEW_LEAD до
expiration) и останавливаем старый. channel_id (uuid4) служит и токеном
в URL push-уведомлений: /gcal/push/<channel_id>, валидация — по полю
doctor.gcal_channel_id. Сбой watch (нет HTTPS, квота, неверифицированное
приложение) — warning: периодический поллинг продолжает работать.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from navbat.calendar.api import CalendarAPIError
from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.calendar.watch")

RENEW_LEAD = timedelta(hours=12)  # новый канал открываем заранее до истечения
PUSH_PATH_PREFIX = "/gcal/push/"


class GcalWatchManager:
    def __init__(self, session_factory, clinic_id, api, public_base_url,
                 clock=lambda: datetime.now(timezone.utc)) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api
        self._base = public_base_url.rstrip("/")
        self._clock = clock

    def ensure_channels(self) -> None:
        """Открыть/продлить каналы всем врачам с календарём. Не бросает."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            rows = session.execute(text(
                "SELECT id, gcal_calendar_id, gcal_channel_id, gcal_resource_id, "
                "gcal_channel_expires_at FROM doctor "
                "WHERE gcal_calendar_id IS NOT NULL")).all()
        now = self._clock()
        for row in rows:
            if (row.gcal_channel_id and row.gcal_channel_expires_at is not None
                    and row.gcal_channel_expires_at - now > RENEW_LEAD):
                continue
            self._open_channel(row)

    def _open_channel(self, row) -> None:
        channel_id = str(uuid.uuid4())
        address = f"{self._base}{PUSH_PATH_PREFIX}{channel_id}"
        try:
            result = self._api.watch_events(row.gcal_calendar_id, channel_id,
                                            address)
        except CalendarAPIError as e:
            log.warning("watch %s не открыт (поллинг прикрывает): %s",
                        row.gcal_calendar_id, e)
            return
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                "UPDATE doctor SET gcal_channel_id = :channel, "
                "gcal_resource_id = :resource, gcal_channel_expires_at = :expires "
                "WHERE id = :id"),
                {"channel": channel_id, "resource": result.get("resourceId"),
                 "expires": _expiration(result), "id": row.id})
        log.info("watch-канал %s открыт для %s", channel_id, row.gcal_calendar_id)
        if row.gcal_channel_id and row.gcal_resource_id:
            try:
                self._api.stop_channel(row.gcal_channel_id, row.gcal_resource_id)
            except CalendarAPIError as e:
                log.warning("старый канал %s не остановлен: %s",
                            row.gcal_channel_id, e)


def _expiration(result: dict) -> datetime | None:
    raw = result.get("expiration")  # миллисекунды epoch, строкой
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
```

- [x] **Step 5: зелёный прогон**

Run: `python -m pytest tests/test_gcal_watch.py tests/test_gcal_api.py -q`
Expected: all passed

- [x] **Step 6: Commit** `feat(calendar): watch-channel manager + doctor channel fields (0015)`

---

### Task 3: push-endpoint `/gcal/push/<token>` на WebhookServer

**Files:**
- Modify: `src/navbat/telegram/transport.py`
- Test: `tests/test_tg_transport.py`

- [x] **Step 1: падающие тесты** — в конец `tests/test_tg_transport.py`:

```python
# ── C-6: push Google Calendar будит синк ─────────────────────────────────────

import threading

from conftest import make_doctor


def _gcal_doctor(admin_engine, clinic_id, channel_id="CH-1"):
    doctor_id = make_doctor(admin_engine, clinic_id)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET gcal_calendar_id = 'cal@x', "
                          "gcal_channel_id = :ch WHERE id = :d"),
                     {"ch": channel_id, "d": doctor_id})


def gcal_post(server, token) -> httpx.Response:
    return httpx.post(f"http://127.0.0.1:{server.port}/gcal/push/{token}",
                      headers={"X-Goog-Resource-State": "exists"})


def test_gcal_push_wakes_sync(app_session_factory, admin_engine, clinic_a):
    _gcal_doctor(admin_engine, clinic_a, channel_id="CH-1")
    wake = threading.Event()
    server = WebhookServer(app_session_factory, clinic_a, secret=SECRET,
                           host="127.0.0.1", port=0, gcal_wake=wake)
    server.start()
    try:
        assert gcal_post(server, "CH-1").status_code == 200
        assert wake.is_set()
    finally:
        server.stop()


def test_gcal_push_unknown_channel_404(app_session_factory, admin_engine, clinic_a):
    _gcal_doctor(admin_engine, clinic_a, channel_id="CH-1")
    wake = threading.Event()
    server = WebhookServer(app_session_factory, clinic_a, secret=SECRET,
                           host="127.0.0.1", port=0, gcal_wake=wake)
    server.start()
    try:
        assert gcal_post(server, "CH-STALE").status_code == 404
        assert not wake.is_set()
    finally:
        server.stop()


def test_gcal_push_without_calendar_404(app_session_factory, admin_engine, clinic_a):
    # календарь выключен: gcal_wake не передан — путь закрыт, telegram живёт
    server = webhook_server(app_session_factory, clinic_a)
    try:
        assert gcal_post(server, "CH-1").status_code == 404
        assert post(server, tg_message(10)).status_code == 200
    finally:
        server.stop()
```

- [x] **Step 2: убедиться, что падают**

Run: `python -m pytest tests/test_tg_transport.py -q -k gcal_push`
Expected: 3 failed, `TypeError: ... unexpected keyword argument 'gcal_wake'`

- [x] **Step 3: реализация** в `transport.py`:

К импортам добавить `from sqlalchemy import text` (сейчас импортируются только `Session, sessionmaker` из `sqlalchemy.orm`). Константа рядом с `SECRET_HEADER`:

```python
GCAL_PUSH_PREFIX = "/gcal/push/"
```

`WebhookServer.__init__` — новый параметр `gcal_wake` (последним):

```python
    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, secret: str,
                 host: str = "0.0.0.0", port: int = 8443,
                 path: str | None = None,
                 gcal_wake: threading.Event | None = None) -> None:
```

и сохранить `self._gcal_wake = gcal_wake` (рядом с `self._session_factory = session_factory`, строка ~106).

В `Handler.do_POST` сразу после вычитки тела (до проверки `self.path != outer.path`):

```python
                if self.path.startswith(GCAL_PUSH_PREFIX):
                    # push Google: тело не парсим (X-Goog-* в заголовках),
                    # факт уведомления = «в календаре что-то поменялось»
                    self._respond(outer._gcal_push(
                        self.path[len(GCAL_PUSH_PREFIX):]))
                    return
```

Метод `WebhookServer` (после `stop`):

```python
    def _gcal_push(self, token: str) -> int:
        """Валидация по channel_id канала; неизвестный/протухший — 404
        (Google сам перестанет слать после expiration)."""
        if self._gcal_wake is None:
            return 404
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            known = session.execute(
                text("SELECT 1 FROM doctor WHERE gcal_channel_id = :token"),
                {"token": token}).scalar_one_or_none()
        if known is None:
            return 404
        self._gcal_wake.set()
        return 200
```

- [x] **Step 4: зелёный прогон файла**

Run: `python -m pytest tests/test_tg_transport.py -q`
Expected: all passed (включая старые webhook-тесты)

- [x] **Step 5: Commit** `feat(telegram): /gcal/push/<channel> endpoint wakes calendar sync`

---

### Task 4: мгновенный алерт `CalendarAuthError` в sync_loop

**Files:**
- Modify: `src/navbat/calendar/sync_loop.py`
- Test: `tests/test_calendar_sync_loop.py`

- [x] **Step 1: падающие тесты** — в конец `tests/test_calendar_sync_loop.py`:

```python
# ── C-6: OAuth-сбой алертится сразу (сам не чинится), раз в день ────────────

from navbat.calendar.api import CalendarAuthError


class _AuthDeadSync:
    def __init__(self) -> None:
        self.fail = True

    def sync_doctor(self, doctor_id) -> None:
        if self.fail:
            raise CalendarAuthError("refresh не удался (400): invalid_grant")


def test_auth_error_alerts_immediately(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()  # ПЕРВЫЙ же цикл — не ждём порога 3
    assert len(notifier.calls) == 1
    assert "переавторизац" in notifier.calls[0][1]


def test_auth_alert_once_per_day_no_threshold_duplicate(app_session_factory,
                                                        admin_engine, clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    for _ in range(FAILURE_ALERT_THRESHOLD + 2):
        loop.run_once()
    # один auth-алерт; генерический порог-алерт НЕ дублирует его
    assert len(notifier.calls) == 1


def test_auth_recovery_notifies_and_rearms(app_session_factory, admin_engine,
                                           clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()
    sync.fail = False
    loop.run_once()  # восстановление (переавторизовали)
    assert len(notifier.calls) == 2
    assert "восстановлена" in notifier.calls[1][1]
    sync.fail = True
    loop.run_once()  # умер снова в тот же день — алертим опять
    assert len(notifier.calls) == 3
```

- [x] **Step 2: убедиться, что падают**

Run: `python -m pytest tests/test_calendar_sync_loop.py -q -k auth`
Expected: 3 failed (алертов 0 или генерический вместо auth)

- [x] **Step 3: реализация** в `sync_loop.py`:

К импортам:

```python
from datetime import datetime, timezone

from navbat.calendar.api import CalendarAuthError
```

В `__init__` после `self._alerted = False`:

```python
        self._auth_alert_date = None  # дата последнего auth-алерта (раз в день)
```

`run_once` — auth-сбой ловится ДО генерического `Exception`:

```python
    def run_once(self) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            doctor_ids = list(session.execute(text(
                "SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL"
            )).scalars())
        failed = False
        auth_error: CalendarAuthError | None = None
        for doctor_id in doctor_ids:
            try:
                self._sync.sync_doctor(doctor_id)
            except CalendarAuthError as e:
                log.error("sync врача %s: OAuth мёртв: %s", doctor_id, e)
                failed = True
                auth_error = e
            except Exception:
                log.exception("sync врача %s упал", doctor_id)
                failed = True
        if auth_error is not None:
            self._alert_auth(auth_error)
        self._record(failed)
```

Новый метод после `run_once`:

```python
    def _alert_auth(self, error: CalendarAuthError) -> None:
        """Auth-сбой сам не чинится — алерт сразу, не после порога; раз в день."""
        today = datetime.now(timezone.utc).date()
        if self._auth_alert_date == today:
            return
        system_alert(
            self._notifier,
            f"Google OAuth-токен мёртв — синхронизация календаря остановилась "
            f"и сама не починится. Нужна переавторизация: "
            f"python -m navbat.calendar.auth. Ошибка: {str(error)[:200]}",
            {"error": str(error)[:200]},
            chat_id=self._admin_chat_id)
        self._auth_alert_date = today
        # генерический порог-алерт не дублируем; recovery-уведомление сработает
        self._alerted = True
```

В `_record`, в ветке восстановления (после `self._alerted = False`):

```python
        self._auth_alert_date = None  # новый auth-сбой в тот же день — алертим снова
```

- [x] **Step 4: зелёный прогон файла**

Run: `python -m pytest tests/test_calendar_sync_loop.py -q`
Expected: all passed (старые M5-тесты не задеты)

- [x] **Step 5: Commit** `feat(calendar): instant OAuth-refresh failure alert (daily, no threshold)`

---

### Task 5: wiring в супервизоре + финал инкремента

**Files:**
- Modify: `src/navbat/supervisor.py` (строки ~239-249 — сборка календаря; ~270-281 — calendar_loop; ~296-318 — webhook + finally)

Юнит-тестов на wiring нет (паттерн проекта: супервизор проверяется `--check` и smoke); вся логика покрыта Task 1-4.

- [x] **Step 1: wake-событие и watch-менеджер**

В блоке сборки календаря (`if gcal_token and not args.no_calendar:`, строка ~239) после создания `calendar_sync` добавить:

```python
        watch_manager = None
        if args.webhook_url:
            from navbat.calendar.watch import GcalWatchManager

            watch_manager = GcalWatchManager(session_factory, args.clinic,
                                             gcal_api, args.webhook_url)
            log.info("календарь: watch-каналы включены (push будит синк)")
```

а в ветке `else` (календарь выключен) — `watch_manager = None` не нужен: объявить `watch_manager = None` рядом с `slot_guard = None` / `calendar_sync = None` (строка ~232).

`sync_wake = threading.Event()` объявить рядом с `stop = threading.Event()`
(строка ~259, ДО блока `if calendar_sync is not None:`) — событие нужно
и календарному потоку, и WebhookServer'у.

`calendar_loop` (строка ~276) — будится push'ем, продлевает каналы:

```python
        def calendar_loop() -> None:
            while not stop.is_set():
                if watch_manager is not None:
                    try:
                        watch_manager.ensure_channels()
                    except Exception:
                        log.exception("watch-каналы: ensure_channels упал")
                sync_loop.run_once()
                sync_wake.wait(args.sync_interval)
                sync_wake.clear()
```

`sync_wake` объявить ДО `if calendar_sync is not None:` (рядом с `stop = threading.Event()`, строка ~259) — он нужен и WebhookServer'у.

- [x] **Step 2: передать wake в WebhookServer** (строка ~302):

```python
            webhook_server = WebhookServer(
                session_factory, args.clinic,
                secret=credentials.webhook_secret, port=args.webhook_port,
                gcal_wake=sync_wake if calendar_sync is not None else None)
```

- [x] **Step 3: graceful-выход** — в `finally` (строка ~316) после `stop.set()`:

```python
        sync_wake.set()  # разбудить календарный поток, чтобы он увидел stop
```

- [x] **Step 4: полный сьют**

Run: `python -m pytest -q`
Expected: all passed (594 + ~13 новых)

- [x] **Step 5: серия прогонов** (конкурентные тесты в сьюте; правило — серия перед «готово»)

Run: 8 последовательных `python -m pytest -q`
Expected: 8/8 зелёные

- [x] **Step 6: восстановить демо + чеклист**

```bash
python -m navbat.onboard --demo
python -m navbat --check
```
Expected: все [OK]

- [x] **Step 7: Commit + push** `feat(calendar): wire watch channels + push wake into supervisor`, отметить чекбоксы плана, обновить якорь CLAUDE.md (C-6 закрыт, следующий — C-7), `git push origin master`.

## Definition of Done (C-6)

- [x] `watch_events`/`stop_channel` в API-клиенте, покрыты MockTransport-тестами.
- [x] Миграция 0015: поля канала в doctor; `GcalWatchManager` открывает/продлевает каналы, сбой → поллинг (без алерта).
- [x] `POST /gcal/push/<channel_id>` на WebhookServer: валидный канал → 200 + wake; чужой/без календаря → 404.
- [x] `CalendarAuthError` → алерт в первый же цикл, раз в день, без дубля порогового; восстановление перевзводит.
- [x] Супервизор: push будит синк, каналы продлеваются из календарного цикла, graceful-выход не подвисает.
- [x] Полный сьют зелёный, серия 8/8, демо восстановлено, `--check` [OK], всё в origin.
