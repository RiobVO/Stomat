# C-2 Публичная поверхность — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Webhook за nginx+TLS, шифрованный webhook-секрет, подтверждаемый setWebhook и /health-эндпоинт — второй инкремент группы C по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md`.

**Architecture:** /health — отдельный stdlib-сервер (паттерн WebhookServer) на внутреннем порту, наружу не проксируется. Супервизор получает webhook-режим (сейчас webhook умеет только `telegram/app.py` — канал без напоминаний/календаря). nginx терминирует TLS и роутит по пути; cert-файлы видны app'у read-only для проверки срока. Живого LLM-пинга в health НЕТ (деньги) — ключи + доля сбоев из llm_usage.

**Tech Stack:** stdlib http.server, cryptography (уже в зависимостях — парсинг x509), alembic, nginx:1.27-alpine, certbot/certbot.

**Контекст кодовой базы:**
- `src/navbat/telegram/transport.py` — WebhookServer (тело вычитывается до ответа — НЕ ломать), `SECRET_HEADER`.
- `src/navbat/telegram/api.py` — `set_webhook` (строка 88), `_call` сам ретраит сеть/5xx/429; логические 4xx бросает сразу.
- `src/navbat/telegram/app.py` — `load_clinic_credentials` (SELECT tg_webhook_secret, строка 50), webhook-ветка main() (125-136).
- `src/navbat/onboard.py:286-299` — запись токена + генерация webhook-секрета (`secrets.token_urlsafe(32)`, открытым текстом).
- `src/navbat/calendar/sync_loop.py` — `_record(failed)` (55-74), успешная ветка сбрасывает счётчик.
- `src/navbat/supervisor.py` — main(): поллинг на 257 (после C-1 — чуть ниже), stop-event + install_sigterm_handler.
- `migrations/versions/0010_clinic_admin_chats.py` — паттерн миграции (op.execute, голый SQL).
- `tests/test_calendar_sync_loop.py` — фейки `_Sync`, `RecordingNotifier` (из test_dialog_booking), `_loop_with_calendar_doctor`.
- `tests/test_tg_transport.py` — паттерн HTTP-тестов на эфемерном порту (`port=0`).
- `message_queue.created_at` существует (миграция 0003) — возраст pending считаем по нему.

---

### Task 1: clinic.gcal_last_sync_at — метка живого синка

**Files:**
- Create: `migrations/versions/0011_gcal_last_sync.py`
- Modify: `src/navbat/calendar/sync_loop.py`
- Test: `tests/test_calendar_sync_loop.py`

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_calendar_sync_loop.py`:

```python
# ── C-2: успешный цикл штампует clinic.gcal_last_sync_at (для /health) ──────

def _last_sync_at(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT gcal_last_sync_at FROM clinic WHERE id = :c"),
            {"c": clinic_id},
        ).scalar_one()


def test_success_stamps_last_sync(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=False), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    assert _last_sync_at(admin_engine, clinic_a) is None
    loop.run_once()
    assert _last_sync_at(admin_engine, clinic_a) is not None


def test_failure_does_not_stamp(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=True), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()
    assert _last_sync_at(admin_engine, clinic_a) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_calendar_sync_loop.py -q`
Expected: 2 FAIL — `column "gcal_last_sync_at" does not exist` (миграции ещё нет)

- [ ] **Step 3: Create migration `migrations/versions/0011_gcal_last_sync.py`**

```python
"""C-2: метка последнего успешного синка календаря.

/health должен отличать «синк живёт» от «синк тихо умер»: цикл синка
штампует поле при каждом успешном прогоне, health сравнивает возраст
с интервалом синка.

Revision ID: 0011
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN gcal_last_sync_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS gcal_last_sync_at")
```

- [ ] **Step 4: Implement в sync_loop**

В `src/navbat/calendar/sync_loop.py` заменить хвост `_record` (после ветки `if failed:`):

```python
        if self._alerted:
            self._notifier.notify(
                self._admin_chat_id,
                "синхронизация Google Calendar восстановлена.", {})
        self._consecutive_failures = 0
        self._alerted = False
        self._stamp_last_sync()

    def _stamp_last_sync(self) -> None:
        """Успешный прогон = календарь жив; /health меряет возраст метки."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                "UPDATE clinic SET gcal_last_sync_at = now() "
                "WHERE id = current_setting('app.clinic_id')::uuid"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_calendar_sync_loop.py -q`
Expected: 5 passed (3 старых + 2 новых)

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0011_gcal_last_sync.py src/navbat/calendar/sync_loop.py tests/test_calendar_sync_loop.py
git commit -m "feat(health): stamp clinic.gcal_last_sync_at on successful sync cycle"
```

---

### Task 2: шифрование tg_webhook_secret

Секрет webhook лежит в БД открытым текстом — несимметрично с токеном бота. Перешифровываем как `tg_bot_token_encrypted`.

**Files:**
- Create: `migrations/versions/0012_webhook_secret_encrypted.py`
- Modify: `src/navbat/telegram/app.py` (load_clinic_credentials)
- Modify: `src/navbat/onboard.py` (запись секрета)
- Test: `tests/test_tg_app.py`

- [ ] **Step 1: Write the failing test**

Добавить в конец `tests/test_tg_app.py`:

```python
# ── C-2: webhook-секрет хранится шифртекстом, наружу отдаётся открытым ──────

def test_credentials_decrypt_webhook_secret(app_session_factory, admin_engine,
                                            clinic_a):
    from sqlalchemy import text

    from navbat.crypto import encrypt_text
    from navbat.telegram.app import load_clinic_credentials

    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok, "
                 "tg_webhook_secret_encrypted = :sec WHERE id = :id"),
            {"tok": encrypt_text("123:token"), "sec": encrypt_text("hook-secret"),
             "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.webhook_secret == "hook-secret"


def test_credentials_without_secret_is_none(app_session_factory, admin_engine,
                                            clinic_a):
    from sqlalchemy import text

    from navbat.crypto import encrypt_text
    from navbat.telegram.app import load_clinic_credentials

    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok WHERE id = :id"),
            {"tok": encrypt_text("123:token"), "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.webhook_secret is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tg_app.py -q -k credentials`
Expected: FAIL — `column "tg_webhook_secret_encrypted" does not exist`

- [ ] **Step 3: Create migration `migrations/versions/0012_webhook_secret_encrypted.py`**

```python
"""C-2: webhook-секрет шифруется как токен бота.

tg_webhook_secret лежал открытым текстом — лишняя поверхность при
компрометации БД и несимметрично с tg_bot_token_encrypted. Backfill
перешифровывает существующие секреты; для этого миграции нужен
NAVBAT_ENC_KEY (тот же, под которым живёт клиника).

Revision ID: 0012
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN tg_webhook_secret_encrypted text")
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, tg_webhook_secret FROM clinic "
        "WHERE tg_webhook_secret IS NOT NULL"
    )).all()
    if rows:
        if not os.environ.get("NAVBAT_ENC_KEY"):
            raise RuntimeError(
                "0012: NAVBAT_ENC_KEY обязателен — есть webhook-секреты "
                "для перешифрования")
        from navbat.crypto import encrypt_text
        for row in rows:
            conn.execute(
                sa.text("UPDATE clinic SET tg_webhook_secret_encrypted = :v "
                        "WHERE id = :id"),
                {"v": encrypt_text(row.tg_webhook_secret), "id": row.id})
    op.execute("ALTER TABLE clinic DROP COLUMN tg_webhook_secret")


def downgrade() -> None:
    # секреты при даунгрейде не восстанавливаем открытым текстом —
    # они перегенерируются onboard'ом при следующей записи токена
    op.execute("ALTER TABLE clinic ADD COLUMN tg_webhook_secret text")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_webhook_secret_encrypted")
```

- [ ] **Step 4: Update код чтения и записи секрета**

`src/navbat/telegram/app.py`, `load_clinic_credentials` — SELECT и сборка:

```python
        row = session.execute(
            text("SELECT tg_bot_token_encrypted, tg_admin_chat_ids, "
                 "tg_webhook_secret_encrypted "
                 "FROM clinic WHERE id = :id"),
            {"id": clinic_id},
        ).one_or_none()
```

и в return:

```python
    return ClinicCredentials(
        token=decrypt_text(row.tg_bot_token_encrypted),
        admin_chat_ids=tuple(row.tg_admin_chat_ids or ()),
        webhook_secret=(decrypt_text(row.tg_webhook_secret_encrypted)
                        if row.tg_webhook_secret_encrypted else None),
    )
```

В webhook-ветке `main()` app.py поправить текст ошибки:

```python
            if not credentials.webhook_secret:
                sys.exit("[FAIL] webhook-режим требует webhook-секрет "
                         "(onboard --tg-token генерирует)")
```

`src/navbat/onboard.py` (строки ~288-298) — колонка и шифрование параметра:

```python
            text("UPDATE clinic SET tg_bot_token_encrypted = :token, "
                 "tg_admin_chat_id = COALESCE(:admin, tg_admin_chat_id), "
                 # --admin-chat задаёт стартовый админ-чат (M4: список из одного);
                 # дальше добавлять/убирать через --add-admin/--remove-admin
                 "tg_admin_chat_ids = CASE WHEN :admin IS NOT NULL "
                 "    THEN ARRAY[:admin]::bigint[] ELSE tg_admin_chat_ids END, "
                 "tg_webhook_secret_encrypted = "
                 "    COALESCE(tg_webhook_secret_encrypted, :secret) "
                 "WHERE id = :id"),
            {"token": encrypt_text(token), "admin": admin_chat,
             "secret": encrypt_text(secrets.token_urlsafe(32)), "id": clinic_id},
```

- [ ] **Step 5: Проверить, что других чтений старой колонки нет**

Run: `grep -rn "tg_webhook_secret\b" src/ tests/ --include=*.py | grep -v encrypted`
Expected: пусто (все обращения — к `tg_webhook_secret_encrypted`)

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_tg_app.py tests/test_onboard_clinic.py -q`
Expected: PASS (включая 2 новых)

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/0012_webhook_secret_encrypted.py src/navbat/telegram/app.py src/navbat/onboard.py tests/test_tg_app.py
git commit -m "feat(privacy): encrypt clinic webhook secret at rest"
```

---

### Task 3: ensure_webhook — подтверждаемая установка webhook

Сейчас `set_webhook` зовётся «в пустоту»: логический отказ Telegram (кривой URL, битый cert) роняет процесс исключением при старте. Делаем повторы + алерт + продолжение работы.

**Files:**
- Modify: `src/navbat/telegram/transport.py`
- Modify: `src/navbat/telegram/app.py` (webhook-ветка)
- Test: `tests/test_tg_transport.py`

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_tg_transport.py`:

```python
# ── C-2: setWebhook с подтверждением — сбой не роняет процесс ────────────────

from navbat.telegram.api import TelegramAPIError
from navbat.telegram.transport import ensure_webhook
from test_dialog_booking import RecordingNotifier


class FakeWebhookAPI:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    def set_webhook(self, url, secret_token):
        self.calls.append((url, secret_token))
        if self.failures:
            self.failures -= 1
            raise TelegramAPIError("bad webhook: HTTPS url must be provided")
        return True


def test_ensure_webhook_success_first_try():
    api = FakeWebhookAPI()
    assert ensure_webhook(api, "https://x.uz/", "s", path="/webhook/abc",
                          waiter=lambda _: None) is True
    assert api.calls == [("https://x.uz/webhook/abc", "s")]


def test_ensure_webhook_retries_then_succeeds():
    api = FakeWebhookAPI(failures=2)
    notifier = RecordingNotifier()
    assert ensure_webhook(api, "https://x.uz", "s", notifier=notifier,
                          path="/webhook/abc", waiter=lambda _: None) is True
    assert len(api.calls) == 3
    assert notifier.calls == []  # успех — алерта нет


def test_ensure_webhook_exhausted_alerts_and_survives():
    api = FakeWebhookAPI(failures=99)
    notifier = RecordingNotifier()
    assert ensure_webhook(api, "https://x.uz", "s", notifier=notifier,
                          path="/webhook/abc", waiter=lambda _: None) is False
    assert len(api.calls) == 3  # WEBHOOK_SETUP_RETRIES
    assert len(notifier.calls) == 1
    assert "webhook" in notifier.calls[0][1].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tg_transport.py -q -k ensure_webhook`
Expected: 3 FAIL — `ImportError: cannot import name 'ensure_webhook'`

- [ ] **Step 3: Implement в transport.py**

Добавить `import time` к импортам `transport.py` и в конец файла:

```python
WEBHOOK_SETUP_RETRIES = 3
WEBHOOK_SETUP_BACKOFF = (2.0, 5.0)  # паузы между попытками, сек


def ensure_webhook(api, url: str, secret: str, notifier=None, path: str = "",
                   waiter=time.sleep) -> bool:
    """setWebhook с подтверждением: сбой — алерт, не падение процесса.

    TelegramAPI._call сам ретраит сеть/5xx/429; здесь добиваем логические
    отказы (кривой URL, битый cert) и шлём алерт после исчерпания —
    nginx/certbot могут подняться позже, процесс должен жить.
    """
    full_url = url.rstrip("/") + path
    for attempt in range(WEBHOOK_SETUP_RETRIES):
        try:
            api.set_webhook(full_url, secret_token=secret)
            log.info("webhook установлен: %s", full_url)
            return True
        except TelegramAPIError as e:
            log.error("setWebhook (попытка %d/%d): %s",
                      attempt + 1, WEBHOOK_SETUP_RETRIES, e)
            if attempt < WEBHOOK_SETUP_RETRIES - 1:
                waiter(WEBHOOK_SETUP_BACKOFF[
                    min(attempt, len(WEBHOOK_SETUP_BACKOFF) - 1)])
    if notifier is not None:
        notifier.notify(
            0,
            f"webhook не установлен после {WEBHOOK_SETUP_RETRIES} попыток — "
            f"бот глух для Telegram. Проверьте домен/cert. URL: {full_url}",
            {})
    return False
```

В `src/navbat/telegram/app.py` webhook-ветку перевести на ensure_webhook
(вместо прямого `api.set_webhook(...)` + `log.info`):

```python
            webhook_server.start()
            ensure_webhook(api, args.webhook_url, credentials.webhook_secret,
                           notifier=notifier, path=webhook_server.path)
            stop.wait()  # до Ctrl+C
```

и импорт: `from navbat.telegram.transport import PollingTransport, WebhookServer, ensure_webhook`

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tg_transport.py -q`
Expected: все PASS (6 старых + 3 новых)

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/transport.py src/navbat/telegram/app.py tests/test_tg_transport.py
git commit -m "feat(deploy): verified setWebhook with retries and admin alert"
```

---

### Task 4: /health — модуль и сервер

**Files:**
- Create: `src/navbat/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write the failing tests**

Создать `tests/test_health.py`:

```python
"""Health-эндпоинт (C-2): db, очередь, календарь, cert, LLM-ключи.

Сервер тестируется живым HTTP на эфемерном порту (паттерн webhook-тестов).
Живого LLM-пинга нет by design (деньги).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from navbat.health import (
    CERT_WARN_DAYS,
    HealthChecker,
    HealthServer,
    days_until_cert_expiry,
)


def _selfsigned(tmp_path, days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    path = tmp_path / "fullchain.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def _get(server: HealthServer, query: str = "") -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{server.port}/health{query}")


def _serving(checker: HealthChecker) -> HealthServer:
    server = HealthServer(checker, host="127.0.0.1", port=0)
    server.start()
    return server


def test_healthy_clinic_returns_ok(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        response = _get(server)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "ok"
    finally:
        server.stop()


def test_light_mode_checks_db_only(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        body = _get(server, "?check=db").json()
        assert body["checks"]["db"] == "ok"
        assert "queue_oldest_pending_sec" not in body["checks"]
    finally:
        server.stop()


def test_stalled_queue_degrades(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
            "payload, status, created_at) VALUES (:c, 1, 100, '{}', 'pending', "
            "now() - interval '10 minutes')"), {"c": clinic_a})
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        response = _get(server)
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
    finally:
        server.stop()


def test_unknown_path_is_404(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        assert httpx.get(
            f"http://127.0.0.1:{server.port}/nope").status_code == 404
    finally:
        server.stop()


def test_stale_calendar_degrades(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE clinic SET gcal_refresh_token_encrypted = 'x', "
            "gcal_last_sync_at = now() - interval '1 hour' WHERE id = :c"),
            {"c": clinic_a})
    checker = HealthChecker(app_session_factory, clinic_a, sync_interval_sec=60)
    ok, checks = checker.snapshot()
    assert ok is False
    assert "calendar" in checks


def test_calendar_not_configured_is_ok(app_session_factory, clinic_a):
    ok, checks = HealthChecker(app_session_factory, clinic_a).snapshot()
    assert ok is True
    assert checks["calendar"] == "not-configured"


def test_cert_expiry_days(tmp_path):
    assert days_until_cert_expiry(_selfsigned(tmp_path, 90)) in (88, 89, 90)
    assert days_until_cert_expiry(str(tmp_path / "missing.pem")) is None


def test_expiring_cert_degrades(app_session_factory, clinic_a, tmp_path):
    cert = _selfsigned(tmp_path, CERT_WARN_DAYS - 5)
    checker = HealthChecker(app_session_factory, clinic_a, cert_path=cert)
    ok, checks = checker.snapshot()
    assert ok is False
    assert checks["cert_days_left"] <= CERT_WARN_DAYS - 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py -q`
Expected: FAIL на импорте — `ModuleNotFoundError: No module named 'navbat.health'`

- [ ] **Step 3: Create `src/navbat/health.py`**

```python
"""Health-эндпоинт: docker healthcheck + ручная диагностика (C-2).

Наружу НЕ публикуется (nginx его не проксирует) — только внутренняя
сеть compose. Живой LLM-пинг сознательно НЕ делается (правило денег):
вместо него наличие ключей + дневная доля сбоев NLU из llm_usage.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction

log = logging.getLogger("navbat.health")

QUEUE_STALL_SEC = 120   # pending старше — очередь стоит
CERT_WARN_DAYS = 14     # cert истекает раньше — degraded
SYNC_AGE_FACTOR = 3     # возраст синка > N интервалов — синк мёртв


def days_until_cert_expiry(cert_path: str) -> int | None:
    """Дни до истечения серта; None — файла нет или он нечитаем."""
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except (OSError, ValueError):
        return None
    return (cert.not_valid_after_utc - datetime.now(timezone.utc)).days


class HealthChecker:
    """Снимок здоровья одной клиники. Каждая проверка — отдельный ключ
    в checks; статус degraded, если хоть одна критичная провалена."""

    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, *, sync_interval_sec: int = 60,
                 cert_path: str | None = None) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._sync_interval_sec = sync_interval_sec
        self._cert_path = cert_path

    def snapshot(self, light: bool = False) -> tuple[bool, dict]:
        checks: dict = {}
        ok = self._check_db(checks)
        if light or not ok:
            return ok, checks
        ok = self._check_queue(checks) and ok
        ok = self._check_calendar(checks) and ok
        ok = self._check_cert(checks) and ok
        self._report_llm(checks)
        return ok, checks

    def _check_db(self, checks: dict) -> bool:
        try:
            with tenant_transaction(self._session_factory, self._clinic_id) as s:
                s.execute(text("SELECT 1"))
            checks["db"] = "ok"
            return True
        except Exception as e:
            checks["db"] = f"fail: {str(e)[:120]}"
            return False

    def _check_queue(self, checks: dict) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            oldest = s.execute(text(
                "SELECT extract(epoch FROM (now() - min(created_at))) "
                "FROM message_queue WHERE status = 'pending'")).scalar_one()
        age = int(oldest or 0)
        checks["queue_oldest_pending_sec"] = age
        return age <= QUEUE_STALL_SEC

    def _check_calendar(self, checks: dict) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            row = s.execute(text(
                "SELECT gcal_refresh_token_encrypted IS NOT NULL AS configured, "
                "extract(epoch FROM (now() - gcal_last_sync_at)) AS age "
                "FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid")).one()
        if not row.configured:
            checks["calendar"] = "not-configured"
            return True
        if row.age is None:
            # настроен, но ни одного успешного цикла с запуска — даём время
            checks["calendar"] = "never-synced"
            return True
        checks["calendar"] = f"synced {int(row.age)}s ago"
        return row.age <= self._sync_interval_sec * SYNC_AGE_FACTOR

    def _check_cert(self, checks: dict) -> bool:
        if not self._cert_path:
            checks["cert_days_left"] = "not-configured"
            return True
        days = days_until_cert_expiry(self._cert_path)
        if days is None:
            # серт ещё не выписан (первый старт до certbot) — не валим
            checks["cert_days_left"] = "missing"
            return True
        checks["cert_days_left"] = days
        return days >= CERT_WARN_DAYS

    def _report_llm(self, checks: dict) -> None:
        """Информационно: ключи и доля сбоев NLU за сегодня (не валит статус —
        деградацию NLU ловит дрифт-алерт, здесь только видимость)."""
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            row = s.execute(text(
                "SELECT requests, failures FROM llm_usage "
                "WHERE day = current_date "
                "AND clinic_id = current_setting('app.clinic_id')::uuid"
            )).one_or_none()
        checks["llm"] = {
            "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
            "gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
            "nlu_today": (f"{row.failures}/{row.requests} сбоев"
                          if row else "0/0 сбоев"),
        }


class HealthServer:
    """Паттерн WebhookServer: stdlib-сервер, мгновенный ответ, стоп штатный."""

    def __init__(self, checker: HealthChecker, host: str = "0.0.0.0",
                 port: int = 8080) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — API stdlib
                path, _, query = self.path.partition("?")
                if path != "/health":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ok, checks = outer._checker.snapshot(light="check=db" in query)
                body = json.dumps({"status": "ok" if ok else "degraded",
                                   "checks": checks}, ensure_ascii=False).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                log.debug("health: " + fmt, *args)

        self._checker = checker
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="health", daemon=True)
        self._thread.start()
        log.info("health-сервер слушает :%d/health", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_health.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/navbat/health.py tests/test_health.py
git commit -m "feat(health): /health endpoint - db, queue, calendar age, cert expiry"
```

---

### Task 5: супервизор — health-поток + webhook-режим

**Files:**
- Modify: `src/navbat/supervisor.py`
- Modify: `deploy/entrypoint.sh`
- Modify: `deploy/.env.example`

- [ ] **Step 1: Add args + wiring в supervisor.main()**

Аргументы (после `--no-calendar`):

```python
    parser.add_argument("--webhook-url", default=None,
                        help="публичный https-URL; без него — long polling")
    parser.add_argument("--webhook-port", type=int, default=8443)
    parser.add_argument("--health-port", type=int,
                        default=int(os.environ.get("NAVBAT_HEALTH_PORT", "8080")))
```

Импорты supervisor.py:

```python
from navbat.health import HealthChecker, HealthServer
from navbat.telegram.transport import PollingTransport, WebhookServer, ensure_webhook
```

В `main()` транспортный блок (вместо текущего try/except вокруг polling):

```python
    health = HealthServer(
        HealthChecker(session_factory, args.clinic,
                      sync_interval_sec=args.sync_interval,
                      cert_path=os.environ.get("NAVBAT_CERT_PATH")),
        port=args.health_port)
    health.start()

    webhook_server = None
    try:
        if args.webhook_url:
            if not credentials.webhook_secret:
                sys.exit("[FAIL] webhook-режим требует webhook-секрет "
                         "(onboard --tg-token генерирует)")
            webhook_server = WebhookServer(
                session_factory, args.clinic,
                secret=credentials.webhook_secret, port=args.webhook_port)
            webhook_server.start()
            ensure_webhook(tg_api, args.webhook_url,
                           credentials.webhook_secret,
                           notifier=notifier, path=webhook_server.path)
            stop.wait()  # до SIGTERM/Ctrl+C
        else:
            tg_api.delete_webhook()  # иначе getUpdates вернёт 409
            PollingTransport(session_factory, args.clinic, tg_api).run(stop)
    except KeyboardInterrupt:
        log.info("останавливаюсь…")
    finally:
        stop.set()
        if webhook_server:
            webhook_server.stop()
        health.stop()
        for thread in threads:
            thread.join(timeout=10)
    return 0
```

- [ ] **Step 2: entrypoint + .env.example**

`deploy/entrypoint.sh` — добавить к exec-строке:

```sh
exec python -m navbat \
    ${NAVBAT_CLINIC_ID:+--clinic "$NAVBAT_CLINIC_ID"} \
    ${NAVBAT_REAL:+--real} \
    ${NAVBAT_WORKERS:+--workers "$NAVBAT_WORKERS"} \
    ${NAVBAT_SYNC_INTERVAL:+--sync-interval "$NAVBAT_SYNC_INTERVAL"} \
    ${NAVBAT_REMINDER_OFFSETS:+--reminder-offsets "$NAVBAT_REMINDER_OFFSETS"} \
    ${NAVBAT_WEBHOOK_URL:+--webhook-url "$NAVBAT_WEBHOOK_URL"} \
    ${NAVBAT_WEBHOOK_PORT:+--webhook-port "$NAVBAT_WEBHOOK_PORT"}
```

`deploy/.env.example` — в секцию «Опционально» добавить, плюс новая секция:

```sh
# ── Webhook + HTTPS (прод; пусто = long polling) ─────────────────────────
# Домен VPS; nginx терминирует TLS, путь добавится сам (/webhook/<clinic>)
NAVBAT_DOMAIN=
NAVBAT_WEBHOOK_URL=
# NAVBAT_WEBHOOK_PORT=8443
# NAVBAT_HEALTH_PORT=8080
```

- [ ] **Step 3: Run быстрая проверка**

Run: `python -m pytest tests/test_supervisor.py tests/test_health.py -q && python -m navbat --check`
Expected: тесты PASS; --check все [OK] (поведение не изменилось)

- [ ] **Step 4: Commit**

```bash
git add src/navbat/supervisor.py deploy/entrypoint.sh deploy/.env.example
git commit -m "feat(deploy): supervisor webhook mode + health server thread"
```

---

### Task 6: nginx + certbot в прод-compose

**Files:**
- Create: `deploy/nginx/templates/default.conf.template`
- Create: `deploy/scripts/check-nginx-config.sh`
- Modify: `deploy/docker-compose.prod.yml`

- [ ] **Step 1: Create `deploy/nginx/templates/default.conf.template`**

```nginx
# Рендерится образом nginx через envsubst (каталог templates).
# resolver 127.0.0.11 (DNS докера) + переменная в proxy_pass: nginx
# стартует и проходит -t даже когда app ещё не поднят.
server {
    listen 80;
    server_name ${NAVBAT_DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name ${NAVBAT_DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${NAVBAT_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${NAVBAT_DOMAIN}/privkey.pem;

    resolver 127.0.0.11 valid=10s;
    set $app_upstream http://app:8443;

    # webhook Telegram и push Google — всё остальное наружу закрыто
    location /webhook/ {
        proxy_pass $app_upstream;
        proxy_set_header Host $host;
    }
    location /gcal/push/ {
        proxy_pass $app_upstream;
    }
    location / {
        return 404;
    }
}
```

- [ ] **Step 2: Create `deploy/scripts/check-nginx-config.sh`**

```sh
#!/bin/sh
# Локальная проверка nginx-шаблона без домена/certbot: рендерим шаблон,
# подсовываем self-signed cert и гоняем nginx -t в контейнере.
set -e
DOMAIN="${NAVBAT_DOMAIN:-smoke.localhost}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
mkdir -p "$TMP/certs/live/$DOMAIN" "$TMP/webroot"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$TMP/certs/live/$DOMAIN/privkey.pem" \
    -out "$TMP/certs/live/$DOMAIN/fullchain.pem" \
    -days 30 -subj "/CN=$DOMAIN" >/dev/null 2>&1
docker run --rm \
    -v "$SCRIPT_DIR/../nginx/templates:/etc/nginx/templates:ro" \
    -v "$TMP/certs:/etc/letsencrypt:ro" \
    -v "$TMP/webroot:/var/www/certbot:ro" \
    -e NAVBAT_DOMAIN="$DOMAIN" \
    --entrypoint sh nginx:1.27-alpine \
    -c "/docker-entrypoint.d/20-envsubst-on-templates.sh && nginx -t"
echo "[OK] nginx-шаблон валиден"
rm -rf "$TMP"
```

(На Windows-хосте git-bash может покалечить пути volume — тогда проверка
выполняется на VPS при деплое; это зафиксировано в DEPLOY.md в C-7.)

- [ ] **Step 3: Дополнить `deploy/docker-compose.prod.yml`**

К сервису `app` добавить:

```yaml
    environment:
      # внутри compose-сети postgres живёт на :5432 под именем сервиса
      NAVBAT_ADMIN_DSN: postgresql+psycopg://postgres:${NAVBAT_PG_PASSWORD}@postgres:5432/navbat
      NAVBAT_APP_DSN: postgresql+psycopg://navbat_app:${NAVBAT_APP_PASSWORD}@postgres:5432/navbat
      NAVBAT_CERT_PATH: /etc/letsencrypt/live/${NAVBAT_DOMAIN:-unset}/fullchain.pem
    volumes:
      - letsencrypt:/etc/letsencrypt:ro
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/health?check=db', timeout=5); sys.exit(0 if r.status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Новые сервисы (после app):

```yaml
  nginx:
    image: nginx:1.27-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    environment:
      NAVBAT_DOMAIN: ${NAVBAT_DOMAIN:-unset}
    volumes:
      - ./nginx/templates:/etc/nginx/templates:ro
      - letsencrypt:/etc/letsencrypt:ro
      - certbot-webroot:/var/www/certbot:ro
    depends_on:
      - app
    profiles: ["web"]   # включается только при наличии домена (прод/VPS)

  certbot:
    image: certbot/certbot
    restart: unless-stopped
    entrypoint: ["/bin/sh", "-c",
                 "trap exit TERM; while :; do certbot renew --webroot -w /var/www/certbot --quiet; sleep 12h & wait $${!}; done"]
    volumes:
      - letsencrypt:/etc/letsencrypt
      - certbot-webroot:/var/www/certbot
    profiles: ["web"]
```

И в `volumes:` внизу добавить:

```yaml
  letsencrypt:
  certbot-webroot:
```

Профиль `web`: локальный smoke (`up -d`) поднимает postgres+app без nginx
(домена нет); на VPS — `docker compose --profile web up -d`.

- [ ] **Step 4: Verify**

```bash
docker compose -f deploy/docker-compose.prod.yml config --quiet && echo CONFIG-OK
bash deploy/scripts/check-nginx-config.sh
```
Expected: `CONFIG-OK`; от nginx — `syntax is ok` + `test is successful` + `[OK] nginx-шаблон валиден` (либо задокументированный отказ из-за путей Windows).

- [ ] **Step 5: Commit**

```bash
git add deploy/nginx/templates/default.conf.template deploy/scripts/check-nginx-config.sh deploy/docker-compose.prod.yml
git commit -m "feat(deploy): nginx TLS termination + certbot renewal in prod compose"
```

---

### Task 7: миграция дев-базы, полный сьют, smoke, push

- [ ] **Step 1: Бэкфилл дев-базы с проверкой round-trip**

Дев-БД содержит демо-клинику с plaintext-секретом — живая проверка backfill 0012.
PowerShell:

```powershell
$old = docker exec navbat-postgres psql -U postgres -d navbat -tAc "SELECT tg_webhook_secret FROM clinic LIMIT 1"
$env:NAVBAT_ENC_KEY = python -c "from navbat.onboard import DEV_ENC_KEY; print(DEV_ENC_KEY)"
alembic upgrade head
python -c "
from navbat.envfile import load_env_file
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.crypto import decrypt_text
from navbat.onboard import DEMO_CLINIC_ID
from sqlalchemy import text
sf = make_session_factory(make_app_engine())
with tenant_transaction(sf, DEMO_CLINIC_ID) as s:
    enc = s.execute(text('SELECT tg_webhook_secret_encrypted FROM clinic')).scalar_one()
print('[OK] backfill round-trip' if decrypt_text(enc) == '$old'.strip() else '[FAIL] секрет не совпал')
"
Remove-Item Env:NAVBAT_ENC_KEY
```

Expected: `[OK] backfill round-trip`

- [ ] **Step 2: Полный сьют**

Run: `python -m pytest -q`
Expected: `569 passed` (554 + 2 sync_loop + 2 credentials + 3 ensure_webhook + 8 health).

- [ ] **Step 3: Smoke прод-стека (postgres+app, без web-профиля)**

```bash
docker compose -f deploy/docker-compose.prod.yml down -v
docker compose -f deploy/docker-compose.prod.yml build app
docker compose -f deploy/docker-compose.prod.yml up -d postgres
docker compose -f deploy/docker-compose.prod.yml run --rm --entrypoint sh app -c "alembic upgrade head && python -m navbat.onboard --demo"
docker compose -f deploy/docker-compose.prod.yml up -d app
sleep 10
docker compose -f deploy/docker-compose.prod.yml exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
docker compose -f deploy/docker-compose.prod.yml ps app
docker compose -f deploy/docker-compose.prod.yml down
```
Expected: JSON со `"status": "ok"`; в `ps` — статус `healthy` (docker healthcheck по /health?check=db).

- [ ] **Step 4: Восстановить дев-демо и запушить**

```bash
python -m navbat.onboard --demo
python -m navbat --check
git push
```
Expected: все [OK]; push успешен.

---

## Definition of Done (C-2)

- [ ] ~15 новых тестов зелёные, полный сьют зелёный, число зафиксировано.
- [ ] Backfill 0012 проверен round-trip'ом на живой дев-базе.
- [ ] Smoke: app-контейнер `healthy` по docker healthcheck, /health отдаёт ok.
- [ ] nginx-шаблон прошёл `nginx -t` (или отказ задокументирован как Windows-путь-лимитация).
- [ ] Демо-клиника восстановлена, `--check` [OK], всё запушено.
