# П-2а Мягкие эскалации — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Эскалация (заморозка + алерт) остаётся ровно в двух пациентских путях: прямая просьба человека и двойной сбой подтверждения записи. «Вопрос вне компетенции» и 2×ExtractionError больше не дёргают админа. Второй инкремент по спеке `docs/superpowers/specs/2026-06-11-dialog-polish-showcase-design.md` (секция 2).

**Architecture:** Детектор просьбы человека — чистая функция в dialog_common (word-boundary regex ru/uz, биграммы для «человек»/«odam» против ложняков «запишите двух человек»), срабатывает в `_process_text` ДО NLU (ноль токенов, работает при лежащем LLM) во всех состояниях, КРОМЕ awaiting_name. Вне рабочего окна пациенту честное «ответит утром» (`_closed_now`). Вопрос без service (после детектора наличия П-1) → строка not_understood + меню, БЕЗ notify; faq_fallback удаляется. 2×ExtractionError → not_understood + повтор текущего шага кнопками через `_with_reprompt` (кнопочный путь покрывает оформление без NLU — путь C-4). Сбой confirm: счётчик `confirm_failures` в DialogContext; 1-й — отпустить hold + re-offer слотов с нотой confirm_retry; 2-й — notify + escalated (пациент доведён до конца, терять нельзя).

**Tech Stack:** stdlib re. Ноль миграций.

**Контекст кодовой базы:**
- `src/navbat/dialog/fsm.py:222–245` — `_process_text`: детектор встаёт между awaiting_name- и awaiting_phone-диспетчем; `_on_nlu_failure` получает session (вызов :238).
- `src/navbat/dialog/fsm.py:363+` — `_answer_question`: ветка без service → not_understood+menu, notify убирается.
- `src/navbat/dialog/fsm.py:192` — `_on_menu`: первая строка — сброс nlu_failures (меню = пациент сориентировался).
- `src/navbat/dialog/booking_flow.py:184–205` — `_confirm_and_finish`: generic except вокруг confirm (engine ведёт СВОИ транзакции — диалоговая сессия не отравлена), hold гасится с suppress.
- `src/navbat/dialog/conversation.py:23–27` — `_BOOKING_FIELDS` + поле `confirm_failures` (clear_booking может ставить None — арифметика через `or 0`).
- `tests/test_dialog_escalation.py` — переписывается: эскалация по сбоям NLU исчезает; сетап escalated-тестов — через «позовите администратора».
- `tests/test_replies_uz.py` — REVIEWED_UZ: faq_fallback удалить; ASCII-апостроф в uz намеренный.
- `tests/test_availability_question.py::test_unrelated_question_keeps_current_path` — переписать под not_understood без алерта.

---

### Task 1: детектор просьбы человека + эскалация по просьбе + сброс счётчика на меню

**Files:** Create `tests/test_soft_escalations.py`; Modify `src/navbat/dialog/dialog_common.py`, `src/navbat/dialog/fsm.py`, `src/navbat/dialog/replies.py`, `tests/test_replies_uz.py`

- [x] Тесты: чистый детектор (позитив: «позовите администратора», «дайте оператора», «соедините с менеджером», «нужен живой человек», «administratorni chaqiring», «operator kerak»; негатив: «запишите двух человек», «ikki odamga», обычные фразы); idle-просьба → escalated + 1 notify «пациент просит администратора»; вне рабочего окна → текст «утром» (clock инжектится); на awaiting_phone — эскалация + hold отменён; на awaiting_name — НЕ срабатывает (текст принят как имя); меню-нажатие сбрасывает nlu_failures.
- [x] Код: `mentions_human_request` (dialog_common); `_escalate_on_request` (notify ДО abort_pending — контекст не потерять); вставка в `_process_text`; сброс счётчика в `_on_menu`; строка `escalated_closed` ru/uz.
- [x] Run новый файл → PASS.

### Task 2: «не понял» вместо «вне компетенции» + мягкий 2×ExtractionError

**Files:** Modify `src/navbat/dialog/fsm.py`, `src/navbat/dialog/replies.py`, `tests/test_dialog_escalation.py`, `tests/test_availability_question.py`, `tests/test_replies_uz.py`

- [x] Тесты: вопрос без service вне контекста → not_understood + меню + подсказка «позовите администратора», notify ПУСТ; 2×ExtractionError в idle → меню, не escalated, notify пуст (и 3-й сбой тоже); 2×ExtractionError на booking_offer_slots → ответ с кнопками слотов (повтор шага); переписать test_dialog_escalation (escalated-сетапы через просьбу человека), test_availability_question::test_unrelated_question_keeps_current_path.
- [x] Код: `_answer_question` без notify; `_on_nlu_failure(session, conv)` мягкий; строка not_understood ru/uz; faq_fallback удалить (replies + REVIEWED_UZ).
- [x] Run затронутые файлы → PASS.

### Task 3: сбой confirm — retry, потом эскалация

**Files:** Modify `src/navbat/dialog/booking_flow.py`, `src/navbat/dialog/conversation.py`, `src/navbat/dialog/replies.py`, `tests/test_soft_escalations.py`, `tests/test_replies_uz.py`

- [x] Тесты (FailingScheduler-обёртка с падающим confirm, пациент существует — create_patient): 1-й сбой → hold отменён, слоты заново с нотой confirm_retry, notify пуст; 2-й сбой → escalated + 1 notify «сбой подтверждения записи»; успешный confirm обнуляет счётчик (clear_booking).
- [x] Код: confirm_failures в DialogContext + _BOOKING_FIELDS; except Exception в `_confirm_and_finish`; строка confirm_retry ru/uz.
- [x] Run → PASS.

### Task 4: финал

- [x] Полный сьют `python -m pytest -q` → зелёный.
- [x] Commit `feat(dialog): escalation only on human request or confirm failure (P2a)` + push.

## Definition of Done (П-2а)

- [x] Единственные текстовые пути к escalated: просьба человека, 2×confirm-сбой (не-998 уйдёт в П-2в).
- [x] «вы принимаете карты?» и двойная абракадабра не дёргают админа.
- [x] Счётчик сбоев сбрасывается меню; повтор шага кнопками работает посреди оформления.
- [x] Полный сьют зелёный; коммит в origin/master.
