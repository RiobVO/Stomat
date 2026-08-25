"""Напоминания пациентам: reconciliation из appointment (BRIEF, Notifier).

Никаких таймеров в памяти — каждое состояние выводится из БД и переживает
рестарт/деплой. Кнопки «Приду»/«Отменить» обрабатывает FSM (actions
attend:<id> / remind_cancel:<id>) — отмена из напоминания освобождает слот.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog import (
    clinic_repo,
    doctors_repo,
    questions_repo,
    services_repo,
    waitlist_repo,
)
from navbat.dialog.conversation import get_chat_lang
from navbat.dialog.escalation import (
    EscalationNotifier,
    LoggingEscalation,
    ops_alert,
    system_alert,
)
from navbat.crypto import decrypt_text
from navbat.dialog.replies import Button, Reply, card_lines, service_label, t
from navbat.retention import cleanup_old_data
from navbat.scheduling.engine import SchedulingEngine
from navbat.stats import (
    collect_daily_stats, render_digest_short, render_questions,
    should_send_digest)
from navbat.telegram import feed_repo
from navbat.telegram.admin_texts import Reason, at
from navbat.telegram.api import ChatUnavailableError
from navbat.telegram.escalation import _as_chat_tuple
from navbat.telegram.today_view import render_day
from navbat.telegram.worker import send_reply

log = logging.getLogger("navbat.reminders")

DEFAULT_OFFSETS = (timedelta(hours=24), timedelta(hours=2))
MAX_ATTEMPTS = 3

# Утренняя сводка: время, а не целый час как DIGEST_HOUR у вечернего
# дайджеста. Полчаса здесь содержательны — сводка должна лечь в чат перед
# открытием в 09:00, но не в 08:00, когда её ещё некому читать.
MORNING_SUMMARY_TIME = dt_time(8, 30)

# Лист ожидания: горизонт поиска слота, антиспам-кулдаун, TTL записи (env)
WAITLIST_HORIZON_DAYS = int(os.environ.get("WAITLIST_HORIZON_DAYS", "14"))
WAITLIST_RENOTIFY_HOURS = int(os.environ.get("WAITLIST_RENOTIFY_HOURS", "6"))
WAITLIST_TTL_DAYS = int(os.environ.get("WAITLIST_TTL_DAYS", "14"))
WAITLIST_ENABLED = os.environ.get("WAITLIST_ENABLED", "1") != "0"


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
        self._digest_chat_ids = _as_chat_tuple(digest_chat_id)
        self._sched = SchedulingEngine(session_factory, clinic_id)
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
                        -- запись переехала: напоминание взводится заново даже
                        -- если старое уже ушло, иначе о новом времени пациенту
                        -- никто не напомнит (перенос сохраняет appointment.id).
                        -- failed не воскрешаем: по нему уже был алерт админу
                        SET send_at = EXCLUDED.send_at, status = 'pending',
                            attempts = 0
                        -- cancelled тоже воскрешаем: строку гасит начавшийся
                        -- приём, а его могут перенести в будущее (правка
                        -- события в Google). Сюда попадают только будущие
                        -- booked-записи живого чата, поэтому отменённая
                        -- запись и «забытый» пациент не воскреснут
                        WHERE reminder.status IN ('pending', 'sent', 'cancelled')
                          AND reminder.send_at IS DISTINCT FROM EXCLUDED.send_at
                    """),
                    {"kind": _kind(offset), "offset": offset},
                )
            # запись отменили/перенесли в прошлое и т.п. — pending гасим.
            # Приём, который уже начался, — тоже: после визита статус остаётся
            # 'booked' навсегда, и строка тянулась бы вечно (перенос ближе
            # офсета оставляет её с прежним send_at, простой процесса — с
            # созревшим)
            session.execute(text("""
                UPDATE reminder r SET status = 'cancelled'
                FROM appointment a
                WHERE a.id = r.appointment_id
                  AND r.status = 'pending'
                  AND (a.status != 'booked' OR lower(a.time_range) <= now())
            """))

    # ── Доставка ─────────────────────────────────────────────────────────

    def send_due(self) -> int:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            due = session.execute(
                text("""
                    SELECT r.id, r.appointment_id, r.attempts, r.send_at,
                           a.tg_chat_id, lower(a.time_range) AS start,
                           s.name AS service, c.timezone, c.address,
                           d.name_encrypted AS doctor_encrypted
                    FROM reminder r
                    JOIN appointment a ON a.id = r.appointment_id
                    JOIN clinic c ON c.id = r.clinic_id
                    LEFT JOIN service s ON s.id = a.service_id
                    LEFT JOIN doctor d ON d.id = a.doctor_id
                    WHERE r.status = 'pending' AND r.send_at <= now()
                      -- о приёме, который уже начался, не напоминают: строка
                      -- могла созреть за простой процесса, а кнопка «Отменить
                      -- запись» из неё ушла бы в сводку как предотвращённая
                      -- неявка с деньгами
                      AND lower(a.time_range) > now()
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
        # строка conversation тут не нужна и не создаётся: кнопки напоминания
        # несут запись сами (сырой callback), а слепая перезапись диалога из
        # фонового потока затирала бы то, что пациент делает прямо сейчас
        local = row.start.astimezone(ZoneInfo(row.timezone))
        doctor = (decrypt_text(row.doctor_encrypted)
                  if row.doctor_encrypted else None)
        reply = Reply(
            t("reminder", lang, service=service_label(row.service or "checkup", lang),
              when=f"{local:%d.%m %H:%M}",
              **card_lines(doctor, row.address)),
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
                ops_alert(
                    self._notifier,
                    Reason("reason_reminder_failed", chat=row.tg_chat_id,
                           attempts=MAX_ATTEMPTS),
                    {"reminder": row.id},
                    chat_id=row.tg_chat_id or 0)
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            # отметка относится к взятой в работу версии: если за время
            # отправки запись перенесли и напоминание перевзвели на новое
            # время, гасить его нельзя — о переносе никто не напомнит
            session.execute(
                text("UPDATE reminder SET status = 'sent', sent_at = now() "
                     "WHERE id = :id AND send_at = :send_at"),
                {"id": row.id, "send_at": row.send_at},
            )
        return True

    # ── Лист ожидания: освободился слот → предложить очереди ──────────────

    def match_waitlist(self) -> int:
        """Предложить освободившийся слот пациентам из очереди. Возвращает
        число отправленных предложений. Освобождение слота даёт cancel()
        (запись выходит из exclusion constraint); матчер ищет ближайший
        свободный слот по услуге в горизонте и шлёт его теми же
        slot:-кнопками — тап = штатная запись."""
        if not WAITLIST_ENABLED:
            return 0
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            waitlist_repo.expire_old(session, WAITLIST_TTL_DAYS)
            rows = waitlist_repo.list_waiting(session)
            if not rows:  # пустая очередь не сканирует слоты
                return 0
            tz = ZoneInfo(clinic_repo.clinic_timezone(session))
            doctor_ids = [d[0] for d in doctors_repo.doctor_list(session)]
        now = datetime.now(tz)
        days = [now.date() + timedelta(days=i)
                for i in range(WAITLIST_HORIZON_DAYS)]
        sent = 0
        offered: set[datetime] = set()
        for row in rows:
            start = self._offer_waitlist_slot(row, doctor_ids, days, tz, now,
                                              offered)
            if start is not None:
                offered.add(start)
                sent += 1
        return sent

    def _earliest_slot(self, service_id, doctor_ids, days, now, offered):
        """Ближайший свободный слот по услуге (любой врач) в горизонте.

        offered — время, уже разосланное в этом же цикле: слот один, кнопка
        несёт только минуту, а врач подбирается при тапе, поэтому одно время
        двум подписчикам = гарантированное «уже занято» для второго.
        """
        for day in days:
            best = None
            for did in doctor_ids:
                for slot in self._sched.find_free_slots(did, service_id, day):
                    if slot.start <= now or slot.start in offered:
                        continue  # прошедшие часы и уже разосланное время
                    if best is None or slot.start < best[0]:
                        best = (slot.start, did)
                    break  # find_free_slots отсортирован — первый = ближайший
            if best:
                return best  # дни по порядку → первый день с слотом = ближайший
        return None

    def _offer_waitlist_slot(self, row, doctor_ids, days, tz, now,
                             offered) -> datetime | None:
        """Разосланное время (для дедупа цикла) либо None, если пуша не было."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            if waitlist_repo.has_future_booked(session, row.tg_chat_id,
                                               row.service_id):
                waitlist_repo.mark_fulfilled(session, row.id)
                return None
        # антиспам: пока тот же слот висит — не долбить уведомлённого
        if row.status == "notified" and row.notified_at and \
                now - row.notified_at < timedelta(hours=WAITLIST_RENOTIFY_HOURS):
            return None
        found = self._earliest_slot(row.service_id, doctor_ids, days, now, offered)
        if found is None:
            return None
        start, doctor_id = found
        local = start.astimezone(tz)
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = get_chat_lang(session, row.tg_chat_id)
            service_key = services_repo.service_name(session, row.service_id) \
                or "checkup"
            svc = service_label(service_key, lang)
        # Кнопка несёт слот сама (сырой wl:, как кнопка выхода рядом):
        # предложение висит часами, а карта кнопок одна на чат и
        # перезаписывается любой следующей отправкой. Услуга берётся из строки
        # очереди при тапе — писать её в диалог из фонового потока нельзя:
        # пациент может в этот момент оформлять совсем другую запись.
        # В callback_data 64 байта, поэтому время — минуты эпохи, а врач
        # подбирается при тапе (очередь и так обещает «любого врача»).
        reply = Reply(
            t("waitlist_slot_offer", lang, service=svc, when=f"{local:%d.%m %H:%M}"),
            button_rows=(
                (Button(f"{local:%d.%m %H:%M}",
                        f"wl:take:{row.id}:{int(start.timestamp()) // 60}"),),
                (Button(t("btn_waitlist_leave", lang), f"wl:leave:{row.id}"),),
            ),
        )
        try:
            if self._tg_api is None:
                raise RuntimeError("tg_api не задан")
            send_reply(self._tg_api, self._session_factory, self._clinic_id,
                       row.tg_chat_id, reply)
        except ChatUnavailableError:
            # пациент заблокировал бота — снять с очереди (как C2)
            with tenant_transaction(self._session_factory,
                                    self._clinic_id) as session:
                waitlist_repo.mark_cancelled(session, row.id)
            return None
        except Exception as e:
            log.warning("waitlist %s: пуш не удался: %s", row.id, e)
            return None
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            waitlist_repo.mark_notified(session, row.id)
        return start

    # ── Вечерняя сводка админу ───────────────────────────────────────────

    def _admin_lang(self, chat_id: int) -> str:
        """Язык консоли этого админ-чата: сводка идёт веером, и каждый
        получатель читает её на своём языке (карта, №16)."""
        from navbat.dialog.conversation import load_conversation
        from navbat.telegram.admin_texts import DEFAULT_LANG, LANGS
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
        lang = conv.context.extras.get("adm_lang")
        return lang if lang in LANGS else DEFAULT_LANG

    def maybe_send_digest(self, now_local=None) -> bool:
        """Раз в день после DIGEST_HOUR; отметка — clinic.last_digest_date."""
        if not self._digest_chat_ids or self._tg_api is None:
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
            questions = questions_repo.for_day(session, moment.date(),
                                               row.timezone)
        delivered = 0
        for chat in self._digest_chat_ids:  # сводка всем админ-чатам (M4)
            # сбой одного получателя не отменяет остальных и не отменяет день:
            # цикл напоминаний повторяет попытку каждые 30 с, пока день не
            # отмечен, и мёртвый чат размножал бы сводку у живых до полуночи
            # (веер алертов изолирован по чатам ровно по этой причине)
            try:
                # у каждого админ-чата свой язык консоли (карта, №16)
                lang = self._admin_lang(chat)
                # короткий дайджест (В): три строки ценности; полная сводка
                # дня — за кнопкой «Подробнее» (сырой stats:full, map не нужен)
                digest = render_digest_short(stats, lang)
                if questions:
                    # вопросы, на которые бот не ответил (П-2б): спрос владельцу
                    digest += "\n\n" + render_questions(questions, lang)
                self._tg_api.send_message(
                    chat, digest, parse_mode="HTML",
                    buttons=(Button(at("digest_more", lang), "stats:full"),))
                delivered += 1
            except Exception as e:
                log.warning("вечерняя сводка: чат %s не получил её (%s)", chat, e)
        if not delivered:
            # не дошла никому — день не отмечаем, попытка повторится
            log.warning("вечерняя сводка не доставлена ни одному админ-чату")
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("UPDATE clinic SET last_digest_date = :day "
                     "WHERE id = current_setting('app.clinic_id')::uuid"),
                {"day": moment.date()},
            )
        return True

    def maybe_send_morning_summary(self, now_local=None) -> bool:
        """День клиники в админ-чаты раз в утро; отметка —
        clinic.last_morning_digest_date.

        Своя отметка, а не last_digest_date вечернего дайджеста: это два
        события одного дня, и общая колонка гасила бы одно другим.
        """
        if not self._digest_chat_ids or self._tg_api is None:
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(text(
                "SELECT timezone, last_morning_digest_date FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).one()
        tz = ZoneInfo(row.timezone)
        moment = now_local or datetime.now(tz)
        if moment.time() < MORNING_SUMMARY_TIME:
            return False
        if row.last_morning_digest_date is not None \
                and row.last_morning_digest_date >= moment.date():
            return False
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            cards = feed_repo.day_cards(session, moment.date(), tz)
        if not cards:
            # пустым днём владельца не будим, но день отмечаем: такт цикла —
            # 30 секунд, и без отметки он до полуночи ходил бы в базу за
            # одним и тем же пустым списком
            self._mark_morning_summary(moment.date())
            return False
        delivered = 0
        for chat in self._digest_chat_ids:
            # сбой одного получателя не отменяет остальных и не отменяет день
            # (та же логика, что у вечернего дайджеста): мёртвый чат иначе
            # размножал бы сводку у живых каждые полминуты
            try:
                # у каждого админ-чата свой язык консоли (карта, №16)
                lang = self._admin_lang(chat)
                body = (at("morning_header", lang) + "\n\n"
                        + render_day(cards, moment.date(), lang, tz))
                self._tg_api.send_message(chat, body, parse_mode="HTML")
                delivered += 1
            except Exception as e:
                log.warning("утренняя сводка: чат %s не получил её (%s)",
                            chat, e)
        if not delivered:
            # не дошла никому — день не отмечаем, попытка повторится
            log.warning("утренняя сводка не доставлена ни одному админ-чату")
            return False
        self._mark_morning_summary(moment.date())
        return True

    def _mark_morning_summary(self, day: date) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("UPDATE clinic SET last_morning_digest_date = :day "
                     "WHERE id = current_setting('app.clinic_id')::uuid"),
                {"day": day},
            )

    def maybe_cleanup(self) -> bool:
        """Retention-чистка раз в календарный день (D.3)."""
        today = date.today()
        if self._cleaned_on == today:
            return False
        # отметка ПОСЛЕ чистки: упавшая (обрыв БД) иначе не повторилась бы до
        # завтра, а срок хранения — обещание в docs/PRIVACY.md. DELETE
        # идемпотентен, повтор в этот же день безвреден
        cleanup_old_data(self._session_factory, self._clinic_id)
        self._cleaned_on = today
        return True

    def run(self, stop: threading.Event, interval: float = 30.0) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.reconcile()
                self.send_due()
                self.match_waitlist()
                self.maybe_send_morning_summary()
                self.maybe_send_digest()
                self.maybe_cleanup()
            except Exception:
                log.exception("цикл напоминаний упал — продолжаю")
            elapsed = time.monotonic() - started
            stop.wait(max(0.0, interval - elapsed))
