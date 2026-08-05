# П-7 Визуал v2: стиль маникюр-бота — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Живой тык 11.06: «визуал очень нудно». Решение пользователя — стиль его маникюр-бота: HTML-разметка (<b>-заголовки), эмодзи-якорь на каждом экране, hero-подтверждение «✅ ЗАПИСЬ ПОДТВЕРЖДЕНА», карточка записи строками 🦷📅👨‍⚕️, эмодзи в постоянном меню и кнопках услуг.

**Architecture:** `parse_mode` — параметр api.send_message/edit_message_text; «HTML» передают ТОЛЬКО пациентские пути (send_reply/edit_reply) и дайджест — эскалации/алерты остаются plain (контекст пациента не должен ломать парсер). Экранирование централизовано: `t()` прогоняет ВСЕ kwargs через html.escape — шаблоны размечены, подстановки (имя клиники, врач, адрес, цены) всегда безопасны; в дайджесте тексты вопросов экранируются в render_questions; в /pause-ответе — причина админа. Шаблоны: формулировки СОХРАНЯЮТСЯ (тесты матчат фразы; uz-строки прошли ревью — слова не трогаем), добавляется обвязка: якорь + <b>. Карточка booked: «✅ <b>ЗАПИСЬ ПОДТВЕРЖДЕНА</b> / 🦷 услуга / 📅 когда / 👨‍⚕️ врач / 🔔 напомним» (суффикс врача в booking_flow → отдельная строка). Меню: «🦷 Записаться · 🔁 Перенести · ❌ Отменить · 💰 Цены · 🌐 Til/Язык»; СТАРЫЕ label'ы у живых пользователей матчятся через _LEGACY_MENU_LABELS (reply-клавиатура у них не перерисуется до следующего menu-сообщения). Кнопки услуг: префикс из SERVICE_EMOJI (✨🦷🩹👑🔩🔍🩻😁🌟), текстовые label'ы в сообщениях — чистые.

**Контекст кодовой базы:** `replies.py` (TEMPLATES, SERVICE_LABELS, t, menu_rows), `fsm.py:_MENU_ACTIONS` (+legacy), `shared_helpers.py:_service_buttons`, `booking_flow.py` (booked doctor-suffix), `api.py`, `worker.py:send_reply/edit_reply`, `reminders.py` (digest parse_mode), `stats.py:render_questions` (escape), `calendar_view.py:_CAPTION`, `tests/test_replies_uz.py` (REVIEWED_UZ — те же слова + разметка).

### Task 1: parse_mode + экранирование

- [x] Тесты: send_message/edit с parse_mode="HTML" в body (и БЕЗ него по умолчанию); t() экранирует kwargs («<script>» не проходит сырым); эскалация уходит без parse_mode; render_questions экранирует.
- [x] Код: api parse_mode-параметр, send_reply/edit_reply → "HTML", digest → "HTML", html.escape в t() и render_questions, эскалации plain.

### Task 2: шаблоны + карточки + меню/услуги

- [x] Тесты: booked-карточка содержит «ЗАПИСЬ ПОДТВЕРЖДЕНА», 🦷/📅 и врача отдельной строкой; greeting-hero с <b>{clinic}</b>; СТАРЫЙ label «Записаться» продолжает матчиться (legacy); кнопки услуг с эмодзи-префиксом; меню-label'ы новые.
- [x] Код: переписать TEMPLATES (обвязка, слова на месте), btn_menu_* + _LEGACY_MENU_LABELS, SERVICE_EMOJI + _service_buttons, booked/resched_done/reminder карточки, booking_flow doctor-строка, _CAPTION; REVIEWED_UZ зеркально.

### Task 3: финал

- [x] Полный сьют зелёный; demo + `--check`; commit `feat(ui): manicure-bot visual style — HTML, hero, emoji (P7)` + push; рестарт бота.
