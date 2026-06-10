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
import signal
import sys
import threading
import uuid
from datetime import timedelta

from sqlalchemy import text

from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.envfile import load_env_file
from navbat.health import HealthChecker, HealthServer
from navbat.nlu.wrappers import (
    BudgetedExtractor,
    DeidentifyingExtractor,
    DriftTrackingExtractor,
    UsageRecorder,
)
from navbat.onboard import DEMO_CLINIC_ID, DEV_ENC_KEY
from navbat.reminders import ReminderService
from navbat.telegram.api import TelegramAPI, TelegramAPIError
from navbat.telegram.app import build_dialog_extractor, load_clinic_credentials
from navbat.telegram.escalation import TelegramEscalation
from navbat.telegram.transport import PollingTransport, WebhookServer, ensure_webhook
from navbat.telegram.worker import UpdateWorker

log = logging.getLogger("navbat")


def parse_offsets(raw: str) -> tuple[timedelta, ...]:
    """«1440,120» (минуты) → офсеты напоминаний. Демо: «2,1»."""
    offsets = tuple(timedelta(minutes=int(part.strip()))
                    for part in raw.split(",") if part.strip())
    if not offsets:
        raise ValueError(f"пустой список офсетов: {raw!r}")
    return offsets


def install_sigterm_handler(stop: threading.Event) -> None:
    """docker stop шлёт SIGTERM — гасим теми же рельсами, что Ctrl+C."""
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())


def validate_real_env() -> list[str]:
    """--real = боевой режим: PII под dev-ключом и пустые API-ключи — отказ."""
    problems = []
    enc_key = os.environ.get("NAVBAT_ENC_KEY")
    if not enc_key or enc_key == DEV_ENC_KEY:
        problems.append(
            "NAVBAT_ENC_KEY: для --real нужен боевой ключ (base64 от 32 байт),"
            " dev-ключ недопустим")
    if not os.environ.get("OPENAI_API_KEY"):
        problems.append("OPENAI_API_KEY не задан — --real без него не работает")
    return problems


def build_real_extractor(session_factory, clinic_id: uuid.UUID, notifier):
    """Боевая сборка NLU: бюджет → деидентификация → fallback(OpenAI, Gemini).

    Деидентификация и бюджет общие на оба провайдера; без GEMINI_API_KEY
    каскада нет — аутэйдж OpenAI уходит в ретрай очереди (как раньше).
    """
    from navbat.nlu.openai_extractor import OpenAIExtractor

    recorder = UsageRecorder(session_factory, clinic_id, notifier=notifier)
    prompt = _load_pinned_prompt(session_factory, clinic_id)
    extractor = OpenAIExtractor(on_usage=recorder.record,
                                on_repair=recorder.record_repair,
                                prompt=prompt)
    if os.environ.get("GEMINI_API_KEY"):
        from navbat.nlu.fallback import FallbackExtractor
        from navbat.nlu.gemini_extractor import GeminiExtractor

        extractor = FallbackExtractor(
            extractor, GeminiExtractor(on_usage=recorder.record,
                                       on_repair=recorder.record_repair,
                                       prompt=prompt))
        log.info("LLM-fallback включён: Gemini")
    else:
        log.warning("GEMINI_API_KEY не задан — fallback-LLM выключен")
    inner = DriftTrackingExtractor(DeidentifyingExtractor(extractor), recorder)
    return BudgetedExtractor(inner, recorder)


def _load_pinned_prompt(session_factory, clinic_id: uuid.UUID) -> str | None:
    """Версия NLU-промпта из БД по пину клиники; None — встроенный файл (B.2)."""
    with tenant_transaction(session_factory, clinic_id) as session:
        row = session.execute(text(
            "SELECT p.version, p.body FROM clinic c "
            "JOIN nlu_prompt p ON p.version = c.nlu_prompt_version "
            "WHERE c.id = current_setting('app.clinic_id')::uuid"
        )).one_or_none()
    if row is None:
        log.info("NLU-промпт: встроенный файл")
        return None
    log.info("NLU-промпт: версия %d из БД", row.version)
    return row.body


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
                text("SELECT name, tg_bot_token_encrypted, tg_admin_chat_ids, "
                     "gcal_refresh_token_encrypted, nlu_prompt_version "
                     "FROM clinic WHERE id = :id"),
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
    admin_chats = clinic.tg_admin_chat_ids or []
    report(bool(admin_chats),
           "админ-чат (эскалации, /stats, сводка)",
           f"{len(admin_chats)} чат(ов)" if admin_chats else None)

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
    report(True, "NLU-промпт",
           f"версия {clinic.nlu_prompt_version} (БД)"
           if clinic.nlu_prompt_version else "встроенный файл")

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
    parser.add_argument("--webhook-url", default=None,
                        help="публичный https-URL; без него — long polling")
    parser.add_argument("--webhook-port", type=int, default=8443)
    parser.add_argument("--health-port", type=int,
                        default=int(os.environ.get("NAVBAT_HEALTH_PORT", "8080")))
    parser.add_argument("--check", action="store_true",
                        help="преддемо-чеклист и выход")
    args = parser.parse_args()

    load_env_file()
    if args.real and not args.check:
        problems = validate_real_env()
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    session_factory = make_session_factory(make_app_engine())

    if args.check:
        return run_check(session_factory, args.clinic, args.real)

    offsets = parse_offsets(args.reminder_offsets)
    credentials = load_clinic_credentials(session_factory, args.clinic)
    tg_api = TelegramAPI(credentials.token)
    me = tg_api.get_me()
    notifier = TelegramEscalation(tg_api, credentials.admin_chat_ids)
    log.info("бот @%s, клиника %s", me.get("username"), args.clinic)

    extractor = build_dialog_extractor(args.real, session_factory,
                                       args.clinic, notifier)

    # календарь: sync-цикл + freeBusy-guard перед confirm
    slot_guard = None
    calendar_sync = None
    watch_manager = None
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
        if args.webhook_url:
            from navbat.calendar.watch import GcalWatchManager

            watch_manager = GcalWatchManager(session_factory, args.clinic,
                                             gcal_api, args.webhook_url)
            log.info("календарь: watch-каналы включены (push будит синк)")
    else:
        log.info("календарь: выключен")

    dialog = DialogEngine(session_factory, args.clinic, extractor=extractor,
                          notifier=notifier, slot_guard=slot_guard)
    reminders = ReminderService(session_factory, args.clinic, tg_api=tg_api,
                                notifier=notifier, offsets=offsets,
                                digest_chat_id=credentials.admin_chat_ids)

    stop = threading.Event()
    sync_wake = threading.Event()  # push /gcal/push/<канал> будит календарь
    install_sigterm_handler(stop)
    threads = [
        threading.Thread(target=reminders.run, args=(stop,), name="reminders"),
    ]
    for index in range(args.workers):
        worker = UpdateWorker(session_factory, args.clinic, dialog=dialog,
                              api=tg_api, notifier=notifier,
                              admin_chat_id=credentials.admin_chat_ids)
        threads.append(threading.Thread(target=worker.run, args=(stop,),
                                        name=f"worker-{index}"))
    if calendar_sync is not None:
        from navbat.calendar.sync_loop import CalendarSyncLoop

        sync_loop = CalendarSyncLoop(session_factory, args.clinic, calendar_sync,
                                     notifier, credentials.admin_chat_ids)

        def calendar_loop() -> None:
            while not stop.is_set():
                if watch_manager is not None:
                    try:
                        watch_manager.ensure_channels()
                    except Exception:
                        log.exception("watch-каналы: ensure_channels упал")
                sync_loop.run_once()
                sync_wake.wait(args.sync_interval)
                sync_wake.clear()

        threads.append(threading.Thread(target=calendar_loop, name="calendar"))

    for thread in threads:
        thread.start()
    log.info("система поднята: %d воркера, напоминания %s",
             args.workers, args.reminder_offsets)

    health = HealthServer(
        HealthChecker(session_factory, args.clinic,
                      sync_interval_sec=args.sync_interval,
                      cert_path=os.environ.get("NAVBAT_CERT_PATH"),
                      notifier=notifier,
                      backup_dir=os.environ.get("NAVBAT_BACKUP_DIR"),
                      backup_interval_sec=int(os.environ.get(
                          "NAVBAT_BACKUP_INTERVAL_SEC", "7200"))),
        port=args.health_port)
    health.start()

    webhook_server = None
    try:
        if args.webhook_url:
            if not credentials.webhook_secret:
                sys.exit("[FAIL] webhook-режим требует webhook-секрет "
                         "(onboard --tg-token генерирует)")
            webhook_server = WebhookServer(
                session_factory, args.clinic,
                secret=credentials.webhook_secret, port=args.webhook_port,
                gcal_wake=sync_wake if calendar_sync is not None else None)
            webhook_server.start()
            ensure_webhook(tg_api, args.webhook_url,
                           credentials.webhook_secret,
                           notifier=notifier, path=webhook_server.path)
            stop.wait()  # до SIGTERM/Ctrl+C
        else:
            tg_api.delete_webhook()  # иначе getUpdates вернёт 409
            PollingTransport(session_factory, args.clinic, tg_api).run(stop)
    except KeyboardInterrupt:
        log.info("останавливаюсь…")
    finally:
        stop.set()
        sync_wake.set()  # разбудить календарный поток, чтобы он увидел stop
        if webhook_server:
            webhook_server.stop()
        health.stop()
        for thread in threads:
            thread.join(timeout=10)
    return 0
