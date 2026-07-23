"""Синхронизация календаря клиники.

    python -m navbat.calendar --clinic <uuid> --once          # разовый прогон
    python -m navbat.calendar --clinic <uuid> --interval 60   # цикл (сек)

Push-каналы Google (watch) требуют публичный HTTPS — подключаются на
деплое; периодический incremental sync (syncToken) — рабочий базис.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.calendar.api import GoogleCalendarAPI
from navbat.calendar.sync import CalendarSync
from navbat.crypto import decrypt_text
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.telegram.api import TelegramAPI
from navbat.telegram.escalation import TelegramEscalation

log = logging.getLogger("navbat.calendar")


def load_refresh_token(session_factory: sessionmaker[Session],
                       clinic_id: uuid.UUID) -> str:
    with tenant_transaction(session_factory, clinic_id) as session:
        token = session.execute(
            text("SELECT gcal_refresh_token_encrypted FROM clinic WHERE id = :id"),
            {"id": clinic_id},
        ).scalar_one_or_none()
    if not token:
        sys.exit(f"[FAIL] у клиники {clinic_id} нет refresh-токена — "
                 f"выполните python -m navbat.calendar.auth")
    return decrypt_text(token)


def build_sync(session_factory: sessionmaker[Session],
               clinic_id: uuid.UUID) -> CalendarSync:
    api = GoogleCalendarAPI(load_refresh_token(session_factory, clinic_id))
    # уведомления пациентам/админу о конфликт-переносах — через бота клиники
    with tenant_transaction(session_factory, clinic_id) as session:
        row = session.execute(
            text("SELECT tg_bot_token_encrypted, tg_admin_chat_id "
                 "FROM clinic WHERE id = :id"),
            {"id": clinic_id},
        ).one()
    tg_api = notifier = None
    if row.tg_bot_token_encrypted:
        tg_api = TelegramAPI(decrypt_text(row.tg_bot_token_encrypted))
        notifier = TelegramEscalation(tg_api, row.tg_admin_chat_id)
    return CalendarSync(session_factory, clinic_id, api=api,
                        notifier=notifier, tg_api=tg_api)


def doctors_with_calendars(session_factory, clinic_id) -> list[uuid.UUID]:
    with tenant_transaction(session_factory, clinic_id) as session:
        return list(session.execute(
            text("SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL")
        ).scalars())


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Google Calendar клиники")
    parser.add_argument("--clinic", required=True, type=uuid.UUID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="один прогон и выход")
    mode.add_argument("--interval", type=int, default=60,
                      help="период цикла в секундах (по умолчанию 60)")
    args = parser.parse_args()
    if not os.environ.get("NAVBAT_ENC_KEY"):
        sys.exit("[FAIL] NAVBAT_ENC_KEY не задан")

    session_factory = make_session_factory(make_app_engine())
    sync = build_sync(session_factory, args.clinic)

    def run_pass() -> None:
        for doctor_id in doctors_with_calendars(session_factory, args.clinic):
            try:
                sync.sync_doctor(doctor_id)
            except Exception:
                # один врач не должен ронять цикл остальных
                log.exception("sync врача %s упал", doctor_id)

    if args.once:
        run_pass()
        return 0
    stop = threading.Event()
    log.info("цикл синхронизации каждые %d с (Ctrl+C — выход)", args.interval)
    try:
        while not stop.is_set():
            run_pass()
            stop.wait(args.interval)
    except KeyboardInterrupt:
        log.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx печатает полный URL запроса — в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(main())
