"""Эскалация человеку — интерфейс. Реальная доставка в TG-чат админа — инкремент 3."""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("navbat.escalation")


class EscalationNotifier(Protocol):
    def notify(self, chat_id: int, reason: str, context: dict) -> None: ...


class LoggingEscalation:
    """Заглушка: фиксирует эскалацию в лог, пока нет канала до админа."""

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        log.warning("эскалация chat=%s: %s | контекст: %s", chat_id, reason, context)


def fyi_alert(notifier, chat_id: int, reason: str, context: dict) -> None:
    """🟡 FYI владельцу: событие произошло, человек не нужен и пациент НЕ
    заморожен (нет слотов две недели, откат ручной правки календаря).

    Отдельный канал от notify(): эскалационная шапка со «Снять: /release»
    вводила владельца в заблуждение и противоречила «эскалаций: 0» в /stats
    (карта продажи, №9). Нотификаторы без notify_fyi (фейки, LoggingEscalation)
    получают обычный notify — сигнал не теряется."""
    handler = getattr(notifier, "notify_fyi", None)
    if handler is not None:
        handler(chat_id, reason, context)
    else:
        notifier.notify(chat_id, reason, context)


def ops_alert(notifier, reason: str, context: dict, chat_id: int = 0,
              detail: str | None = None) -> None:
    """Операционный сигнал КЛИНИКЕ (и владельцу системы заодно): бот
    что-то не смог, и с этим должен работать администратор — напоминание
    не доставлено, синк календаря стоит, дневной лимит токенов исчерпан,
    сообщение пациента не обработано.

    Отличается от system_alert адресатом: инфраструктура (cert, бэкапы,
    webhook, NLU-дрифт) клинике бесполезна, а эти четыре — её работа
    (ревью волны B, блокер 2). detail — техническая часть (текст
    исключения): уходит только владельцу, клиника читает причину
    человеческим языком (карта продажи, №10)."""
    handler = getattr(notifier, "notify_ops", None)
    if handler is not None:
        handler(reason, context, detail=detail)
    else:
        # у фолбэка (логи, фейки тестов) адресата разделять не на кого —
        # техническая часть не должна потеряться при диагностике
        notifier.notify(chat_id, f"{reason} ({detail})" if detail else reason,
                        context)


def system_alert(notifier, reason: str, context: dict, chat_id: int = 0) -> None:
    """Системный алерт (не пациентская эскалация): cert, синк, cap, дрифт,
    dead-letter. TelegramEscalation шлёт его и владельцу системы; нотификаторы
    без notify_system (фейки, LoggingEscalation) получают обычный notify."""
    handler = getattr(notifier, "notify_system", None)
    if handler is not None:
        handler(reason, context)
    else:
        notifier.notify(chat_id, reason, context)
