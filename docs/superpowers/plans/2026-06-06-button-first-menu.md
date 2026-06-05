# Кнопочный вход (button-first, LLM-fallback) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** /start с выбором языка, постоянное reply-меню («📅 Записаться / 🔄 Перенести / ❌ Отменить / 💰 Цены / 🌐 Til/Язык»), перехват нажатий до NLU — ноль токенов на happy path; свободный текст остаётся LLM-fallback.

**Architecture:** Нажатие reply-кнопки приходит в Telegram обычным текстом — FSM перехватывает его точным матчем по словарю label'ов (оба языка) ДО вызова экстрактора и ДО PII-эвристик. Кнопки меню маппятся на существующие пути FSM (`_advance_booking`, `_start_reschedule`, `_start_cancel`); новые экраны — выбор языка и прайс-лист. БД/миграции не меняются (язык уже живёт в `conversation.context["lang"]`).

**Tech Stack:** Python 3.12, SQLAlchemy + PostgreSQL (docker, порт **5434**), pytest, httpx. Тесты гоняются против реального postgres: `docker compose up -d` обязателен.

**Спека:** `docs/superpowers/specs/2026-06-06-button-first-menu-design.md`

**Конвенции проекта (обязательны):**
- Комментарии и docstrings — русский; идентификаторы и коммиты — английский.
- NLU в тестах — только FakeExtractor/CountingExtractor, НИКАКИХ вызовов OpenAI.
- Запуск тестов: `python -m pytest` из корня `E:\Stomat`.
- Перед финальным «готово» сьют гоняется серией 8 раз (есть конкурентные тесты).

**Карта файлов:**

| Файл | Что меняется |
|---|---|
| `src/navbat/dialog/replies.py` | `Reply.menu`, шаблоны меню/языка/прайса, `menu_rows()`; в конце — удаление `remove_keyboard` |
| `src/navbat/dialog/fsm.py` | перехват `/start`+меню до NLU, `_on_start`, `_on_menu`, `_abort_pending`, `_price_list`, action `lang:`; контакт-шаг возвращает меню |
| `src/navbat/telegram/api.py` | рендер persistent `ReplyKeyboardMarkup` из `menu` |
| `src/navbat/telegram/worker.py` | `send_reply` пробрасывает `menu` |
| `src/navbat/demo.py` | печать меню подсказкой |
| `tests/test_dialog_menu.py` | новый файл — все FSM-тесты меню |
| `tests/test_dialog_contact.py`, `tests/test_tg_api.py`, `tests/test_tg_worker.py` | обновление под меню вместо `remove_keyboard` |

---

### Task 1: Модель Reply + шаблоны меню

Чистая модель и конфиг-строки — отдельных тестов не пишем (правило проекта: конфиги не тестируются), всё накрывается FSM-тестами задач 2–4. После правки существующий сьют обязан остаться зелёным.

**Files:**
- Modify: `src/navbat/dialog/replies.py`

- [ ] **Step 1: Добавить поле `menu` в Reply**

В `src/navbat/dialog/replies.py` заменить класс `Reply` (строки 18–28) на:

```python
@dataclass(frozen=True)
class Reply:
    """contact_request, buttons и menu взаимоисключающие: в Telegram reply_markup один.

    contact_request — label кнопки «Поделиться контактом» (ReplyKeyboardMarkup,
    request_contact=True); remove_keyboard убирает reply-клавиатуру;
    menu — ряды label'ов постоянной reply-клавиатуры главного меню.
    """
    text: str
    buttons: tuple[Button, ...] = ()
    contact_request: str | None = None
    remove_keyboard: bool = False
    menu: tuple[tuple[str, ...], ...] | None = None
```

(`remove_keyboard` пока остаётся — выпиливается в Task 5 вместе с последним продюсером.)

- [ ] **Step 2: Добавить шаблоны**

В словарь `TEMPLATES` (перед закрывающей скобкой, после `"text_only"`) добавить:

```python
    "choose_lang": {
        "ru": "Tilni tanlang / Выберите язык:",
        "uz": "Tilni tanlang / Выберите язык:",
    },
    "menu_hint": {
        "ru": "Выберите действие или напишите своими словами:",
        "uz": "Amalni tanlang yoki o'z so'zlaringiz bilan yozing:",
    },
    "lang_changed": {
        "ru": "Язык переключён на русский.",
        "uz": "Til o'zbek tiliga o'zgartirildi.",
    },
    "price_header": {"ru": "Наши цены:", "uz": "Narxlarimiz:"},
    "price_line": {
        "ru": "• {service} — {price} сум",
        "uz": "• {service} — {price} so'm",
    },
    "price_line_unknown": {
        "ru": "• {service} — цену уточнит администратор",
        "uz": "• {service} — narxini administrator aniqlashtiradi",
    },
    "price_empty": {
        "ru": "Прайс уточнит администратор.",
        "uz": "Narxlarni administrator aniqlashtiradi.",
    },
    "btn_menu_book": {"ru": "📅 Записаться", "uz": "📅 Yozilish"},
    "btn_menu_resched": {"ru": "🔄 Перенести", "uz": "🔄 Ko'chirish"},
    "btn_menu_cancel": {"ru": "❌ Отменить", "uz": "❌ Bekor qilish"},
    "btn_menu_prices": {"ru": "💰 Цены", "uz": "💰 Narxlar"},
    "btn_menu_lang": {"ru": "🌐 Til / Язык", "uz": "🌐 Til / Язык"},
    "btn_lang_uz": {"ru": "O'zbekcha", "uz": "O'zbekcha"},
    "btn_lang_ru": {"ru": "Русский", "uz": "Русский"},
```

Узбекские строки — черновик, как и остальные (проверка носителем — общий хвост проекта).

- [ ] **Step 3: Добавить `menu_rows()`**

В конец `replies.py` (после `service_label`):

```python
def menu_rows(lang: str) -> tuple[tuple[str, ...], ...]:
    """Ряды постоянной reply-клавиатуры главного меню."""
    return (
        (t("btn_menu_book", lang),),
        (t("btn_menu_resched", lang), t("btn_menu_cancel", lang)),
        (t("btn_menu_prices", lang), t("btn_menu_lang", lang)),
    )
```

- [ ] **Step 4: Прогнать сьют — регрессов нет**

Run: `python -m pytest -q`
Expected: все тесты зелёные (поле аддитивное).

- [ ] **Step 5: Commit**

```bash
git add src/navbat/dialog/replies.py
git commit -m "feat(dialog): menu templates and Reply.menu field"
```

---

### Task 2: /start и выбор языка

**Files:**
- Modify: `src/navbat/dialog/fsm.py`
- Test: `tests/test_dialog_menu.py` (создать)

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_dialog_menu.py`:

```python
"""Кнопочный вход: /start, выбор языка, главное меню — всё до NLU.

Нажатие reply-кнопки приходит текстом; FSM матчит label (оба языка)
до экстрактора — CountingExtractor доказывает ноль вызовов NLU.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES, menu_rows
from navbat.nlu.extractor import ExtractionError
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    appt_status,
    explicit,
    extr,
    fsm_state,
    slot_buttons,
)
from test_dialog_contact import CountingExtractor


def counting_engine(app_session_factory, clinic_id, script=()):
    extractor = CountingExtractor(list(script))
    engine = DialogEngine(app_session_factory, clinic_id, extractor=extractor,
                          notifier=RecordingNotifier())
    return engine, extractor


# ── /start и язык ────────────────────────────────────────────────────────────

def test_start_first_time_offers_language_choice(app_session_factory, clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    reply = engine.handle_text(CHAT, "/start")
    assert [b.action for b in reply.buttons] == ["lang:uz", "lang:ru"]
    assert "Tilni tanlang" in reply.text
    assert "Clinic A" not in reply.text, "приветствие — после выбора языка"
    assert extractor.calls == [], "/start не должен уходить в NLU"


def test_lang_choice_shows_greeting_with_menu(app_session_factory, admin_engine,
                                              clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    reply = engine.handle_action(CHAT, "lang:uz")
    assert reply.menu == menu_rows("uz")
    assert "Clinic A" in reply.text, "приветствие-дисклеймер (P0 BRIEF)"
    assert extractor.calls == []
    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "uz"


def test_start_repeat_skips_language_choice(app_session_factory, clinic_a):
    engine, _ = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, "lang:ru")
    again = engine.handle_text(CHAT, "/start")
    assert again.menu == menu_rows("ru")
    assert not again.buttons, "язык уже выбран — сразу меню"


def test_start_after_text_dialog_keeps_detected_lang(app_session_factory, clinic_a,
                                                     doctor_a, service_cleaning):
    # пациент начал текстом (язык детектнут NLU) — /start не переспрашивает язык
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()),
                     language="uz")])
    engine.handle_text(CHAT, "ertaga tish tozalashga yozilmoqchiman")
    reply = engine.handle_text(CHAT, "/start")
    assert reply.menu == menu_rows("uz")
    assert not reply.buttons
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: 4 FAIL (`/start` сейчас уходит в NLU → `ExtractionError` → reask; `lang:uz` — неизвестный action → `other_fallback`).

- [ ] **Step 3: Реализация в fsm.py**

В `src/navbat/dialog/fsm.py`:

3a. Дополнить импорты: в `from contextlib import suppress` (новая строка после `from dataclasses import replace`); в импорт из `navbat.dialog.replies` добавить `TEMPLATES` и `menu_rows`:

```python
from contextlib import suppress
```

```python
from navbat.dialog.replies import (
    MEDICAL_DISCLAIMER,
    TEMPLATES,
    Button,
    Reply,
    menu_rows,
    service_label,
    t,
)
```

3b. После констант (`_BOOKING_KEYS`) добавить словарь label → ключ:

```python
_MENU_KEYS = ("btn_menu_book", "btn_menu_resched", "btn_menu_cancel",
              "btn_menu_prices", "btn_menu_lang")
# нажатие reply-кнопки приходит ТЕКСТОМ — матчим label'ы обоих языков
_MENU_ACTIONS = {
    TEMPLATES[key][lang]: key for key in _MENU_KEYS for lang in ("ru", "uz")
}
```

3c. Заменить `handle_text` (строки 89–101) на:

```python
    def handle_text(self, chat_id: int, message: str) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._handle_command_or_menu(session, conv, message)
            if reply is None:
                first_contact = "greeting_shown" not in conv.context
                reply = self._process_text(session, conv, message)
                if first_contact:
                    # P0 BRIEF: дисклеймер при первом контакте
                    conv.context["greeting_shown"] = True
                    greeting = t("greeting", self._lang(conv),
                                 clinic=self._clinic_name(session))
                    reply = Reply(f"{greeting}\n\n{reply.text}", reply.buttons)
            save_conversation(session, conv)
        return reply
```

3d. После `handle_contact` добавить секцию (перед `# ── Текст ──`):

```python
    # ── /start и главное меню (перехват до NLU) ──────────────────────────

    def _handle_command_or_menu(self, session: Session, conv: Conversation,
                                message: str) -> Reply | None:
        """Перехват /start и кнопок меню ДО NLU и до PII-эвристик: ноль токенов.

        None — обычный текст, идёт штатным путём (LLM-fallback).
        """
        if conv.state == "escalated":
            return None  # стоп-состояние не обходится кнопками
        stripped = message.strip()
        if stripped == "/start":
            return self._on_start(session, conv)
        key = _MENU_ACTIONS.get(stripped)
        if key is None:
            return None
        return self._on_menu(session, conv, key)

    def _on_start(self, session: Session, conv: Conversation) -> Reply:
        self._abort_pending(conv)
        if "lang" not in conv.context:
            return self._lang_screen(conv)
        return self._greeting_with_menu(session, conv)

    def _on_menu(self, session: Session, conv: Conversation, key: str) -> Reply:
        # заполняется в Task 3–4; до тех пор меню-текст падает в NLU-путь
        return None

    def _lang_screen(self, conv: Conversation) -> Reply:
        return Reply(t("choose_lang", self._lang(conv)),
                     (Button(t("btn_lang_uz", "ru"), "lang:uz"),
                      Button(t("btn_lang_ru", "ru"), "lang:ru")))

    def _greeting_with_menu(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        conv.context["greeting_shown"] = True
        greeting = t("greeting", lang, clinic=self._clinic_name(session))
        return Reply(f"{greeting}\n\n{t('menu_hint', lang)}", menu=menu_rows(lang))

    def _abort_pending(self, conv: Conversation) -> None:
        """Меню/старт посреди сценария = явная смена намерения.

        Висящий hold отпускаем: бронь слота не должна переживать отказ
        от записи. Протухший/уже отменённый hold — цель и так достигнута.
        """
        appt = conv.context.get("appointment_id")
        if appt:
            with suppress(AppointmentNotFoundError):
                self._sched.cancel(uuid.UUID(appt))
        self._clear_booking(conv)
        conv.state = "idle"
```

ВНИМАНИЕ: `_on_menu` в этой задаче — заглушка `return None`; тесты задачи 2 её не трогают (тип нарушен сознательно и временно — Task 3 её реализует. Если линтер/ревью ругается, допустимо `key in _MENU_ACTIONS`-ветку в `_handle_command_or_menu` добавить только в Task 3, а `_on_menu` не объявлять вовсе — решает исполнитель, тесты задают поведение).

3e. В `_process_action` добавить обработку `lang:` — после строки `kind, _, rest = action.partition(":")` (строка 292), перед `if kind == "service":`:

```python
        if kind == "lang":
            conv.context["lang"] = rest
            if not conv.context.get("greeting_shown"):
                return self._greeting_with_menu(session, conv)
            note = Reply(t("lang_changed", rest), menu=menu_rows(rest))
            return self._with_reprompt(session, conv, note)
```

- [ ] **Step 4: Тесты зелёные**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: 4 PASS.

- [ ] **Step 5: Полный сьют — нет регрессов**

Run: `python -m pytest -q`
Expected: все зелёные. Особо смотреть `test_dialog_booking.py` (греетинг-обёртка первого контакта реструктурирована).

- [ ] **Step 6: Commit**

```bash
git add src/navbat/dialog/fsm.py tests/test_dialog_menu.py
git commit -m "feat(dialog): /start with language choice and main menu greeting"
```

---

### Task 3: Кнопки меню — Записаться / Перенести / Отменить

**Files:**
- Modify: `src/navbat/dialog/fsm.py`
- Test: `tests/test_dialog_menu.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dialog_menu.py`:

```python
# ── Кнопки меню: запись, перенос, отмена ─────────────────────────────────────

def start_with_menu(engine, lang="ru"):
    """Доводит чат до состояния «меню показано, язык выбран»."""
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, f"lang:{lang}")


def test_menu_book_starts_booking_without_nlu(app_session_factory, admin_engine,
                                              clinic_a, doctor_a, service_cleaning):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert fsm_state(admin_engine) == "booking_collect"
    assert extractor.calls == [], "кнопка меню не должна уходить в NLU"


def test_menu_book_uz_label_matches_too(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine, lang="uz")
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["uz"])
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert extractor.calls == []


def test_menu_book_mid_booking_releases_hold(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    # пациент дошёл до шага имени (hold создан) и передумал — жмёт «Записаться»
    engine, extractor = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()))])
    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    assert fsm_state(admin_engine) == "awaiting_name"

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert appt_status(admin_engine) == "cancelled", "hold отпущен"
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert len(extractor.calls) == 1, "только исходная фраза, кнопка — нет"


def test_menu_resched_without_appointment(app_session_factory, clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_resched"]["ru"])
    assert reply.text == TEMPLATES["resched_none"]["ru"]
    assert extractor.calls == []


def test_menu_resched_with_booking_asks_date(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appt = sched.hold(doctor_a, service_cleaning,
                      at_tashkent(next_monday(), "09:00"), tg_chat_id=CHAT)
    sched.confirm(appt)

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_resched"]["ru"])
    assert any(b.action.startswith("date:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "resched_offer_slots"
    assert extractor.calls == []


def test_menu_cancel_confirms_active_booking(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appt = sched.hold(doctor_a, service_cleaning,
                      at_tashkent(next_monday(), "09:00"), tg_chat_id=CHAT)
    sched.confirm(appt)

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_cancel"]["ru"])
    assert [b.action for b in reply.buttons] == ["cancel_yes", "cancel_no"]
    assert extractor.calls == []

    done = engine.handle_action(CHAT, "cancel_yes")
    assert done.text == TEMPLATES["cancel_done"]["ru"]
    assert appt_status(admin_engine) == "cancelled"


def test_menu_in_escalated_state_stays_blocked(app_session_factory, admin_engine,
                                               clinic_a):
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[ExtractionError("кривой JSON"), ExtractionError("кривой JSON")])
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "абракадабра ещё раз")
    assert fsm_state(admin_engine) == "escalated"

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert reply.text == TEMPLATES["escalated"]["ru"], \
        "кнопки не обходят стоп-состояние"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: новые тесты FAIL (label меню уходит в NLU → ExtractionError/реask), кроме `test_menu_in_escalated_state_stays_blocked` — он может пройти сразу (escalated-ветка уже есть); это нормально, тест — страховка от регресса.

- [ ] **Step 3: Реализовать `_on_menu` (книга/перенос/отмена)**

В `fsm.py` заменить заглушку `_on_menu` на:

```python
    def _on_menu(self, session: Session, conv: Conversation, key: str) -> Reply:
        if key == "btn_menu_book":
            self._abort_pending(conv)
            return self._advance_booking(session, conv)
        if key == "btn_menu_resched":
            self._abort_pending(conv)
            return self._start_reschedule(session, conv,
                                          self._empty_extraction(conv, "reschedule"))
        if key == "btn_menu_cancel":
            # hold текущей незавершённой записи отпускаем: «Отменить»
            # посреди оформления = отказ от него
            self._abort_pending(conv)
            return self._start_cancel(session, conv)
        if key == "btn_menu_prices":
            return self._with_reprompt(session, conv,
                                       self._price_list(session, conv))
        return self._lang_screen(conv)  # btn_menu_lang

    def _empty_extraction(self, conv: Conversation, intent: str) -> Extraction:
        """Кнопка меню = чистый intent без слотов (минуя NLU)."""
        lang = self._lang(conv)
        return Extraction(intent=intent, service=None, doctor=None,
                          date_ref=None, time_ref=None,
                          language=lang, is_medical=False)
```

`_price_list` появится в Task 4 — чтобы Task 3 был зелёным самостоятельно, добавить ВРЕМЕННУЮ заглушку (заменяется в Task 4):

```python
    def _price_list(self, session: Session, conv: Conversation) -> Reply:
        return Reply(t("price_empty", self._lang(conv)))
```

- [ ] **Step 4: Тесты зелёные**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: PASS все.

- [ ] **Step 5: Полный сьют**

Run: `python -m pytest -q`
Expected: зелёный.

- [ ] **Step 6: Commit**

```bash
git add src/navbat/dialog/fsm.py tests/test_dialog_menu.py
git commit -m "feat(dialog): menu buttons book/reschedule/cancel bypass NLU"
```

---

### Task 4: Прайс-лист и смена языка из меню

**Files:**
- Modify: `src/navbat/dialog/fsm.py`
- Test: `tests/test_dialog_menu.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dialog_menu.py`:

```python
# ── Прайс и язык ─────────────────────────────────────────────────────────────

def test_menu_prices_lists_catalog(app_session_factory, admin_engine, clinic_a,
                                   service_cleaning):
    # услуга с ценой и услуга без цены — обе в списке
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET price = 200000 WHERE name = 'cleaning'"))
    make_service(admin_engine, clinic_a, "checkup", 30)  # без цены

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_prices"]["ru"])
    assert "Чистка — 200 000 сум" in reply.text
    assert "Осмотр — цену уточнит администратор" in reply.text
    assert extractor.calls == []


def test_menu_prices_mid_booking_reprompts_step(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    # вопрос цены посреди записи — ответ + повтор шага, сценарий не сброшен
    engine, _ = counting_engine(app_session_factory, clinic_a,
                                script=[extr(service="cleaning")])
    engine.handle_text(CHAT, "хочу чистку")          # booking_collect, спросил дату
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_prices"]["ru"])
    assert TEMPLATES["price_header"]["ru"] in reply.text
    assert TEMPLATES["ask_date"]["ru"] in reply.text, "шаг повторён"
    assert any(b.action.startswith("date:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "booking_collect"


def test_menu_lang_switch_mid_booking_reprompts_in_new_lang(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    engine, _ = counting_engine(app_session_factory, clinic_a,
                                script=[extr(service="cleaning")])
    engine.handle_text(CHAT, "хочу чистку")          # booking_collect (ru)
    screen = engine.handle_text(CHAT, TEMPLATES["btn_menu_lang"]["ru"])
    assert [b.action for b in screen.buttons] == ["lang:uz", "lang:ru"]

    reply = engine.handle_action(CHAT, "lang:uz")
    assert TEMPLATES["lang_changed"]["uz"] in reply.text
    assert TEMPLATES["ask_date"]["uz"] in reply.text, "повтор шага на новом языке"
    assert fsm_state(admin_engine) == "booking_collect"
```

- [ ] **Step 2: Убедиться, что падают**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: 3 новых FAIL (прайс-заглушка отвечает `price_empty`; остальное может частично работать — смотреть фактический вывод).

- [ ] **Step 3: Реализовать `_price_list`**

Заменить заглушку из Task 3 на (в секции `# ── Вопросы ──` рядом с `_answer_question`):

```python
    def _price_list(self, session: Session, conv: Conversation) -> Reply:
        """Весь прайс из каталога services; пустой каталог — к администратору."""
        lang = self._lang(conv)
        rows = session.execute(
            text("SELECT name, price FROM service ORDER BY name")).all()
        if not rows:
            return Reply(t("price_empty", lang))
        lines = []
        for row in rows:
            label = service_label(row.name, lang)
            if row.price is None:
                lines.append(t("price_line_unknown", lang, service=label))
            else:
                price = f"{int(row.price):,}".replace(",", " ")
                lines.append(t("price_line", lang, service=label, price=price))
        return Reply(t("price_header", lang) + "\n" + "\n".join(lines))
```

(Форматирование цены — тот же приём, что в `_answer_question` строка 571.)

- [ ] **Step 4: Тесты зелёные**

Run: `python -m pytest tests/test_dialog_menu.py -q`
Expected: PASS.

- [ ] **Step 5: Полный сьют**

Run: `python -m pytest -q`
Expected: зелёный.

- [ ] **Step 6: Commit**

```bash
git add src/navbat/dialog/fsm.py tests/test_dialog_menu.py
git commit -m "feat(dialog): price list and language switch from menu"
```

---

### Task 5: Транспорт меню до Telegram + контакт-шаг возвращает меню + выпил remove_keyboard

`remove_keyboard` после этой задачи теряет последнего продюсера (fsm.py:415) — выпиливается целиком из Reply/api/worker (дед-код недопустим).

**Files:**
- Modify: `src/navbat/telegram/api.py`, `src/navbat/telegram/worker.py`, `src/navbat/dialog/fsm.py:407-416`, `src/navbat/dialog/replies.py` (Reply)
- Test: `tests/test_tg_api.py`, `tests/test_tg_worker.py`, `tests/test_dialog_contact.py:69-84`, `tests/test_dialog_menu.py`

- [ ] **Step 1: Написать падающие тесты**

1a. В `tests/test_tg_api.py` ЗАМЕНИТЬ `test_send_message_remove_keyboard` (строки 59–63) на:

```python
def test_send_message_builds_persistent_menu_keyboard():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Меню:", menu=(("📅 Записаться",),
                                         ("💰 Цены", "🌐 Til / Язык")))
    markup = json.loads(requests[0].content)["reply_markup"]
    assert markup["keyboard"] == [[{"text": "📅 Записаться"}],
                                  [{"text": "💰 Цены"}, {"text": "🌐 Til / Язык"}]]
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is True
```

1b. В `tests/test_tg_worker.py`: в `FakeAPI.send_message` заменить сигнатуру и журнал — `remove_keyboard` уходит, `menu` приходит. Было (строки 29–39): `keyboards.append((contact_request, remove_keyboard))`; стало:

```python
        self.keyboards: list[tuple] = []  # (contact_request, menu)
```

```python
    def send_message(self, chat_id, text, buttons=(),
                     contact_request=None, menu=None):
        ...
        self.keyboards.append((contact_request, menu))
```

(Многоточие — существующее тело метода, его не менять.) Тест на строках ~208–214 переписать:

```python
def test_menu_keyboard_passes_through_send_reply(app_session_factory, clinic_a):
    api = FakeAPI()
    menu = (("📅 Записаться",), ("💰 Цены",))
    send_reply(api, app_session_factory, clinic_a, 500,
               Reply("Меню:", menu=menu))
    assert api.keyboards[-1] == (None, menu), "menu дошёл до API"
```

(Имена фикстур/хелперов взять из фактического файла — исполнитель смотрит контекст вокруг строки 208.)

1c. В `tests/test_dialog_contact.py` переименовать и переписать `test_own_contact_books_and_removes_keyboard` (строки 69–84):

```python
def test_own_contact_books_and_restores_menu(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, booking_script())
    to_phone_step(engine)

    # Telegram отдаёт phone_number без плюса
    done = engine.handle_contact(CHAT, "998901234567", own=True)
    assert done.menu == menu_rows("ru"), \
        "контакт-клавиатура заменяется главным меню"
    assert "09:00" in done.text
    assert appt_status(admin_engine) == "booked"
    assert fsm_state(admin_engine) == "idle"
    with admin_engine.begin() as conn:
        linked = conn.execute(text(
            "SELECT p.id FROM appointment a JOIN patient p ON p.id = a.patient_id"
        )).one_or_none()
    assert linked is not None, "пациент создан и привязан к записи"
```

Импорт: добавить `menu_rows` в строку `from navbat.dialog.replies import TEMPLATES`.

- [ ] **Step 2: Убедиться, что падают**

Run: `python -m pytest tests/test_tg_api.py tests/test_tg_worker.py tests/test_dialog_contact.py -q`
Expected: FAIL (нет параметра `menu`, `done.menu is None`).

- [ ] **Step 3: Реализация**

3a. `api.py` — `send_message` (строки 49–69):

```python
    def send_message(self, chat_id: int, text: str,
                     buttons: Sequence[Button] = (),
                     contact_request: str | None = None,
                     menu: Sequence[Sequence[str]] | None = None) -> dict:
        params: dict = {"chat_id": chat_id, "text": text}
        if contact_request:
            # one_time_keyboard: клавиатура прячется после нажатия сама
            params["reply_markup"] = {
                "keyboard": [[{"text": contact_request, "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
        elif buttons:
            params["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b.label, "callback_data": b.action}] for b in buttons
                ]
            }
        elif menu:
            # постоянное главное меню; держится до следующей reply-клавиатуры
            params["reply_markup"] = {
                "keyboard": [[{"text": label} for label in row] for row in menu],
                "resize_keyboard": True,
                "is_persistent": True,
            }
        return self._call("sendMessage", **params)
```

3b. `worker.py` — `send_reply` (строка 210–212):

```python
    api.send_message(chat_id, reply.text, buttons,
                     contact_request=reply.contact_request,
                     menu=reply.menu)
```

3c. `fsm.py` `_process_contact` (строки 407–416) — заменить блок `if conv.state == "idle":`:

```python
        if conv.state == "idle":
            # привязка пациента к записи — после confirm: его транзакция
            # обновляет ту же строку, держать её под нашим локом нельзя
            session.execute(
                text("UPDATE appointment SET patient_id = :p WHERE id = :a"),
                {"p": patient_id, "a": appointment_id},
            )
            # запись подтверждена — контакт-клавиатуру заменяет главное меню
            reply = replace(reply, menu=menu_rows(lang))
        return reply
```

3d. `replies.py` — удалить поле `remove_keyboard` из `Reply` и упоминание из docstring.

3e. Проверить, что продюсеров/потребителей не осталось:

Run: `python -m pytest -q 2>&1 | Select-Object -Last 5` после `Grep remove_keyboard` по `src/` и `tests/` — вхождений быть не должно (docs не считаются).

- [ ] **Step 4: Тесты зелёные**

Run: `python -m pytest tests/test_tg_api.py tests/test_tg_worker.py tests/test_dialog_contact.py tests/test_dialog_menu.py -q`
Expected: PASS.

- [ ] **Step 5: Полный сьют**

Run: `python -m pytest -q`
Expected: зелёный.

- [ ] **Step 6: Commit**

```bash
git add src/navbat tests
git commit -m "feat(telegram): persistent menu keyboard; contact step restores menu instead of remove_keyboard"
```

---

### Task 6: Консольное демо рендерит меню

Консольная обвязка без тестов (демо не покрыто тестами — существующее решение проекта).

**Files:**
- Modify: `src/navbat/demo.py:58-72`

- [ ] **Step 1: Дополнить `render`**

После цикла печати кнопок (перед `if reply.contact_request:`) добавить:

```python
    if reply.menu:
        labels = " | ".join(label for row in reply.menu for label in row)
        print(f"  [меню — введите текст кнопки]: {labels}")
```

Нажатие кнопки меню в консоли = ввод её текста (идёт через `handle_text`, как в Telegram) — отдельной механики не нужно.

- [ ] **Step 2: Ручная проверка демо**

Run: `'﻿/start' | python -m navbat.demo` — нет; проще интерактивно НЕ гонять, а проверить выводом:

```powershell
"/start`n1`n/exit" | python -m navbat.demo
```

Expected: первый ответ — выбор языка (кнопки 1/2), после «1» — приветствие + строка `[меню — введите текст кнопки]: 📅 Yozilish | ...`. (Поднятый postgres обязателен; после pytest демо-клинику пересоздаёт сам `seed_demo_clinic`.)

- [ ] **Step 3: Commit**

```bash
git add src/navbat/demo.py
git commit -m "feat(demo): render main menu hint in console"
```

---

### Task 7: Серия прогонов, документация, push

- [ ] **Step 1: Сьют серией 8 раз (правило проекта: конкурентные тесты)**

```powershell
$fails = 0; foreach ($i in 1..8) { python -m pytest -q | Select-Object -Last 1; if (-not $?) { $fails++ } }; "FAILS: $fails"
```

Expected: `FAILS: 0`. Любой флак — стоп, диагностика до фикса (skill systematic-debugging), серию начать заново.

- [ ] **Step 2: Обновить CLAUDE.md проекта**

В раздел «Принятые решения (конвенции)» добавить строку:

```
- Вход кнопочный (button-first): /start → выбор языка → постоянное reply-меню
  (Записаться/Перенести/Отменить/Цены/Язык); label'ы меню перехватываются
  точным матчем ДО NLU. Свободный текст — LLM-fallback (фишка сохранена).
```

В раздел «Текущий этап» при необходимости — упоминание кнопочного входа в демо.

- [ ] **Step 3: Восстановить демо-клинику (pytest её TRUNCATE'ит)**

```powershell
python -m navbat.onboard --demo
```

Expected: `[OK]`-строки; токен/админ-чат подтягиваются из локального `.env`.

- [ ] **Step 4: Commit + push**

```bash
git add CLAUDE.md
git commit -m "docs(rules): button-first entry convention"
git push
```

---

## Self-review (выполнен при написании)

- Покрытие спеки: /start+язык (Task 2), меню book/resched/cancel + escalated-блок + abort hold (Task 3), прайс + смена языка + reprompt (Task 4), транспорт + contact-шаг + удаление remove_keyboard (Task 5), демо (Task 6), регресс-серия (Task 7). Краевые случаи спеки: текст-first без /start — покрыт существующими тестами (греетинг-ветка сохранена); «перенос/отмена без записи» — Task 3; «устаревшая клавиатура» — поведение не менялось.
- Отклонение от спеки (улучшение, согласовано логикой interrupt'ов): «💰 Цены» и «🌐 Язык» посреди сценария НЕ сбрасывают его, а работают как прерывание вбок (ответ + повтор шага) — сброс только у Записаться/Перенести/Отменить.
- Типы: `Reply.menu: tuple[tuple[str, ...], ...] | None` единообразно в fsm/worker/api (в api — `Sequence[Sequence[str]]`, шире, осознанно). `_on_menu` в Task 2 — временная заглушка, заменяется в Task 3.
