"""Онбординг клиники без ручного SQL.

    python -m navbat.onboard --demo                                  # демо-клиника
    python -m navbat.onboard --clinic <uuid> --tg-token <token> --admin-chat <id>
    python -m navbat.onboard --clinic <uuid> --doctor <uuid> --calendar <gcal-id>
    python -m navbat.onboard --clinic <uuid> --list                  # врачи/услуги

Секреты пишутся шифртекстом (NAVBAT_ENC_KEY); webhook-secret генерируется
автоматически при записи токена.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import sys
import uuid

from sqlalchemy import text

from navbat.crypto import encrypt_text
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction

log = logging.getLogger("navbat.onboard")

# фиксированный tenant демо-клиники: сидинг идемпотентен от запуска к запуску
DEMO_CLINIC_ID = uuid.UUID("00000000-0000-4000-8000-000000000d31")
# dev-ключ шифрования: только локально, прод задаёт NAVBAT_ENC_KEY явно
DEV_ENC_KEY = base64.b64encode(b"demo-key-32-bytes-padded-00000!!").decode()

WORKING_INTERVALS = {
    day: [["09:00", "13:00"], ["14:00", "18:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat")
}
DOCTORS = ("Akmal aka", "Dilnoza opa")
SERVICES = {  # (длительность мин, цена сум|None)
    "cleaning": (30, 350_000), "filling": (60, 400_000), "extraction": (30, None),
    "implant": (90, None), "crown": (60, None), "whitening": (60, 500_000),
    "braces": (60, None), "checkup": (30, 150_000), "xray": (15, 80_000),
}


def seed_demo_clinic(session_factory) -> None:
    """Создаёт демо-клинику с врачами и услугами, если её ещё нет."""
    with tenant_transaction(session_factory, DEMO_CLINIC_ID) as session:
        exists = session.execute(
            text("SELECT 1 FROM clinic WHERE id = :id"), {"id": DEMO_CLINIC_ID}
        ).scalar_one_or_none()
        if exists:
            return
        session.execute(
            text("INSERT INTO clinic (id, name, salt, timezone) "
                 "VALUES (:id, 'Navbat Demo', 'demo-salt', 'Asia/Tashkent')"),
            {"id": DEMO_CLINIC_ID},
        )
        for doctor in DOCTORS:
            session.execute(
                text("INSERT INTO doctor (clinic_id, name_encrypted, working_intervals, "
                     "buffer_min) VALUES (:c, :name, :wi, 10)"),
                {"c": DEMO_CLINIC_ID, "name": encrypt_text(doctor),
                 "wi": json.dumps(WORKING_INTERVALS)},
            )
        for name, (duration, price) in SERVICES.items():
            session.execute(
                text("INSERT INTO service (clinic_id, name, duration_min, price) "
                     "VALUES (:c, :name, :dur, :price)"),
                {"c": DEMO_CLINIC_ID, "name": name, "dur": duration, "price": price},
            )
    log.info("демо-клиника создана: 2 врача, %d услуг", len(SERVICES))


def set_telegram(session_factory, clinic_id: uuid.UUID, token: str,
                 admin_chat: int | None) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :token, "
                 "tg_admin_chat_id = COALESCE(:admin, tg_admin_chat_id), "
                 "tg_webhook_secret = COALESCE(tg_webhook_secret, :secret) "
                 "WHERE id = :id"),
            {"token": encrypt_text(token), "admin": admin_chat,
             "secret": secrets.token_urlsafe(32), "id": clinic_id},
        )
    print(f"[OK] токен бота записан для клиники {clinic_id}")


def bind_calendar(session_factory, clinic_id: uuid.UUID, doctor_id: uuid.UUID,
                  calendar_id: str) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE doctor SET gcal_calendar_id = :cal WHERE id = :doc "
                 "RETURNING id"),
            {"cal": calendar_id, "doc": doctor_id},
        ).scalar_one_or_none()
    if updated is None:
        sys.exit(f"[FAIL] врач {doctor_id} не найден в клинике {clinic_id}")
    print(f"[OK] календарь {calendar_id} привязан к врачу {doctor_id}")


def show_clinic(session_factory, clinic_id: uuid.UUID) -> None:
    from navbat.crypto import decrypt_text

    with tenant_transaction(session_factory, clinic_id) as session:
        clinic = session.execute(
            text("SELECT name, tg_bot_token_encrypted IS NOT NULL AS has_token, "
                 "tg_admin_chat_id, gcal_refresh_token_encrypted IS NOT NULL AS has_gcal "
                 "FROM clinic WHERE id = :id"), {"id": clinic_id},
        ).one_or_none()
        if clinic is None:
            sys.exit(f"[FAIL] клиника {clinic_id} не найдена")
        doctors = session.execute(
            text("SELECT id, name_encrypted, gcal_calendar_id FROM doctor ORDER BY id")
        ).all()
        services = session.execute(
            text("SELECT name, duration_min, price FROM service ORDER BY name")
        ).all()
    print(f"Клиника: {clinic.name}")
    print(f"  TG-токен: {'есть' if clinic.has_token else 'НЕТ'}; "
          f"админ-чат: {clinic.tg_admin_chat_id or 'НЕТ'}; "
          f"GCal: {'есть' if clinic.has_gcal else 'НЕТ'}")
    print("Врачи:")
    for doctor in doctors:
        name = decrypt_text(doctor.name_encrypted) if doctor.name_encrypted else "(без имени)"
        print(f"  {doctor.id}  {name}  календарь: {doctor.gcal_calendar_id or '—'}")
    print("Услуги:")
    for service in services:
        price = f"{int(service.price):,}".replace(",", " ") if service.price else "—"
        print(f"  {service.name}: {service.duration_min} мин, {price} сум")


def main() -> int:
    parser = argparse.ArgumentParser(description="Онбординг клиники Navbat")
    parser.add_argument("--demo", action="store_true", help="создать демо-клинику")
    parser.add_argument("--clinic", type=uuid.UUID)
    parser.add_argument("--tg-token")
    parser.add_argument("--admin-chat", type=int)
    parser.add_argument("--doctor", type=uuid.UUID)
    parser.add_argument("--calendar")
    parser.add_argument("--list", action="store_true", help="показать клинику")
    args = parser.parse_args()

    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    session_factory = make_session_factory(make_app_engine())

    if args.demo:
        seed_demo_clinic(session_factory)
        print(f"[OK] демо-клиника: {DEMO_CLINIC_ID}")
        print(f"Дальше: python -m navbat.onboard --clinic {DEMO_CLINIC_ID} "
              f"--tg-token <токен от @BotFather> --admin-chat <ваш chat id>")
        return 0
    if not args.clinic:
        parser.error("нужен --clinic (или --demo)")
    if args.tg_token:
        set_telegram(session_factory, args.clinic, args.tg_token, args.admin_chat)
        return 0
    if args.doctor and args.calendar:
        bind_calendar(session_factory, args.clinic, args.doctor, args.calendar)
        return 0
    if args.list:
        show_clinic(session_factory, args.clinic)
        return 0
    parser.error("укажите действие: --tg-token | --doctor+--calendar | --list")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
