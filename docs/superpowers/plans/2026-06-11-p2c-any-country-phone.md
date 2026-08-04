# П-2в Номер любой страны — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Контакт из кнопки Telegram принимается с НОМЕРОМ ЛЮБОЙ СТРАНЫ (решение пользователя 11.06: номер из кнопки всегда подлинный — это номер аккаунта). Эскалация-тупик «не-узбекский номер» исчезает. Четвёртый инкремент по спеке (секция 2, таблица диспозиций).

**Architecture:** `normalize_phone`: 9 цифр → префикс 998 (локальный узбекский, как раньше); любые 7–15 цифр (E.164) → как есть; иначе ValueError (мусор). Хэш-механика (SHA-256 + per-clinic соль) работает с любой строкой — не меняется. Граница очереди `_redact_contact_phone` теперь хэширует и иностранные номера (открытый номер по-прежнему вырезается ДО записи payload). `phone_hash=None` в диалоге становится «номер не распознан» (практически невозможно с кнопки) → повтор кнопки вместо эскалации. PRIVACY.md разд. 7/9 синхронизируются.

**Контекст кодовой базы:**
- `src/navbat/dialog/patients.py:28–35` — normalize_phone (единственная точка валидации).
- `src/navbat/telegram/queue.py:30–48` — _redact_contact_phone (docstring «не-998 → эскалация» устарел).
- `src/navbat/dialog/booking_flow.py:~165` — ветка phone_hash is None → было escalated.
- `tests/test_dialog_contact.py:143–155,183–196`, `tests/test_queue.py:182–191` — переписать под приём; `tests/test_dialog_booking.py:76–94` — m1-тест использует не-998 как сетап эскалации → пересетапить через «позовите администратора» на шаге телефона.

### Task 1 (единственная)

- [x] Тесты: иностранный номер из кнопки → запись завершена, хэш в patient = sha256(номер+соль), эскалаций нет; очередь хэширует иностранный номер (сырые цифры не утекли); мусорный contact (3 цифры) → без хэша → повтор кнопки (не эскалация); m1-тест через просьбу человека.
- [x] Код: normalize_phone (7–15 цифр), docstrings очереди/handle_contact, booking_flow None-ветка → press_contact_button.
- [x] PRIVACY.md разд. 7/9: «номер любой страны хэшируется; нераспознанный контакт остаётся без хэша и без записи».
- [x] Полный сьют зелёный; commit `feat(dialog): accept any-country phone from contact button (P2c)` + push.
