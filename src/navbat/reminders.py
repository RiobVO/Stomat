"""Напоминания пациентам: reconciliation из appointment (BRIEF, Notifier).

Никаких таймеров в памяти — каждое состояние выводится из БД и переживает
рестарт/деплой. Кнопки «Приду»/«Отменить» обрабатывает FSM (actions
attend:<id> / remind_cancel:<id>) — отмена из напоминания освобождает слот.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import (
    get_chat_lang,
    load_conversation,
    save_conversation,
)
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.replies import Button, Reply, service_label, t
from navbat.retention import cleanup_old_data
from navbat.stats import collect_daily_stats, render_stats, should_send_digest
from navbat.telegram.worker import send_reply

log = logging.getLogger("navbat.reminders")

DEFAULT_OFFSETS = (timedelta(hours=24), timedelta(hours=2))
MAX_ATTEMPTS = 3


def _kind(offset: timedelta) -> str:
    return f"{int(offset.total_seconds() // 60)}m"


class ReminderService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        tg_api=None,
        notifier: EscalationNotifier | None = None,
        offsets: tuple[timedelta, ...] = DEFAULT_OFFSETS,
        digest_chat_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._tg_api = tg_api
        self._notifier = notifier or LoggingEscalation()
        self._offsets = offsets
        self._digest_chat_id = digest_chat_id
        # retention: раз в календарный день; отметка в памяти — DELETE
        # идемпотентен, повтор после рестарта безвреден
        self._cleaned_on: date | None = None

    # ── Reconciliation: БД — единственный источник ───────────────────────

    def reconcile(self) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            for offset in self._offsets:
                # будущие booked-записи пациентов; send_at в прошлом не имеет
                # смысла (запись создана позже момента напоминания)
                session.execute(
                    text("""
                        INSERT INTO reminder (clinic_id, appointment_id, kind, send_at)
                        SELECT a.clinic_id, a.id, :kind, lower(a.time_range) - :offset
                        FROM appointment a
                        WHERE a.status = 'booked' AND a.tg_chat_id IS NOT NULL
                          AND a.source != 'gcal_import'
                          AND lower(a.time_range) - :offset > now()
                        ON CONFLICT (appointment_id, kind) DO UPDATE
                        SET send_at = EXCLUDED.send_at
                        WHERE reminder.status = 'pending'
                          AND reminder.send_at IS DISTINCT FROM EXCLUDED.send_at
                    """),
                    {"kind": _kind(offset), "offset": offset},
                )
            # запись отменили/перенесли в прошлое и т.п. — pending гасим
            session.execute(text("""
                UPDATE reminder r SET status = 'cancelled'
                FROM appointment a
                WHERE a.id = r.appointment_id
                  AND r.status = 'pending' AND a.status != 'booked'
            """))

    # ── Доставка ─────────────────────────────────────────────────────────

    def send_due(self) -> int:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            due = session.execute(
                text("""
                    SELECT r.id, r.appointment_id, r.attempts, a.tg_chat_id,
                           lower(a.time_range) AS start, s.name AS service,
                           c.timezone
                    FROM reminder r
                    JOIN appointment a ON a.id = r.appointment_id
                    JOIN clinic c ON c.id = r.clinic_id
                    LEFT JOIN service s ON s.id = a.service_id
                    WHERE r.status = 'pending' AND r.send_at <= now()
                    ORDER BY r.send_at
                """)
            ).all()
        sent = 0
        for row in due:
            if self._deliver(row):
                sent += 1
        return sent

    def _deliver(self, row) -> bool:
        from zoneinfo import ZoneInfo

        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = get_chat_lang(session, row.tg_chat_id)
            # строка conversation обязана существовать: send_reply кладёт
            # туда map кнопок (callback придёт спустя часы)
            save_conversation(session, load_conversation(session, row.tg_chat_id))
        local = row.start.astimezone(ZoneInfo(row.timezone))
        reply = Reply(
            t("reminder", lang, service=service_label(row.service or "checkup", lang),
              when=f"{local:%d.%m %H:%M}"),
            (Button(t("btn_attend", lang), f"attend:{row.appointment_id}"),
             Button(t("btn_remind_cancel", lang),
                    f"remind_cancel:{row.appointment_id}")),
        )
        try:
            if self._tg_api is None:
                raise RuntimeError("tg_api не задан")
            send_reply(self._tg_api, self._session_factory, self._clinic_id,
                       row.tg_chat_id, reply)
        except Exception as e:
            log.warning("напоминание %s: отправка не удалась (попытка %d): %s",
                        row.id, row.attempts + 1, e)
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                status = session.execute(
                    text("UPDATE reminder SET attempts = attempts + 1, "
                         "status = CASE WHEN attempts + 1 >= :max THEN 'failed' "
                         "ELSE status END WHERE id = :id RETURNING status"),
                    {"id": row.id, "max": MAX_ATTEMPTS},
                ).scalar_one()
            if status == "failed":
                self._notifier.notify(
                    row.tg_chat_id or 0,
                    f"напоминание о записи {row.appointment_id} не доставлено "
                    f"после {MAX_ATTEMPTS} попыток",
                    {"reminder": row.id})
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("UPDATE reminder SET status = 'sent', sent_at = now() "
                     "WHERE id = :id"),
                {"id": row.id},
            )
        return True

    # ── Вечерняя сводка админу ───────────────────────────────────────────

    def maybe_send_digest(self, now_local=None) -> bool:
        """Раз в день после DIGEST_HOUR; отметка — clinic.last_digest_date."""
        if self._digest_chat_id is None or self._tg_api is None:
            return False
        from datetime import datetime
        from zoneinfo import ZoneInfo

        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(text(
                "SELECT timezone, last_digest_date FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).one()
        tz = ZoneInfo(row.timezone)
        moment = now_local or datetime.now(tz)
        if not should_send_digest(moment, row.last_digest_date):
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            stats = collect_daily_stats(session, moment.date(), tz)
        try:
            self._tg_api.send_message(self._digest_chat_id,
                                      render_stats(stats, moment.date()))
        except Exception as e:
            log.warning("вечерняя сводка не доставлена: %s", e)
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("UPDATE clinic SET last_digest_date = :day "
                     "WHERE id = current_setting('app.clinic_id')::uuid"),
                {"day": moment.date()},
            )
        return True

    def maybe_cleanup(self) -> bool:
        """Retention-чистка раз в календарный день (D.3)."""
        today = date.today()
        if self._cleaned_on == today:
            return False
        self._cleaned_on = today
        cleanup_old_data(self._session_factory, self._clinic_id)
        return True

    def run(self, stop: threading.Event, interval: float = 30.0) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.reconcile()
                self.send_due()
                self.maybe_send_digest()
                self.maybe_cleanup()
            except Exception:
                log.exception("цикл напоминаний упал — продолжаю")
            elapsed = time.monotonic() - started
            stop.wait(max(0.0, interval - elapsed))
