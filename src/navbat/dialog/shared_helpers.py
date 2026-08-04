"""Общие хелперы диалога (_SharedHelpersMixin): поиск слотов, доступ к
клинике/врачам/услугам/датам, язык и очистка контекста. Вынесены из
god-object DialogEngine (R4); используются всеми сценариями через self —
DialogEngine наследует этот mixin вместе со сценарными."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from navbat.dialog import appointments_repo, clinic_repo, doctors_repo, services_repo
from navbat.dialog.conversation import Conversation, DialogContext
from navbat.dialog.dates import exact_time_ref, matches_time_ref
from navbat.dialog.dialog_common import NEAREST_DAY_SCAN
from navbat.dialog.replies import Button, service_label, t
from navbat.nlu.extractor import ExtractionError
from navbat.nlu.schema import Extraction
from navbat.scheduling.calendar_rules import open_bounds


class _SharedHelpersMixin:
    def _try_extract(self, message: str) -> Extraction | None:
        try:
            return self._extractor.extract(message)
        except ExtractionError:
            return None

    def _lang(self, conv: Conversation) -> str:
        return conv.context.lang or "ru"

    def _clear_booking(self, conv: Conversation) -> None:
        conv.context.clear_booking()

    def _escalation_context(self, conv: Conversation) -> dict:
        """Контекст для эскалации без PII пациента (имя) — он уходит
        в админ-чат и логи (m1)."""
        return conv.context.escalation_dict()

    def _clinic_name(self, session: Session) -> str:
        return clinic_repo.clinic_name(session)

    def _clinic_tz(self, session: Session) -> ZoneInfo:
        if self._tz is None:
            self._tz = ZoneInfo(clinic_repo.clinic_timezone(session))
        return self._tz

    def _today(self, session: Session) -> date:
        return self._clock().astimezone(self._clinic_tz(session)).date()

    def _guard_allows(self, session: Session, appointment_id: uuid.UUID) -> bool:
        row = appointments_repo.slot_bounds(session, appointment_id)
        return self._slot_guard.is_free(row.doctor_id, row.start, row.finish)

    def _find_active_appointment(self, session: Session, chat_id: int):
        """Ближайшая будущая активная запись чата (для переноса/отмены)."""
        return appointments_repo.active_by_chat(session, chat_id)

    def _service_name(self, session: Session, service_id) -> str | None:
        if service_id is None:
            return None
        return services_repo.service_name(session, service_id)

    def _service_id(self, session: Session, service_key: str) -> uuid.UUID | None:
        return services_repo.service_id(session, service_key)

    def _service_buttons(self, session: Session, lang: str) -> tuple[Button, ...]:
        names = services_repo.service_keys(session)
        return tuple(Button(service_label(n, lang), f"service:{n}") for n in names)

    def _date_buttons(self, session: Session, lang: str) -> tuple[Button, ...]:
        today = self._today(session)
        return (
            Button(t("btn_today", lang), f"date:{today.isoformat()}"),
            Button(t("btn_tomorrow", lang), f"date:{(today + timedelta(days=1)).isoformat()}"),
            Button(t("btn_after_tomorrow", lang),
                   f"date:{(today + timedelta(days=2)).isoformat()}"),
        )

    def _doctors(self, session: Session,
                 only_id: str | None = None) -> list[tuple[uuid.UUID, str | None]]:
        doctors = doctors_repo.doctor_list(session)
        if only_id:
            doctors = [d for d in doctors if str(d[0]) == only_id]
        return doctors

    def _resolve_doctor(self, session: Session, ctx: DialogContext,
                        name: str) -> None:
        target = name.casefold()
        for doctor_id, doctor_name in self._doctors(session):
            if doctor_name and (target in doctor_name.casefold()
                                or doctor_name.casefold() in target):
                ctx.doctor_id = str(doctor_id)
                return
        ctx.doctor_miss = True

    def _collect_slots(self, session: Session, doctors, service_id,
                       asked: date, time_ref: str | None):
        """Первый день (от asked, до 2 недель вперёд) с подходящими слотами."""
        tz = self._clinic_tz(session)
        start_day = max(asked, self._today(session))
        now_utc = self._clock()
        target = exact_time_ref(time_ref)  # точное HH:MM — мягкое предпочтение
        for offset in range(NEAREST_DAY_SCAN + 1):
            day = start_day + timedelta(days=offset)
            free = [
                (slot.start, doctor_id, doctor_name)
                for doctor_id, doctor_name in doctors
                for slot in self._sched.find_free_slots(doctor_id, service_id, day)
                if slot.start > now_utc  # сегодняшние прошедшие слоты не предлагаем
            ]
            strict = [s for s in free
                      if matches_time_ref(time_ref, s[0].astimezone(tz).time())]
            if strict:
                strict.sort(key=lambda s: (s[0], str(s[1])))
                return day, strict
            # некруглое время без совпадения с сеткой: не врать «нет слотов»,
            # показать слоты дня, ближайшие к запрошенному времени (M1)
            if target is not None and free:
                tmin = target.hour * 60 + target.minute
                free.sort(key=lambda s: (
                    abs(self._local_minutes(s[0], tz) - tmin), s[0], str(s[1])))
                return day, free
        return start_day, []

    @staticmethod
    def _local_minutes(start: datetime, tz) -> int:
        local = start.astimezone(tz).time()
        return local.hour * 60 + local.minute

    def _offer_body(self, session: Session, lang: str, asked: date, day: date,
                    time_ref: str | None = None) -> str:
        today = self._today(session)
        if max(asked, today) == today and self._closed_now(session):
            # пациент метит в «сегодня», а клиника вне рабочего окна: говорим
            # прямо «закрыто» — иначе ответ читается как «всё занято» (P0 BRIEF)
            return t("closed_now_slots", lang, date=f"{day:%d.%m}")
        prefix = self._outside_hours_line(session, lang, max(asked, today),
                                          time_ref)
        if day == asked:
            return prefix + t("offer_slots", lang, date=f"{day:%d.%m}")
        return prefix + t("offer_slots_other_day", lang, asked=f"{asked:%d.%m}",
                          date=f"{day:%d.%m}")

    def _outside_hours_line(self, session: Session, lang: str, day: date,
                            time_ref: str | None) -> str:
        """Точное запрошенное время вне рабочего окна дня → честная строка
        «Клиника работает с X до Y» перед слотами (П-3). Окна morning/evening
        не считаются; день целиком закрыт — существующий скан и так уводит
        на ближайший рабочий день."""
        target = exact_time_ref(time_ref)
        if target is None:
            return ""
        window = self._day_window(session, day)
        if window is None:
            return ""
        lo, hi = window
        if lo.time() <= target < hi.time():
            return ""
        return t("outside_hours", lang,
                 open=f"{lo:%H:%M}", close=f"{hi:%H:%M}") + "\n"

    def _day_window(self, session: Session, day: date):
        """Рабочее окно дня (union графиков врачей) в локальном времени
        клиники; None — день целиком закрыт."""
        tz = self._clinic_tz(session)
        schedules = doctors_repo.working_intervals(session)
        holidays = clinic_repo.holidays_on(session, day)
        bounds = open_bounds(schedules, day, tz, holidays)
        if bounds is None:
            return None
        lo, hi = bounds
        return lo.astimezone(tz), hi.astimezone(tz)

    def _closed_now(self, session: Session) -> bool:
        """Клиника вне рабочего окна прямо сейчас (выходной/праздник — тоже)."""
        today = self._today(session)
        schedules = doctors_repo.working_intervals(session)
        holidays = clinic_repo.holidays_on(session, today)
        bounds = open_bounds(schedules, today, self._clinic_tz(session), holidays)
        if bounds is None:
            return True
        lo, hi = bounds
        return not (lo <= self._clock() < hi)

    def _slot_label(self, start: datetime, doctor_name: str | None,
                    tz: ZoneInfo, multi_doctor: bool) -> str:
        local = start.astimezone(tz)
        label = f"{local:%d.%m %H:%M}"
        if multi_doctor and doctor_name:
            label += f" · {doctor_name}"
        return label
