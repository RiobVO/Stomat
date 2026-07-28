"""Единый каталог услуг: schema.SERVICE_KEYS — единственный источник
канонических ключей. Остальные места обязаны совпадать с ним, иначе
«добавить услугу» молча рассинхронит NLU/метки/онбординг (R3)."""
from __future__ import annotations

from navbat.dialog.replies import SERVICE_LABELS
from navbat.nlu.gemini_extractor import _RESPONSE_SCHEMA
from navbat.nlu.schema import SERVICE_KEYS
from navbat.onboard import SERVICES


def test_gemini_service_enum_matches_canonical():
    enum = _RESPONSE_SCHEMA["properties"]["service"]["enum"]
    assert list(enum) == list(SERVICE_KEYS)


def test_service_labels_cover_exactly_canonical():
    assert set(SERVICE_LABELS) == set(SERVICE_KEYS)


def test_demo_services_cover_exactly_canonical():
    assert set(SERVICES) == set(SERVICE_KEYS)


def test_every_label_has_both_languages():
    assert all({"ru", "uz"} <= set(SERVICE_LABELS[key]) for key in SERVICE_KEYS)
