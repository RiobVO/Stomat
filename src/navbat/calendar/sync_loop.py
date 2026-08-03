"""Цикл синхронизации календаря с алертом при затяжном сбое (M5).

Тихая смерть синка опасна: если протух Google refresh-token или Google
недоступен, бот перестаёт видеть ручные события врача и может записать
пациента на занятое время — а владелец об этом не узнает. N циклов подряд
со сбоем → эскалация админу; восстановление → отдельное уведомление.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from navbat.calendar.api import CalendarAuthError
from navbat.db.base import tenant_transaction
from navbat.dialog.escalation import system_alert
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
        self._auth_alert_date = None  # дата последнего auth-алерта (раз в день)

    def run_once(self) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            doctor_ids = list(session.execute(text(
                "SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL"
            )).scalars())
        failed = False
        auth_error: CalendarAuthError | None = None
        for doctor_id in doctor_ids:
            try:
                self._sync.sync_doctor(doctor_id)
            except CalendarAuthError as e:
                log.error("sync врача %s: OAuth мёртв: %s", doctor_id, e)
                failed = True
                auth_error = e
            except Exception:
                log.exception("sync врача %s упал", doctor_id)
                failed = True
        if auth_error is not None:
            self._alert_auth(auth_error)
        self._record(failed)

    def _alert_auth(self, error: CalendarAuthError) -> None:
        """Auth-сбой сам не чинится — алерт сразу, не после порога; раз в день."""
        today = datetime.now(timezone.utc).date()
        if self._auth_alert_date == today:
            return
        system_alert(
            self._notifier,
            f"Google OAuth-токен мёртв — синхронизация календаря остановилась "
            f"и сама не починится. Нужна переавторизация: "
            f"python -m navbat.calendar.auth. Ошибка: {str(error)[:200]}",
            {"error": str(error)[:200]},
            chat_id=self._admin_chat_id)
        self._auth_alert_date = today
        # генерический порог-алерт не дублируем; recovery-уведомление сработает
        self._alerted = True

    def _record(self, failed: bool) -> None:
        if failed:
            self._consecutive_failures += 1
            if (self._consecutive_failures >= FAILURE_ALERT_THRESHOLD
                    and not self._alerted):
                system_alert(
                    self._notifier,
                    f"синхронизация Google Calendar не работает "
                    f"{self._consecutive_failures} циклов подряд — проверьте "
                    f"доступ Google (возможно, протух токен). Пока синк стоит, "
                    f"бот может записать пациента на занятое врачом время.",
                    {"consecutive_failures": self._consecutive_failures},
                    chat_id=self._admin_chat_id)
                self._alerted = True
            return
        if self._alerted:
            system_alert(self._notifier,
                         "синхронизация Google Calendar восстановлена.", {},
                         chat_id=self._admin_chat_id)
        self._consecutive_failures = 0
        self._alerted = False
        self._auth_alert_date = None  # новый auth-сбой в тот же день — алертим снова
        self._stamp_last_sync()

    def _stamp_last_sync(self) -> None:
        """Успешный прогон = календарь жив; /health меряет возраст метки."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                "UPDATE clinic SET gcal_last_sync_at = now() "
                "WHERE id = current_setting('app.clinic_id')::uuid"))
