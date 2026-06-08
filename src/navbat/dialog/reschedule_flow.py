"""Сценарий переноса (_RescheduleFlowMixin): найти активную запись,
предложить новые слоты у того же врача, выполнить reschedule. Вынесен из
god-object DialogEngine (R4); общие хелперы — через self."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from navbat.dialog.conversation import Conversation
from navbat.dialog.dialog_common import SLOTS_PER_REPLY
from navbat.dialog.replies import Button, Reply, t
from navbat.nlu.schema import Extraction
from navbat.scheduling.errors import (
    AppointmentNotFoundError,
    InvalidSlotError,
    SlotTakenError,
)


class _RescheduleFlowMixin:
    def _start_reschedule(self, session: Session, conv: Conversation,
                          extraction: Extraction) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        appointment = self._find_active_appointment(session, conv.chat_id)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        ctx.resched_id = str(appointment.id)
        ctx.resched_doctor = str(appointment.doctor_id)
        # услугу не переспрашиваем — переносим ту же; перенос остаётся
        # у того же врача (engine.reschedule врача не меняет)
        ctx.service = self._service_name(session, appointment.service_id) or "checkup"
        self._merge_when(session, ctx, extraction)
        conv.state = "resched_offer_slots"
        if not ctx.date:
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        return self._offer_resched_slots(session, conv)

    def _offer_resched_slots(self, session: Session, conv: Conversation,
                             note: str | None = None) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx.service)
        doctors = self._doctors(session, ctx.resched_doctor)
        tz = self._clinic_tz(session)
        asked = date.fromisoformat(ctx.date)
        day, slots = self._collect_slots(session, doctors, service_id, asked,
                                         ctx.time_ref)
        if not slots:
            self._notifier.notify(conv.chat_id,
                                  "перенос: нет слотов на 2 недели вперёд",
                                  self._escalation_context(conv))
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("no_slots_at_all", lang))
        buttons = [
            Button(self._slot_label(start, doctor_name, tz, multi_doctor=False),
                   f"reslot:{start.isoformat()}")
            for start, _doctor_id, doctor_name in slots[:SLOTS_PER_REPLY]
        ]
        buttons.append(Button(t("btn_other_time", lang), "ask_date"))
        prefix = t(note, lang) + "\n" if note else ""
        ctx.date = day.isoformat()
        conv.state = "resched_offer_slots"
        return Reply(prefix + self._offer_body(session, lang, asked, day),
                     tuple(buttons))

    def _on_reslot(self, session: Session, conv: Conversation, start_iso: str) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        try:
            self._sched.reschedule(uuid.UUID(ctx.resched_id),
                                   datetime.fromisoformat(start_iso))
        except (SlotTakenError, InvalidSlotError):
            return self._offer_resched_slots(session, conv, note="slot_taken")
        except AppointmentNotFoundError:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        local = datetime.fromisoformat(start_iso).astimezone(self._clinic_tz(session))
        self._clear_booking(conv)
        conv.state = "idle"
        return Reply(t("resched_done", lang, when=f"{local:%d.%m %H:%M}"))
