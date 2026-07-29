"""Сценарий отмены (_CancelFlowMixin): найти запись (по чату или по id из
напоминания), подтвердить и отменить. Вынесен из god-object DialogEngine
(R4); общие хелперы — через self."""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from navbat.dialog import appointments_repo
from navbat.dialog.conversation import Conversation
from navbat.dialog.replies import Button, Reply, t
from navbat.scheduling.errors import AppointmentNotFoundError


class _CancelFlowMixin:
    def _start_cancel(self, session: Session, conv: Conversation) -> Reply:
        return self._begin_cancel(
            session, conv, self._find_active_appointment(session, conv.chat_id))

    def _start_cancel_by_id(self, session: Session, conv: Conversation,
                            appointment_id: str) -> Reply:
        """Кнопка «Отменить» из напоминания: запись известна по id."""
        appointment = appointments_repo.active_by_id(session, appointment_id)
        reply = self._begin_cancel(session, conv, appointment)
        if conv.context.cancel_id:
            # источник отмены — напоминание: метрика предотвращённых неявок
            conv.context.cancel_via = "reminder"
        return reply

    def _begin_cancel(self, session: Session, conv: Conversation,
                      appointment) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("cancel_none", lang))
        local = appointment.start.astimezone(self._clinic_tz(session))
        ctx.cancel_id = str(appointment.id)
        ctx.cancel_when = f"{local:%d.%m %H:%M}"
        conv.state = "cancel_confirm"
        return self._cancel_prompt(conv)

    def _cancel_prompt(self, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        return Reply(
            t("cancel_confirm_q", lang, when=conv.context.cancel_when),
            (Button(t("btn_yes", lang), "cancel_yes"),
             Button(t("btn_no", lang), "cancel_no")),
        )

    def _on_cancel_confirmed(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        cancel_id = conv.context.cancel_id
        cancel_via = conv.context.cancel_via  # до _clear_booking
        self._clear_booking(conv)
        conv.state = "idle"
        try:
            self._sched.cancel(uuid.UUID(cancel_id), actor=cancel_via)
        except AppointmentNotFoundError:
            return Reply(t("cancel_none", lang))
        return Reply(t("cancel_done", lang))
