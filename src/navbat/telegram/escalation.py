"""Эскалация человеку через Telegram-чат админа клиники (P0 BRIEF).

Реализует EscalationNotifier (dialog/escalation.py). Сбой доставки не
роняет обработку пациента: эскалация — сигнал, не транзакция.
"""
from __future__ import annotations

import json
import logging

from navbat.telegram.api import TelegramAPIError

log = logging.getLogger("navbat.escalation")


class TelegramEscalation:
    def __init__(self, api, admin_chat_id: int | None) -> None:
        self._api = api
        self._admin_chat_id = admin_chat_id

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        if self._admin_chat_id is None:
            log.warning("эскалация chat=%s (admin_chat_id не задан): %s | %s",
                        chat_id, reason, context)
            return
        message = (f"Эскалация: чат {chat_id}\n"
                   f"Причина: {reason}\n"
                   f"Контекст: {json.dumps(context, ensure_ascii=False)}\n"
                   f"Снять: /release {chat_id}")
        try:
            self._api.send_message(self._admin_chat_id, message)
        except TelegramAPIError as e:
            log.error("эскалация chat=%s не доставлена админу: %s | %s",
                      chat_id, e, reason)
