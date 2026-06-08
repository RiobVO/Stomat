"""Типизированное состояние диалога (DialogContext) — round-trip JSONB,
очистка сценария, фильтр PII для эскалации (R2)."""
from __future__ import annotations

from navbat.dialog.conversation import DialogContext


def test_empty_context_serializes_to_empty_dict():
    # пустой контекст должен давать {} как прежний словарь (миграция/storage)
    assert DialogContext().to_dict() == {}


def test_to_dict_omits_defaults_keeps_set_fields():
    ctx = DialogContext(lang="uz", service="cleaning", nlu_failures=2)
    assert ctx.to_dict() == {"lang": "uz", "service": "cleaning",
                             "nlu_failures": 2}


def test_roundtrip_preserves_known_and_adapter_keys():
    raw = {"lang": "ru", "service": "implant", "greeting_shown": True,
           "tg_actions": {"1": "reslot:..."}}  # tg_actions — ключ адаптера
    ctx = DialogContext.from_dict(raw)
    assert ctx.lang == "ru"
    assert ctx.service == "implant"
    assert ctx.greeting_shown is True
    assert ctx.extras == {"tg_actions": {"1": "reslot:..."}}
    # round-trip не теряет adapter-ключи (иначе reslot-поток сломается)
    assert ctx.to_dict() == raw


def test_from_dict_tolerates_none_and_unknown():
    assert DialogContext.from_dict(None).to_dict() == {}
    ctx = DialogContext.from_dict({"unknown_future_key": 7})
    assert ctx.extras == {"unknown_future_key": 7}


def test_clear_booking_resets_scenario_keeps_session_and_extras():
    ctx = DialogContext(
        lang="uz", greeting_shown=True, nlu_failures=1,
        service="crown", date="2026-03-21", appointment_id="a1",
        cancel_id="c1", pending_name="Алишер",
        extras={"tg_actions": {"1": "x"}},
    )
    ctx.clear_booking()
    # сценарные поля сброшены
    assert ctx.service is None
    assert ctx.date is None
    assert ctx.appointment_id is None
    assert ctx.cancel_id is None
    assert ctx.pending_name is None
    # сессия и adapter-ключи сохранены
    assert ctx.lang == "uz"
    assert ctx.greeting_shown is True
    assert ctx.nlu_failures == 1
    assert ctx.extras == {"tg_actions": {"1": "x"}}


def test_escalation_dict_drops_pii_only():
    ctx = DialogContext(lang="ru", service="xray", pending_name="Гульнора")
    data = ctx.escalation_dict()
    assert "pending_name" not in data
    assert "Гульнора" not in str(data)
    assert data["service"] == "xray"
    assert data["lang"] == "ru"
