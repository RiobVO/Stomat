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

from navbat.dialog.escalation import fyi_alert, ops_alert, system_alert
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


# ── Ревью волны B, блокер 2: операционные сигналы обязаны дойти клинике ─────

def test_ops_alert_reaches_clinic_and_owner(monkeypatch):
    """«Синк не работает», «напоминание не доставлено», «лимит исчерпан» —
    это работа администратора клиники, а не только диагностика платформы."""
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(OWNER_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_ops("синхронизация Google Calendar не работает", {})

    assert sorted(chat for chat, _, _ in api.sent) == sorted([ADMIN_CHAT,
                                                              OWNER_CHAT])


def test_ops_alert_hides_technical_detail_from_clinic(monkeypatch):
    """Причина — клинике человеческим языком, трассировка — владельцу
    (карта, №10: покупатель не должен читать текст исключения)."""
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(OWNER_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_ops("сообщение пациента не обработано", {},
                          detail="KeyError: 'message'")

    to_clinic = next(t for chat, t, _ in api.sent if chat == ADMIN_CHAT)
    to_owner = next(t for chat, t, _ in api.sent if chat == OWNER_CHAT)
    assert "KeyError" not in to_clinic, to_clinic
    assert "сообщение пациента не обработано" in to_clinic
    assert "KeyError" in to_owner


def test_ops_alert_falls_back_for_legacy_notifiers():
    legacy = _Legacy()

    ops_alert(legacy, "синк умер", {}, chat_id=7)

    assert legacy.calls == [(7, "синк умер", {})]


def test_infrastructure_alerts_do_not_reach_clinic(monkeypatch):
    """Сертификат, бэкапы, webhook и NLU-дрифт клинике бесполезны."""
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(OWNER_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)

    escalation.notify_system("TLS-cert истекает через 3 дн.", {})

    assert [chat for chat, _, _ in api.sent] == [OWNER_CHAT]


def test_owner_in_admin_chats_still_gets_detail(monkeypatch):
    """Владелец системы часто и есть админ-чат клиники (пилот на одном
    аккаунте). Раньше он попадал в ветку клиники и терял detail — при
    dead letter это ровно та строка, по которой чинят (ре-ревью, дефект 1)."""
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", str(ADMIN_CHAT))
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_CHAT, 888])

    escalation.notify_ops("сообщение пациента не обработано", {},
                          detail="KeyError: 'message'")

    to_owner = next(t for chat, t, _ in api.sent if chat == ADMIN_CHAT)
    to_other = next(t for chat, t, _ in api.sent if chat == 888)
    assert "KeyError" in to_owner, "владельцу нужна техническая часть"
    assert "KeyError" not in to_other, "второй админ-чат — без трассировки"
    assert len([1 for chat, _, _ in api.sent if chat == ADMIN_CHAT]) == 1, \
        "дубля сообщения быть не должно"
