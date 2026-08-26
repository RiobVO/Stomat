# Пакет показа, инкремент 3: статусы подтверждения визита

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Правила проекта: субагенты НЕ запускают pytest/alembic; кодовые тела — агенты Opus.

**Goal:** фича 3 спеки `2026-08-02-selling-demo-pack-design.md` — «Приду»
перестаёт быть пустым «ок»: система помнит, кто подтвердил, а /today и
утренняя сводка показывают, кому стоит позвонить.

**Architecture:** миграция `appointment.confirm_status` + `confirmed_at`;
кнопка `attend:<uuid>` (субъект уже в кнопке) пишет статус с проверкой
принадлежности чату; «молчит» не хранится — вычисляется (напоминание
`sent`, статус NULL). Потребители — рендер дня и утренняя сводка.

---

### Task 1: миграция 0024 + запись статуса по кнопке

- Create `migrations/versions/0024_confirm_status.py`:
  `ALTER TABLE appointment ADD COLUMN confirm_status text`,
  `ADD COLUMN confirmed_at timestamptz`; CHECK (confirm_status IN
  ('confirmed')) NULL-able; downgrade DROP оба.
- Modify `src/navbat/dialog/appointments_repo.py`: функция
  `confirm_attendance(session, appointment_id, tg_chat_id) -> bool` —
  UPDATE … SET confirm_status='confirmed', confirmed_at=now()
  WHERE id=:id AND tg_chat_id=:chat AND status='booked'; rowcount>0.
  Чужой uuid или отменённая запись → False (инвариант кнопок: субъект
  проверяется на месте).
- Modify `src/navbat/dialog/fsm.py` ветка `kind == "attend"`: вызвать
  репо; True → прежний `attend_ok`; False → `stale_button`-поведение
  (кнопка от чужой/отменённой записи не подтверждает ничего). Работа в
  escalated сохраняется (комментарий-причина остаётся).
- Тесты (test_confirm_status.py): тап пишет статус и confirmed_at;
  повторный тап идемпотентен; чужой chat → False и статус не тронут;
  отменённая запись → False; attend в escalated по-прежнему отвечает
  attend_ok (и пишет статус).

### Task 2: потребители — /today и утренняя сводка

- Modify `src/navbat/telegram/feed_repo.py`: Card + confirm-поля;
  `day_cards` добирает `confirm_status` и признак «напоминание отправлено»
  (EXISTS reminder sent по appointment).
- Modify `src/navbat/telegram/today_view.py`: значок в строке —
  ✅ подтверждён; ⏳ напоминание ушло, ответа нет; без значка — напоминание
  ещё не уходило. Шапка: «⚠️ Без ответа после напоминания: N — стоит
  позвонить» (только если N>0; строка admin_texts ru/uz).
- Утренняя сводка получает то же автоматически (реюз render_today) —
  тест это фиксирует.
- Тесты: значки по трём состояниям; строка ⚠️ появляется/исчезает;
  сводка несёт ⚠️.

### Task 3: финал (оркестратор)

UZ_STRINGS (стражи), alembic up/down/up, сьют ×3, демо + --check,
Codex-ревью, push, журнал.
