# C-1 Контейнеризация — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Приложение запускается контейнером: миграции при старте, graceful по SIGTERM, прод-compose (postgres+app) с паролями из env — первый инкремент группы C по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md`.

**Architecture:** Прод-стек живёт в `deploy/` и не трогает дев-окружение (корневой `docker-compose.yml` остаётся). Образ — python:3.12-slim с `pip install -e .[llm]` (editable обязателен: `telegram/app.py:36` резолвит фикстуры как `parents[3]/spike_nlu` — site-packages-установка сломает путь). Entrypoint: `alembic upgrade head` → `exec python -m navbat` (exec → PID 1 получает SIGTERM).

**Tech Stack:** stdlib (signal, threading), Docker/Compose, alembic. Ноль новых Python-зависимостей.

**Контекст кодовой базы (прочитать перед стартом):**
- `src/navbat/supervisor.py` — точка входа `main()`, stop-event на строке 228, polling на 257, `KeyboardInterrupt`-обработка 258-263. `DEV_ENC_KEY` импортируется из `navbat.onboard` (строка 31).
- `src/navbat/envfile.py` — `.env` грузится в `os.environ`, реальное окружение главнее файла.
- `tests/conftest.py` — фикстуры ходят в postgres :5434, ставят тестовый `NAVBAT_ENC_KEY` (≠ DEV-ключа).
- Тесты НЕ гонять параллельно с другим pytest (TRUNCATE-фикстуры на общей базе).

---

### Task 1: SIGTERM = graceful shutdown

Сейчас `docker stop` убивает процесс мгновенно: graceful есть только у Ctrl+C (KeyboardInterrupt). Делаем SIGTERM эквивалентом — взводит тот же stop-event.

**Files:**
- Modify: `src/navbat/supervisor.py`
- Test: `tests/test_supervisor.py`

- [x] **Step 1: Write the failing test**

Добавить в конец `tests/test_supervisor.py`:

```python
# ── SIGTERM (C-1): docker stop должен гасить штатно, как Ctrl+C ─────────────

def test_sigterm_handler_sets_stop_event():
    import signal
    import threading

    from navbat.supervisor import install_sigterm_handler

    previous = signal.getsignal(signal.SIGTERM)
    try:
        stop = threading.Event()
        install_sigterm_handler(stop)
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # прямой вызов: кросс-платформенно
        assert stop.is_set()
    finally:
        signal.signal(signal.SIGTERM, previous)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_supervisor.py::test_sigterm_handler_sets_stop_event -q`
Expected: FAIL — `ImportError: cannot import name 'install_sigterm_handler'`

- [x] **Step 3: Write minimal implementation**

В `src/navbat/supervisor.py` добавить `import signal` к импортам (после `import os`) и функцию после `parse_offsets`:

```python
def install_sigterm_handler(stop: threading.Event) -> None:
    """docker stop шлёт SIGTERM — гасим теми же рельсами, что Ctrl+C."""
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
```

В `main()` сразу после `stop = threading.Event()` (строка ~228):

```python
    stop = threading.Event()
    install_sigterm_handler(stop)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_supervisor.py -q`
Expected: все PASS (4 старых + 1 новый)

- [x] **Step 5: Commit**

```bash
git add src/navbat/supervisor.py tests/test_supervisor.py
git commit -m "feat(deploy): graceful shutdown on SIGTERM (docker stop)"
```

---

### Task 2: fail-fast валидация env для --real

`--real` с dev-ключом шифрования = боевые PII под общеизвестным ключом. Сейчас `main()` молча делает `setdefault(NAVBAT_ENC_KEY, DEV_ENC_KEY)` для любого режима.

**Files:**
- Modify: `src/navbat/supervisor.py`
- Test: `tests/test_supervisor.py`

- [x] **Step 1: Write the failing tests**

Добавить в конец `tests/test_supervisor.py`:

```python
# ── env-валидация --real (C-1): dev-ключ и пустые API-ключи недопустимы ─────

def _fresh_key() -> str:
    import base64
    import os as _os
    return base64.b64encode(_os.urandom(32)).decode()


def test_validate_real_env_rejects_dev_enc_key(monkeypatch):
    from navbat.onboard import DEV_ENC_KEY
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    problems = validate_real_env()
    assert any("NAVBAT_ENC_KEY" in p for p in problems)


def test_validate_real_env_rejects_missing_enc_key(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.delenv("NAVBAT_ENC_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    problems = validate_real_env()
    assert any("NAVBAT_ENC_KEY" in p for p in problems)


def test_validate_real_env_requires_openai_key(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", _fresh_key())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    problems = validate_real_env()
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_validate_real_env_accepts_prod_config(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", _fresh_key())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert validate_real_env() == []
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_supervisor.py -q -k validate_real_env`
Expected: 4 FAIL — `ImportError: cannot import name 'validate_real_env'`

- [x] **Step 3: Write minimal implementation**

В `src/navbat/supervisor.py` после `install_sigterm_handler`:

```python
def validate_real_env() -> list[str]:
    """--real = боевой режим: PII под dev-ключом и пустые API-ключи — отказ."""
    problems = []
    enc_key = os.environ.get("NAVBAT_ENC_KEY")
    if not enc_key or enc_key == DEV_ENC_KEY:
        problems.append(
            "NAVBAT_ENC_KEY: для --real нужен боевой ключ (base64 от 32 байт),"
            " dev-ключ недопустим")
    if not os.environ.get("OPENAI_API_KEY"):
        problems.append("OPENAI_API_KEY не задан — --real без него не работает")
    return problems
```

В `main()` после `load_env_file()` и ПЕРЕД `os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)`:

```python
    load_env_file()
    if args.real and not args.check:
        problems = validate_real_env()
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
```

(`--check --real` не валидируем жёстко: run_check сам печатает [FAIL] по OPENAI_API_KEY — поведение чек-листа не меняем.)

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_supervisor.py -q`
Expected: все PASS

- [x] **Step 5: Commit**

```bash
git add src/navbat/supervisor.py tests/test_supervisor.py
git commit -m "feat(deploy): fail-fast env validation for --real mode"
```

---

### Task 3: Dockerfile + entrypoint + .gitattributes

**Files:**
- Create: `deploy/Dockerfile`
- Create: `deploy/entrypoint.sh`
- Create: `.gitattributes` (в корне репо — его ещё нет)

- [x] **Step 1: Create `.gitattributes`**

Хост — Windows (CRLF), скрипты уходят в Linux-образ: без принудительного LF entrypoint упадёт с `/bin/sh^M: bad interpreter`.

```gitattributes
# shell-скрипты исполняются в Linux-контейнерах — строго LF
*.sh text eol=lf
```

- [x] **Step 2: Create `deploy/entrypoint.sh`**

```sh
#!/bin/sh
# Миграции при каждом старте (идемпотентно), затем exec: PID 1 = python,
# SIGTERM от docker stop доходит до supervisor.install_sigterm_handler.
set -e

if [ -z "$NAVBAT_ADMIN_DSN" ]; then
    echo "[entrypoint] FAIL: NAVBAT_ADMIN_DSN не задан (нужен для alembic)" >&2
    exit 1
fi

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

exec python -m navbat \
    ${NAVBAT_CLINIC_ID:+--clinic "$NAVBAT_CLINIC_ID"} \
    ${NAVBAT_REAL:+--real} \
    ${NAVBAT_WORKERS:+--workers "$NAVBAT_WORKERS"} \
    ${NAVBAT_SYNC_INTERVAL:+--sync-interval "$NAVBAT_SYNC_INTERVAL"} \
    ${NAVBAT_REMINDER_OFFSETS:+--reminder-offsets "$NAVBAT_REMINDER_OFFSETS"}
```

- [x] **Step 3: Create `deploy/Dockerfile`**

```dockerfile
# Сборка из корня репо: docker build -f deploy/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

# editable-установка обязательна: код резолвит spike_nlu относительно
# исходного дерева (telegram/app.py: parents[3]/spike_nlu) — обычная
# установка в site-packages ломает путь к фикстурам и промпту.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .[llm]

COPY alembic.ini ./
COPY migrations ./migrations
COPY spike_nlu/prompts ./spike_nlu/prompts
COPY spike_nlu/data/messages.jsonl ./spike_nlu/data/messages.jsonl
COPY deploy/entrypoint.sh /entrypoint.sh

RUN useradd --create-home navbat && chmod +x /entrypoint.sh
USER navbat

ENTRYPOINT ["/entrypoint.sh"]
```

- [x] **Step 4: Verify build**

Run (из корня репо): `docker build -f deploy/Dockerfile -t navbat:dev . 2>&1 | tail -5`
Expected: последняя строка содержит `naming to docker.io/library/navbat:dev` (или `Successfully tagged navbat:dev`), exit code 0.

- [x] **Step 5: Commit**

```bash
git add .gitattributes deploy/Dockerfile deploy/entrypoint.sh
git commit -m "feat(deploy): app Dockerfile with migrations-on-start entrypoint"
```

---

### Task 4: прод-compose + параметризованная роль БД + .env.example

**Files:**
- Create: `deploy/docker-compose.prod.yml`
- Create: `deploy/initdb/01-app-role.sh`
- Create: `deploy/.env.example`

- [x] **Step 1: Create `deploy/initdb/01-app-role.sh`**

Прод-аналог `docker/init.sql`, но пароль из env, не захардкожен:

```sh
#!/bin/sh
# Роль приложения: непривилегированная, НЕ владелец таблиц, НЕ bypassrls —
# RLS работает только против такой роли. Пароль из NAVBAT_APP_PASSWORD.
set -e

if [ -z "$NAVBAT_APP_PASSWORD" ]; then
    echo "[initdb] FAIL: NAVBAT_APP_PASSWORD не задан" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'navbat_app') THEN
        CREATE ROLE navbat_app LOGIN PASSWORD '${NAVBAT_APP_PASSWORD}';
    ELSE
        ALTER ROLE navbat_app LOGIN PASSWORD '${NAVBAT_APP_PASSWORD}';
    END IF;
END
\$\$;
SQL
```

- [x] **Step 2: Create `deploy/docker-compose.prod.yml`**

```yaml
# Прод-стек Navbat: docker compose -f docker-compose.prod.yml up -d
# Запускать из deploy/; конфигурация — deploy/.env (см. .env.example).
# nginx/certbot добавляются в C-2, backup-sidecar — в C-5.
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${NAVBAT_PG_PASSWORD:?задай в deploy/.env}
      POSTGRES_DB: navbat
      NAVBAT_APP_PASSWORD: ${NAVBAT_APP_PASSWORD:?задай в deploy/.env}
    volumes:
      - ./initdb:/docker-entrypoint-initdb.d:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d navbat"]
      interval: 5s
      timeout: 3s
      retries: 12
    # порт наружу НЕ публикуем: БД доступна только внутри compose-сети

  app:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    image: navbat:latest
    restart: unless-stopped
    env_file: .env
    environment:
      # внутри compose-сети postgres живёт на :5432 под именем сервиса
      NAVBAT_ADMIN_DSN: postgresql+psycopg://postgres:${NAVBAT_PG_PASSWORD}@postgres:5432/navbat
      NAVBAT_APP_DSN: postgresql+psycopg://navbat_app:${NAVBAT_APP_PASSWORD}@postgres:5432/navbat
    depends_on:
      postgres:
        condition: service_healthy
    # polling-цикл проверяет stop раз в long-poll (до 30 с) — даём время
    stop_grace_period: 40s

volumes:
  pgdata:
```

- [x] **Step 3: Create `deploy/.env.example`**

```sh
# Скопируй в deploy/.env и заполни. Файл .env — в .gitignore, не коммитить.

# ── PostgreSQL ───────────────────────────────────────────────────────────
# Суперпользователь БД (миграции) и роль приложения. Сгенерируй стойкие:
#   python -c "import secrets; print(secrets.token_urlsafe(24))"
NAVBAT_PG_PASSWORD=
NAVBAT_APP_PASSWORD=

# ── Шифрование PII (имена пациентов, токены) ────────────────────────────
# base64 от 32 случайных байт. Сгенерируй:
#   python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
# ХРАНИ КОПИЮ ВНЕ СЕРВЕРА: без ключа бэкап БД не расшифровать.
NAVBAT_ENC_KEY=

# ── Клиника ──────────────────────────────────────────────────────────────
# UUID клиники (python -m navbat.onboard --list); пусто = демо-клиника.
NAVBAT_CLINIC_ID=
# 1 = боевой NLU (gpt-4o-mini, платно). Пусто = фикстурный NLU (демо).
NAVBAT_REAL=

# ── LLM ──────────────────────────────────────────────────────────────────
OPENAI_API_KEY=
# fallback-провайдер; пусто = каскада нет
GEMINI_API_KEY=

# ── Онбординг демо-клиники (только для smoke/демо) ──────────────────────
NAVBAT_TG_TOKEN=
NAVBAT_TG_ADMIN_CHAT=

# ── Опционально ──────────────────────────────────────────────────────────
# NAVBAT_WORKERS=2
# NAVBAT_SYNC_INTERVAL=60
# NAVBAT_REMINDER_OFFSETS=1440,120
```

- [x] **Step 4: Verify compose config**

Run (из `deploy/`, предварительно `cp .env.example .env` и заполнив NAVBAT_PG_PASSWORD/NAVBAT_APP_PASSWORD/NAVBAT_ENC_KEY любыми валидными значениями):
`docker compose -f docker-compose.prod.yml config --quiet && echo CONFIG-OK`
Expected: `CONFIG-OK` (без warning'ов о незаданных переменных)

- [x] **Step 5: Commit**

```bash
git add deploy/docker-compose.prod.yml deploy/initdb/01-app-role.sh deploy/.env.example
git commit -m "feat(deploy): production compose with parameterized db role"
```

---

### Task 5: локальный smoke прод-стека + полный сьют

Прогон всей цепочки на чистом томе: initdb-роль → миграции entrypoint'ом → онбординг демо → `--check` изнутри контейнера → graceful stop.

**Files:** только runbook-вывод (фиксируется в DEPLOY.md в финальном инкременте; здесь — проверка руками).

- [x] **Step 1: Чистый запуск postgres**

Run (из `deploy/`):
```bash
docker compose -f docker-compose.prod.yml down -v   # чистый том
docker compose -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.prod.yml logs postgres | grep -c "01-app-role" || true
```
Expected: в логах postgres есть строка про выполнение `01-app-role.sh`, ошибок нет.

- [x] **Step 2: Миграции + онбординг демо через app-образ**

У compose `run` entrypoint сохраняется — обходим его явно:
```bash
docker compose -f docker-compose.prod.yml run --rm --entrypoint sh app -c "alembic upgrade head && python -m navbat.onboard --demo"
```
Expected: `[OK] демо-клиника: 00000000-0000-4000-8000-000000000d31` и `[OK] токен бота записан…` (токен берётся из NAVBAT_TG_TOKEN в deploy/.env).

- [x] **Step 3: Старт app + --check изнутри**

```bash
docker compose -f docker-compose.prod.yml up -d app
docker compose -f docker-compose.prod.yml exec app python -m navbat --check
```
Expected: все строки `[OK]` (Google Calendar — «не настроен», это OK), exit code 0.

- [x] **Step 4: Graceful stop**

```bash
docker compose -f docker-compose.prod.yml stop app
docker compose -f docker-compose.prod.yml logs app | tail -5
```
Expected: в хвосте логов `останавливаюсь…` (SIGTERM дошёл и обработан штатно), контейнер остановился без SIGKILL (`docker inspect` ExitCode=0; проверка: `docker inspect deploy-app-1 --format '{{.State.ExitCode}}'` → `0`).

- [x] **Step 5: Прибраться и прогнать полный сьют против дев-БД**

```bash
docker compose -f docker-compose.prod.yml down
cd .. && python -m pytest -q
```
Expected: `554 passed` (549 базовых + 1 SIGTERM + 4 env-валидации), без падений.
ВАЖНО: дев-postgres (:5434, корневой compose) должен быть поднят; прод-стек портов не публикует и не конфликтует.

- [x] **Step 6: Восстановить демо дев-базы и финальный коммит-пуш**

pytest TRUNCATE'ит дев-базу — вернуть демо-клинику:
```bash
python -m navbat.onboard --demo
python -m navbat --check
git push
```
Expected: `--check` все [OK]; push успешен.

---

## Definition of Done (C-1)

- [x] SIGTERM-тест и 4 env-валидационных теста зелёные; полный сьют зелёный.
- [x] `docker build` проходит; smoke Task 5 пройден целиком с записанными выводами.
- [x] Дев-окружение не задето: корневой `docker-compose.yml` не изменён, демо-клиника восстановлена, `--check` [OK].
- [x] Все коммиты запушены в origin/master.
