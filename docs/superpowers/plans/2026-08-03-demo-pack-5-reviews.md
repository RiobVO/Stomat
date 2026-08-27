# Пакет показа, инкремент 5: отзывы — перехват негатива

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Правила проекта: субагенты НЕ запускают pytest/alembic; кодовые тела — Opus.

**Goal:** фича 5 спеки `2026-08-02-selling-demo-pack-design.md` — после
приёма бот спрашивает «Как всё прошло?»: 4–5 → просьба оставить отзыв
(ссылка клиники), 1–3 → мгновенный алерт владельцу вместо публичного
негатива; свайп-ответ уже работает (якоря admin_relay).

**Architecture:** таблица `review` (appointment_id UNIQUE — одна просьба
на приём) + `clinic.review_url`; reconciliation в цикле напоминаний
(окно 09–21 через `_in_recall_window`-механику, приём закончился ≥2 ч
назад и не старше 48 ч); кнопки `rate:<uuid>:1..5` — сырые, субъект и
принадлежность чату проверяются на месте; язык — `appointment.lang`
(теперь пишется). Тап гасит кнопки edit'ом, повтор — toast «уже учтено».

**Решения:** алерт 1–3 — БЕЗ заморозки пациента, свой текст (не
эскалационная шапка) + якорь `admin_relay` (свайп-ответ владельца);
/stats — «⭐ средняя оценка: X.X (N)» за период по rated_at, прячется при
нуле; ретеншен review — 180 дней (паттерн recall_outreach), `/forget`
сносит строки чата; demo_history отзывы НЕ сеет (хвост в якоре).

---

### Task 1: миграция 0026 + просьба об оценке + тап

- `migrations/versions/0026_reviews.py`: `ALTER TABLE clinic ADD COLUMN
  review_url text`; `CREATE TABLE review (id bigserial PK, clinic_id uuid
  NOT NULL REFERENCES clinic(id), appointment_id uuid NOT NULL UNIQUE,
  tg_chat_id bigint NOT NULL, lang char(2) DEFAULT 'ru', rating int
  CHECK (rating BETWEEN 1 AND 5), requested_at timestamptz NOT NULL
  DEFAULT now(), rated_at timestamptz)` + RLS-обвязка 0022/0025.
- `reminders.send_review_requests()` в run(): окно 09–21; выборка —
  booked-приёмы, `upper(time_range)` в [now−48ч, now−2ч],
  `tg_chat_id IS NOT NULL`, нет строки review; INSERT ON CONFLICT DO
  NOTHING + отправка `t("review_ask", lang)` с рядом кнопок
  `rate:<uuid>:1..5` (⭐ лейблы); сбой доставки — лог, строка остаётся.
- fsm: ветка `rate:<uuid>:<n>` (сырой префикс `rate:` в
  RAW_CALLBACK_PREFIXES): uuid → str(parsed), n в 1..5; UPDATE review
  SET rating, rated_at WHERE appointment_id и tg_chat_id совпали И
  rated_at IS NULL (repo-модуль review_repo); успех: 4–5 →
  `review_thanks_good` (+ строка с review_url, если задан), 1–3 →
  `review_thanks_bad` (пациенту — нейтральная благодарность) + алерт
  (Task 2, здесь — вызов хелпера); ответ с edit=True гасит кнопки;
  уже оценено → toast `review_already`; чужой/битый → stale_button.
- onboard `--review-url` (+ пустое значение очищает — паттерн --phone).
- Тесты: просьба уходит в окне с 5 кнопками на языке приёма; вторая
  итерация — дубля нет; старше 48 ч / моложе 2 ч / NULL-чат — тишина;
  тап 5 → rating/rated_at, благодарность со ссылкой, кнопки погашены;
  тап 2 → благодарность без ссылки; повторный тап → toast, rating не
  перезаписан; чужой чат → stale; url не задан → «спасибо» без ссылки.

### Task 2: алерт владельцу + /stats + гигиена

- `TelegramEscalation.review_alert(chat_id, rating, card_info)` — веер по
  админ-чатам на языке каждого: «⭐{n} от чата {chat}: {услуга}, {дата}»
  (admin_texts `alert_review`), якорь `_save_anchor` на каждое сообщение
  (свайп-ответ работает); хелпер в dialog/escalation.py с getattr-фолбэком
  (тишина у фейков). Вызов из fsm-ветки rate при n≤3 — ПОСЛЕ коммита
  оценки, сбой алерта не роняет ответ пациенту (паттерн booking_feed —
  savepoint не нужен, если алерт вне транзакции? посмотреть, где стоит
  вызов; решение имплементера с обоснованием).
- /stats: collect_stats — reviews_count, reviews_avg за период (rated_at);
  рендер «⭐ Оценки: {avg} (из {count})», прячется при count==0.
- retention: DELETE review старше 180 дней (константа рядом с recall);
  `/forget` — DELETE review WHERE tg_chat_id (в общий блок).
- PRIVACY.md: review в карту (разд. 1, 7; /forget разд. 6); UZ_STRINGS —
  все новые ключи.
- Тесты: алерт 1–3 уходит всем админ-чатам с якорями, пациент НЕ
  заморожен (следующее сообщение обрабатывается нормально); 4–5 алерта
  нет; /stats средняя по двум оценкам, строка прячется при нуле;
  ретеншен/форгет чистят.

### Task 3: финал (оркестратор)

alembic up/down/up, сьют ×3, демо + --check, Codex-ревью, push, журнал.
