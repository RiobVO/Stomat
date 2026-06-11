# Полировка-2 (до-батч поверх П-1…П-7) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Три блока спеки `2026-06-11-polish-2-addon-design.md`: кнопка
«👤 Позвать администратора» в фоллбэках (осознанное отступление от
«только текст»), FAQ-темы оплата/телефон + карточка «ℹ️ О клинике»
в меню, /stats v2 (клиенты, топ врачей, хит-услуга, тренды ≥10,
кнопки периодов, короткий дайджест с «Подробнее»).

**Architecture:** Кнопка — callback `call_admin` через штатный
tg_actions-map, обработчик в `_process_action` зовёт существующий
`_escalate_on_request`. FAQ — зеркало `--address`/`clinic_address`
(миграция 0017: clinic.payment_info, clinic.phone), детекторы — стиль
dialog_common П-2б. /stats — `collect_stats` дособирает секции, тренды =
второй вызов за предыдущий равный период; кнопки периодов — сырой
callback `stats:` мимо tg_actions (паттерн `cal:` из П-4), перехват
на уровне ВОРКЕРА только для админ-чатов (в DialogEngine не попадает);
переключение — edit на месте, «Подробнее» из дайджеста — НОВОЕ сообщение
(дайджест с вопросами должен остаться на экране).

**Tech Stack:** alembic (0017), stdlib. Ноль новых зависимостей.

**Контекст кодовой базы:**
- `src/navbat/dialog/fsm.py`: 355–366 `_on_nlu_failure` (ветка
  not_understood), 444–460 `_answer_question` (фоллбэк + копилка),
  378–424 `_process_action` (сюда `call_admin`), 341–353
  `_escalate_on_request` (переиспользуем как есть), 462–495
  `_faq_answer`/`_hours_reply`, 69–74 `_MENU_KEYS`, 207–227 `_on_menu`,
  497–508 `_with_reprompt`.
- `src/navbat/dialog/replies.py`: 20–37 — Reply: `buttons` и `menu`
  ВЗАИМОИСКЛЮЧАЮЩИЕ (reply_markup один). В фоллбэках кнопка call_admin
  ЗАМЕНЯЕТ `menu=menu_rows(...)`: персистентная reply-клавиатура у
  пациента и так на экране (api.py:90 `is_persistent`), M7 не ломается.
  343–351 ключи кнопок меню, 369–375 `menu_rows`.
- `src/navbat/telegram/worker.py`: 188–211 callback-ветка (паттерн
  сырого `cal:` для `stats:`), 472–499 `_number_buttons` — пропуск
  сырых префиксов расширить до `("cal:", "stats:")`, 416–435
  `_stats_reply`, 502–532 `send_reply`/`edit_reply`.
- `src/navbat/telegram/api.py`: 94–120 `edit_message_text` /
  `answer_callback_query` — готовы, не трогаем.
- `src/navbat/stats.py`: 23–37 `DailyStats` (новые поля — с дефолтами,
  чтобы не ломать конструкторы в тестах, паттерн П-6), 44–151
  `collect_stats`, 154–177 `render_stats`, 183–192 `render_questions`.
- `src/navbat/reminders.py`: 169–205 `maybe_send_digest` (дайджест шлёт
  `api.send_message` напрямую — кнопка «Подробнее» уходит как
  `buttons=(Button(...),)`, `stats:` сырой, map не нужен).
- `src/navbat/onboard.py`: 157–165 `set_clinic_address`, 378–379 и
  462–465 — паттерн для `--payment`/`--phone`.
- `src/navbat/supervisor.py`: 128–149 `run_check` — подсказки по
  незаполненным полям (зеркалить address).
- `migrations/versions/0016_faq_layer.py` — паттерн ALTER TABLE clinic.
- `src/navbat/dialog/doctors_repo.py`: 21–28 `doctor_list` —
  расшифровка имён врачей (name_encrypted, AES) для топа.
- `migrations/versions/0001_initial.py`: 64–71 — у patient НЕТ
  created_at: «новых/вернувшихся» считаем по `appointment.created_at`
  (статусы 'booked','done','cancelled'; hold/expired — не визит),
  без новой миграции.
- Конвенция uz-строк: `tests/test_replies_uz.py` (REVIEWED_UZ).

---

### Task А: кнопка «👤 Позвать администратора» в фоллбэках

**Files:** Create `tests/test_call_admin_button.py`; Modify
`src/navbat/dialog/fsm.py`, `src/navbat/dialog/replies.py`,
`tests/test_replies_uz.py` (+ правка assert'ов menu в существующих
тестах фоллбэков, если упадут).

- [ ] Тесты: callback `call_admin` → state `escalated`, notifier.notify
  с причиной «пациент просит администратора», ответ escalated /
  escalated_closed (по `_closed_now`, паттерн test_dialog_escalation);
  второй клик в escalated → t("escalated") и notify НЕ повторён;
  2-й сбой NLU вне сценария → ответ содержит Button(action="call_admin")
  и НЕ содержит menu; 1-й сбой («reask») — кнопки нет, menu есть;
  вопрос вне FAQ → кнопка есть И вопрос по-прежнему пишется в
  unanswered_question; посреди оформления 2-й сбой → `_with_reprompt`
  отдаёт кнопки шага (кнопка call_admin не обязана выживать — шаг важнее).
- [ ] Код: шаблон `btn_call_admin` {ru: «👤 Позвать администратора»,
  uz: «👤 Administratorni chaqirish»}; в `_process_action` ветка
  `kind == "call_admin"` → `return self._escalate_on_request(session,
  conv)` (до неё стоит проверка escalated — повторный алерт исключён);
  в `_on_nlu_failure` (ветка `failures >= MAX_NLU_FAILURES`) и в
  фоллбэке `_answer_question` — `Reply(t("not_understood", lang),
  (Button(t("btn_call_admin", lang), "call_admin"),))` вместо
  `menu=menu_rows(lang)`.
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(dialog): call-admin button in fallback replies` + push.

### Task Б: FAQ оплата/телефон + карточка «ℹ️ О клинике»

**Files:** Create `migrations/versions/0017_faq_topics.py`,
`tests/test_about_clinic.py`; Modify `src/navbat/dialog/dialog_common.py`,
`src/navbat/dialog/fsm.py`, `src/navbat/dialog/replies.py`,
`src/navbat/dialog/clinic_repo.py`, `src/navbat/onboard.py`,
`src/navbat/supervisor.py`, `tests/test_replies_uz.py` (+ тесты,
закрепляющие форму menu_rows, если есть).

- [ ] Тесты: детекторы payment/phone — позитив ru/uz («рассрочка
  есть?», «bo'lib to'lasa bo'ladimi?», «какой у вас номер?»,
  «qo'ng'iroq qilsam bo'ladimi?»), негатив («оставил номер соседу»,
  «сколько стоит чистка» → НЕ payment); поле заполнено → ответ из
  поля и вопрос НЕ в копилке; поле NULL → штатный путь (not_understood
  + кнопка call_admin + копилка); кнопка меню «ℹ️ О клинике» → карточка
  (часы есть всегда; address/payment/phone — только заполненные,
  пустые строки не рендерятся); «О клинике» посреди слотов →
  `_with_reprompt` сохраняет шаг; onboard `--payment`/`--phone`
  пишут поля (и пустое значение очищает, паттерн --address);
  меню стало 6 кнопок — `_MENU_ACTIONS` ловит label обоих языков.
- [ ] Миграция 0017: `ALTER TABLE clinic ADD COLUMN payment_info text;
  ALTER TABLE clinic ADD COLUMN phone text` (downgrade — DROP, паттерн
  0016 без новой таблицы).
- [ ] Код: `mentions_payment_question`/`mentions_phone_question` в
  dialog_common (словари: оплат\w*|рассрочк\w*|карт(?:ой|а|у)\b|
  наличн\w*|to'lov|bo'lib|karta|naqd; телефон\w*|позвонить|дозвон\w*|
  номер\s+(?:клиники|у\s+вас)|telefon|qo'ng'iroq|raqam\w*\s+bormi —
  довести с тестами, негативы обязательны); `clinic_repo.
  clinic_payment_info`/`clinic_phone` (зеркала clinic_address);
  ветки в `_faq_answer` ПОСЛЕ address; шаблоны `clinic_payment`
  («💳 Оплата: {info}»), `clinic_phone` («📞 Телефон: {phone}»),
  `btn_menu_about` («ℹ️ О клинике» / «ℹ️ Klinika haqida»),
  `about_header` («ℹ️ <b>{clinic}</b>»); `fsm._about_clinic` —
  заголовок + строки: текст `_hours_reply` (есть всегда при графиках),
  clinic_address, payment, phone; `btn_menu_about` в `_MENU_KEYS` +
  ветка в `_on_menu` через `_with_reprompt`; `menu_rows` →
  `((book,), (resched, cancel), (prices, about), (lang,))`;
  onboard `set_clinic_payment`/`set_clinic_phone` + аргументы +
  [OK]-строки; run_check — подсказки «не задан — onboard --payment/
  --phone» (паттерн address, supervisor.py:147–149).
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(dialog): payment/phone FAQ topics + about-clinic
  menu card` + push.

### Task В: /stats v2 — клиенты, топ врачей, хит, тренды, кнопки

**Files:** Modify `src/navbat/stats.py`, `src/navbat/telegram/worker.py`,
`src/navbat/reminders.py`; Tests: `tests/test_stats.py`,
`tests/test_tg_worker.py`.

- [ ] Тесты: новый/вернувшийся (первая запись пациента в периоде →
  new=1 returned=0; пациент с записью до периода и в периоде →
  returned=1; голый hold — не визит); топ врачей: сортировка по числу
  confirm-аудитов, сумма по прейскуранту, имя РАСШИФРОВАНО
  (doctor_list), врач без имени → «Врач»; хит-услуга по confirm'ам
  периода (label через service_label); тренд: booked 12 против 10 →
  «↑20%», booked 4 против 8 → процент НЕ показан (выборка <10);
  рендер содержит секции 👥/👨‍⚕️/✨ и НЕ содержит пустые (0 врачей —
  секции нет); /stats ответ несёт button_rows «📅 День ✓ | 7 дней |
  30 дней» (активный помечен ✓); callback `stats:7` ИЗ админ-чата →
  edit на месте с заголовком периода; `stats:7` из НЕ-админ чата →
  ветка воркера не срабатывает (штатный путь, без эскалаций — тест);
  дайджест: короткий (записи/деньги/эскалации, БЕЗ ⚙️-строки), блок
  вопросов остаётся, кнопка «📊 Подробнее» (`stats:full`); `stats:full`
  → полная сводка дня НОВЫМ сообщением (digest не отредактирован).
- [ ] Код stats.py: поля DailyStats с дефолтами: `new_patients=0`,
  `returning_patients=0`, `top_doctors=()` (кортеж (имя, записей,
  сумма)), `hit_service=None` (кортеж (ключ, записей) | None);
  сбор в collect_stats: новые/вернувшиеся одним SQL по
  appointment.created_at (CTE: min(день первой не-hold записи)
  на пациента + наличие записи в периоде), топ-3 врачей confirm-аудиты
  периода JOIN appointment LEFT JOIN service (имена — doctor_list
  в коде), хит — max по service у confirm'ов; `_trend(cur, prev) ->
  str` («» при cur<10 or prev<10, иначе « ↑N%»/« ↓N%», prev=0 → «»);
  `render_stats(stats, day, last=None, prev=None)` — тренды на booked
  и cancelled, новые секции между «Ценностью» и «Служебным»;
  `render_digest_short(stats)` — 3 строки: записи (+вне часов),
  предотвращено/сохранено, эскалаций.
- [ ] Код worker.py: `_stats_reply(command)` собирает prev-период
  (тот же размер окна назад) и `button_rows` периодов
  (`stats:1|7|30`, активный с «✓»); новый `_stats_edit_reply(days)`
  → тот же рендер с `edit=True`; в callback-ветке ПЕРЕД общим путём:
  `data.startswith("stats:") and chat_id in self._admin_chat_ids` →
  answer_callback + (`stats:full` → `self._send(chat_id,
  self._stats_reply())` / иначе edit_reply); ветка стоит ДО проверки
  `_bot_paused()` — админ-поверхность живёт при паузе (конвенция C-4,
  тест на это); `_number_buttons` — пропуск сырых префиксов
  `("cal:", "stats:")`.
- [ ] Код reminders.py: `maybe_send_digest` — `render_digest_short` +
  render_questions + `buttons=(Button("📊 Подробнее", "stats:full"),)`
  в send_message.
- [ ] Run полный сьют → PASS.
- [ ] Commit `feat(stats): clients, top doctors, hit service, trends +
  period buttons` + push.

### Task Финал: DEMO.md + серия

- [ ] DEMO.md: чеклист тест-диалогов += кнопка «Позвать администратора»
  (и /release после), «ℹ️ О клинике», «рассрочка есть?»; маршрут
  продажи — шаг /stats упоминает тренды и топ врачей.
- [ ] Полный сьют СЕРИЕЙ 8/8 (конкурентные тесты — правило проекта),
  демо-клиника восстановлена (финализатор), `python -m navbat --check`
  → все [OK].
- [ ] Commit `docs(demo): polish-2 walkthrough additions` + push;
  обновить якорь CLAUDE.md (полировка-2 закрыта + следующий шаг).

## Definition of Done (полировка-2)

- [ ] Непонятый пациент может позвать человека ОДНИМ нажатием; алерт
  приходит только после нажатия или текстовой просьбы.
- [ ] «Рассрочка есть?», «какой номер?» и кнопка «ℹ️ О клинике»
  отвечаются ботом без LLM и без админа; поля настраиваются onboard.
- [ ] /stats отвечает на вопрос владельца «бот окупается?» за день/
  неделю/месяц в два клика; тренды не врут на малых выборках.
- [ ] Дайджест короткий, полная сводка — по кнопке.
- [ ] Полный сьют серия 8/8, --check [OK], CI зелёный, всё в origin.
