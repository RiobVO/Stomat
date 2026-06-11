# П-4 Кнопочная инфраструктура — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Транспортный фундамент инлайн-календаря (П-5): многорядная inline-клавиатура, редактирование сообщения на месте, toast на кнопке, сырые короткие `cal:`-callback'и мимо tg_actions-маппинга. Чистая инфраструктура — поведение диалога не меняется.

**Architecture:** `Reply` получает `button_rows` (ряды кнопок; flat `buttons` = одна колонка, как раньше), `edit` (воркер редактирует сообщение-источник callback'а) и `toast` (текст answerCallbackQuery; пустой `text` → сообщение не шлётся). `api.py`: общий `_inline_keyboard(buttons, button_rows)`, `edit_message_text` (ошибка «message is not modified» гасится тихо — повторный клик по навигации не валит воркер), `answer_callback_query(..., text=)`. Нумерация кнопок выносится в `_numbered_rows`: action'ы с префиксом `cal:` НЕ нумеруются и идут сырыми (≤64 байт) — они переживают перезапись tg_actions любым следующим сообщением (напоминание!), остальное — a:N как раньше. Воркер: data с префиксом `cal:` → действие напрямую (мимо lookup), `reply.edit` → editMessageText по `callback.message.message_id`.

**Контекст кодовой базы:** `replies.py:19–30` (Reply), `api.py:55–94` (send_message/answer_callback_query, TelegramAPIError), `worker.py:187–198` (callback-ветка), `worker.py:448–468` (send_reply — единственная точка нумерации), `tests/test_tg_api.py` (MockTransport-паттерн), `tests/test_tg_worker.py:27–61` (FakeTelegramAPI, make_worker с dialog=).

### Task 1: api-слой (rows, edit, toast)

- [x] Тесты: send_message c button_rows → inline_keyboard рядами; edit_message_text → editMessageText c chat_id/message_id/reply_markup; «message is not modified» (400) глотается, прочие ошибки — нет; answer_callback_query с text.
- [x] Код: `_inline_keyboard`, `send_message(..., button_rows=())`, `edit_message_text`, `answer_callback_query(..., text=None)`.

### Task 2: send_reply/edit_reply + воркер

- [x] Тесты: send_reply с button_rows — сквозная нумерация по рядам, map в tg_actions, cal:-кнопки сырыми; воркер: callback `cal:x` → dialog получил `cal:x` (spy), мусор → `stale`; reply.edit → edit_message_text с message_id callback'а; reply.toast → answer_callback_query c текстом; text=="" → сообщение не отправлено.
- [x] Код: `_numbered_rows` (+ cal:-passthrough), `edit_reply`, callback-ветка воркера, FakeTelegramAPI (button_rows, edited, toasts).

### Task 3: финал

- [x] Полный сьют зелёный; commit `feat(telegram): button rows, message edit, toast, raw cal callbacks (P4)` + push.
