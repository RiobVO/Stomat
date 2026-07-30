"""Цикл синхронизации календаря с алертом при затяжном сбое (M5).

Тихая смерть синка опасна: если протух Google refresh-token или Google
недоступен, бот перестаёт видеть ручные события врача и может записать
пациента на занятое время — а владелец об этом не узнает. N циклов подряд
со сбоем → эскалация админу; восстановление → отдельное уведомление.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.telegram.escalation import _as_chat_tuple

log = logging.getLogger("navbat.calendar.sync_loop")

FAILURE_ALERT_THRESHOLD = 3  # циклов подряд со сбоем до эскалации админу


class CalendarSyncLoop:
    """Один прогон sync по всем врачам с календарём + учёт затяжных сбоев.

    Вынесен из инлайн-замыкания супервизора, чтобы поведение алерта было
    тестируемо без запуска всего процесса."""

    def __init__(self, session_factory, clinic_id, sync, notifier,
                 admin_chat_id=None) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._sync = sync
        self._notifier = notifier
        # системный алерт без пациента: notifier сам веером шлёт всем админам,
        # здесь нужен лишь «чат» для строки контекста — берём первый или 0 (M4)
        chats = _as_chat_tuple(admin_chat_id)
        self._admin_chat_id = chats[0] if chats else 0
        self._consecutive_failures = 0
        self._alerted = False

    def run_once(self) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            doctor_ids = list(session.execute(text(
                "SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL"
            )).scalars())
        failed = False
        for doctor_id in doctor_ids:
            try:
                self._sync.sync_doctor(doctor_id)
            except Exception:
                log.exception("sync врача %s упал", doctor_id)
                failed = True
        self._record(failed)

    def _record(self, failed: bool) -> None:
        if failed:
            self._consecutive_failures += 1
            if (self._consecutive_failures >= FAILURE_ALERT_THRESHOLD
                    and not self._alerted):
                self._notifier.notify(
                    self._admin_chat_id,
                    f"синхронизация Google Calendar не работает "
                    f"{self._consecutive_failures} циклов подряд — проверьте "
                    f"доступ Google (возможно, протух токен). Пока синк стоит, "
                    f"бот может записать пациента на занятое врачом время.",
                    {"consecutive_failures": self._consecutive_failures})
                self._alerted = True
            return
        if self._alerted:
            self._notifier.notify(
                self._admin_chat_id,
                "синхронизация Google Calendar восстановлена.", {})
        self._consecutive_failures = 0
        self._alerted = False
