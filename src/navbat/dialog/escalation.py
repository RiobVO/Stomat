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
