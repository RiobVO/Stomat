"""C1: точка входа `navbat.telegram --real` обязана деидентифицировать
текст так же, как прод-путь супервизора — телефоны не уходят в OpenAI.

Регрессия: раньше build_extractor(--real) возвращал голый OpenAIExtractor
без DeidentifyingExtractor/бюджета (telegram/app.py).
"""
from __future__ import annotations

import navbat.nlu.openai_extractor as openai_mod
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram.app import build_dialog_extractor
from test_dialog_booking import RecordingNotifier, extr


class _RecordingPrimary:
    """Подмена OpenAIExtractor: пишет полученный текст, не ходит в сеть."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def extract(self, message: str):
        self.texts.append(message)
        return extr()


def test_real_path_masks_phone_before_llm(app_session_factory, clinic_a,
                                          monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # без каскада — один primary
    # OpenAIExtractor подменён ниже — реальный ключ не нужен; ставим dummy,
    # чтобы guard --real не звал sys.exit (иначе тест зелён лишь там, где
    # ключ есть в окружении — CI без ключа падал, C1-регрессия не при чём)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    primary = _RecordingPrimary()
    monkeypatch.setattr(openai_mod, "OpenAIExtractor", lambda **kw: primary)

    extractor = build_dialog_extractor(
        use_real=True, session_factory=app_session_factory,
        clinic_id=clinic_a, notifier=RecordingNotifier())
    extractor.extract("позвоните на +998 90 123-45-67")

    assert primary.texts, "primary должен получить вызов"
    assert "[phone]" in primary.texts[0]
    assert "123" not in primary.texts[0], "телефон ушёл в LLM открытым"


def test_fake_path_uses_fixtures(app_session_factory, clinic_a):
    from navbat.nlu.wrappers import GatedExtractor

    extractor = build_dialog_extractor(
        use_real=False, session_factory=app_session_factory,
        clinic_id=clinic_a, notifier=RecordingNotifier())
    # C-4: снаружи рубильник (/llm off действует и на фейковом пути),
    # внутри — фикстурный NLU без API-вызовов
    assert isinstance(extractor, GatedExtractor)
    assert isinstance(extractor._inner, FakeExtractor)
