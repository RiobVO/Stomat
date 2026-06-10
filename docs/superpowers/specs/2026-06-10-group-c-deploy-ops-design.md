# Спека: группа C — деплой и эксплуатация (Production-Ready v1.0)

Дата: 2026-06-10. Статус: одобрено пользователем (приоритет «группа C →
затем витрина», стек stdlib-минимализм, внешних ресурсов нет).

## Решения пользователя, зафиксированные на старте

- Внешних ресурсов НЕТ (VPS, домен, S3, аккаунты) — строим
  **provider-agnostic заготовку**: всё разворачивается и проверяется
  локально в Docker; при покупке VPS — деплой по runbook за час.
- Стек: **stdlib-минимализм** — ноль новых Python-зависимостей;
  расширяем существующие паттерны (stdlib HTTP-сервер, logging,
  cryptography уже в зависимостях).
- После группы C — отдельный батч «витрина для покупателя»
  (демо-сценарий продажи, вид /stats глазами владельца клиники).

## Цель и критерий готовности

Закрыть 6 пунктов группы C чеклиста BRIEF разд. 14. Готово =

1. Прод-стек поднимается локально одной командой
   (`docker compose -f deploy/docker-compose.prod.yml up -d`),
   smoke-тест проходит: миграции применились, app отвечает на /health,
   nginx проксирует webhook-путь.
2. Restore из бэкапа **прогнан руками локально**, шаги и фактический
   RTO записаны в runbook.
3. Все новые код-пути закрыты TDD-тестами, полный сьют — серия 8/8.
4. `docs/DEPLOY.md` ведёт от голого VPS до работающей клиники без
   обращения к разработчику исходников.

## Ограничения и принципы

- ДЕНЬГИ: никаких живых LLM-вызовов (в т.ч. в /health), никаких платных
  сервисов/подписок. S3-выгрузка бэкапов — опциональный конфиг,
  выключенный по умолчанию.
- Дев-окружение не трогаем: корневой `docker-compose.yml` (postgres
  :5434) остаётся как есть; прод-стек целиком в `deploy/`.
- Одна клиника = один app-процесс (решение v1.0); мультиклиника на
  одном VPS = несколько app-контейнеров за одним nginx. Флот-операционка
  (canary, центральный мониторинг) — Ф3, сюда не тянем.
- Async-стек (FastAPI/uvicorn) отвергнут: переписывает обкатанный
  транспорт, выигрыш только на флоте 10+ клиник (Ф3).

## Архитектура целевого стенда

```
VPS (docker compose, restart: unless-stopped везде)
├── nginx        :443 TLS (certbot) ──┬── /webhook/<clinic_id> → app:8443
│                :80 → redirect/ACME  ├── /gcal/push/<token>   → app:8443
│                                     └── (health НЕ публикуется наружу)
├── certbot      webroot-renewal, сертификаты в named volume
├── app          (контейнер на клинику) entrypoint:
│                alembic upgrade head → exec python -m navbat
│                ├── webhook-сервер :8443 (существующий + /gcal/push)
│                └── health-сервер  :8080 (новый, внутренняя сеть)
├── postgres     16-alpine, archive_mode=on, WAL → отдельный volume
└── backup       sidecar: pg_basebackup по cron каждые 2 ч + ротация
                 + опциональный push в S3-совместимое (выключен)
```

## Секция 1. Контейнеризация

- `deploy/Dockerfile`: python:3.12-slim, `pip install .`,
  непривилегированный пользователь, `COPY` только нужного
  (`src/`, `migrations/`, `alembic.ini`, `spike_nlu/prompts/`).
- `deploy/entrypoint.sh`: `alembic upgrade head` под `NAVBAT_ADMIN_DSN`,
  затем `exec python -m navbat ...` (exec → PID 1 получает сигналы).
  Флаги supervisor передаются через env (`NAVBAT_CLINIC_ID`,
  `NAVBAT_REAL=1`, `NAVBAT_WEBHOOK_URL`).
- `supervisor.py`: обработчик SIGTERM → `stop.set()` (сейчас
  `docker stop` убивает процесс без graceful — блокер). Юнит-тест:
  хэндлер взводит stop-event.
- Fail-fast валидация env при старте: `--real` с dev-ключом
  `NAVBAT_ENC_KEY` → отказ с понятным сообщением; отсутствие
  обязательных переменных — то же.
- `deploy/docker-compose.prod.yml`: все сервисы схемы выше,
  `restart: unless-stopped`, healthcheck app по /health,
  пароли postgres/navbat_app — из `.env` (`NAVBAT_PG_PASSWORD`,
  `NAVBAT_APP_PASSWORD`), а не захардкоженные. Создание роли
  navbat_app — init-скрипт, читающий env (дев-фолбэк остаётся
  для локального compose).
- `deploy/.env.example` — полный список переменных с комментариями.

## Секция 2. Webhook + HTTPS + /health

- `deploy/nginx/`: шаблон конфига с `${DOMAIN}`; TLS-терминация,
  HTTP→HTTPS redirect + ACME webroot; `/webhook/<clinic_id>` и
  `/gcal/push/<token>` → соответствующий app-контейнер (мультиклиника —
  роутинг по пути). Health наружу не публикуется.
- `setWebhook` сейчас не проверяет результат: добавить retry с backoff
  и алерт (админ-чат + канал владельца) после 3 неудач; процесс не
  падает — nginx/certbot могут подняться позже.
- `clinic.tg_webhook_secret` шифруется AES-256-GCM как токен бота
  (миграция + backfill существующих строк).
- `/health` — отдельный stdlib HTTP-сервер (поток в supervisor,
  `NAVBAT_HEALTH_PORT`, default 8080):
  - `db`: `SELECT 1`;
  - `queue`: возраст старейшего pending в `message_queue`
    (порог — алерт «очередь стоит»);
  - `calendar`: возраст `clinic.gcal_last_sync_at` (новое поле, пишет
    sync_loop при успешном цикле) против интервала синка; «календарь
    не настроен» = ok;
  - `cert`: дни до истечения по `fullchain.pem` (ro-volume, парсинг
    через уже имеющийся `cryptography`); < 14 дней → degraded + алерт
    владельцу (раз в день);
  - `llm`: НЕ живой вызов — наличие ключей + доля сбоев из `llm_usage`
    за сегодня;
  - `p95`: за последний час из таймстемпов очереди (секция 3).
  - Ответ: 200 `{"status":"ok",...}` / 503 `{"status":"degraded",
    "checks":{...}}`. Лёгкий режим `?check=db` для docker healthcheck.

## Секция 3. Наблюдаемость

- JSON-логи штатным `logging.Formatter`-наследником:
  `NAVBAT_LOG_FORMAT=json|plain` (plain — дефолт для дев, json — в
  контейнере через env). Поля: ts, level, logger, message + extra
  (clinic_id, chat_id — где доступны). Без structlog.
- p95 ответа: `message_queue.claimed_at` / `completed_at` (миграция),
  заполняются в claim/ack; строка p95 за день — в `/stats` и вечернем
  дайджесте; за час — в /health. SLA-ориентир BRIEF: p95 < 5 с.
- Канал владельца `NAVBAT_OWNER_CHAT_ID` (env, опционален): системные
  алерты — cert истекает, синк календаря мёртв, dead-letter, NLU-дрифт,
  token cap, провал OAuth-refresh — дублируются владельцу системы,
  не только в админ-чаты клиники. Реализация — расширение
  `escalation.py` (`notify_owner`), шлёт ботом клиники.
- Error-tracking «Sentry-класс» в v1.0 закрывается минимально:
  ERROR-уровень в JSON-логах (агрегируемо grep'ом/Loki в Ф3) +
  rate-limited алерт владельцу при необработанном исключении воркера.
  Полноценный Sentry/GlitchTip — осознанный перенос в Ф3, не дыра.

## Секция 4. Kill-switch

- Миграция: `clinic.bot_paused` (bool, default false),
  `clinic.llm_enabled` (bool, default true).
- `/pause [причина]` / `/resume` в админ-чате: при паузе входящие
  пациентов получают вежливое «запись временно по телефону …» (строки
  ru/uz), очередь не копит эскалации, напоминания продолжают ходить
  (отменять записи пауза не должна).
- `/llm off` / `/llm on`: кнопочные сценарии работают полностью,
  свободный текст → меню (тот же мягкий путь, что при token cap);
  NLU не вызывается.
- Глобальный рубильник: `NAVBAT_LLM_DISABLED=1` (extractor-цепочка
  собирается без LLM) и `docker compose stop app` — обе процедуры
  в `docs/OPERATIONS.md`. Для одного VPS этого достаточно; единый
  флот-рубильник — Ф3.

## Секция 5. Бэкапы + проверенный runbook

- postgres: `archive_mode=on`,
  `archive_command='test ! -f /wal_archive/%f && cp %p /wal_archive/%f'`,
  WAL — отдельный named volume.
- backup-sidecar (alpine + postgres-client): `pg_basebackup` каждые
  2 ч (cron), ротация (хранить последние N, default 12), лог результата;
  опциональный push в S3-совместимое хранилище (rclone/awscli) — на
  env-переменных, по умолчанию выключен, включается когда хранилище
  появится.
- RPO: ≤ 2 ч базовым бэкапом, с WAL-архивом — минуты.
  RTO: измерить фактический при локальном прогоне restore.
- Restore-runbook (в `docs/DEPLOY.md`): стоп стека → чистый pgdata →
  разворачивание basebackup → `recovery.signal` (+ PITR
  `recovery_target_time` при необходимости) → старт → проверка
  (`pg_isready`, `alembic current`, `SELECT count(*) FROM clinic`).
  Прогнать руками локально ДО объявления «готово»; зафиксировать RTO.
- Риск, фиксируемый в runbook: бэкапы на том же диске ≠ защита от
  смерти диска; пункт «подключить внешнее хранилище» — первый шаг
  после покупки VPS.
- `NAVBAT_ENC_KEY` и `.env` — в runbook отдельным пунктом: без ключа
  бэкап БД не расшифровать (имена, токены); хранить копию ключа вне
  VPS (менеджер паролей).

## Секция 6. GCal watch + алерт OAuth-refresh

- Push-endpoint `POST /gcal/push/<channel_token>` на существующем
  webhook-сервере: валидация токена канала, тело не парсим (Google
  шлёт заголовки `X-Goog-*`), действие — разбудить sync_loop
  (threading.Event) для немедленного цикла.
- Каналы: `events.watch` per врач-календарь; поля в `doctor`:
  `gcal_channel_id`, `gcal_resource_id`, `gcal_channel_expires_at`
  (миграция). Продление — из sync_loop при приближении expiration.
  Сбой watch (нет HTTPS, квота, верификация) → поллинг продолжает
  работать как раньше; watch включается автоматически при заданном
  `NAVBAT_WEBHOOK_URL` (webhook-режим = публичный HTTPS есть),
  отдельного флага нет.
- `CalendarAuthError` при refresh → алерт сразу (админ-чаты + владелец,
  раз в день), не после 3 циклов: auth-сбой сам не чинится. Порог
  3 циклов остаётся для сетевых/5xx.
- Тесты — фейковый Google-сервер по паттерну существующих test_gcal_*.
- Верификация Google-приложения — шаг runbook на стороне пользователя
  (consent screen → заявка, ~1 неделя; до верификации refresh-токен
  testing-режима живёт 7 дней — предупреждение в DEPLOY.md).

## Что осознанно НЕ делаем в v1.0

- Sentry/GlitchTip-инстанс, Loki/Grafana, Prometheus /metrics — Ф3.
- Async-стек, горизонтальное масштабирование, флот-supervisor — Ф3.
- Живой LLM-пинг в /health — деньги; ключи + дрифт-метрика вместо него.
- Покупка/настройка реальных VPS, домена, S3 — сторона пользователя,
  по runbook.

## Последовательность инкрементов

1. **C-1 Контейнеризация**: Dockerfile, entrypoint (миграции), SIGTERM,
   env-валидация, prod-compose (пока без nginx), параметризация паролей.
2. **C-2 Публичная поверхность**: nginx+certbot, шифрование
   webhook-secret, проверка setWebhook, /health (+ `gcal_last_sync_at`).
3. **C-3 Наблюдаемость**: JSON-логи, таймстемпы очереди + p95,
   канал владельца, алерт на необработанные исключения.
4. **C-4 Kill-switch**: миграция полей, /pause /resume, /llm on|off,
   глобальный рубильник, OPERATIONS.md.
5. **C-5 Бэкапы**: WAL-архив, sidecar, ротация, локальный прогон
   restore, фиксация RTO/RPO.
6. **C-6 GCal watch**: push-endpoint, каналы+продление, мгновенный
   алерт auth-сбоя.
7. **Финал**: DEPLOY.md целиком, smoke прод-стека локально, чекбоксы
   BRIEF разд. 14, серия 8/8, обновление якоря CLAUDE.md.

Каждый инкремент: TDD (RED→GREEN), отдельный коммит с пушем, полный
сьют зелёный. Конкурентные тесты — серия прогонов перед «готово».

## Тестовая стратегия

- Юнит/интеграционные (pytest, существующая инфраструктура): health-чеки
  на фейках, SIGTERM-хэндлер, шифрование webhook-secret (round-trip +
  backfill-миграция на живой БД теста), kill-switch пути воркера и FSM,
  JSON-formatter, p95-запрос, watch-каналы с фейковым Google,
  алерт OAuth-refresh, env-валидация.
- Не юнит-тестируемое (compose, nginx, certbot, restore): локальный
  прогон с записанным выводом в спеку/runbook — `[OK]`-чеклист в
  DEPLOY.md, плюс docker healthcheck как постоянная проверка.
- Платных вызовов в тестах нет; NLU — фейковый экстрактор, как всюду.

## Риски

- ThreadingHTTPServer держит соединения потоками — на масштабе пилота
  (единицы сообщений/сек) запас велик; порог пересмотра — флот Ф3.
- Windows-хост для локальной проверки prod-compose: пути volume и
  права отличаются от Linux-VPS; runbook пишется под Linux, локальный
  smoke помечает отличия.
- Бэкап на том же диске до появления внешнего хранилища — осознанный
  остаточный риск, записан в runbook первым шагом после покупки VPS.
- 7-дневный TTL refresh-токена до верификации Google — алерт + пункт
  runbook; механически не закрывается с нашей стороны.

## Шаги на стороне пользователя (когда решит)

1. VPS (Ташкент) + домен → деплой по DEPLOY.md.
2. S3-совместимое хранилище → включить push бэкапов (env).
3. Верификация Google-приложения (consent screen, заявка).
4. `NAVBAT_OWNER_CHAT_ID` — свой Telegram chat_id для системных алертов.
