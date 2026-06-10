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
    assert "приостановлена" in api.sent[0][1]
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
