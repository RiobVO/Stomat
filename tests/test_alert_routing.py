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


def test_alert_frame_uses_admin_chat_language(app_session_factory, admin_engine,
                                              clinic_a):
    """Каркас алерта («Эскалация», «Причина», «Снять») уходил только
    по-русски — узбекский владелец видел русский экран ровно там, где
    бот его зовёт (ревью исправлений волны C)."""
    from navbat.db.base import tenant_transaction
    from navbat.dialog.conversation import load_conversation, save_conversation

    admin_chat = 909
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = load_conversation(session, admin_chat)
        conv.context.extras["adm_lang"] = "uz"
        save_conversation(session, conv)

    api = FakeTelegramAPI()
    escalation = TelegramEscalation(
        api, admin_chat_id=admin_chat,
        lang_of=lambda chat: _admin_lang(app_session_factory, clinic_a, chat))
    escalation.notify(555, "bemor odam so'radi", {})
    body = api.sent[-1][1]
    assert "Эскалация" not in body and "Причина" not in body, body
    assert "/release 555" in body, "подсказка снятия обязана остаться"

    # каркас переведён во ВСЕХ каналах, а не только в эскалации
    escalation.notify_fyi(555, "vaqt yo'q", {})
    assert "К сведению" not in api.sent[-1][1], api.sent[-1][1]
    escalation.notify_ops("kalendar sinxronizatsiyasi to'xtadi", {})
    assert "Системный алерт" not in api.sent[-1][1], api.sent[-1][1]
    escalation.notify_system("zaxira nusxa olinmadi", {})
    assert "Системный алерт" not in api.sent[-1][1], api.sent[-1][1]


def test_reason_speaks_the_language_of_each_admin_chat(app_session_factory,
                                                      clinic_a):
    """Причина ВНУТРИ алерта — тоже текст для владельца.

    Каркас перевели волной C, а причину служебные пути отдавали готовой русской
    строкой: узбекский админ-чат получал половину экрана на своём языке, а
    вторую — на чужом (остаток по №16). Разные админ-чаты одной клиники могут
    держать разные языки, поэтому переводить можно только при отправке."""
    from navbat.dialog.conversation import load_conversation, save_conversation
    from navbat.db.base import tenant_transaction
    from navbat.telegram.admin_texts import Reason

    ru_chat, uz_chat = 4242, 4343
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = load_conversation(session, uz_chat)
        conv.context.extras["adm_lang"] = "uz"
        save_conversation(session, conv)

    api = FakeTelegramAPI()
    escalation = TelegramEscalation(
        api, admin_chat_id=[ru_chat, uz_chat],
        lang_of=lambda chat: _admin_lang(app_session_factory, clinic_a, chat))

    escalation.notify_ops(Reason("reason_sync_restored"), {})
    sent = {chat: text for chat, text, _ in api.sent}
    assert "восстановлена" in sent[ru_chat], sent[ru_chat]
    assert "tiklandi" in sent[uz_chat], sent[uz_chat]

    # с подстановками и на другом канале: FYI о пустом горизонте
    escalation.notify_fyi(555, Reason("reason_reminder_failed", chat=555,
                                      attempts=3), {})
    sent = {chat: text for chat, text, _ in api.sent[-2:]}
    assert "не доставлено" in sent[ru_chat], sent[ru_chat]
    assert "yetib bormadi" in sent[uz_chat], sent[uz_chat]

    # системные причины остаются строкой: их читает тот, кто чинит
    escalation.notify_system("бэкапы БД не снимаются", {})
    assert "бэкапы БД не снимаются" in api.sent[-1][1]


def test_escalation_alert_is_translated_too(app_session_factory, clinic_a):
    """Тот же перевод в канале 🔴 эскалации, вместе с выжимкой брони.

    Проверяется отдельно от ops/FYI: возврат `reason=reason` именно в notify()
    оставил бы остальные тесты зелёными, а узбекский владелец снова читал бы
    русский экран там, где бот его зовёт (ревью)."""
    from navbat.dialog.conversation import load_conversation, save_conversation
    from navbat.db.base import tenant_transaction
    from navbat.telegram.admin_texts import Reason

    ru_chat, uz_chat = 5252, 5353
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = load_conversation(session, uz_chat)
        conv.context.extras["adm_lang"] = "uz"
        save_conversation(session, conv)

    api = FakeTelegramAPI()
    escalation = TelegramEscalation(
        api, admin_chat_id=[ru_chat, uz_chat],
        lang_of=lambda chat: _admin_lang(app_session_factory, clinic_a, chat))

    escalation.notify(555, Reason("reason_wants_human"),
                      {"service": "cleaning", "date": "2026-08-01"})

    sent = {chat: text for chat, text, _ in api.sent}
    assert "просит администратора" in sent[ru_chat], sent[ru_chat]
    assert "услуга — Чистка" in sent[ru_chat], sent[ru_chat]
    assert "gaplashmoqchi" in sent[uz_chat], sent[uz_chat]
    assert "xizmat — Tish tozalash" in sent[uz_chat], sent[uz_chat]


def test_notifier_factory_always_knows_the_language(app_session_factory,
                                                    admin_engine, clinic_a):
    """Нотификатор собирается фабрикой, потому что забыть резолвер языка легко:
    standalone-синк (`python -m navbat.calendar`) собирал его сам и рассылал
    причины по-русски даже узбекскому владельцу (ревью)."""
    from navbat.dialog.conversation import load_conversation, save_conversation
    from navbat.db.base import tenant_transaction
    from navbat.telegram.admin_texts import Reason
    from navbat.telegram.escalation import build_escalation

    uz_chat = 6464
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = load_conversation(session, uz_chat)
        conv.context.extras["adm_lang"] = "uz"
        save_conversation(session, conv)

    api = FakeTelegramAPI()
    escalation = build_escalation(api, app_session_factory, clinic_a, [uz_chat])
    escalation.notify_ops(Reason("reason_sync_restored"), {})

    assert "tiklandi" in api.sent[-1][1], api.sent[-1][1]


def test_broken_reason_template_does_not_swallow_the_alert(caplog):
    """Сигнал важнее перевода.

    Причина рендерится в момент отправки, поэтому опечатка в шаблоне (а
    узбекские строки ещё будет править носитель) роняла рассылку: ни текущий
    получатель, ни следующие не узнавали, что синк стоит. Ошибка обязана
    остаться в логах, а алерт — дойти хотя бы по-русски (ревью)."""
    from navbat.telegram import admin_texts

    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_CHAT, 888])

    # опечатка в ключе и в имени подстановки — обе из служебного пути
    escalation.notify_ops(admin_texts.Reason("reason_sync_stuk", cycles=3), {})
    assert len(api.sent) == 2, "алерт должен уйти всем админ-чатам"
    escalation.notify_ops(admin_texts.Reason("reason_sync_stuck", cycle=3), {})
    assert len(api.sent) == 4, api.sent

    # параметров не передали вовсе: подстановка не должна уехать владельцу
    # буквальным «{cycles}» — это не сообщение, а мусор
    escalation.notify_ops(admin_texts.Reason("reason_sync_stuck"), {})
    assert "{cycles}" not in api.sent[-1][1], api.sent[-1][1]

    # шаблон повреждён (такое внесёт вычитка узбекского): сигнал обязан дойти,
    # причём без сырых скобок в тексте и с ошибкой в логе — иначе поломка
    # перевода останется незамеченной
    broken = dict(admin_texts.TEMPLATES["reason_sync_restored"])
    broken["ru"] = "{cycles[typo]}"
    original = admin_texts.TEMPLATES["reason_sync_restored"]
    admin_texts.TEMPLATES["reason_sync_restored"] = broken
    with caplog.at_level("ERROR", logger="navbat.admin_texts"):
        try:
            escalation.notify_ops(admin_texts.Reason("reason_sync_restored"), {})
        finally:
            admin_texts.TEMPLATES["reason_sync_restored"] = original

    assert len(api.sent) == 8, "повреждённый шаблон не должен гасить сигнал"
    assert "{" not in api.sent[-1][1], api.sent[-1][1]
    assert any("reason_sync_restored" in record.message
               for record in caplog.records), caplog.records


def _admin_lang(session_factory, clinic_id, chat_id: int) -> str:
    from navbat.db.base import tenant_transaction
    from navbat.dialog.conversation import load_conversation
    with tenant_transaction(session_factory, clinic_id) as session:
        conv = load_conversation(session, chat_id)
    return conv.context.extras.get("adm_lang", "ru")
