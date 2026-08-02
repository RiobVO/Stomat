# Пакет показа, инкремент 1: фиксы + takeover — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Правило проекта: субагенты НЕ запускают pytest против общей базы —
> тесты гоняет оркестратор-сессия эксклюзивно.

**Goal:** починить два подтверждённых дефекта показа и построить канал
«админ отвечает пациенту через бота» (фича 1 спеки
`docs/superpowers/specs/2026-08-02-selling-demo-pack-design.md`).

**Architecture:** свайп-реплай админа на алерт → таблица-якорь
`admin_relay` связывает сообщение в админ-чате с пациентом → бот
пересылает текст пациенту. Входящие escalated-пациента идут карточками 💬
во все админ-чаты через расширение нотификатора (getattr-фолбэк, как
`fyi_alert`). Якоря пишутся при отправке алертов эскалации и карточек 💬.

**Tech Stack:** Python 3.12, PostgreSQL (RLS FORCE), SQLAlchemy + Alembic,
pytest; телеграм-слой — свой тонкий httpx-клиент.

**Инварианты проекта (нарушение = провал задачи):**
- Тест пишется ПЕРВЫМ, реализация — до зелёного; каждый тест проверяется
  откатом фикса (слепой тест = провал).
- В fsm нет сырого SQL — только репозитории. Тексты пациенту —
  `replies.py` (`t()`, ru+uz), админу — `telegram/admin_texts.py`
  (`at()`, ru+uz).
- Новые арендные таблицы: RLS ENABLE+FORCE, policy `tenant_isolation`,
  GRANT для navbat_app (+ sequence) — образец `migrations/versions/0019_waitlist.py`.
- Комментарии в коде объясняют «почему», на русском, в стиле соседних.
- Коммиты: prefix feat/fix/docs, автор dejavuu, никаких упоминаний
  ассистентов.

---

### Task 1: фикс потери полей Reply под мед-дисклеймером

**Files:**
- Modify: `src/navbat/dialog/fsm.py` — `_with_medical_disclaimer` (~476)
- Test: `tests/test_dialog_medical.py` (дописать в существующий)

Баг: обёртка собирает `Reply(text, reply.buttons)` и теряет
`button_rows`/`menu`/`contact_request`. Достижимо: медицинское сообщение
с временем при 2+ свободных врачах → `_ask_doctor` отдаёт `button_rows`
(`shared_helpers.py:226`) → пациент получает дисклеймер без кнопок выбора.

- [ ] **Step 1: failing-тест.** В `tests/test_dialog_medical.py` по образцу
  соседних тестов файла (фикстуры диалога с фейковым NLU): сообщение с
  `is_medical=True`, интентом book и слотом времени, на которое свободны
  ДВА врача (см. как test-файлы `test_doctor_choice.py` готовят двух
  врачей). Ассерты: `reply.text` начинается с дисклеймера, И
  `reply.button_rows` НЕ пуст (кнопки врачей дожили).
- [ ] **Step 2: прогон (оркестратор)** — тест падает на пустых button_rows.
- [ ] **Step 3: фикс.** В `_with_medical_disclaimer` заменить сборку нового
  Reply на `replace(reply, text=f"{disclaimer}\n\n{reply.text}")`
  (`dataclasses.replace` уже импортирован в fsm.py). Комментарий: почему
  replace — обёртка обязана сохранять ВСЕ поля Reply, ручная сборка уже
  теряла button_rows.
- [ ] **Step 4: прогон (оркестратор)** — зелёный; проверка откатом фикса.
- [ ] **Step 5: commit** `fix(dialog): мед-дисклеймер сохраняет все поля Reply`

### Task 2: телефон клиники в сообщении паузы

**Files:**
- Modify: `src/navbat/dialog/replies.py` — новый шаблон `bot_paused_phone`
- Modify: `src/navbat/telegram/worker.py` — `_paused_reply` (~335)
- Modify: `docs/UZ_STRINGS.md` — новая строка в пакет вычитки
- Test: `tests/test_kill_switch.py` (дописать)

- [ ] **Step 1: failing-тест.** Пауза включена, у клиники задан
  `phone='+998 71 200-00-00'` → пациент получает текст с этим номером.
  Второй тест: телефон НЕ задан → прежний текст `bot_paused` без "None".
- [ ] **Step 2: прогон (оркестратор)** — падает.
- [ ] **Step 3: реализация.** Шаблон `bot_paused_phone` (ru/uz):
  ru: «Запись через бота временно приостановлена. Позвоните в клинику:
  {phone} — или загляните позже.»
  uz: «Bot orqali yozilish vaqtincha to'xtatildi. Klinikaga qo'ng'iroq
  qiling: {phone} — yoki keyinroq urinib ko'ring.»
  `_paused_reply`: в существующей транзакции добрать `clinic.phone`
  (запрос по образцу `_bot_paused`), выбор шаблона по наличию.
  UZ_STRINGS.md дополнить (формат подскажет его тест-страж).
- [ ] **Step 4: прогон (оркестратор)** — зелёный; откат-проверка.
- [ ] **Step 5: commit** `feat(dialog): телефон клиники в ответе паузы`

### Task 3: миграция 0022 — таблица admin_relay

**Files:**
- Create: `migrations/versions/0022_admin_relay.py`

DDL (по образцу 0019, тот же набор RLS/GRANT):

```sql
CREATE TABLE admin_relay (
    id bigserial PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinic(id),
    admin_chat_id bigint NOT NULL,
    message_id bigint NOT NULL,
    patient_chat_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, admin_chat_id, message_id)
);
-- + ENABLE/FORCE RLS, POLICY tenant_isolation (образец 0019),
-- GRANT S/I/U/D ON admin_relay, GRANT USAGE,SELECT ON admin_relay_id_seq
```

`down_revision = "0021"`. downgrade — `DROP TABLE IF EXISTS admin_relay`.

- [ ] **Step 1:** написать миграцию.
- [ ] **Step 2 (оркестратор):** `alembic upgrade head` → `alembic downgrade
  0021` → `alembic upgrade head` — идемпотентно и обратимо, без ошибок.
- [ ] **Step 3: commit** `feat(db): таблица admin_relay — якоря свайп-ответов админа`

### Task 4: репозиторий якорей + запись при алертах эскалации

**Files:**
- Create: `src/navbat/telegram/relay_repo.py`
- Modify: `src/navbat/telegram/escalation.py` — `TelegramEscalation.notify`,
  `build_escalation`
- Modify: тестовый FakeTelegramAPI (найти общий фейк в
  `tests/conftest.py`/тестах алертов): `send_message` должен возвращать
  `{"message_id": <счётчик>}` как реальный Bot API
- Test: `tests/test_admin_relay.py` (новый)

`relay_repo.py` (сырой SQL как в других repo, работает внутри
tenant_transaction):

```python
def save_anchor(session, admin_chat_id: int, message_id: int,
                patient_chat_id: int) -> None: ...
    # INSERT ... ON CONFLICT (clinic_id, admin_chat_id, message_id) DO NOTHING
def patient_for(session, admin_chat_id: int, message_id: int) -> int | None: ...
def cleanup_old(session, days: int = 7) -> int: ...   # DELETE + rowcount
```

`TelegramEscalation.__init__` получает новый опциональный параметр
`anchor_writer: Callable[[int, int, int], None] | None` (admin_chat,
message_id, patient_chat). В `notify()` после успешной отправки каждому
админ-чату: если writer задан — записать якорь с message_id из ответа
API (`resp.get("message_id")`; None — не писать). Сбой записи якоря НЕ
роняет рассылку (лог error, как сбой доставки: эскалация — сигнал).
`build_escalation` собирает writer-замыкание через `tenant_transaction`.
CLI/тесты без БД создают TelegramEscalation без writer'а — поведение
прежнее.

- [ ] **Step 1: failing-тесты** в `tests/test_admin_relay.py`: (а) notify
  с writer'ом пишет якорь на КАЖДЫЙ админ-чат с его message_id;
  (б) repo: save → patient_for находит, чужой message_id → None;
  (в) ON CONFLICT: повторный save не падает; (г) cleanup_old(7) сносит
  старые (created_at подделать UPDATE'ом), свежие живы.
- [ ] **Step 2: прогон (оркестратор)** — падают.
- [ ] **Step 3: реализация** по сигнатурам выше.
- [ ] **Step 4: прогон (оркестратор)** — зелёные; откат-проверка (б):
  убрать save — тест падает.
- [ ] **Step 5: commit** `feat(escalation): якоря admin_relay при алертах`

### Task 5: входящие escalated-пациента → карточки 💬 админам

**Files:**
- Modify: `src/navbat/dialog/escalation.py` — хелпер `relay_from_patient`
  (getattr-фолбэк по образцу `fyi_alert`; фолбэк — обычный `notify`)
- Modify: `src/navbat/telegram/escalation.py` — метод
  `relay_from_patient(chat_id, text)`: во все админ-чаты
  `at("relay_card", lang, chat=chat_id, text=text)` + якорь на каждое
  отправленное сообщение (тот же anchor_writer)
- Modify: `src/navbat/dialog/fsm.py` — в гейте `conv.state == "escalated"`
  для ТЕКСТА (строка ~282): перед `_escalated_reply` дёрнуть хелпер
- Modify: `src/navbat/telegram/admin_texts.py` — строка `relay_card`
  ru: «💬 Чат {chat}: {text}», uz: «💬 Chat {chat}: {text}»
- Test: `tests/test_admin_relay.py` (дописать)

Краевые условия: карточка НЕ шлётся для callback'ов (гейт ~504 не
трогаем — тап по старой кнопке не сообщение); пустой список админ-чатов —
лог, поведение пациента прежнее (`_escalated_reply` как был).

- [ ] **Step 1: failing-тесты:** (а) пациент в escalated пишет текст →
  все админ-чаты получили 💬-карточку с текстом, на карточки записаны
  якоря; (б) пациент получил прежний `escalated`-ответ; (в) тап по кнопке
  в escalated карточку НЕ шлёт.
- [ ] **Step 2: прогон (оркестратор)** — падают.
- [ ] **Step 3: реализация.**
- [ ] **Step 4: прогон (оркестратор)** — зелёные; откат-проверка (а).
- [ ] **Step 5: commit** `feat(escalation): пересылка сообщений escalated-пациента в админ-чаты`

### Task 6: свайп-ответ админа → пациенту

**Files:**
- Modify: `src/navbat/telegram/worker.py` — text-ветка админ-чата
  (~строка 217, ПОСЛЕ слэш-команд, ДО `_admin.handle_text`)
- Modify: `src/navbat/dialog/replies.py` — шаблон `relay_from_admin`
  ru: «👤 Администратор: {text}», uz: «👤 Administrator: {text}»
- Modify: `src/navbat/telegram/admin_texts.py` — `relay_delivered`
  (ru «✅ Доставлено», uz «✅ Yetkazildi»), `relay_failed`
  (ru «⚠️ Не доставлено: {error}», uz «⚠️ Yetkazilmadi: {error}»),
  `relay_no_anchor` (ru «Отвечайте реплаем на алерт эскалации или
  карточку 💬», uz «Eskalatsiya ogohlantirishiga yoki 💬 kartochkaga
  javob (reply) qiling»)
- Test: `tests/test_admin_relay.py` (дописать)

Логика ветки: `message` содержит `reply_to_message` → достать
`reply_to_message.message_id`, `relay_repo.patient_for(...)`. Найден →
язык пациента `get_chat_lang` → `t("relay_from_admin", lang,
text=<текст админа>)` пациенту; успех → админу `at("relay_delivered")»;
`TelegramAPIError` → админу `at("relay_failed", error=...)`. Якорь не
найден → `at("relay_no_anchor")` и НЕ передавать текст в консоль (реплай
на консольное сообщение — явно не консольный ввод). Сообщение без
`reply_to_message` — путь прежний (консоль). Языки: подтверждения — язык
админ-чата (`_admin_lang`).

- [ ] **Step 1: failing-тесты:** (а) полный путь: эскалация → алерт (якорь)
  → админ реплаит на алерт → пациент получил «👤 Администратор: …» на
  СВОЁМ языке (пациент uz, админ ru), админ получил «✅ Доставлено»;
  (б) реплай на сообщение без якоря → подсказка, пациенту ничего;
  (в) сбой API при отправке пациенту → админу relay_failed;
  (г) реплай админа на 💬-карточку (якорь из Task 5) тоже доставляется.
- [ ] **Step 2: прогон (оркестратор)** — падают.
- [ ] **Step 3: реализация.**
- [ ] **Step 4: прогон (оркестратор)** — зелёные; откат-проверка (а).
- [ ] **Step 5: commit** `feat(telegram): свайп-ответ админа пациенту через бота`

### Task 7: ретеншен якорей + UZ_STRINGS + PRIVACY

**Files:**
- Modify: `src/navbat/retention.py` — в `cleanup_old_data` добавить
  `DELETE FROM admin_relay WHERE created_at < now() - interval '7 days'`
  (константа `RELAY_RETENTION_DAYS = 7`, комментарий: якорь живёт, пока
  жива переписка по эскалации; неделя покрывает выходные)
- Modify: `docs/UZ_STRINGS.md` — новые пациентские строки
  (`relay_from_admin`, `bot_paused_phone`)
- Modify: `docs/PRIVACY.md` — admin_relay в карту данных (chat_id-пары,
  7 дней)
- Test: `tests/test_retention.py` (дописать)

- [ ] **Step 1: failing-тест:** старый якорь (created_at -8 дней) удалён,
  свежий жив.
- [ ] **Step 2: прогон (оркестратор)** — падает.
- [ ] **Step 3: реализация + доки.**
- [ ] **Step 4: прогон (оркестратор)** — зелёный; откат-проверка.
- [ ] **Step 5: commit** `feat(retention): чистка якорей admin_relay + карта данных`

### Task 8: финальная верификация (оркестратор, не субагент)

- [ ] Полный сьют: `python -m pytest` (docker поднят, бот погашен),
  при зелёном — повторить 3 раза (конкурентных путей мало, но воркер
  тронут).
- [ ] `python -m navbat.onboard --demo && python -m navbat.onboard
  --demo-history` (сьют стёр демо), затем `python -m navbat --check` —
  все [OK].
- [ ] Codex-ревью пакета (read-only, фоном) — весь дифф инкремента 1.
- [ ] Push. Спека/журнал — в конце сессии.
