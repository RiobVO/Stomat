# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Navbat — AI-администратор записи для стоматологий (Ташкент)

Бот в Telegram, записывает пациентов 24/7 на узбекском и русском.
Полный чертёж: BRIEF.md. Это карта пункта назначения, не текущей задачи.

## ТОЧКА ПРОДОЛЖЕНИЯ — новая сессия начинает отсюда

- Ф1.5 закрыта (D.4 кросс-ревью LLM, коммит 0b7815a; «юрист по ПД» СНЯТ
  решением пользователя — тему не поднимать; docs/PRIVACY.md остаётся
  инженерной картой данных).
- 08.06.2026 по состязательному аудиту кода (3 прохода) заведён
  docs/PILOT_HARDENING.md и закрыт БЛОК ДЫР через TDD: C1 (PII на --real,
  9c3e59a), M2 (имена в LLM, 8295a02), M1 (некруглое время → эскалация,
  0cc1b19), C2 (блок бота → спам эскалаций, 9cd3775), m1 (имя в эскалации,
  26d6514). Отложены с обоснованием: m2 (NLU-сигнал, на пилот), m3 (нет
  репро без искусств. состояния), m4 (product-решение). Серия 8/8 зелёная,
  демо восстановлено, --check [OK].
- P3 РЕФАКТОРИНГ ЯДРА ЗАКРЫТ ЦЕЛИКОМ (порядок по риску R3→R1→R2→R4):
  R3 единый каталог услуг (857608d); R1 слой данных — fsm.py без сырого SQL
  (R1a 92e6781, R1b 88e314b, R1c 5c1b1f7); R2 типизированное состояние
  DialogContext (3dc1ba8); R4 god-object DialogEngine разбит на роутер
  fsm.py + mixin'ы сценариев booking/reschedule/cancel + shared_helpers +
  dialog_common (b00f89c). R4 доказан поведенчески-идентичным: множество
  методов до/после 58↔58 без дублей, тела перенесены дословно, + чистое
  независимое код-ревью (критичного нет). Тестов 526, всё в origin/master.
- CI ЗЕЛЁНЫЙ. Был красным всю сессию по НЕ связанной с P3 причине: тест
  test_telegram_real_path зависел от ambient OPENAI_API_KEY (в GitHub нет →
  sys.exit) — поправлен dummy-ключом (c5edd33). Хвост Node-20 deprecation
  закрыт бампом checkout@v5 + setup-python@v6 (259b59d), annotation ушёл.
- m3 ЗАКРЫТ (cc41b5d): stale-slot без услуги деградирует штатно (R2 снял
  KeyError-класс), закреплён воспроизводящим тестом.
- 09.06.2026 ПРОДУКТОВЫЙ АУДИТ «глазами покупателя» (4 параллельных
  агента: UX пациента, владелец клиники, надёжность, безопасность) →
  закрыт батч B/M (7 пунктов, все CI-зелёные, миграции 0009–0010):
  B1+B2+B3 онбординг реальной клиники без SQL + криптослучайная соль +
  salt NOT NULL (619e5c2); M2 ROI-метрика не показывает 0 — отмены-заранее
  как ценность (f014062); M5 алерт при тихой смерти синка календаря
  (a50ce5b); M3 читаемый контекст эскалации (2446f70); M7 меню-выход не
  понятому пациенту + фикс greeting-wrap (1e7b671); M4 несколько админ-чатов
  на клинику — авторизация по членству + веер алертов (abca97a); M1
  узбекский — ресёрч живой речи подтвердил решения, апостроф закрыт,
  правка консистентности (1c8a35d). M6 (voice/STT) СНЯТ решением
  пользователя. Тестов 545.
- 09.06.2026 ЗАКРЫТ последний неугаданный код-пункт (security MEDIUM
  «открытый телефон в очереди», d5cd3c9, TDD): номер из кнопки контакта
  хэшируется на границе enqueue (queue._redact_contact_phone) — в
  message_queue.payload вместо message.contact.phone_number кладётся
  phone_hash (тот же SHA-256 с солью, что в patient); не-998 → без хэша →
  воркер эскалирует лид. Чистая функция, per-chat порядок очереди цел.
  Диалог: новый вход handle_contact_hashed (хэш-путь воркера) + сырой
  handle_contact сохранён для демо/каналов без очереди; create_patient(raw)
  не тронут (5 фикстур), добавлен create_patient_with_hash. PRIVACY.md
  разд. 7/9 синхронизированы. Серия 8/8 зелёная, демо восстановлено,
  --check [OK], CI зелёный. Тестов 549.
- 10.06.2026 ГРУППА C ЗАПУЩЕНА ПО ЯВНОЙ КОМАНДЕ пользователя («сделать
  production-ready»). Спека одобрена: docs/superpowers/specs/
  2026-06-10-group-c-deploy-ops-design.md (ресурсов нет → provider-agnostic,
  stdlib-минимализм, всё проверяется локально в Docker). Решение: после
  группы C — батч «витрина для покупателя» (демо-показ, /stats глазами
  владельца). Планы по инкрементам: docs/superpowers/plans/2026-06-10-*.
- ЗАКРЫТЫ C-1…C-6 (всё в origin/master, по плану на инкремент, TDD):
  C-1 контейнеризация — Dockerfile (editable: parents[3]/spike_nlu!),
  entrypoint с alembic upgrade, SIGTERM graceful, validate_real_env,
  deploy/docker-compose.prod.yml, smoke на чистом томе (ExitCode=0);
  C-2 публичная поверхность — nginx+certbot (profile web), шифрование
  tg_webhook_secret (0012, backfill проверен round-trip), ensure_webhook
  c retry+алертом, /health (db/queue/calendar/cert/llm-ключи; БЕЗ живого
  LLM-пинга — деньги), clinic.gcal_last_sync_at (0011), webhook-режим
  супервизора, app healthcheck → healthy;
  C-3 наблюдаемость — JSON-логи (NAVBAT_LOG_FORMAT, logging_setup.py),
  p95 ответа (0013 completed_at; /stats+дайджест+health), канал владельца
  NAVBAT_OWNER_CHAT_ID (notify_system + system_alert c фолбэком для фейков),
  дневной cert-алерт;
  C-4 kill-switch — 0014 (bot_paused, llm_enabled), /pause /resume /llm,
  GatedExtractor → LLMDisabledError → меню БЕЗ эскалации, глобальный
  NAVBAT_LLM_DISABLED, docs/OPERATIONS.md. Тестов 594, --check [OK];
  C-5 бэкапы — WAL-архив (archive_command + init-сервис wal-perms: named
  volume отдаётся root'ом, chown до старта БД), sidecar pg_basebackup
  -Ft -z раз в 2 ч с ротацией (busybox head -n -N работает), initdb
  02-replication-hba.sh; restore ПРОГНАН РУКАМИ на прод-стенде: маркер
  после бэкапа доехал из WAL, RTO = 24 сек, runbook + PITR-вариант в
  docs/OPERATIONS.md (грабли: stopped-контейнер держит том → rm -f -s;
  вложенные кавычки из PowerShell → printf \047). Python-кода ноль,
  pytest не гонялся — дев-демо цело, --check [OK];
  C-6 GCal watch — 0015 (поля канала в doctor), API watch_events/
  stop_channel, GcalWatchManager (продление = новый канал за RENEW_LEAD
  до expiration + stop старого; сбой watch → warning, поллинг прикрывает;
  только при заданном --webhook-url), POST /gcal/push/<channel_id> на
  WebhookServer (валидация по doctor.gcal_channel_id, тело не парсится)
  взводит sync_wake → календарный цикл просыпается сразу; CalendarAuthError
  в sync_loop → алерт в ПЕРВЫЙ цикл (раз в день, без дубля порогового),
  восстановление перевзводит. Тестов 607.
- СЛЕДУЮЩИЙ ШАГ: C-7 финал: docs/DEPLOY.md
  («голый VPS → клиника», включая верификацию Google-приложения и
  7-дневный TTL refresh-токена testing-режима), smoke прод-стека,
  чекбоксы BRIEF разд. 14 группы C,
  серия 8/8, обновить этот якорь. Рабочие заметки: Docker Desktop может
  быть выключен — стартануть и ждать демона; deploy/.env сгенерирован
  локально (gitignored, tg-токен скопирован из корневого .env);
  .recon_group_c.md в корне — рабочий артефакт, не коммитить; pytest
  стирает демо-клинику — восстанавливать onboard --demo в конце инкремента.
- На стороне пользователя (когда решит): VPS+домен, S3-хранилище,
  верификация Google-приложения, NAVBAT_OWNER_CHAT_ID. Платные прогоны
  (P4 T1/T2), глубокий узбекский (носитель), m2/m4 — гейты прежние.
- Пилот Ф2 — строго по явной команде.
- Эту секцию ОБНОВЛЯТЬ в конце каждой сессии: где остановились + следующий
  шаг. Это якорь преемственности между чатами.

## Статус проекта

- Ф0 (спайк) и Ф1 (5 инкрементов кодового ядра + button-first вход) — ЗАКРЫТЫ.
  Вся система запускается `python -m navbat`.
- СТРАТЕГИЯ v5 (решение пользователя 06.06.2026): строим до инженерных 100%
  → пилот как запуск готового продукта → продажи. Никаких «для демо сойдёт».
- Ф1.5 Production-Ready v1.0: КОДОВАЯ ЧАСТЬ ЗАКРЫТА 07.06.2026 — группы A
  (диалоговая надёжность), B (LLM-устойчивость), D.2–D.3 (приватность),
  E (продукт); 308 тестов, миграции 0001–0008, всё в origin/master.
  Детали — чекбоксы BRIEF разд. 14. D.4 (узбекские строки) ЗАКРЫТ
  07.06.2026 кросс-ревью LLM; D.1 (юрист по ПД) СНЯТ решением
  пользователя 07.06.2026. Осталась только группа C — строго по явной
  команде пользователя.
- Ключевые решения Ф1.5 (поверх Ф1):
  - escalated не вечен: /start пациентом или /release в админ-чате; счётчик
    сбоев при выходе сбрасывается;
  - «Сейчас клиника закрыта» при запросе «на сегодня» вне рабочего окна дня
    (union графиков врачей; обед ≠ закрыто); clock инжектируется
    в DialogEngine — время тестируемо;
  - выходные дни: /dayoff DD.MM [причина] и /dayopen DD.MM в админ-чате
    (предзаполненный календарь праздников ОТМЕНЁН — решение пользователя:
    многие частные клиники работают в праздники);
  - fallback-LLM: OpenAI → Gemini (nlu/fallback.py + gemini_extractor.py,
    тонкий httpx; включается наличием GEMINI_API_KEY в .env — задан).
    Аутэйдж = ProviderDownError (сеть/5xx/429), кривой JSON failover
    НЕ триггерит. Eval на узбекском ОТМЕНЁН (free tier Google = 20
    запросов/день, billing не включаем) — валидация Gemini живым пилотом,
    риск осознан; eval.py умеет gemini-* (--rpm, --source) на будущее;
  - метрика NLU-дрифта: failures/repairs в llm_usage (0007), алерт админу
    при >20% сбоев за день (≥20 запросов, NAVBAT_NLU_DRIFT_THRESHOLD);
  - промпт в БД: nlu_prompt (0008) + clinic.nlu_prompt_version (NULL =
    встроенный файл); staging = демо-клиника; смена промпта = пин + рестарт;
  - /forget <chat_id> — анонимизация пациента (записи остаются обезличенными,
    будущие НЕ отменяются); retention: чистка очереди/диалогов старше
    NAVBAT_RETENTION_DAYS=90, раз в день из цикла напоминаний;
  - дашборд денег: отмена из напоминания (actor='reminder' в аудите)
    с перезаписью слота → «предотвращено неявок + сохранено сум»
    в /stats и дайджесте.
- Команды админ-чата: /stats, /release <chat>, /dayoff DD.MM [причина],
  /dayopen DD.MM, /forget <chat>. Подсказка /release приходит в алерте
  эскалации.
- Демо-бот НАСТРОЕН и проверен: @MyCompanyDev_bot привязан к демо-клинике
  (токен в clinic.tg_bot_token_encrypted, admin_chat 7082498953, тестовый),
  `python -m navbat --check` — все [OK]. Запуск: `python -m navbat
  --reminder-offsets 4,2`.
- ВАЖНО: pytest TRUNCATE'ит ВСЮ базу, включая демо-клинику с токеном.
  Восстановление — ОДНА команда: `python -m navbat.onboard --demo`
  (токен и админ-чат подтягиваются из локального .env: NAVBAT_TG_TOKEN,
  NAVBAT_TG_ADMIN_CHAT; файл в .gitignore, у пользователя не спрашивать).

## Архитектура (карта для быстрого старта)

Принцип: «LLM — рот, код — мозг» — все решения детерминированы, NLU только
извлекает слоты. Слои (поток сообщения сверху вниз):

- **Вход (`telegram/`).** `transport.py` (polling/webhook) → durable-очередь
  `queue.py` (таблица message_queue, UNIQUE(clinic_id, update_id), двухфазный
  клейм + SKIP LOCKED → порядок per-chat, переживает рестарт). `worker.py`
  тянет из очереди, перехватывает админ-команды (авторизация по членству в
  `clinic.tg_admin_chat_ids`), нумерует callback-кнопки (>64 байт → map в
  `conversation.context['tg_actions']`), зовёт диалог. `api.py` — тонкий
  httpx-клиент Bot API; `escalation.py` шлёт алерты ВСЕМ админ-чатам.
- **Диалог (`dialog/`).** `fsm.py` — роутер `DialogEngine` (входные точки,
  /start+меню до NLU, маршрутизация интента, callback-actions) поверх
  mixin'ов сценариев `booking_flow`/`reschedule_flow`/`cancel_flow` + общих
  `shared_helpers`; константы/протокол — `dialog_common.py`. Состояние —
  типизированный `DialogContext` (`conversation.py`), персист в JSONB (поле
  `extras` хранит adapter-ключи tg_actions). Весь доступ к БД — через
  репозитории (`*_repo.py`, `patients.py`); в fsm НЕТ сырого SQL.
- **Расписание (`scheduling/`).** `engine.py` — hold/confirm/cancel/reschedule;
  занятость гарантирует БД (exclusion constraint с буфером в выражении) +
  advisory-lock на врача (анти-дедлок), код занятость не проверяет.
  `calendar_rules.py` — сетка слотов из working_intervals/праздников.
- **Календарь (`calendar/`).** `sync.py` — reconciliation-синк с Google
  (ручные события вытесняют ботовские с авто-переносом), `guard.py` —
  freeBusy-guard перед confirm, `sync_loop.py` — цикл с алертом при затяжном
  сбое.
- **NLU (`nlu/`).** `schema.py` (Extraction + `SERVICE_KEYS` — единый источник
  услуг), `extractor.py` (Fake/интерфейс), `openai_extractor.py`/
  `gemini_extractor.py` за `fallback.py`, `wrappers.py` (деидентификация
  телефонов перед LLM, дневной token-cap, метрика дрейфа). Промпт —
  `spike_nlu/prompts/system.md` (тот же файл в проде).
- **Фон + супервизор.** `reminders.py` (напоминания через reconciliation из
  appointment, переживают рестарт; вечерний дайджест всем админам),
  `retention.py` (чистка >90 дней). `supervisor.py` (`python -m navbat`) —
  транспорт+воркеры+календарь+напоминания одним процессом; `--check` —
  преддемо-чеклист. Онбординг — `onboard.py`.
- **Мультитенант + крипта (`db/base.py`, `crypto.py`).** Каждая транзакция
  ставит `app.clinic_id` (SET LOCAL); RLS FORCE на всех таблицах изолирует
  клиники; приложение под непривилегированной ролью navbat_app. Имена —
  AES-256-GCM, телефоны — SHA-256-хэш с per-clinic солью.

## Что проверено (факты, не трогать)

- Модель: gpt-4o-mini. Тянет узбекский/русский/смешанный. Nano-модели не тянут.
- NLU на РЕАЛЬНЫХ сообщениях (40 живых): core-5 ~80%; по полям (весь сет 410):
  intent 91%, service 89%, date 94%, time 99%; is_medical 97.5% на real.
- Харнесс, данные и конвенции разметки: `spike_nlu/` (eval.py, prompts/system.md,
  data/messages.jsonl — 410 размеченных, из них 40 real).
- Стоимость: ~$0.26 / 1000 сообщений (копейки; основная статья — хостинг).
- Известная дыра: book↔question на косвенных вопросах о наличии
  («есть время сегодня?») — модель уходит в question. Прикрывается на уровне FSM:
  на вопрос о наличии бот ВСЕГДА отвечает слотами. Промпт под это НЕ дожимать
  (подгонка под тест на малой выборке).
- Реальных сообщений мало (40) — добрать на первом пилоте.

## Принятые решения (конвенции)

- LLM — рот, код — мозг. Все решения (слоты, даты, переходы) — детерминированный код.
- LLM извлекает относительную ссылку времени; абсолютную дату считает код.
- intent enum строго 5: book|reschedule|cancel|question|other. Форсить через
  strict json_schema. Никаких выдуманных значений (medical/kids/greeting запрещены).
- Симптом без услуги → book + service:checkup (это лид, не «медицинский вопрос»).
- Медицинский дисклеймер — код-слой через флаг is_medical, НЕ интент.
- Услуги (canonical, 9): cleaning, extraction, filling, crown, implant,
  checkup, xray, braces, whitening. «consultation» — алиас checkup, не отдельный ключ.
  Оплата/рассрочка → question + service:null.
- date_ref словарь: today|tomorrow|after_tomorrow|next_week|weekday_*|explicit_DD.MM|null.
  Срочность («срочно», «hozir», «tez yordam») → today.
- Телефон пациента — ТОЛЬКО кнопкой Telegram «Поделиться контактом»
  (request_contact, без fallback на ручной ввод — решение пользователя).
  Принимается только собственный контакт (contact.user_id == from.id);
  чужой контакт/текст → повтор кнопки; свой контакт с не-998 номером →
  эскалация админу (тупик). Текст на шагах имени/телефона в NLU не уходит
  (PII), кроме вопросоподобного.
- Вход кнопочный (button-first): /start → выбор языка → постоянное reply-меню
  (Записаться/Перенести/Отменить/Цены/Язык); label'ы меню перехватываются
  точным матчем ДО NLU (ноль токенов на happy path). Свободный текст —
  LLM-fallback (фишка «понимает узбекский» сохранена). «Отменить» посреди
  оформления гасит hold и сразу отвечает «запись отменена»; Цены/Язык —
  прерывание вбок без сброса сценария. Спека:
  docs/superpowers/specs/2026-06-06-button-first-menu-design.md.
- Стек: Python, PostgreSQL (Docker), SQLAlchemy + Alembic, pytest. Без Redis.
- Коммерция: разовая настройка + месячное обслуживание.

## Прогресс по инкрементам Ф1

- [x] 1. Scheduling engine + модель данных — ГОТОВ (24 теста зелёные, включая
      50-поточную гонку; postgres на :5434 — 5433 занят соседним проектом;
      буфер в выражении exclusion constraint через timezone('UTC',...) —
      timestamptz-арифметика в индексах требует IMMUTABLE; записи по врачу
      сериализованы advisory-локом — UPDATE строки в predicate exclusion
      constraint дедлочится с конкурентными INSERT, одиночный прогон теста
      этого НЕ ловит, гонять полный сьют многократно)
- [x] 2. FSM + slot-filling — ГОТОВ (102 теста зелёные, серия 8/8; всё из
      скоупа заложено: кнопки в модели Reply, бэкстоп question+date_ref→слоты
      (без услуги — сетка по checkup), прерывание вопросом = ответ + повтор шага,
      is_medical дисклеймер раз за диалог, 2 ExtractionError подряд → escalated,
      conversation в миграции 0002, демо `python -m navbat.demo` на фикстурах
      спайка — 410 фраз как бесплатные NLU-тесты; имя/телефон до confirm:
      телефон нормализуется к 998…, hash с clinic.salt, имя AES-256-GCM через
      NAVBAT_ENC_KEY; грабли: Windows-пайпы суют BOM в stdin демо)
- [x] 3. Channel adapter — ГОТОВ (140 тестов, серия 8/8; очередь message_queue
      в миграции 0003: ack после обработки (двухфазный клейм), дедуп по
      UNIQUE(clinic_id, update_id), per-chat порядок решает сам клейм-запрос
      (NOT EXISTS + SKIP LOCKED, advisory lock не нужен); транспорты polling и
      webhook (stdlib HTTP-сервер, secret-заголовок) за одной очередью;
      тонкий httpx-клиент Bot API без asyncio-фреймворков; callback_data
      длиннее 64 байт — кнопки нумеруются, map в conversation.context,
      устаревшая кнопка → повтор шага; greeting-дисклеймер первого контакта;
      эскалации в tg_admin_chat_id; запуск: `python -m navbat.telegram
      --clinic <uuid>`, NLU по умолчанию фикстурный — live-smoke бесплатен)
- [x] 4. Календарь — ГОТОВ (173 теста, серия 10/10; тонкий httpx-клиент GCal
      (без google-api-python-client), календарь на врача, reconciliation-sync:
      экспорт по diff time_range↔gcal_synced_range (миграция 0005, ноль чтений
      Google), импорт ручных событий как gcal_import-записи (all-day закрывает
      день), бот — истина для своих событий (ручная правка откатывается+алерт);
      конфликт: ручное вытесняет ботовскую → авто-перенос на ближайший слот
      + reslot-кнопки пациенту + эскалация, живые hold не вытесняются (ждём
      TTL); freeBusy-guard перед confirm (graceful при недоступном Google);
      онбординг python -m navbat.calendar.auth (loopback OAuth), sync-CLI
      python -m navbat.calendar; push-каналы watch — на деплое, базис —
      периодический syncToken-инкремент; грабли: stdlib HTTP-сервер обязан
      вычитать тело запроса до ответа-отказа, иначе флаки WinError 10053)
- [x] 5. Напоминания, надёжность, демо-продукт — ГОТОВ (203 теста, серия 8/8,
      CI зелёный; reminder-таблица в миграции 0006, reconciliation из
      appointment (не таймеры — переживает рестарт), кнопки «Приду»/«Отменить»
      в напоминании (отмена освобождает слот), retry 3 → failed + алерт;
      деидентификация телефонов перед LLM + имена в LLM не уходят (эвристика
      шага имени); дневной token cap (llm_usage, NAVBAT_DAILY_TOKEN_CAP) +
      rate limit >5 сообщ/10с; LLM-таймаут 8с; /stats в админ-чате + вечерний
      дайджест 21:00 (clinic.last_digest_date); супервизор `python -m navbat`
      (транспорт+воркеры+календарь+напоминания одним процессом), онбординг
      `python -m navbat.onboard` (--demo/--tg-token/--calendar/--list),
      преддемо-чеклист `python -m navbat --check`; CI: GitHub Actions,
      сьют ×3 против реального postgres)

Кодовое ядро Ф1 закрыто. Дальше: живое демо → пилот → деплой после продажи.

## Окружение и команды

- Репозиторий: https://github.com/RiobVO/Stomat (origin). Коммиты пушить, не копить локально.
- БД: `docker compose up -d` → postgres :5434 (5433 занят соседним проектом).
- Секреты: локальный `.env` в корне (gitignored) — NAVBAT_TG_TOKEN,
  NAVBAT_TG_ADMIN_CHAT и пр.; грузится автоматически (navbat/envfile.py)
  в supervisor, onboard и demo. Окружение главнее файла.
- Тесты: `python -m pytest` (полный сьют); одиночный —
  `python -m pytest tests/test_x.py::test_y -q`, по подстроке — `-k имя`.
  Lint/format-тулинга нет (только pytest в pyproject). Конкурентные тесты —
  гонять сьют 5–10 раз перед «готово», одиночный прогон дедлоки не ловит;
  параллельный pytest против общей базы ЗАПРЕЩЁН (TRUNCATE-фикстуры рушат).
  CI: GitHub Actions гоняет сьют ×3 на каждый push. Docker Desktop может
  быть не запущен — стартануть и дождаться демона перед docker compose.
- ВСЯ СИСТЕМА: `python -m navbat` (супервизор: канал+календарь+напоминания;
  `--check` — преддемо-чеклист; `--real` — платный NLU, только по явной команде;
  `--reminder-offsets 4,2` — минуты, для демо).
- Онбординг: `python -m navbat.onboard` (--demo | --tg-token | --doctor
  +--calendar | --prompt-upload FILE [--note] | --prompt-pin <N|file> |
  --import-calendar | --list). Выходные дни — /dayoff в админ-чате,
  не онбординг. Сценарий показа и чеклист 5 тест-диалогов: docs/DEMO.md.
- Демо диалога в консоли: `python -m navbat.demo` (фейковый NLU, без API).
- NLU-харнесс: `cd spike_nlu; python eval.py` (OPENAI_API_KEY в user-env).

## Хвосты (мелкое, не забыть)

- Перепрогон цифр по real после правки голда u_020 (~$0.01, только по явной
  команде — деньги). Сама правка сделана (service=checkup).
- Крупные хвосты (узбекские строки носителем, GCal watch+верификация,
  LLM-fallback, VPS-деплой) переехали в чеклист Production-Ready v1.0 —
  BRIEF.md разд. 14, не дублировать здесь.

## Правила работы

- ДЕНЬГИ: вызовы LLM API (OpenAI, Gemini; eval.py, любые LLM-запросы) — ТОЛЬКО
  по явной команде пользователя. Тесты и разработка — на фейковом/записанном
  экстракторе, никаких «прогоню разок проверить». Бюджет токенов ограничен.
  Включение billing/платных тарифов НЕ предлагать (решение 07.06.2026).
- ДЕПЛОЙ/VPS: НЕ предлагать, не планировать, не включать в «следующие шаги».
  Пользователь решает сам; группа C чеклиста — самый последний пункт, строго
  по его явной команде.
- Сложная задача — сначала plan mode (вопросы + структура + тесты ДО кода).
- Verification-first: тесты пишем первыми, реализация — до зелёного.
- Строим строго текущий инкремент. Ничего из будущих инкрементов «на будущее».
- Не пересказывай BRIEF.md — читай сам.
