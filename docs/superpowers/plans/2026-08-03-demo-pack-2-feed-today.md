# Пакет показа, инкремент 2: лента записей + /today + утренняя сводка

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Правило проекта: субагенты НЕ запускают pytest/alembic — прогоны у
> оркестратора. Кодовые тела пишут агенты Opus по постановкам ниже.

**Goal:** фича 2 спеки `2026-08-02-selling-demo-pack-design.md` — клиника
видит каждую запись в момент события и весь день одним экраном.

**Architecture:** карточки ленты шлёт новый метод нотификатора
`booking_event` (веер по админ-чатам, язык получателя, getattr-фолбэк как
`fyi_alert`); данные карточки собирает ОДНА репо-функция по
appointment_id — точки вызова (диалог, напоминания, синк) не собирают
ничего сами. /today и утренняя сводка делят один рендер списка дня.
Сводка — по образцу вечернего дайджеста (`maybe_send_digest`,
`last_digest_date`), своя отметка `last_morning_digest_date` (миграция).

**Инварианты:** тесты первыми + проверка откатом; в fsm нет сырого SQL;
пациентские строки TEMPLATES ru/uz, админские admin_texts ru/uz +
UZ_STRINGS.md; фоновые рассылки не трогают conversation; RLS-паттерн
0019/0022; комментарии русские «почему»; коммиты dejavuu без ассистентов.

---

### Task 1: миграция 0023 + сбор данных карточки

**Files:** Create `migrations/versions/0023_morning_digest.py`;
Create `src/navbat/telegram/feed_repo.py`; Test `tests/test_booking_feed.py`.

- Миграция: `ALTER TABLE clinic ADD COLUMN last_morning_digest_date date`
  (паттерн соседних ALTER-миграций; downgrade — DROP COLUMN; clinic уже
  под RLS, новых политик не нужно).
- `feed_repo.appointment_card(session, appointment_id) -> Card | None`:
  один SELECT c JOIN doctor/service/patient — время начала, услуга
  (canonical name), врач (name_encrypted → decrypt), имя пациента
  (decrypt, может быть None), телефон (decrypt, может None), tg_chat_id,
  created_at, статус. dataclass Card. None — запись не найдена.
- Флаг «ночная»: `is_after_hours(created_at, clinic tz, working_hours)`
  — вне 09–18? БРАТЬ ГОТОВОЕ: как /stats считает after_hours_booked
  (stats.py) — переиспользовать ту же логику/условие, не изобретать.

### Task 2: booking_event + вызовы во всех точках изменений

**Files:** Modify `src/navbat/dialog/escalation.py` (хелпер
`booking_feed(notifier, session, appointment_id, kind)` — getattr,
фолбэк-молчание); Modify `src/navbat/telegram/escalation.py`
(`TelegramEscalation.booking_event(kind, card)` — веер, язык чата,
`at("feed_" + kind, ...)`, ошибки доставки логировать и слать остальным);
Modify точки:
- `src/navbat/dialog/booking_flow.py:206` (после confirm) — kind="booked";
- `src/navbat/dialog/cancel_flow.py:76` — kind="cancelled";
- `src/navbat/dialog/fsm.py:243` (remind_cancel) — kind="cancelled"
  (карточка та же; «предотвращённая неявка» уже видна в /stats);
- `src/navbat/dialog/reschedule_flow.py:133` — kind="resched";
- `src/navbat/calendar/sync.py:596` — kind="resched", `:642` —
  kind="cancelled" (вытеснение: эскалация уже шлётся — карточка ДОПОЛНЯЕТ,
  не заменяет);
- booking_flow.py:202/218 — прочитать контекст: если это откат hold/этап
  resched — карточку НЕ слать (не событие для владельца), решение
  обосновать в отчёте.
Строки `feed_booked`/`feed_cancelled`/`feed_resched` в admin_texts ru/uz:
«✅ Запись: {patient}, {service}, {when}, {doctor}»,
«❌ Отмена: …», «🔄 Перенос: {patient}, {service} → {when}, {doctor}»;
суффикс « 🌙» при ночной. patient=None → «без имени» (строка-заглушка
ru/uz). Тесты: карточка при записи/отмене/переносе; обе точки синка;
🌙 у ночной; язык чата получателя; фолбэк-нотификатор без метода — тишина.

### Task 3: /today + кнопка консоли

**Files:** Modify `src/navbat/telegram/worker.py` (команда `/today` в
блоке админ-команд); Modify `src/navbat/telegram/admin_console.py`
(кнопка «📅 Сегодня» в главное меню, callback `adm:today`); Create render
в `src/navbat/telegram/today_view.py` (чистое view, как calendar_view):
`render_today(session, lang) -> str` — booked-записи дня клиники по
времени: `HH:MM Имя (телефон) — услуга, врач`; пусто → «сегодня записей
нет»; шапка с датой. Тесты: список отсортирован, телефон расшифрован,
пустой день, язык admin-чата, кнопка консоли отвечает тем же рендером.

### Task 4: утренняя сводка

**Files:** Modify `src/navbat/reminders.py` — `maybe_send_morning_summary`
по образцу `maybe_send_digest` (строки 329–380): MORNING_SUMMARY при
времени ≥ 08:30 локали клиники, отметка `last_morning_digest_date`,
шапка `at("morning_header", lang, date=…)` + `render_today`; пустой день —
НЕ слать вообще (нет записей — нечего читать в 8:30, дёргать владельца
нулём нельзя); вызов из `run()` рядом с maybe_send_digest. Тесты: шлёт
раз в день всем админ-чатам; до 08:30 молчит; пустой день — молчит, но
отметка ставится (иначе цикл будет проверять весь день).

### Task 5: финал (оркестратор)

UZ_STRINGS.md (стражи сами укажут пропуски), PRIVACY.md (лента: имя и
телефон пациента уходят в админ-чаты — тот же доверенный канал, что
календарь врача), полный сьют ×3, alembic up/down/up, `--demo` +
`--demo-history` + `--check`, Codex-ревью инкремента, push, журнал.
