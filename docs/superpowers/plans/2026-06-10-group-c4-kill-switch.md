# C-4 Kill-switch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Рубильники без убийства процесса: пауза бота клиники (/pause), выключение LLM (/llm off — кнопки работают), глобальный env-рубильник; процедуры — в docs/OPERATIONS.md. Четвёртый инкремент по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md`.

**Architecture:** Два независимых флага в clinic: `bot_paused` (гейт в воркере ДО диалога: пациент получает вежливое сообщение, админ-команды продолжают работать — иначе /resume невозможен) и `llm_enabled` (гейт в NLU-цепочке: `GatedExtractor` бросает `LLMDisabledError` ДО вызова LLM; FSM на него отвечает меню БЕЗ инкремента счётчика сбоев — это режим, не сбой; кнопочные пути NLU не дёргают и работают полностью). Глобальный рубильник — env `NAVBAT_LLM_DISABLED=1` проверяется тем же GatedExtractor первым. Обёртка ставится в `build_dialog_extractor` (единая точка для fake и real путей).

**Tech Stack:** alembic, stdlib. Ноль новых зависимостей.

**Контекст кодовой базы:**
- `src/navbat/dialog/fsm.py:231-234` — `_process_text`: `ExtractionError` → `_on_nlu_failure` (2 подряд → escalated). Сюда встаёт ветка `except LLMDisabledError` ПЕРЕД `except ExtractionError` (наследник!).
- `src/navbat/dialog/shared_helpers.py:24-28` — `_try_extract` глотает ExtractionError → None: LLMDisabledError там автоматически мягкий.
- `src/navbat/nlu/extractor.py` — здесь живёт базовый `ExtractionError`; `LLMDisabledError` кладём рядом (fsm не должен зависеть от wrappers).
- `src/navbat/nlu/wrappers.py` — паттерн обёрток (BudgetedExtractor); `GatedExtractor` сюда.
- `src/navbat/telegram/app.py:65-81` — `build_dialog_extractor` — единая точка сборки NLU для supervisor и канала; принимает session_factory и clinic_id — есть всё для обёртки.
- `src/navbat/telegram/worker.py:109-170` — `_handle`: админ-команды матчятся по `message["text"].split()[:1]` + `chat_id in self._admin_chat_ids`; паттерн ответов `_release_reply` и пр.
- `src/navbat/dialog/replies.py` — TEMPLATES["reask"] (строка 118) — паттерн ключей ru/uz; ASCII-апостроф в uz намеренно (test_replies_uz).
- `tests/test_tg_worker.py` — `make_worker(app_session_factory, clinic, script, admin_chat_id=...)`, `put_message`, `put_callback`, `FakeTelegramAPI.sent/keyboards`.
- Миграции: голова 0013.

---

### Task 1: миграция 0014 + LLMDisabledError + GatedExtractor + мягкий путь FSM

**Files:**
- Create: `migrations/versions/0014_kill_switch.py`, `tests/test_kill_switch.py`
- Modify: `src/navbat/nlu/extractor.py`, `src/navbat/nlu/wrappers.py`, `src/navbat/dialog/fsm.py`, `src/navbat/dialog/replies.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_kill_switch.py`:

```python
"""Kill-switch (C-4): /pause, /llm off, глобальный env-рубильник.

LLM-off — режим, не сбой: меню работает, счётчик сбоев не растёт,
эскалации нет. Пауза — гейт воркера: пациент получает вежливый ответ,
админ-команды продолжают работать.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor, LLMDisabledError
from navbat.nlu.wrappers import GatedExtractor
from test_dialog_booking import CHAT, RecordingNotifier, extr
from test_tg_worker import FakeTelegramAPI, make_worker, put_message

ADMIN = 900


def _flag(admin_engine, clinic_id, column):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {column} FROM clinic WHERE id = :c"),
            {"c": clinic_id}).scalar_one()


# ── GatedExtractor ───────────────────────────────────────────────────────────

def test_gate_passes_when_enabled(app_session_factory, clinic_a):
    gated = GatedExtractor(FakeExtractor(script=[extr("book")]),
                           app_session_factory, clinic_a)
    assert gated.extract("запишите меня").intent == "book"


def test_gate_blocks_when_clinic_llm_disabled(app_session_factory, admin_engine,
                                              clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET llm_enabled = false WHERE id = :c"),
                     {"c": clinic_a})
    gated = GatedExtractor(FakeExtractor(script=[extr("book")]),
                           app_session_factory, clinic_a)
    with pytest.raises(LLMDisabledError):
        gated.extract("запишите меня")


def test_gate_blocks_globally_via_env(app_session_factory, clinic_a, monkeypatch):
    monkeypatch.setenv("NAVBAT_LLM_DISABLED", "1")
    gated = GatedExtractor(FakeExtractor(script=[extr("book")]),
                           app_session_factory, clinic_a)
    with pytest.raises(LLMDisabledError):
        gated.extract("запишите меня")


# ── FSM: LLM-off — меню без эскалации ───────────────────────────────────────

class _DisabledExtractor:
    def extract(self, message):
        raise LLMDisabledError("LLM выключен")


def test_llm_off_free_text_gets_menu_without_failure_count(app_session_factory,
                                                           admin_engine, clinic_a):
    notifier = RecordingNotifier()
    dialog = DialogEngine(app_session_factory, clinic_a,
                          extractor=_DisabledExtractor(), notifier=notifier)
    for _ in range(3):  # больше MAX_NLU_FAILURES — эскалации быть не должно
        reply = dialog.handle_text(CHAT, "хочу на чистку завтра")
    assert reply.menu  # кнопки самообслуживания в ответе
    assert notifier.calls == []  # не эскалировали
    with admin_engine.begin() as conn:
        state = conn.execute(text(
            "SELECT fsm_state FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).scalar_one()
    assert state != "escalated"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_kill_switch.py -q` → FAIL (нет LLMDisabledError / колонок)

- [ ] **Step 3: Migration `0014_kill_switch.py`**:

```python
"""C-4: kill-switch — пауза бота и выключение LLM на клинику.

bot_paused: воркер отвечает пациентам «временно по телефону», команды
админа работают. llm_enabled=false: кнопочные сценарии живут, свободный
текст уходит в меню без вызова LLM и без эскалаций.

Revision ID: 0014
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN bot_paused boolean "
               "NOT NULL DEFAULT false")
    op.execute("ALTER TABLE clinic ADD COLUMN llm_enabled boolean "
               "NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS bot_paused")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS llm_enabled")
```

- [ ] **Step 4: Код.**

`nlu/extractor.py` — рядом с ExtractionError:

```python
class LLMDisabledError(ExtractionError):
    """LLM выключен рубильником (/llm off или NAVBAT_LLM_DISABLED) —
    это режим, не сбой: FSM отвечает меню без счётчика сбоев."""
```

`nlu/wrappers.py` — конец файла (импорт LLMDisabledError добавить к импорту из extractor):

```python
class GatedExtractor:
    """Рубильник LLM (C-4): env NAVBAT_LLM_DISABLED=1 (глобально) или
    clinic.llm_enabled=false (/llm off) → LLMDisabledError, вызова нет."""

    def __init__(self, inner: Extractor, session_factory, clinic_id) -> None:
        self._inner = inner
        self._session_factory = session_factory
        self._clinic_id = clinic_id

    def extract(self, message: str) -> Extraction:
        if os.environ.get("NAVBAT_LLM_DISABLED"):
            raise LLMDisabledError("LLM выключен глобально (NAVBAT_LLM_DISABLED)")
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            enabled = session.execute(text(
                "SELECT llm_enabled FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).scalar_one()
        if not enabled:
            raise LLMDisabledError("LLM выключен для клиники (/llm on — включить)")
        return self._inner.extract(message)
```

`dialog/fsm.py` — импорт `LLMDisabledError` из nlu.extractor; в `_process_text`:

```python
        try:
            extraction = self._extractor.extract(message)
        except LLMDisabledError:
            # рубильник /llm off: свободный текст без NLU — мягко в кнопки,
            # счётчик сбоев не трогаем (это режим, не сбой)
            return Reply(t("llm_off_menu", lang), menu=menu_rows(lang))
        except ExtractionError:
            return self._on_nlu_failure(conv)
```

`dialog/replies.py` — после "reask":

```python
    "llm_off_menu": {
        "ru": "Сейчас запись принимается через кнопки меню — выберите "
              "нужное действие.",
        "uz": "Hozir yozilish menyu tugmalari orqali qabul qilinadi — "
              "kerakli amalni tanlang.",
    },
```

- [ ] **Step 5: Run** `python -m pytest tests/test_kill_switch.py tests/test_replies_uz.py -q` → PASS

- [ ] **Step 6: Commit** `feat(ops): llm kill-switch - GatedExtractor + soft menu path`

---

### Task 2: пауза бота — гейт воркера + строки

**Files:**
- Modify: `src/navbat/telegram/worker.py`, `src/navbat/dialog/replies.py`
- Test: `tests/test_kill_switch.py`

- [ ] **Step 1: Write the failing tests** (в конец test_kill_switch.py):

```python
# ── Пауза бота: гейт воркера ─────────────────────────────────────────────────

def _pause(admin_engine, clinic_id, value=True):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET bot_paused = :v WHERE id = :c"),
                     {"v": value, "c": clinic_id})


def test_paused_bot_replies_politely_without_dialog(app_session_factory,
                                                    admin_engine, clinic_a):
    _pause(admin_engine, clinic_a)
    worker, api, notifier = make_worker(app_session_factory, clinic_a, script=[])
    put_message(app_session_factory, clinic_a, "хочу записаться")
    assert worker.process_one() is True
    assert len(api.sent) == 1
    assert "приостановлен" in api.sent[0][1]
    assert notifier.calls == []  # пауза не плодит эскалаций


def test_paused_bot_still_serves_admin_commands(app_session_factory,
                                                admin_engine, clinic_a):
    _pause(admin_engine, clinic_a)
    worker, api, _ = make_worker(app_session_factory, clinic_a, script=[],
                                 admin_chat_id=ADMIN)
    put_message(app_session_factory, clinic_a, "/stats", chat_id=ADMIN)
    worker.process_one()
    assert len(api.sent) == 1
    assert "Сводка" in api.sent[0][1]
```

- [ ] **Step 2: Run** → FAIL (пациент получает обычный диалог)

- [ ] **Step 3: Код.** `worker.py` — в `_handle`, в ветке `"message"` ПОСЛЕ блока админ-команд (все `if ... in self._admin_chat_ids` идут первыми) и в ветке `callback_query`/`contact` ДО обработки — добавить гейт. Конкретно: вынести проверку в метод:

```python
    def _bot_paused(self) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(text(
                "SELECT bot_paused FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).scalar_one()

    def _paused_reply(self, session_lang_chat: int) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = get_chat_lang(session, session_lang_chat)
        return Reply(t("bot_paused", lang))
```

Вставки (3 места, после ветки contact — у contact тоже):
- в text-ветке после пяти админ-`if` и ДО `_rate_verdict`:

```python
                if self._bot_paused():
                    self._send(chat_id, self._paused_reply(chat_id))
                    return
```

- в contact-ветке первой строкой (до handle_contact_hashed) — тот же блок;
- в callback-ветке после `answer_callback_query` вместо вызова диалога:

```python
            if self._bot_paused():
                self._api.answer_callback_query(callback["id"])
                self._send(chat_id, self._paused_reply(chat_id))
                return
```

(в callback-ветке гейт ставится ПЕРЕД `self._dialog.handle_action`.)

`replies.py` — после "llm_off_menu":

```python
    "bot_paused": {
        "ru": "Запись через бота временно приостановлена. Позвоните в клинику "
              "или загляните позже.",
        "uz": "Bot orqali yozilish vaqtincha to'xtatildi. Klinikaga qo'ng'iroq "
              "qiling yoki keyinroq urinib ko'ring.",
    },
```

- [ ] **Step 4: Run** `python -m pytest tests/test_kill_switch.py tests/test_tg_worker.py tests/test_replies_uz.py -q` → PASS

- [ ] **Step 5: Commit** `feat(ops): bot pause gate in worker - polite reply, admin commands alive`

---

### Task 3: админ-команды /pause /resume /llm

**Files:**
- Modify: `src/navbat/telegram/worker.py`
- Test: `tests/test_kill_switch.py`

- [ ] **Step 1: Write the failing tests**:

```python
# ── Админ-команды рубильников ────────────────────────────────────────────────

def test_pause_and_resume_commands(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, script=[],
                                 admin_chat_id=ADMIN)
    put_message(app_session_factory, clinic_a, "/pause ремонт кабинета",
                chat_id=ADMIN)
    worker.process_one()
    assert _flag(admin_engine, clinic_a, "bot_paused") is True
    assert "[OK]" in api.sent[-1][1] and "ремонт кабинета" in api.sent[-1][1]

    put_message(app_session_factory, clinic_a, "/resume", chat_id=ADMIN)
    worker.process_one()
    assert _flag(admin_engine, clinic_a, "bot_paused") is False


def test_llm_toggle_commands(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, script=[],
                                 admin_chat_id=ADMIN)
    put_message(app_session_factory, clinic_a, "/llm off", chat_id=ADMIN)
    worker.process_one()
    assert _flag(admin_engine, clinic_a, "llm_enabled") is False

    put_message(app_session_factory, clinic_a, "/llm on", chat_id=ADMIN)
    worker.process_one()
    assert _flag(admin_engine, clinic_a, "llm_enabled") is True

    put_message(app_session_factory, clinic_a, "/llm", chat_id=ADMIN)
    worker.process_one()
    assert "Формат" in api.sent[-1][1]  # подсказка формата


def test_pause_requires_admin(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a,
                                 script=[extr("other")], admin_chat_id=ADMIN)
    put_message(app_session_factory, clinic_a, "/pause")  # пациентский чат
    worker.process_one()
    assert _flag(admin_engine, clinic_a, "bot_paused") is False  # не сработала
```

- [ ] **Step 2: Run** → FAIL

- [ ] **Step 3: Код** — worker._handle, после `/forget`-блока, тем же паттерном:

```python
                if (message["text"].split()[:1] == ["/pause"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._pause_reply(message["text"]))
                    return
                if (message["text"].strip() == "/resume"
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._resume_reply())
                    return
                if (message["text"].split()[:1] == ["/llm"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._llm_reply(message["text"]))
                    return
```

и методы (рядом с _release_reply):

```python
    def _set_clinic_flag(self, column: str, value: bool) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                f"UPDATE clinic SET {column} = :v "
                f"WHERE id = current_setting('app.clinic_id')::uuid"),
                {"v": value})

    def _pause_reply(self, command: str) -> Reply:
        """Пауза бота: /pause [причина] (C-4). Пациенты получают вежливое
        сообщение, напоминания продолжают ходить, /resume — обратно."""
        reason = command.partition(" ")[2].strip()
        self._set_clinic_flag("bot_paused", True)
        suffix = f" ({reason})" if reason else ""
        return Reply(f"[OK] бот на паузе{suffix}. Пациентам отвечаем "
                     f"«запись временно по телефону». Вернуть: /resume")

    def _resume_reply(self) -> Reply:
        self._set_clinic_flag("bot_paused", False)
        return Reply("[OK] бот снова принимает запись")

    def _llm_reply(self, command: str) -> Reply:
        """Рубильник NLU: /llm off — кнопки работают, свободный текст → меню."""
        arg = command.split()[1:2]
        if arg == ["off"]:
            self._set_clinic_flag("llm_enabled", False)
            return Reply("[OK] LLM выключен: кнопки работают, свободный "
                         "текст уходит в меню. Вернуть: /llm on")
        if arg == ["on"]:
            self._set_clinic_flag("llm_enabled", True)
            return Reply("[OK] LLM включён")
        return Reply("Формат: /llm on | /llm off")
```

- [ ] **Step 4: Run** `python -m pytest tests/test_kill_switch.py -q` → PASS (все 9)

- [ ] **Step 5: Commit** `feat(ops): /pause /resume /llm admin commands`

---

### Task 4: проводка GatedExtractor + OPERATIONS.md + env

**Files:**
- Modify: `src/navbat/telegram/app.py` (build_dialog_extractor), `deploy/.env.example`
- Create: `docs/OPERATIONS.md`

- [ ] **Step 1: Проводка** — `build_dialog_extractor` оборачивает ОБА пути (fake и real):

```python
def build_dialog_extractor(use_real: bool, session_factory, clinic_id, notifier):
    """... (докстринг дополнить строкой про GatedExtractor: рубильник C-4
    действует и на фейковом пути — поведение /llm off одинаково везде)"""
    from navbat.nlu.wrappers import GatedExtractor

    if use_real:
        ...
        return GatedExtractor(build_real_extractor(...), session_factory, clinic_id)
    ...
    return GatedExtractor(extractor, session_factory, clinic_id)
```

(точная вставка: обернуть оба return; лог-строки не трогать).

- [ ] **Step 2: `docs/OPERATIONS.md`** — раздел «Рубильники» (полный текст в файле): /pause-/resume (что видит пациент, что продолжает работать — напоминания), /llm on|off (мягкий режим), глобальный LLM-рубильник (`NAVBAT_LLM_DISABLED=1` в deploy/.env + `docker compose -f docker-compose.prod.yml restart app`), полная остановка (`docker compose ... stop app` — очередь durable, апдейты Telegram доживут до старта), и таблица «симптом → действие». Плюс заглушка-раздел «Restore из бэкапа» со ссылкой «появится в C-5».

- [ ] **Step 3: `.env.example`** — в секцию владельца добавить:

```sh
# Глобальный рубильник LLM (1 = выключен; рестарт app обязателен)
# NAVBAT_LLM_DISABLED=
```

- [ ] **Step 4: Run** `python -m pytest tests/test_kill_switch.py tests/test_tg_app.py tests/test_telegram_real_path.py -q` → PASS

- [ ] **Step 5: Commit** `feat(ops): wire LLM gate into extractor build + OPERATIONS runbook`

---

### Task 5: финал

- [ ] **Step 1:** `python -m pytest -q` → `588 passed` (579 + 4 Task1 + 2 Task2 + 3 Task3... пересчёт: 4+2+4(=test_llm_toggle*3 ассерта — один тест) — фактически тестов: 4+2+3 = 9 → 588).
- [ ] **Step 2:** `python -m navbat.onboard --demo`; `python -m navbat --check` → [OK].
- [ ] **Step 3:** отметить чекбоксы плана; `git push`.

## Definition of Done (C-4)

- [ ] 9 новых тестов зелёные; полный сьют зелёный; uz-строки прошли test_replies_uz.
- [ ] /pause не плодит эскалаций; админ-команды живы на паузе; /llm off не ведёт в escalated.
- [ ] OPERATIONS.md покрывает все три рубильника; демо восстановлено; всё в origin.
