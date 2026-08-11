"""Scheduling engine — единственный хозяин занятости.

Разделение ответственности:
- код решает, ЧТО предлагать (график, праздники, буфер, живые hold);
- БД гарантирует, что НЕ случится (exclusion constraint отклоняет пересечения,
  включая буфер) — гонки разруливает constraint, не код.

Каждый публичный метод — одна транзакция с тенант-контекстом (RLS).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.scheduling.calendar_rules import day_intervals, slot_candidates
from navbat.scheduling.errors import (
    AppointmentNotFoundError,
    DuplicateMessageError,
    HoldExpiredError,
    InvalidSlotError,
    SlotTakenError,
)

HOLD_TTL = timedelta(minutes=3)
DEFAULT_STEP_MIN = 30
# окно выборки занятости вокруг дня: записи соседних дней с буфером не пересекают
# его границы дальше, чем на максимум длительности + буфера
BUSY_WINDOW_MARGIN = timedelta(hours=3)


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


class SchedulingEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        actor: str = "bot",
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._actor = actor

    # ── Чтение ───────────────────────────────────────────────────────────

    def find_free_slots(
        self,
        doctor_id: uuid.UUID,
        service_id: uuid.UUID,
        day: date,
        step_min: int = DEFAULT_STEP_MIN,
    ) -> list[Slot]:
        with self._txn() as session:
            return self._free_slots(session, doctor_id, service_id, day, step_min)

    # ── Запись ───────────────────────────────────────────────────────────

    def hold(
        self,
        doctor_id: uuid.UUID,
        service_id: uuid.UUID,
        start: datetime,
        patient_id: uuid.UUID | None = None,
        tg_chat_id: int | None = None,
        tg_message_id: int | None = None,
        source: str = "bot",
    ) -> uuid.UUID:
        with self._txn() as session:
            # сериализация записей по врачу: см. _lock_doctor
            self._lock_doctor(session, doctor_id)
            # протухшие hold физически блокируют constraint (now() в predicate
            # невозможен) — экспирим их перед вставкой в той же транзакции
            self._expire_stale_holds(session, doctor_id)

            slot = self._validated_slot(session, doctor_id, service_id, start)
            buffer_min = self._doctor_buffer(session, doctor_id)
            try:
                appt_id = session.execute(
                    text("""
                        INSERT INTO appointment
                            (clinic_id, doctor_id, service_id, patient_id, time_range,
                             buffer_min, status, source, hold_expires_at,
                             tg_chat_id, tg_message_id)
                        VALUES
                            (current_setting('app.clinic_id')::uuid, :doctor, :service,
                             :patient, tstzrange(:start, :end, '[)'), :buf, 'hold',
                             :source, now() + :ttl, :chat, :msg)
                        RETURNING id
                    """),
                    {
                        "doctor": doctor_id, "service": service_id, "patient": patient_id,
                        "start": slot.start, "end": slot.end, "buf": buffer_min,
                        "source": source, "ttl": HOLD_TTL,
                        "chat": tg_chat_id, "msg": tg_message_id,
                    },
                ).scalar_one()
            except IntegrityError as e:
                raise self._map_integrity(e) from e
            self._audit(session, appt_id, "hold", None,
                        {"status": "hold", "start": slot.start.isoformat()})
            return appt_id

    def confirm(self, appointment_id: uuid.UUID) -> None:
        with self._txn() as session:
            doctor_id = session.execute(
                text("SELECT doctor_id FROM appointment WHERE id = :id"),
                {"id": appointment_id},
            ).scalar_one_or_none()
            if doctor_id is None:
                raise AppointmentNotFoundError(str(appointment_id))
            # UPDATE hold->booked перепроверяет exclusion constraint (status в его
            # predicate) и дедлочится с конкурентными INSERT того же врача
            self._lock_doctor(session, doctor_id)
            updated = session.execute(
                text("UPDATE appointment SET status = 'booked', hold_expires_at = NULL "
                     "WHERE id = :id AND status = 'hold' AND hold_expires_at > now() "
                     "RETURNING id"),
                {"id": appointment_id},
            ).scalar_one_or_none()
            if updated is None:
                raise self._confirm_failure(session, appointment_id)
            self._audit(session, appointment_id, "confirm",
                        {"status": "hold"}, {"status": "booked"})

    def cancel(self, appointment_id: uuid.UUID, actor: str | None = None) -> None:
        # actor «reminder» отличает отмену из напоминания — топливо метрики
        # предотвращённых неявок (E.1); None → default-актор движка
        with self._txn() as session:
            row = session.execute(
                text("UPDATE appointment SET status = 'cancelled' "
                     "WHERE id = :id AND status IN ('hold', 'booked') "
                     "RETURNING status"),
                {"id": appointment_id},
            ).scalar_one_or_none()
            if row is None:
                raise AppointmentNotFoundError(str(appointment_id))
            self._audit(session, appointment_id, "cancel",
                        {"status": "active"}, {"status": "cancelled"},
                        actor=actor)

    def reschedule(self, appointment_id: uuid.UUID, new_start: datetime) -> None:
        with self._txn() as session:
            # advisory-лок врача ПЕРВЫМ (как hold/confirm) — единый порядок
            # захвата; row-lock (FOR UPDATE) берём уже под ним. Обратный порядок
            # (FOR UPDATE → advisory) дедлочится с UPDATE-в-predicate под advisory.
            doctor_id = session.execute(
                text("SELECT doctor_id FROM appointment "
                     "WHERE id = :id AND status IN ('hold', 'booked')"),
                {"id": appointment_id},
            ).scalar_one_or_none()
            if doctor_id is None:
                raise AppointmentNotFoundError(str(appointment_id))
            self._lock_doctor(session, doctor_id)
            current = session.execute(
                text("SELECT service_id, lower(time_range) AS lo FROM appointment "
                     "WHERE id = :id AND status IN ('hold', 'booked') FOR UPDATE"),
                {"id": appointment_id},
            ).one_or_none()
            if current is None:
                raise AppointmentNotFoundError(str(appointment_id))

            slot = self._validated_slot(
                session, doctor_id, current.service_id, new_start
            )
            try:
                session.execute(
                    text("UPDATE appointment SET time_range = tstzrange(:s, :e, '[)') "
                         "WHERE id = :id"),
                    {"s": slot.start, "e": slot.end, "id": appointment_id},
                )
                # exclusion проверяется на UPDATE; конфликт всплывает на flush
                session.flush()
            except IntegrityError as e:
                raise self._map_integrity(e) from e
            self._audit(session, appointment_id, "reschedule",
                        {"start": current.lo.isoformat()},
                        {"start": slot.start.isoformat()})

    # ── Внутреннее ───────────────────────────────────────────────────────

    def _txn(self):
        return tenant_transaction(self._session_factory, self._clinic_id)

    def _free_slots(
        self,
        session: Session,
        doctor_id: uuid.UUID,
        service_id: uuid.UUID,
        day: date,
        step_min: int,
    ) -> list[Slot]:
        candidates, buffer_min = self._grid(session, doctor_id, service_id, day, step_min)
        if not candidates:
            return []
        busy = session.execute(
            text("""
                SELECT lower(time_range) AS lo,
                       upper(time_range) + (buffer_min * interval '1 minute') AS up_buf
                FROM appointment
                WHERE doctor_id = :doctor
                  AND (status = 'booked'
                       OR (status = 'hold' AND hold_expires_at > now()))
                  AND time_range && tstzrange(:win_lo, :win_hi, '[)')
            """),
            {
                "doctor": doctor_id,
                "win_lo": candidates[0][0] - BUSY_WINDOW_MARGIN,
                "win_hi": candidates[-1][1] + BUSY_WINDOW_MARGIN,
            },
        ).all()
        buf = timedelta(minutes=buffer_min)
        return [
            Slot(start, end)
            for start, end in candidates
            # пересечение с учётом буфера с ОБЕИХ сторон (зеркало constraint)
            if not any(start < b.up_buf and b.lo < end + buf for b in busy)
        ]

    def _grid(
        self, session: Session, doctor_id, service_id, day: date, step_min: int
    ) -> tuple[list[tuple[datetime, datetime]], int]:
        """Сетка кандидатов по рабочему времени (без учёта занятости)."""
        doctor = session.execute(
            text("SELECT working_intervals, buffer_min FROM doctor WHERE id = :id"),
            {"id": doctor_id},
        ).one_or_none()
        duration_min = session.execute(
            text("SELECT duration_min FROM service WHERE id = :id"), {"id": service_id}
        ).scalar_one_or_none()
        if doctor is None or duration_min is None:
            return [], 0  # врач/услуга не существуют или чужие (RLS)

        tz = ZoneInfo(session.execute(
            text("SELECT timezone FROM clinic "
                 "WHERE id = current_setting('app.clinic_id')::uuid")
        ).scalar_one())
        holidays = set(session.execute(
            text("SELECT date FROM holiday WHERE date = :day"), {"day": day}
        ).scalars())

        intervals = day_intervals(doctor.working_intervals, day, tz, holidays)
        return slot_candidates(intervals, duration_min, step_min), doctor.buffer_min

    def _validated_slot(
        self, session: Session, doctor_id, service_id, start: datetime
    ) -> Slot:
        """Старт обязан лежать на сетке рабочего времени. Занятость НЕ проверяем —
        её атомарно решает exclusion constraint (иначе гонка между проверкой и вставкой)."""
        tz = ZoneInfo(session.execute(
            text("SELECT timezone FROM clinic "
                 "WHERE id = current_setting('app.clinic_id')::uuid")
        ).scalar_one())
        day = start.astimezone(tz).date()
        candidates, _ = self._grid(session, doctor_id, service_id, day, DEFAULT_STEP_MIN)
        for cand_start, cand_end in candidates:
            if cand_start == start:
                return Slot(cand_start, cand_end)
        raise InvalidSlotError(f"{start.isoformat()} вне рабочей сетки врача")

    def _lock_doctor(self, session: Session, doctor_id: uuid.UUID) -> None:
        """Advisory-лок на врача до конца транзакции.

        Убирает дедлок: конкурентные INSERT ждут внутри проверки exclusion
        constraint чужую транзакцию, а confirm/reschedule (UPDATE строк в
        predicate constraint) перепроверяют constraint навстречу — взаимное
        ожидание. Сериализация записей по врачу решает это структурно; масштаб
        клиники (1–4 кресла) её не почувствует. cancel лок не берёт: строка
        уходит из partial-индекса, перепроверки нет.
        """
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:d AS text), 0))"),
            {"d": doctor_id},
        )

    def _doctor_buffer(self, session: Session, doctor_id) -> int:
        return session.execute(
            text("SELECT buffer_min FROM doctor WHERE id = :id"), {"id": doctor_id}
        ).scalar_one()

    def _expire_stale_holds(self, session: Session, doctor_id) -> None:
        expired = session.execute(
            text("UPDATE appointment SET status = 'expired' "
                 "WHERE doctor_id = :doctor AND status = 'hold' "
                 "AND hold_expires_at <= now() RETURNING id"),
            {"doctor": doctor_id},
        ).scalars().all()
        for appt_id in expired:
            self._audit(session, appt_id, "expire",
                        {"status": "hold"}, {"status": "expired"}, actor="engine")

    def _confirm_failure(self, session: Session, appointment_id) -> Exception:
        """Разбор причины несработавшего confirm (строка уже под транзакцией)."""
        row = session.execute(
            text("SELECT status, hold_expires_at FROM appointment WHERE id = :id"),
            {"id": appointment_id},
        ).one_or_none()
        if row is None:
            return AppointmentNotFoundError(str(appointment_id))
        if row.status == "hold":
            return HoldExpiredError(str(appointment_id))
        return AppointmentNotFoundError(
            f"{appointment_id}: status={row.status}, подтверждать нечего"
        )

    def _map_integrity(self, error: IntegrityError) -> Exception:
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate == "23P01":  # exclusion_violation
            return SlotTakenError("слот занят")
        if sqlstate == "23505" and "tg_chat_id" in str(error.orig):
            return DuplicateMessageError("дубль tg-сообщения")
        return error

    def _audit(
        self, session: Session, appointment_id, action: str,
        before: dict | None, after: dict | None, actor: str | None = None,
    ) -> None:
        session.execute(
            text("INSERT INTO appointment_audit "
                 "(clinic_id, appointment_id, actor, action, before, after) "
                 "VALUES (current_setting('app.clinic_id')::uuid, :appt, :actor, "
                 ":action, CAST(:before AS jsonb), CAST(:after AS jsonb))"),
            {
                "appt": appointment_id, "actor": actor or self._actor, "action": action,
                "before": json.dumps(before) if before else None,
                "after": json.dumps(after) if after else None,
            },
        )
