"""Супервизор: вся система одним процессом — канал, календарь, напоминания.

    python -m navbat                          # демо-клиника, фикстурный NLU
    python -m navbat --clinic <uuid> --real   # ДЕНЬГИ: живой gpt-4o-mini
    python -m navbat --check                  # преддемо-чеклист [OK]/[FAIL]

Потоки: polling-транспорт, N воркеров очереди, календарный sync (если
настроен Google), напоминания + вечерняя сводка. Ctrl+C — graceful.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid
from datetime import timedelta

from sqlalchemy import text

from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.envfile import load_env_file
from navbat.nlu.wrappers import (
    BudgetedExtractor,
    DeidentifyingExtractor,
    UsageRecorder,
)
from navbat.onboard import DEMO_CLINIC_ID, DEV_ENC_KEY
from navbat.reminders import ReminderService
from navbat.telegram.api import TelegramAPI, TelegramAPIError
from navbat.telegram.app import build_extractor, load_clinic_credentials
from navbat.telegram.escalation import TelegramEscalation
from navbat.telegram.transport import PollingTransport
from navbat.telegram.worker import UpdateWorker

log = logging.getLogger("navbat")


def parse_offsets(raw: str) -> tuple[timedelta, ...]:
    """«1440,120» (минуты) → офсеты напоминаний. Демо: «2,1»."""
    offsets = tuple(timedelta(minutes=int(part.strip()))
                    for part in raw.split(",") if part.strip())
    if not offsets:
        raise ValueError(f"пустой список офсетов: {raw!r}")
    return offsets


def build_real_extractor(session_factory, clinic_id: uuid.UUID, notifier):
    """Боевая сборка NLU: бюджет → деидентификация → fallback(OpenAI, Gemini).

    Деидентификация и бюджет общие на оба провайдера; без GEMINI_API_KEY
    каскада нет — аутэйдж OpenAI уходит в ретрай очереди (как раньше).
    """
    from navbat.nlu.openai_extractor import OpenAIExtractor

    recorder = UsageRecorder(session_factory, clinic_id, notifier=notifier)
    extractor = OpenAIExtractor(on_usage=recorder.record)
    if os.environ.get("GEMINI_API_KEY"):
        from navbat.nlu.fallback import FallbackExtractor
        from navbat.nlu.gemini_extractor import GeminiExtractor

        extractor = FallbackExtractor(
            extractor, GeminiExtractor(on_usage=recorder.record))
        log.info("LLM-fallback включён: Gemini")
    else:
        log.warning("GEMINI_API_KEY не задан — fallback-LLM выключен")
    inner = DeidentifyingExtractor(extractor)
    return BudgetedExtractor(inner, recorder)


def run_check(session_factory, clinic_id: uuid.UUID, use_real: bool) -> int:
    """Преддемо-чеклист. Возвращает exit code."""
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        suffix = f" — {detail}" if detail else ""
        print(f"{'[OK]' if ok else '[FAIL]'} {label}{suffix}")

    try:
        with tenant_transaction(session_factory, clinic_id) as session:
            session.execute(text("SELECT 1 FROM reminder LIMIT 0"))  # миграции 0006+
            clinic = session.execute(
                text("SELECT name, tg_bot_token_encrypted, tg_admin_chat_id, "
                     "gcal_refresh_token_encrypted FROM clinic WHERE id = :id"),
                {"id": clinic_id},
            ).one_or_none()
            doctors = session.execute(text("SELECT count(*) FROM doctor")).scalar_one()
            services = session.execute(text("SELECT count(*) FROM service")).scalar_one()
        report(True, "БД и миграции")
    except Exception as e:
        report(False, "БД и миграции", str(e)[:120])
        return 1

    if clinic is None:
        report(False, "клиника", f"{clinic_id} не найдена — python -m navbat.onboard")
        return 1
    report(True, "клиника", clinic.name)
    report(doctors > 0 and services > 0, "врачи и услуги",
           f"{doctors} врач(а), {services} услуг")

    if clinic.tg_bot_token_encrypted:
        from navbat.crypto import decrypt_text
        try:
            me = TelegramAPI(decrypt_text(clinic.tg_bot_token_encrypted)).get_me()
            report(True, "Telegram-бот", f"@{me.get('username')}")
        except TelegramAPIError as e:
            report(False, "Telegram-бот", str(e)[:120])
    else:
        report(False, "Telegram-бот", "токен не задан (onboard --tg-token)")
    report(clinic.tg_admin_chat_id is not None, "админ-чат (эскалации, /stats, сводка)")

    if clinic.gcal_refresh_token_encrypted:
        from navbat.calendar.api import CalendarAuthError, GoogleCalendarAPI
        from navbat.crypto import decrypt_text
        try:
            GoogleCalendarAPI(decrypt_text(clinic.gcal_refresh_token_encrypted)).check_auth()
            report(True, "Google Calendar (refresh-токен жив)")
        except CalendarAuthError as e:
            report(False, "Google Calendar", str(e)[:120])
    else:
        report(True, "Google Calendar", "не настроен — бот работает без календаря")

    if os.environ.get("GEMINI_API_KEY"):
        report(True, "fallback-LLM", "Gemini (ключ задан)")
    else:
        report(True, "fallback-LLM",
               "не настроен — при аутэйдже OpenAI бот без NLU (GEMINI_API_KEY)")

    if use_real:
        report(bool(os.environ.get("OPENAI_API_KEY")), "OPENAI_API_KEY для --real")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Navbat: канал + календарь + напоминания одним процессом")
    parser.add_argument("--clinic", type=uuid.UUID, default=DEMO_CLINIC_ID,
                        help="по умолчанию — демо-клиника")
    parser.add_argument("--real", action="store_true",
                        help="реальный gpt-4o-mini (платно!)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--reminder-offsets", default="1440,120",
                        help="минуты до приёма, CSV; демо: 2,1")
    parser.add_argument("--sync-interval", type=int, default=60)
    parser.add_argument("--no-calendar", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="преддемо-чеклист и выход")
    args = parser.parse_args()

    load_env_file()
    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    session_factory = make_session_factory(make_app_engine())

    if args.check:
        return run_check(session_factory, args.clinic, args.real)

    offsets = parse_offsets(args.reminder_offsets)
    credentials = load_clinic_credentials(session_factory, args.clinic)
    tg_api = TelegramAPI(credentials.token)
    me = tg_api.get_me()
    notifier = TelegramEscalation(tg_api, credentials.admin_chat_id)
    log.info("бот @%s, клиника %s", me.get("username"), args.clinic)

    if args.real:
        extractor = build_real_extractor(session_factory, args.clinic, notifier)
        log.warning("NLU: gpt-4o-mini — каждое сообщение стоит денег")
    else:
        extractor = build_extractor(use_real=False)

    # календарь: sync-цикл + freeBusy-guard перед confirm
    slot_guard = None
    calendar_sync = None
    with tenant_transaction(session_factory, args.clinic) as session:
        gcal_token = session.execute(
            text("SELECT gcal_refresh_token_encrypted FROM clinic WHERE id = :id"),
            {"id": args.clinic},
        ).scalar_one_or_none()
    if gcal_token and not args.no_calendar:
        from navbat.calendar.guard import CalendarSlotGuard
        from navbat.calendar.sync import CalendarSync
        from navbat.calendar.api import GoogleCalendarAPI
        from navbat.crypto import decrypt_text

        gcal_api = GoogleCalendarAPI(decrypt_text(gcal_token))
        slot_guard = CalendarSlotGuard(session_factory, args.clinic, gcal_api)
        calendar_sync = CalendarSync(session_factory, args.clinic, api=gcal_api,
                                     notifier=notifier, tg_api=tg_api)
        log.info("календарь: sync каждые %d с + freeBusy-guard", args.sync_interval)
    else:
        log.info("календарь: выключен")

    dialog = DialogEngine(session_factory, args.clinic, extractor=extractor,
                          notifier=notifier, slot_guard=slot_guard)
    reminders = ReminderService(session_factory, args.clinic, tg_api=tg_api,
                                notifier=notifier, offsets=offsets,
                                digest_chat_id=credentials.admin_chat_id)

    stop = threading.Event()
    threads = [
        threading.Thread(target=reminders.run, args=(stop,), name="reminders"),
    ]
    for index in range(args.workers):
        worker = UpdateWorker(session_factory, args.clinic, dialog=dialog,
                              api=tg_api, notifier=notifier,
                              admin_chat_id=credentials.admin_chat_id)
        threads.append(threading.Thread(target=worker.run, args=(stop,),
                                        name=f"worker-{index}"))
    if calendar_sync is not None:
        def calendar_loop() -> None:
            while not stop.is_set():
                with tenant_transaction(session_factory, args.clinic) as session:
                    doctor_ids = list(session.execute(text(
                        "SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL"
                    )).scalars())
                for doctor_id in doctor_ids:
                    try:
                        calendar_sync.sync_doctor(doctor_id)
                    except Exception:
                        log.exception("sync врача %s упал", doctor_id)
                stop.wait(args.sync_interval)

        threads.append(threading.Thread(target=calendar_loop, name="calendar"))

    for thread in threads:
        thread.start()
    log.info("система поднята: %d воркера, напоминания %s",
             args.workers, args.reminder_offsets)
    try:
        tg_api.delete_webhook()  # иначе getUpdates вернёт 409
        PollingTransport(session_factory, args.clinic, tg_api).run(stop)
    except KeyboardInterrupt:
        log.info("останавливаюсь…")
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
    return 0
