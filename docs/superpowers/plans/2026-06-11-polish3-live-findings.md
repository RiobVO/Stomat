# Полировка-3 (находки живого теста) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Спека: `2026-06-11-polish-3-live-findings-design.md` (одобрена).

**Goal:** Закрыть пять находок живого теста: OAuth-гонка (хвост),
book↔other дыра «на завтра», сужение контекст-правила П-1, телефон
шифрованным + русское/именное GCal-событие, сетка дат варианта B.

**Внимание исполнителям:** живой бот ОСТАНОВЛЕН контролёром на время
батча; pytest строго последовательно (общая база :5434).

---

### Task 0: тест + коммит фикса OAuth-гонки (правка уже в дереве)

**Files:** Modify `src/navbat/calendar/auth.py` (уже исправлен — только
если тест потребует доводки); Create/Modify `tests/test_calendar_auth.py`
(если файла нет — создать).

- [ ] Тест: ThreadingHTTPServer c Handler из run_loopback_flow недоступен
  напрямую (замыкание) — вынести фабрику `_make_handler(received,
  got_code)` на уровень модуля (чистый рефактор для тестируемости,
  поведение то же); тест: GET `/?code=abc` потом GET `/favicon.ico` →
  received["code"] == "abc" (не затёрт); GET без code ПЕРВЫМ →
  got_code не взведён.
- [ ] Run полный сьют → PASS.
- [ ] Commit `fix(calendar): oauth loopback no longer loses code to
  favicon race` + push.

### Task А: «на завтра» — other с датой = про запись

**Files:** Modify `src/navbat/dialog/fsm.py` (~строка 279, booking_like);
Tests `tests/test_dialog_backstop.py` (или рядом по паттерну).

- [ ] Тесты: state booking_collect (service задан), extraction
  other+date_ref=tomorrow → ответ со слотами завтра (state
  booking_offer_slots); idle, other+date_ref → запись начинается
  (вопрос услуги при отсутствии service); other БЕЗ даты — поведение
  прежнее (фоллбэк/меню вне сценария).
- [ ] Код: `booking_like = intent == "book" or (intent in ("question",
  "other") and (date_ref or time_ref))`; checkup-дефолт для question
  оставить как есть, для other добавить ТОЛЬКО внутри сценария
  доуточнения не делать (минимальный дифф — довести тестами).
- [ ] Run полный сьют → PASS.
- [ ] Commit `fix(dialog): date-bearing other intent flows into booking`
  + push.

### Task Б: сужение контекст-правила наличия (П-1 ревизия)

**Files:** Modify `src/navbat/dialog/fsm.py` (`_asks_availability`,
`_route_intent`); Tests: `tests/test_availability_question.py` (ревизия
закреплённого контекст-поведения), `tests/test_call_admin_button.py`
(мусор посреди сценария), новые кейсы.

- [ ] Тесты: «уыкп» (other, без слотов) на шаге УСЛУГИ → повтор вопроса
  услуги (кнопки услуг), НЕ выбор дня; второй мусор подряд там же →
  «не понял» + кнопка call_admin + повтор шага; «привет» (other) в
  booking_collect → повтор шага дня (текущий шаг!), не availability-
  прыжок с дефолтной услугой; словарное «а ещё?» посреди сценария →
  по-прежнему выбор дня (П-1 жив для маркеров); «есть окошки?» вне
  сценария → по-прежнему слоты.
- [ ] Код: `_asks_availability` — контекстная ветка (state in booking_*)
  убирается; остаются словарь + `ctx.service or ctx.date` вне сценария?
  НЕТ: правка минимальная — контекст-ветка `state in (...)` заменяется
  на `mentions_availability(message)`-требование; ветка
  `ctx.service or ctx.date` (вне сценария после показа слотов) остаётся.
  Непонятый question/other посреди сценария уходит в `_on_nlu_failure`-
  путь (повтор текущего шага через `_with_reprompt`, счётчик сбоев).
  Довести точную форму тестами; П-1-тесты, закреплявшие старый прыжок,
  переписать осознанно (поведение пересмотрено пользователем).
- [ ] Run полный сьют → PASS.
- [ ] Commit `fix(dialog): availability jump only on explicit markers,
  garbage reprompts current step` + push.

### Task В: телефон пациента шифрованным (модель данных)

**Files:** Create `migrations/versions/0018_patient_phone_encrypted.py`;
Modify `src/navbat/telegram/queue.py`, `src/navbat/dialog/patients.py`,
`src/navbat/dialog/booking_flow.py` (вызов create_*), `src/navbat/
rotate_key.py`, `src/navbat/retention.py` или `/forget`-путь (найти),
`docs/PRIVACY.md`; Tests: `tests/test_queue_redaction.py`,
`tests/test_patients.py` (по фактическим именам файлов).

- [ ] Тесты: enqueue контакта → payload несёт phone_hash И
  phone_encrypted, открытого номера в payload НЕТ; create_patient_with_
  hash(…, phone_encrypted) пишет колонку; demo-путь create_patient(raw)
  шифрует сам; расшифровка возвращает исходный номер; /forget →
  phone_encrypted = NULL; rotate_key перешифровывает колонку (тест
  паттерном существующих 5 колонок).
- [ ] Миграция 0018: `ALTER TABLE patient ADD COLUMN phone_encrypted
  text` (downgrade DROP).
- [ ] Код: шифрование на границе очереди рядом с хэшированием
  (`_redact_contact_phone` → плюс `encrypt_text`), воркер передаёт оба
  в `handle_contact_hashed(chat_id, phone_hash, phone_encrypted, own)`
  (сигнатуру расширить, существующий сырой `handle_contact` шифрует
  сам); PRIVACY.md разд. 7/9 синхронизировать.
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(privacy): store patient phone AES-encrypted alongside
  hash` + push.

### Task Г: GCal-событие — русская услуга, имя, телефон

**Files:** Modify `src/navbat/calendar/sync.py` (сборка тела события —
найти точное место), `docs/PRIVACY.md`; Tests: `tests/test_calendar_
sync.py` (по фактическому имени).

- [ ] Тесты: событие confirm-записи → summary «Отбеливание — {имя}
  (Navbat)» (label через service_label('ru'), имя расшифровано);
  description содержит «Телефон: +998…» (расшифрован); пациент без
  имени/телефона → строки пропущены, summary деградирует до
  «Отбеливание (Navbat)»; gcal_import-события не трогаются.
- [ ] Код: расшифровка имени/телефона в точке сборки события (паттерн
  doctor_list/decrypt_text); PRIVACY.md «внешние сервисы»: имя+номер
  уходят в Google Calendar клиники (решение владельца 11.06).
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(calendar): russian service label, patient name and
  phone in event` + push.

### Task Д: сетка дат варианта B вместо списка

**Files:** Modify `src/navbat/dialog/calendar_view.py` (перепись вьюхи),
`src/navbat/dialog/calendar_flow.py` (обработчики cal:), `src/navbat/
dialog/replies.py` (шаблоны, если нужны); Tests: `tests/test_calendar_
view.py`, `tests/test_calendar_flow.py` (по фактическим именам).

- [ ] Тесты: «📅 Выбрать дату» → сетка текущего месяца: заголовок
  «Июнь 2026», ряды по 7 (пн–вс), свободные дни «•N» (callback
  cal:day:ISO), занятые/прошлые «N» (noop + toast), навигация
  «◀ | ▶» по месяцам (edit на месте, прошлый месяц не листается,
  горизонт 90 дней вперёд); клик «•N» → существующий day-view слотов;
  legacy cal:nav:ISO из старых сообщений → открывает сетку месяца этой
  даты; cal:noop → toast без падения; пустой месяц (0 свободных) →
  сетка без «•» + строка «нет свободных дней в этом месяце».
- [ ] Код: calendar_view.py — чистый рендер сетки (месяц → строки
  кнопок); flow — маршрутизация cal:nav (теперь месяц), cal:day,
  cal:noop; кнопки шага дня (Сегодня/Завтра/Послезавтра/Выбрать дату)
  не трогать.
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(dialog): month grid with free-day markers replaces
  date list` + push.

### Task Финал

- [ ] Полный сьют СЕРИЕЙ 8/8; демо-клиника восстановлена; поля FAQ
  (address/payment/phone) и календарь врача перепривязаны (онбординг-
  команды у контролёра); `python -m navbat --check` все [OK]; CI зелёный.
- [ ] DEMO.md: сетка дат вместо списка в сценарии показа (правка
  формулировок П-5-шагов), GCal-событие с именем.
- [ ] Commit `docs(demo): polish-3 grid + calendar event walkthrough`
  + push; якорь CLAUDE.md (полировка-3 закрыта + следующий шаг);
  бот перезапущен для продолжения живого теста.

## Definition of Done

- «на завтра» на шаге дня даёт слоты завтра; «привет»/мусор посреди
  записи повторяет текущий шаг (2-й раз — кнопка call_admin), словарное
  «а ещё?» по-прежнему даёт выбор дня.
- Событие в Google: «Услуга — Имя (Navbat)» + телефон в description.
- «📅 Выбрать дату» открывает месячную сетку с «•», листается месяцами.
- OAuth-фикс закреплён тестом; серия 8/8; --check [OK]; CI зелёный;
  живой бот снова поднят.
