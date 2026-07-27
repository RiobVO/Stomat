"""Кому и под каким заголовком уходят алерты (карта, №9 и №10).

№9: через эскалационный рендер шли уведомления, эскалацией не являющиеся —
🟡 «нет слотов две недели» и откаты ручных правок календаря. Владелец
читал «Эскалация: чат N … Снять: /release N», хотя пациент не заморожен
(а при chat_id=0 подсказка вырождалась в «/release 0»), и это прямо
противоречит обещанию показа «эскалаций: 0».

№10: системный алерт (dead letter, cert, бэкапы) веером уходил во ВСЕ
админ-чаты клиники — на показе покупатель читает текст исключения в том
же чате, который у него на экране. Есть канал владельца системы
(NAVBAT_OWNER_CHAT_ID) — техника адресована ему.
"""
from __future__ import annotations

import pytest

from navbat.dialog.escalation import fyi_alert, system_alert
from navbat.telegram.escalation import TelegramEscalation
from test_tg_worker import FakeTelegramAPI

ADMIN_CHAT = 777
OWNER_CHAT = 999


class _Legacy:
    """Нотификатор без новых методов (фейки тестов, LoggingEscalation)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def notify(self, chat_id, reason, context) -> None:
        self.calls.append((chat_id, reason, context))


# ── №9: FYI это не эскалация ────────────────────────────────────────────────

def test_fyi_is_not_titled_escalation():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_fyi(100, "нет слотов на 2 недели вперёд", {})

    text = api.sent[0][1]
    assert "Эскалация" not in text, f"FYI не эскалация: {text}"
    assert "/release" not in text, "пациент не заморожен — снимать нечего"
    assert "нет слотов на 2 недели вперёд" in text


def test_fyi_keeps_booking_context_for_owner():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_fyi(100, "нет слотов", {"service": "cleaning"})

    assert "Чистка" in api.sent[0][1], "владельцу нужен предмет спроса"


def test_fyi_reaches_every_admin_chat():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_CHAT, 888])

    escalation.notify_fyi(100, "нет слотов", {})

    assert [chat for chat, _, _ in api.sent] == [ADMIN_CHAT, 888]


def test_fyi_falls_back_for_notifiers_without_the_method():
    """Фейки тестов и LoggingEscalation обязаны продолжать работать."""
    legacy = _Legacy()

    fyi_alert(legacy, 100, "нет слотов", {"service": "cleaning"})

    assert legacy.calls == [(100, "нет слотов", {"service": "cleaning"})]


# ── №10: техника — владельцу системы, не в чат клиники ──────────────────────

def test_system_alert_goes_to_owner_only(monkeypatch):
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(OWNER_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_system("апдейт 4711 в dead letter: KeyError", {})

    assert [chat for chat, _, _ in api.sent] == [OWNER_CHAT], \
        "клиника не должна читать трассировки при покупателе"


def test_system_alert_falls_back_to_clinic_without_owner(monkeypatch):
    """Без канала владельца алерт обязан дойти хоть куда-то — потерять
    «бэкапы не снимаются» страшнее, чем показать его клинике."""
    monkeypatch.delenv("NAVBAT_OWNER_CHAT_ID", raising=False)
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_system("бэкапы БД не снимаются", {})

    assert [chat for chat, _, _ in api.sent] == [ADMIN_CHAT]


@pytest.mark.parametrize("reason", ["дневной лимит токенов исчерпан",
                                    "TLS-cert истекает через 3 дн."])
def test_system_alert_still_delivered(monkeypatch, reason):
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(OWNER_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    system_alert(escalation, reason, {})

    assert len(api.sent) == 1 and reason in api.sent[0][1]
