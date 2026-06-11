# П-1 Детектор наличия — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вопрос о наличии в любой формулировке («а больше слотов нету?», «а ещё?», «другой день?») ведёт к выбору дня (как «Другое время»), БЕЗ алерта админу. Первый инкремент по спеке `docs/superpowers/specs/2026-06-11-dialog-polish-showcase-design.md` (секция 1).

**Architecture:** Детерминированный детектор в коде (LLM не трогаем). Два независимых триггера для intent∈{question,other} без service: (1) контекст — диалог в состоянии предложения слотов (`booking_collect`/`booking_offer_slots`/`resched_offer_slots`) или в контексте уже есть service/date — повторить предложение безопасно всегда; (2) словарь — availability-маркеры ru/uz (узкий, word-boundary regex, нормализация узбекских апострофов). Ответ = существующий путь `ask_date` (кнопки Сегодня/Завтра/Послезавтра; календарь добавится в П-5), без услуги в контексте — дефолт checkup (тот же принцип, что в book-бэкстопе fsm.py:259–263). Существующий бэкстоп question+date_ref→слоты не меняется.

**Tech Stack:** stdlib re. Ноль новых зависимостей, ноль миграций.

**Контекст кодовой базы:**
- `src/navbat/dialog/fsm.py:246–274` — `_route_intent(session, conv, extraction)`: сюда встаёт ветка наличия ПОСЛЕ booking_like-блоков и ДО `_answer_question`; сигнатура получает `message` (вызов — fsm.py:243).
- `src/navbat/dialog/fsm.py:320–323` — ветка `ask_date` в `_process_action`: тело выносится в хелпер `_ask_date`, используется обоими путями.
- `src/navbat/dialog/dialog_common.py` — чистая функция `mentions_availability` + regex (модуль без зависимостей от mixin'ов — тестируется без БД).
- `tests/test_dialog_booking.py` — `CHAT, RecordingNotifier, extr, explicit, slot_buttons, fsm_state`; conftest: `make_service, next_monday`, фикстуры `app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning`.
- `tests/test_dialog_reschedule_cancel.py:18–22` — `book_directly` для компактного сетапа переноса.
- Известная дыра NLU (CLAUDE.md): book↔question на косвенных вопросах — прикрываем FSM, промпт не дожимаем.

---

### Task 1: детектор + маршрутизация + тесты

**Files:**
- Create: `tests/test_availability_question.py`
- Modify: `src/navbat/dialog/dialog_common.py`, `src/navbat/dialog/fsm.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_availability_question.py`: чистый детектор (параметризованные позитив/негатив, включая uz-апострофы U+02BB/U+2019 и негатив «мой друг посоветовал»); idle + маркер → 3 кнопки `date:` + state `booking_collect` + notifier пуст; контекст после показа слотов (фраза БЕЗ маркеров) → кнопки дат, без алерта; resched_offer_slots + вопрос наличия → state остаётся resched; question+service → прайс как раньше; вопрос без маркеров вне контекста → старый путь (алерт, до П-2а); выбранная дата после availability-ответа → слоты по checkup (end-to-end без тупика).
- [ ] **Step 2: Run** → FAIL (нет mentions_availability / ветки маршрутизации)
- [ ] **Step 3: Код** — `dialog_common.py`: `mentions_availability` (casefold + нормализация апострофов + word-boundary regex: ещё|еще|друго\w*|други\w*|свободн\w*|окошк\w*|мест\w*|слот\w*|вариант\w*|попозже|пораньше|boshqa|yana|bo'sh\w*|joy\w*|vaqt\w*). `fsm.py`: `_route_intent(..., message)`; ветка наличия; `_asks_availability(conv, message)` (контекст ИЛИ словарь); `_availability_reply` (дефолт checkup вне resched → `_ask_date`); ветка `ask_date` в `_process_action` → `self._ask_date(session, conv)`.
- [ ] **Step 4: Run** новый файл + test_dialog_booking + test_dialog_question + test_dialog_menu + test_dialog_reschedule_cancel → PASS
- [ ] **Step 5:** полный сьют `python -m pytest -q` → зелёный
- [ ] **Step 6: Commit** `feat(dialog): availability questions get date picker, not escalation (P1)` + push

## Definition of Done (П-1)

- [ ] «а больше слотов нету?» (и uz-аналоги) → кнопки дат, notifier.calls == [].
- [ ] Контекст предложения слотов делает ЛЮБОЙ вопрос без услуги вопросом наличия.
- [ ] Прайс (question+service) и бэкстоп question+date_ref не задеты.
- [ ] Полный сьют зелёный; коммит в origin/master.
