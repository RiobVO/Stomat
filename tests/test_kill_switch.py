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
