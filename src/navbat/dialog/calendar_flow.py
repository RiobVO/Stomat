"""Сценарий выбора даты списком (_CalendarFlowMixin, П-5/П-5б).

Общий для записи и переноса: кнопки слотов в day-view — штатные
slot:/reslot:, дальше работает существующий путь hold→confirm. Сообщение
с датами живёт долго — устаревшие клики валидируются (toast/перерисовка),
не падают. Вынесен mixin'ом по R4-структуре; хелперы и роутер — через self.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy.orm import Session

from navbat.dialog.calendar_view import (
    DAYS_PER_PAGE, HORIZON_DAYS, dates_view)
from navbat.dialog.conversation import Conversation
from navbat.dialog.replies import Button, Reply, t

SLOTS_PER_DAY_ROW = 4      # сетка времени в day-view
SLOTS_PER_DAY_ROW_MULTI = 2  # с именем врача ячейки шире


class _CalendarFlowMixin:
    def _on_calendar(self, session: Session, conv: Conversation,
                     rest: str) -> Reply:
        """Диспетчер cal:-callback'ов."""
        lang = self._lang(conv)
        kind, _, value = rest.partition(":")
        if kind == "noop":
            return Reply("")  # заглушки старых сообщений: молча
        if kind == "none":
            return Reply("", toast=t("cal_no_slots", lang))
        if kind == "nav":
            start = self._parse_nav(session, value)
            if start is None:
                return self._with_reprompt(session, conv,
                                           Reply(t("stale_button", lang)))
            return self._dates_reply(session, conv, start, edit=True)
        if kind == "day":
            return self._on_calendar_day(session, conv, value)
        return self._with_reprompt(session, conv, Reply(t("stale_button", lang)))

    def _parse_nav(self, session: Session, value: str) -> date | None:
        """Старт страницы дат. ISO-дата; legacy YYYY-MM из старых сообщений
        (месячная сетка П-5) → 1 число месяца. Вне [today, today+90] —
        первая страница; мусор — None (stale)."""
        try:
            start = date.fromisoformat(value if len(value) > 7 else f"{value}-01")
        except ValueError:
            return None
        today = self._today(session)
        if not today <= start <= today + timedelta(days=HORIZON_DAYS):
            return today
        return start

    def _dates_reply(self, session: Session, conv: Conversation,
                     start: date, edit: bool) -> Reply:
        if not conv.context.resched_id:
            conv.state = "booking_collect"
        self._ensure_service(session, conv)
        days, has_more = self._available_days_from(session, conv, start)
        if not days:
            # в горизонте пусто: честный текст без мёртвых кнопок
            return Reply(t("no_slots_horizon", self._lang(conv)), edit=edit)
        caption, rows = dates_view(days, start, self._today(session),
                                   has_more, self._lang(conv))
        return Reply(caption, button_rows=rows, edit=edit)

    def _available_days_from(self, session: Session, conv: Conversation,
                             start: date) -> tuple[list[date], bool]:
        """Первые DAYS_PER_PAGE доступных дней от start (скан с ранним
        выходом, горизонт HORIZON_DAYS от сегодня) + есть ли дальше."""
        ctx = conv.context
        service_id = self._service_id(session, ctx.service) if ctx.service else None
        if service_id is None:
            return [], False
        doctor_filter = ctx.resched_doctor if ctx.resched_id else ctx.doctor_id
        doctors = self._doctors(session, doctor_filter)
        today = self._today(session)
        now = self._clock()
        last_day = today + timedelta(days=HORIZON_DAYS)
        days: list[date] = []
        day = max(start, today)
        while day <= last_day:
            for doctor_id, _name in doctors:
                slots = self._sched.find_free_slots(doctor_id, service_id, day)
                if any(slot.start > now for slot in slots):
                    if len(days) == DAYS_PER_PAGE:
                        return days, True  # нашёлся 11-й — будет «Ещё даты»
                    days.append(day)
                    break
            day += timedelta(days=1)
        return days, False

    def _on_calendar_day(self, session: Session, conv: Conversation,
                         value: str) -> Reply:
        lang = self._lang(conv)
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        today = self._today(session)
        if day < today:
            # клик в старое сообщение: честный toast + свежая первая страница
            reply = self._dates_reply(session, conv, today, edit=True)
            return replace(reply, toast=t("cal_past_day", lang))
        conv.context.date = day.isoformat()
        return self._calendar_day_reply(session, conv, day)

    def _calendar_day_reply(self, session: Session, conv: Conversation,
                            day: date) -> Reply:
        """Слоты выбранного дня тем же сообщением: день выбран явно —
        показываем ВСЕ слоты сеткой, не SLOTS_PER_REPLY."""
        ctx = conv.context
        lang = self._lang(conv)
        self._ensure_service(session, conv)
        service_id = self._service_id(session, ctx.service) if ctx.service else None
        resched = bool(ctx.resched_id)
        doctors = self._doctors(session,
                                ctx.resched_doctor if resched else ctx.doctor_id)
        now = self._clock()
        found = []
        if service_id is not None:
            for doctor_id, doctor_name in doctors:
                for slot in self._sched.find_free_slots(doctor_id, service_id, day):
                    if slot.start > now:
                        found.append((slot.start, doctor_id, doctor_name))
        if not found:
            # день опустел, пока пациент думал: toast + свежий список дат
            reply = self._dates_reply(session, conv, day, edit=True)
            return replace(reply, toast=t("cal_no_slots", lang))
        found.sort(key=lambda item: (item[0], str(item[1])))

        tz = self._clinic_tz(session)
        multi = len(doctors) > 1
        buttons = []
        for start, doctor_id, doctor_name in found:
            label = f"{start.astimezone(tz):%H:%M}"
            if multi and doctor_name:
                label += f" · {doctor_name}"
            action = (f"reslot:{start.isoformat()}" if resched
                      else f"slot:{doctor_id}:{start.isoformat()}")
            buttons.append(Button(label, action))
        per_row = SLOTS_PER_DAY_ROW_MULTI if multi else SLOTS_PER_DAY_ROW
        rows = [tuple(buttons[i:i + per_row])
                for i in range(0, len(buttons), per_row)]
        rows.append((Button(t("btn_back_calendar", lang),
                            f"cal:nav:{day.isoformat()}"),))
        conv.state = "resched_offer_slots" if resched else "booking_offer_slots"
        return Reply(t("offer_slots", lang, date=f"{day:%d.%m}"),
                     button_rows=tuple(rows), edit=True)

    def _ensure_service(self, session: Session, conv: Conversation) -> None:
        """Без услуги сетка считается по осмотру — тот же дефолт, что в
        book-бэкстопе; перенос услугу не трогает."""
        if not conv.context.resched_id and not conv.context.service \
                and self._service_id(session, "checkup") is not None:
            conv.context.service = "checkup"

    def _no_slots_calendar(self, session: Session, conv: Conversation,
                           reason: str) -> Reply:
        """Пустые 2 недели: пациенту — список дальних доступных дней (или
        честное «времени нет»), владельцу — FYI раз в день (чаще это
        незаведённый график, чем спрос). Дедуп в памяти процесса: после
        рестарта повторится — для FYI ок."""
        today = self._today(session)
        if self._no_slots_fyi_date != today:
            self._no_slots_fyi_date = today
            self._notifier.notify(conv.chat_id, reason,
                                  self._escalation_context(conv))
        reply = self._dates_reply(session, conv, today, edit=False)
        if reply.button_rows:
            return replace(reply, text=(f"{t('no_slots_calendar', self._lang(conv))}"
                                        f"\n\n{reply.text}"))
        return reply