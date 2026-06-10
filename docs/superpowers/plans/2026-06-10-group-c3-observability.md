# C-3 Наблюдаемость — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JSON-логи, метрика p95 ответа (очередь → /stats, дайджест, /health), канал владельца для системных алертов — третий инкремент группы C по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md`.

**Architecture:** Системные алерты идут через новый помощник `system_alert(notifier, ...)`: если notifier умеет `notify_system` (TelegramEscalation) — веер админ-чатам + владельцу (`NAVBAT_OWNER_CHAT_ID`); иначе фолбэк в старый `notify()` — существующие тест-фейки не трогаем. «Sentry-класс» закрыт минимально: ERROR в JSON-логах + dead-letter-алерт дублируется владельцу; отдельный per-exception алерт ОТКЛОНЁН — dead-letter и есть алерт о необработанном исключении (после ретраев), второй канал дублировал бы каждый сбой дважды.

**Tech Stack:** stdlib logging/json, alembic, percentile_cont (Postgres).

**Контекст кодовой базы:**
- `src/navbat/telegram/queue.py` — `complete()` (строка 111) ставит только status; `claimed_at` уже есть (0003).
- `src/navbat/stats.py` — `DailyStats` (frozen dataclass), `collect_daily_stats(session, day, tz)`, `render_stats`; в тестах DailyStats напрямую не конструируется.
- `src/navbat/telegram/escalation.py` — `TelegramEscalation.notify()` веером по `_admin_chat_ids`.
- `src/navbat/dialog/escalation.py` — Protocol `EscalationNotifier` (только notify) + `LoggingEscalation`.
- Системные алерт-точки: `calendar/sync_loop.py:60,70` (нотификации), `nlu/wrappers.py:125,143` (drift, cap), `telegram/transport.py` (ensure_webhook), `telegram/worker.py:84` (dead-letter), `reminders.py:146` (недоставленное напоминание).
- `src/navbat/health.py` — `HealthChecker.snapshot()`; cert-проверка `_check_cert`.
- basicConfig: `src/navbat/__main__.py:9`, `telegram/__main__.py:9`, `calendar/__main__.py:107` (одинаковый формат) — заменяются на setup_logging; CLI-тулзы (demo, onboard, auth) остаются plain.
- `tests/test_stats.py`, `tests/test_queue.py`, `tests/test_tg_escalation.py` — паттерны тестов.

---

### Task 1: JSON-логи

**Files:**
- Create: `src/navbat/logging_setup.py`
- Modify: `src/navbat/__main__.py`, `src/navbat/telegram/__main__.py`, `src/navbat/calendar/__main__.py`
- Modify: `deploy/docker-compose.prod.yml`, `deploy/.env.example`
- Test: `tests/test_logging_setup.py`

- [x] **Step 1: Write the failing tests** — `tests/test_logging_setup.py`:

```python
"""JSON-формат логов (C-3): контейнер пишет машинно-разбираемые строки."""
from __future__ import annotations

import json
import logging

from navbat.logging_setup import make_formatter


def _record(level=logging.INFO, msg="hello %s", args=("world",), exc=None):
    return logging.LogRecord("navbat.test", level, __file__, 1, msg, args, exc)


def test_json_formatter_emits_parseable_line():
    line = make_formatter("json").format(_record())
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["logger"] == "navbat.test"
    assert data["message"] == "hello world"
    assert "ts" in data


def test_json_formatter_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        line = make_formatter("json").format(_record(exc=sys.exc_info()))
    data = json.loads(line)
    assert "ValueError: boom" in data["exc"]


def test_plain_formatter_keeps_legacy_format():
    line = make_formatter("plain").format(_record())
    assert line == "INFO navbat.test: hello world"
```

- [x] **Step 2: Run** `python -m pytest tests/test_logging_setup.py -q` → FAIL (нет модуля)

- [x] **Step 3: Create `src/navbat/logging_setup.py`**:

```python
"""Конфигурация логов: plain для консоли разработчика, json для контейнера.

NAVBAT_LOG_FORMAT=json включается в прод-compose: stdout контейнера
становится потоком структурных событий (готов к Loki/grep без парсинга
свободного текста). Никаких сторонних пакетов — stdlib Formatter.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

PLAIN_FORMAT = "%(levelname)s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                  .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def make_formatter(kind: str) -> logging.Formatter:
    return JsonFormatter() if kind == "json" else logging.Formatter(PLAIN_FORMAT)


def setup_logging() -> None:
    """Точка входа процессов (supervisor/канал/календарь): формат из env."""
    handler = logging.StreamHandler()
    handler.setFormatter(make_formatter(os.environ.get("NAVBAT_LOG_FORMAT", "plain")))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx печатает полный URL запроса — в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
```

- [x] **Step 4: Переключить три __main__** — в каждом заменить basicConfig-блок (и httpx-строку, где она есть) на:

```python
from navbat.logging_setup import setup_logging
...
    setup_logging()
```

- [x] **Step 5: compose + .env.example** — в `deploy/docker-compose.prod.yml` к environment app добавить `NAVBAT_LOG_FORMAT: json`; в `.env.example` секцию «Опционально» дополнить `# NAVBAT_LOG_FORMAT=json` с комментарием.

- [x] **Step 6: Run** `python -m pytest tests/test_logging_setup.py -q` → 3 passed; `python -m navbat --check` → [OK] (plain-формат не сломан)

- [x] **Step 7: Commit** `feat(obs): JSON log format via NAVBAT_LOG_FORMAT`

---

### Task 2: p95 ответа — completed_at + stats + health

**Files:**
- Create: `migrations/versions/0013_queue_completed_at.py`
- Modify: `src/navbat/telegram/queue.py` (complete), `src/navbat/stats.py`, `src/navbat/health.py`
- Test: `tests/test_queue.py`, `tests/test_stats.py`, `tests/test_health.py`

- [x] **Step 1: Write the failing tests.**

В `tests/test_queue.py` (конец файла):

```python
# ── C-3: completed_at для p95 ответа ─────────────────────────────────────────

def test_complete_stamps_completed_at(app_session_factory, admin_engine, clinic_a):
    from navbat.db.base import tenant_transaction
    from navbat.telegram.queue import claim_next, complete, enqueue

    with tenant_transaction(app_session_factory, clinic_a) as session:
        enqueue(session, 1, 100, {"message": {"chat": {"id": 100}, "text": "hi"}})
    claimed = claim_next(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        complete(session, claimed.id)
    with admin_engine.begin() as conn:
        done_at = conn.execute(text(
            "SELECT completed_at FROM message_queue WHERE id = :id"),
            {"id": claimed.id}).scalar_one()
    assert done_at is not None
```

В `tests/test_stats.py` (конец файла):

```python
# ── C-3: p95 ответа за день ──────────────────────────────────────────────────

def test_p95_response_from_done_queue(app_session_factory, admin_engine, clinic_a):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from navbat.stats import collect_daily_stats

    tz = ZoneInfo("Asia/Tashkent")
    with admin_engine.begin() as conn:
        for upd, secs in ((1, 1), (2, 10)):
            conn.execute(text(
                "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
                "payload, status, created_at, completed_at) VALUES "
                "(:c, :u, 100, '{}', 'done', now() - make_interval(secs => :s), "
                "now())"), {"c": clinic_a, "u": upd, "s": secs})
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_daily_stats(session, datetime.now(tz).date(), tz)
    assert stats.p95_response_sec is not None
    assert 9.0 < stats.p95_response_sec < 10.0  # percentile_cont([1,10], 0.95)


def test_p95_rendered_in_stats():
    from navbat.stats import render_stats
    # собрать через collect нельзя без БД — рендер проверяем на готовом объекте
    # (DailyStats напрямую в тестах не конструировался — теперь поле с дефолтом)
    from navbat.stats import DailyStats

    stats = DailyStats(booked=1, cancelled=0, escalated=0, reminders_sent=0,
                       llm_requests=0, llm_tokens=0, nlu_failures=0,
                       nlu_repairs=0, prevented_noshows=0, saved_revenue=0,
                       p95_response_sec=2.3)
    from datetime import date
    assert "p95" in render_stats(stats, date(2026, 6, 10))
```

(Импорт `text` в test_stats.py — проверить шапку файла, добавить при отсутствии.)

В `tests/test_health.py` (конец файла):

```python
def test_p95_reported_in_health(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
            "payload, status, created_at, completed_at) VALUES "
            "(:c, 1, 100, '{}', 'done', now() - interval '2 seconds', now())"),
            {"c": clinic_a})
    ok, checks = HealthChecker(app_session_factory, clinic_a).snapshot()
    assert ok is True
    assert checks["p95_response_sec_1h"] is not None
```

- [x] **Step 2: Run** оба новых стат/квалификационных теста → FAIL (нет колонки/поля)

- [x] **Step 3: Migration `0013_queue_completed_at.py`** (паттерн 0011, `down_revision = "0012"`):

```python
"""C-3: момент завершения обработки — для метрики p95 ответа.

BRIEF SLA: ответ p95 < 5 с. created_at → completed_at покрывает весь путь
пациентского сообщения: ожидание в очереди + FSM + отправка ответа.

Revision ID: 0013
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE message_queue ADD COLUMN completed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE message_queue DROP COLUMN IF EXISTS completed_at")
```

- [x] **Step 4: Код.**

`queue.py` `complete()`:

```python
def complete(session: Session, queue_id: int) -> None:
    session.execute(
        text("UPDATE message_queue SET status = 'done', completed_at = now() "
             "WHERE id = :id"),
        {"id": queue_id},
    )
```

`stats.py`: поле `p95_response_sec: float | None = None` в конец DailyStats; в `collect_daily_stats` перед return:

```python
    p95 = session.execute(
        text("""
            SELECT extract(epoch FROM percentile_cont(0.95)
                   WITHIN GROUP (ORDER BY completed_at - created_at))
            FROM message_queue
            WHERE status = 'done' AND completed_at IS NOT NULL
              AND (completed_at AT TIME ZONE :tz)::date = :day
        """),
        {"tz": str(tz), "day": day},
    ).scalar_one()
```

и `p95_response_sec=round(float(p95), 1) if p95 is not None else None` в конструктор. В `render_stats` добавить строку после LLM:

```python
    p95_line = (f"\n• p95 ответа: {stats.p95_response_sec} с (SLA < 5 с)"
                if stats.p95_response_sec is not None else "")
```

и `+ p95_line` к возвращаемой строке.

`health.py` в `snapshot()` после `_report_llm` добавить `self._report_p95(checks)`:

```python
    def _report_p95(self, checks: dict) -> None:
        """Информационно: p95 ответа за час (SLA-видимость, статус не валит)."""
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            p95 = s.execute(text(
                "SELECT extract(epoch FROM percentile_cont(0.95) "
                "WITHIN GROUP (ORDER BY completed_at - created_at)) "
                "FROM message_queue WHERE status = 'done' "
                "AND completed_at > now() - interval '1 hour'")).scalar_one()
        checks["p95_response_sec_1h"] = (round(float(p95), 1)
                                         if p95 is not None else None)
```

- [x] **Step 5: Run** `python -m pytest tests/test_queue.py tests/test_stats.py tests/test_health.py -q` → PASS

- [x] **Step 6: Commit** `feat(obs): p95 response metric in stats, digest and health`

---

### Task 3: канал владельца — notify_system + system_alert

**Files:**
- Modify: `src/navbat/telegram/escalation.py`, `src/navbat/dialog/escalation.py`
- Modify (точки вызова): `src/navbat/calendar/sync_loop.py`, `src/navbat/nlu/wrappers.py`, `src/navbat/telegram/transport.py`, `src/navbat/telegram/worker.py`, `src/navbat/reminders.py`
- Modify: `deploy/.env.example`
- Test: `tests/test_tg_escalation.py`

- [x] **Step 1: Write the failing tests** (в конец `tests/test_tg_escalation.py`; фейк уже импортирован в шапке — `FakeTelegramAPI` из test_tg_worker, элементы `api.sent` — 3-кортежи):

```python
# ── C-3: системные алерты — админ-чаты + канал владельца ────────────────────

def test_notify_system_fans_to_admins_and_owner(monkeypatch):
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", "555")
    api = FakeTelegramAPI()
    esc = TelegramEscalation(api, [111, 222])
    esc.notify_system("синк умер", {})
    chats = [entry[0] for entry in api.sent]
    assert chats == [111, 222, 555]
    assert "Системный алерт" in api.sent[0][1]


def test_notify_system_without_owner_env(monkeypatch):
    monkeypatch.delenv("NAVBAT_OWNER_CHAT_ID", raising=False)
    api = FakeTelegramAPI()
    esc = TelegramEscalation(api, [111])
    esc.notify_system("cap исчерпан", {})
    assert [entry[0] for entry in api.sent] == [111]


def test_system_alert_falls_back_to_notify():
    from navbat.dialog.escalation import system_alert
    from test_dialog_booking import RecordingNotifier

    notifier = RecordingNotifier()
    system_alert(notifier, "проблема", {}, chat_id=42)
    assert notifier.calls == [(42, "проблема")]
```

- [x] **Step 2: Run** → FAIL (нет notify_system / system_alert)

- [x] **Step 3: Реализация.**

`dialog/escalation.py` — добавить:

```python
def system_alert(notifier, reason: str, context: dict, chat_id: int = 0) -> None:
    """Системный алерт (не пациентская эскалация): cert, синк, cap, дрифт,
    dead-letter. TelegramEscalation шлёт его и владельцу системы; нотификаторы
    без notify_system (фейки, LoggingEscalation) получают обычный notify."""
    handler = getattr(notifier, "notify_system", None)
    if handler is not None:
        handler(reason, context)
    else:
        notifier.notify(chat_id, reason, context)
```

`telegram/escalation.py` — в `TelegramEscalation.__init__` прочитать `os.environ.get("NAVBAT_OWNER_CHAT_ID")` (int или None; `import os` добавить) и метод:

```python
    def notify_system(self, reason: str, context: dict) -> None:
        """Системный алерт: веер админ-чатам + владельцу системы (env)."""
        message = f"⚠ Системный алерт\n{reason}"
        targets = list(self._admin_chat_ids)
        if self._owner_chat and self._owner_chat not in targets:
            targets.append(self._owner_chat)
        if not targets:
            log.warning("системный алерт (чаты не заданы): %s | %s",
                        reason, context)
            return
        for chat in targets:
            try:
                self._api.send_message(chat, message)
            except TelegramAPIError as e:
                log.error("системный алерт не доставлен в %s: %s | %s",
                          chat, e, reason)
```

Точки вызова — заменить `self._notifier.notify(X, reason, ctx)` на `system_alert(self._notifier, reason, ctx, chat_id=X)` (импорт `from navbat.dialog.escalation import system_alert`):
- `sync_loop.py` — оба notify (chat_id=self._admin_chat_id);
- `wrappers.py` — `maybe_alert_drift` и `alert_once` (chat_id=0);
- `transport.py` — ensure_webhook (chat_id=0; та ветка с `notifier is not None` остаётся);
- `worker.py` — dead-letter (chat_id=claimed.tg_chat_id);
- `reminders.py` — недоставленное напоминание (chat_id=row.tg_chat_id or 0).

`.env.example` — секция Webhook дополняется:

```sh
# Telegram chat_id владельца системы: системные алерты (cert, синк,
# cap, дрифт, dead-letter) идут и сюда, не только в админ-чаты клиники
NAVBAT_OWNER_CHAT_ID=
```

- [x] **Step 4: Run** `python -m pytest tests/test_tg_escalation.py tests/test_calendar_sync_loop.py tests/test_nlu_drift.py tests/test_nlu_wrappers.py tests/test_tg_worker.py tests/test_reminders.py tests/test_tg_transport.py -q` → PASS (фейки живут на фолбэке)

- [x] **Step 5: Commit** `feat(obs): owner alert channel for system alerts`

---

### Task 4: cert-алерт раз в день из HealthChecker

**Files:**
- Modify: `src/navbat/health.py`, `src/navbat/supervisor.py` (передать notifier)
- Test: `tests/test_health.py`

- [x] **Step 1: Write the failing test** (конец test_health.py):

```python
def test_expiring_cert_alerts_owner_once_per_day(app_session_factory, clinic_a,
                                                 tmp_path):
    from test_dialog_booking import RecordingNotifier

    cert = _selfsigned(tmp_path, CERT_WARN_DAYS - 5)
    notifier = RecordingNotifier()
    checker = HealthChecker(app_session_factory, clinic_a, cert_path=cert,
                            notifier=notifier)
    checker.snapshot()
    checker.snapshot()  # тот же день — без повтора
    assert len(notifier.calls) == 1
    assert "cert" in notifier.calls[0][1].lower() or "серт" in notifier.calls[0][1]
```

- [x] **Step 2: Run** → FAIL (нет параметра notifier)

- [x] **Step 3: Реализация** — `HealthChecker.__init__` принимает `notifier=None`, хранит `self._notifier`, `self._cert_alerted_on: date | None = None` (импорт date). `_check_cert` при `days < CERT_WARN_DAYS`:

```python
        if days < CERT_WARN_DAYS and self._notifier is not None:
            today = datetime.now(timezone.utc).date()
            if self._cert_alerted_on != today:
                self._cert_alerted_on = today
                system_alert(self._notifier,
                             f"TLS-cert истекает через {days} дн. — проверьте "
                             f"certbot (renewal каждые 12 ч в compose)", {})
        return days >= CERT_WARN_DAYS
```

(импорт `from navbat.dialog.escalation import system_alert`). В `supervisor.py` HealthChecker получает `notifier=notifier`.

- [x] **Step 4: Run** `python -m pytest tests/test_health.py tests/test_supervisor.py -q` → PASS

- [x] **Step 5: Commit** `feat(obs): daily owner alert on expiring TLS cert`

---

### Task 5: финал — полный сьют, smoke JSON-логов, push

- [x] **Step 1:** `python -m pytest -q` → `579 passed` (568 + 3 logging + 4 p95/queue/health + 3 escalation + 1 cert-алерт).
- [x] **Step 2:** smoke: пересборка app-образа, `up -d`, проверить `docker compose ... logs app | tail` — строки парсятся как JSON (`{"ts": ...`); `/health` содержит `p95_response_sec_1h`; down.
- [x] **Step 3:** `python -m navbat.onboard --demo`, `python -m navbat --check` → [OK]; отметить чекбоксы плана; `git push`.

## Definition of Done (C-3)

- [x] Новые тесты зелёные, полный сьют зелёный (число зафиксировано).
- [x] В контейнере stdout — JSON-строки; /health отдаёт p95.
- [x] Системные алерты доходят до владельца (юнит-доказательство веером).
- [x] Демо восстановлено, --check [OK], всё в origin.
