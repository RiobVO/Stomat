# Пакет показа, инкремент 4: recall — возврат пациентов

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Правила проекта: субагенты НЕ запускают pytest/alembic; кодовые тела — Opus.

**Goal:** фича 4 спеки `2026-08-02-selling-demo-pack-design.md` — бот сам
возвращает пациентов: прошло N месяцев после услуги — приглашение с
кнопкой записи, конверсия видна в /stats.

**Architecture:** `service.recall_months` (NULL = выключен) + таблица
`recall_outreach` (одна отправка на приём, UNIQUE(appointment_id));
reconciliation в цикле напоминаний (паттерн `reconcile`/`match_waitlist`);
кнопка `rcl:<uuid исходного приёма>` — сырая, субъект+проверка на месте,
стартует booking flow с услугой приёма; конверсия — отметка `booked_at`
при confirm записи, начатой с recall-кнопки.

**Ключевые решения:**
- Анонимизированные не беспокоятся бесплатно: `/forget` уже обнуляет
  `appointment.tg_chat_id` → условие `tg_chat_id IS NOT NULL` в выборке.
- Язык приглашения — `appointment.lang`; чат — `appointment.tg_chat_id`;
  новых PII-колонок нет.
- Окно отправки 09–21 локали клиники; вне окна — просто ждём следующего
  цикла (без очередей).
- «Вернулось» = recall_outreach с `booked_at`; сумма — цена услуги
  recall'а (та же услуга; честно для витрины, новая запись не джойнится).
- ПРАВИЛО ФОНА: рассылка не трогает conversation; всё в кнопке.

---

### Task 1: миграция 0025 + reconciliation-рассылка

**Files:** Create `migrations/versions/0025_recall.py`;
Modify `src/navbat/reminders.py`; Modify `src/navbat/dialog/replies.py`;
Test `tests/test_recall.py`.

- Миграция: `ALTER TABLE service ADD COLUMN recall_months int` (NULL);
  `CREATE TABLE recall_outreach (id bigserial PK, clinic_id uuid NOT NULL
  REFERENCES clinic(id), appointment_id uuid NOT NULL UNIQUE,
  tg_chat_id bigint NOT NULL, lang char(2) DEFAULT 'ru',
  sent_at timestamptz NOT NULL DEFAULT now(), booked_at timestamptz)`
  + RLS ENABLE/FORCE + policy + GRANT'ы (образец 0022). downgrade — DROP
  таблицы и колонки.
- `ReminderService.send_recalls()` в цикле run() (частота — как
  match_waitlist): SELECT прошедших booked-приёмов, где услуга с
  `recall_months NOT NULL`, `upper(time_range) + make_interval(months =>
  s.recall_months) <= now()`, `a.tg_chat_id IS NOT NULL`, у чата НЕТ
  будущей booked-записи, НЕТ строки recall_outreach; окно 09–21 локали;
  INSERT outreach (ON CONFLICT DO NOTHING — гонка циклов) + отправка
  «{service}: прошло N мес. — пора на осмотр. Записать?» с кнопкой
  `rcl:<uuid приёма>` (сырая, ≤64 байта). Сбой отправки — лог, outreach
  остаётся (не дублируем при следующем цикле; комментарий-решение).
- Шаблоны TEMPLATES ru/uz: `recall_invite` (+ months), `btn_recall_book`.
- Тесты: (а) прошедший приём + recall_months → в окне уходит приглашение
  на языке приёма с rcl-кнопкой, outreach записан; (б) вторая итерация —
  дубля нет; (в) будущая запись у чата → тишина; (г) tg_chat_id NULL
  (/forget) → тишина; (д) вне окна (23:00) → тишина, outreach не создан;
  (е) услуга без recall_months → тишина.

### Task 2: кнопка rcl: → запись + конверсия + консоль + /stats

**Files:** Modify `src/navbat/dialog/fsm.py` (+`RAW_CALLBACK_PREFIXES`
в worker.py — `rcl:`); Modify `src/navbat/dialog/conversation.py`
(поле контекста `recall_source`); Modify `src/navbat/dialog/booking_flow.py`
(отметка конверсии при confirm); Modify `src/navbat/telegram/admin_console.py`
(+admin_texts) — интервал в карточке услуги; Modify `src/navbat/stats.py`
+ рендер /stats; Test `tests/test_recall.py` (дописать).

- fsm: ветка `rcl:<uuid>` — проверка субъекта на месте (приём принадлежит
  этому чату: SELECT по id+tg_chat_id; чужой/битый uuid → stale_button);
  услуга приёма → `conv.context.service`, `conv.context.recall_source =
  <uuid>`, дальше обычный `_advance_booking` (выбор дня). Если у пациента
  уже есть будущая запись — обычный booking всё равно продолжается
  (пациент сам решает).
- booking_flow при confirm: если `recall_source` в контексте — UPDATE
  recall_outreach SET booked_at=now() WHERE appointment_id=:src AND
  booked_at IS NULL; поле из контекста очистить.
- Консоль, карточка услуги (`_service_card`): строка «🔁 Повторный визит:
  N мес / выкл» + кнопки «выкл/3/6/12» (`adm:svc:<key>:recall:<n|off>`),
  строки admin_texts ru/uz.
- /stats (collect_stats + рендер + дайджест НЕ трогаем — только /stats):
  «🔁 Recall: отправлено N, вернулось M (+сумма)» за период; сумма — по
  ценам услуг исходных приёмов вернувшихся. Строка прячется при N==0
  (витрина без нулей — конвенция рендера).
- Тесты: (ж) тап rcl → выбор дня с услугой приёма; (з) полный путь до
  confirm → booked_at стоит, /stats показывает «вернулось 1 (+цена)»;
  (и) чужой rcl-uuid → stale_button; (к) карточка услуги: тап «6 мес» →
  recall_months=6, «выкл» → NULL; (л) /stats при нуле отправок — строки
  нет.

### Task 3: финал (оркестратор)

UZ_STRINGS (стражи), PRIVACY.md (recall_outreach в карту: chat_id+lang;
ретеншен — DELETE строк старше 180 дней в cleanup_old_data, окно
конверсии к этому времени закрыто, /forget-строки уже без адресата —
попадает в Task 2 фикс-агенту вместе с картой), alembic up/down/up,
сьют ×3, демо + --check, Codex-ревью, push, журнал.
