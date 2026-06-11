"""Выбор модели OpenAI: NAVBAT_OPENAI_MODEL переопределяет дефолт.

Зачем: включение fine-tuned модели (ft:gpt-4o-mini-...:suffix:id) одной
переменной в .env без правки кода — и откат удалением переменной
(паттерн NAVBAT_GEMINI_MODEL). Сеть не дёргается: проверяется только
сконструированный self._model. openai — optional [llm], без неё скип.
"""
from __future__ import annotations

import pytest

pytest.importorskip("openai")

from navbat.nlu.openai_extractor import DEFAULT_MODEL, OpenAIExtractor

FT_ID = "ft:gpt-4o-mini-2024-07-18:acme:navbat-nlu-v1:abc123"


def test_default_model_without_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("NAVBAT_OPENAI_MODEL", raising=False)
    assert OpenAIExtractor()._model == DEFAULT_MODEL


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NAVBAT_OPENAI_MODEL", FT_ID)
    assert OpenAIExtractor()._model == FT_ID  # ft-id с двоеточиями целиком


def test_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("NAVBAT_OPENAI_MODEL", FT_ID)
    assert OpenAIExtractor(model="gpt-4o-mini")._model == "gpt-4o-mini"
