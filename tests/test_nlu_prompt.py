"""Версионированный NLU-промпт в БД: upload, per-clinic pin, сборка (Ф1.5 B.2).

Staging-процедура: upload → pin на демо-клинику → перезапуск демо-бота →
тест-диалоги → pin живым. Без пина (NULL) экстракторы живут на встроенном
файле — текущее поведение не меняется.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from navbat.nlu.gemini_extractor import GeminiExtractor
from navbat.onboard import pin_prompt, show_clinic, upload_prompt


def prompt_version_of(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT nlu_prompt_version FROM clinic WHERE id = :c"),
            {"c": clinic_id}).scalar_one()


# ── Загрузка и пин ───────────────────────────────────────────────────────────

def test_upload_creates_incrementing_versions(app_session_factory, admin_engine):
    v1 = upload_prompt(app_session_factory, "Промпт раз", note="первый")
    v2 = upload_prompt(app_session_factory, "Промпт два")
    assert v2 == v1 + 1

    with admin_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT body, note FROM nlu_prompt ORDER BY version")).all()
    assert [(r.body, r.note) for r in rows] == [("Промпт раз", "первый"),
                                                ("Промпт два", None)]


def test_pin_and_unpin(app_session_factory, admin_engine, clinic_a):
    version = upload_prompt(app_session_factory, "тело")
    pin_prompt(app_session_factory, clinic_a, str(version))
    assert prompt_version_of(admin_engine, clinic_a) == version

    pin_prompt(app_session_factory, clinic_a, "file")  # откат на встроенный
    assert prompt_version_of(admin_engine, clinic_a) is None


def test_pin_unknown_version_fails(app_session_factory, clinic_a):
    with pytest.raises(SystemExit):
        pin_prompt(app_session_factory, clinic_a, "999")


def test_show_clinic_prints_prompt_version(app_session_factory, admin_engine,
                                           clinic_a, capsys):
    version = upload_prompt(app_session_factory, "тело")
    pin_prompt(app_session_factory, clinic_a, str(version))
    show_clinic(app_session_factory, clinic_a)
    assert f"NLU-промпт: версия {version}" in capsys.readouterr().out


# ── Экстракторы и боевая сборка ──────────────────────────────────────────────

def test_gemini_constructor_takes_prompt():
    assert GeminiExtractor(api_key="k", prompt="X")._system_prompt == "X"


def test_openai_constructor_takes_prompt(monkeypatch):
    pytest.importorskip("openai")
    from navbat.nlu.openai_extractor import OpenAIExtractor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert OpenAIExtractor(prompt="X")._system_prompt == "X"


def test_build_uses_pinned_prompt(monkeypatch, app_session_factory, clinic_a):
    pytest.importorskip("openai")
    from navbat.supervisor import build_real_extractor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    version = upload_prompt(app_session_factory, "Промпт из БД")
    pin_prompt(app_session_factory, clinic_a, str(version))

    chain = build_real_extractor(app_session_factory, clinic_a, notifier=None)
    fallback = chain._inner._inner._inner  # Budgeted→Drift→Deidentifying→Fallback
    assert fallback._primary._system_prompt == "Промпт из БД"
    assert fallback._secondary._system_prompt == "Промпт из БД"


def test_build_without_pin_uses_builtin_file(monkeypatch, app_session_factory,
                                             clinic_a):
    pytest.importorskip("openai")
    from navbat.nlu.openai_extractor import _PROMPT_PATH
    from navbat.supervisor import build_real_extractor

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    chain = build_real_extractor(app_session_factory, clinic_a, notifier=None)
    extractor = chain._inner._inner._inner
    assert extractor._system_prompt == _PROMPT_PATH.read_text(encoding="utf-8")
