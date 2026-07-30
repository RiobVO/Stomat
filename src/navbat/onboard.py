"""Онбординг клиники без ручного SQL.

    python -m navbat.onboard --demo                                  # демо-клиника
    python -m navbat.onboard --new-clinic "Smile Dent" [--tz Asia/Tashkent]
    python -m navbat.onboard --clinic <uuid> --add-doctor "Иванов" [--schedule-json '{...}'] [--buffer 10]
    python -m navbat.onboard --clinic <uuid> --add-service cleaning --duration 30 --price 350000
    python -m navbat.onboard --clinic <uuid> --set-price cleaning --price 400000
    python -m navbat.onboard --clinic <uuid> --set-schedule <doctor-uuid> --schedule-json '{"mon":[["09:00","18:00"]]}'
    python -m navbat.onboard --clinic <uuid> --tg-token <token> --admin-chat <id>
    python -m navbat.onboard --clinic <uuid> --doctor <uuid> --calendar <gcal-id>
    python -m navbat.onboard --prompt-upload prompt.md --note "v2: ..."
    python -m navbat.onboard --clinic <uuid> --prompt-pin <N|file>
    python -m navbat.onboard --clinic <uuid> --list                  # врачи/услуги

Услуги — только канонические ключи (navbat.nlu.schema.SERVICE_KEYS), иначе
NLU не сможет на них сослаться. График: {день: [[HH:MM, HH:MM], ...]},
дни mon..sun.

Staging-процедура нового NLU-промпта (B.2): --prompt-upload → --demo
--prompt-pin N → перезапуск демо-бота → тест-диалоги → pin живым клиникам.
Выходные дни клиника закрывает сама командой /dayoff в админ-чате —
предзаполненного календаря праздников нет (решение 06.06.2026). Секреты
пишутся шифртекстом (NAVBAT_ENC_KEY); webhook-secret генерируется
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
from pathlib import Path

from sqlalchemy import text

from navbat.crypto import encrypt_text
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.envfile import load_env_file
from navbat.nlu.schema import SERVICE_KEYS

log = logging.getLogger("navbat.onboard")

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

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


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, _, m = str(s).partition(":")
    hh, mm = int(h), int(m)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"некорректное время: {s!r}")
    return hh, mm


def _validate_intervals(intervals: dict) -> dict:
    """Структура графика: {день: [[HH:MM, HH:MM], ...]}; день из _DAYS,
    интервал — пара валидного времени, начало < конца. Возвращает тот же
    словарь (для удобства вызова), бросает ValueError на кривом."""
    if not isinstance(intervals, dict) or not intervals:
        raise ValueError("график пуст или не словарь")
    for day, spans in intervals.items():
        if day not in _DAYS:
            raise ValueError(f"неизвестный день {day!r} (нужно из {_DAYS})")
        for span in spans:
            if len(span) != 2:
                raise ValueError(f"{day}: интервал должен быть [начало, конец], не {span}")
            if _parse_hhmm(span[0]) >= _parse_hhmm(span[1]):
                raise ValueError(f"{day}: начало {span[0]} не раньше конца {span[1]}")
    return intervals


def create_clinic(session_factory, name: str,
                  timezone: str = "Asia/Tashkent") -> uuid.UUID:
    """Создать клинику с криптослучайной солью (B3): без уникальной соли
    хэши телефонов пациентов обратимы перебором. Возвращает id."""
    clinic_id = uuid.uuid4()
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("INSERT INTO clinic (id, name, salt, timezone) "
                 "VALUES (:id, :name, :salt, :tz)"),
            {"id": clinic_id, "name": name,
             "salt": secrets.token_hex(32), "tz": timezone},
        )
    log.info("клиника создана: %s (%s)", name, clinic_id)
    return clinic_id


def add_doctor(session_factory, clinic_id: uuid.UUID, name: str,
               intervals: dict | None = None, buffer_min: int = 10) -> uuid.UUID:
    """Добавить врача. intervals — график {день: [[HH:MM, HH:MM]]};
    по умолчанию стандартная пн–сб 09–13/14–18. Возвращает id."""
    schedule = _validate_intervals(intervals) if intervals is not None else WORKING_INTERVALS
    doctor_id = uuid.uuid4()
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("INSERT INTO doctor (id, clinic_id, name_encrypted, "
                 "working_intervals, buffer_min) "
                 "VALUES (:id, :c, :name, :wi, :buf)"),
            {"id": doctor_id, "c": clinic_id, "name": encrypt_text(name),
             "wi": json.dumps(schedule), "buf": buffer_min},
        )
    return doctor_id


def add_service(session_factory, clinic_id: uuid.UUID, name: str,
                duration_min: int, price: int | None = None) -> uuid.UUID:
    """Добавить услугу. name — канонический ключ из SERVICE_KEYS (иначе
    NLU не сможет на неё сослаться). Дубль ключа в клинике — ошибка."""
    if name not in SERVICE_KEYS:
        raise ValueError(
            f"услуга {name!r} не из каталога: {', '.join(SERVICE_KEYS)}")
    service_id = uuid.uuid4()
    with tenant_transaction(session_factory, clinic_id) as session:
        if session.execute(
            text("SELECT 1 FROM service WHERE name = :n"), {"n": name}
        ).scalar_one_or_none() is not None:
            raise ValueError(f"услуга {name!r} уже есть в клинике")
        session.execute(
            text("INSERT INTO service (id, clinic_id, name, duration_min, price) "
                 "VALUES (:id, :c, :n, :dur, :price)"),
            {"id": service_id, "c": clinic_id, "n": name,
             "dur": duration_min, "price": price},
        )
    return service_id


def set_service_price(session_factory, clinic_id: uuid.UUID, name: str,
                      price: int | None) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE service SET price = :p WHERE name = :n RETURNING id"),
            {"p": price, "n": name},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"услуга {name!r} не найдена в клинике {clinic_id}")


def set_doctor_schedule(session_factory, clinic_id: uuid.UUID,
                        doctor_id: uuid.UUID, intervals: dict) -> None:
    schedule = _validate_intervals(intervals)
    with tenant_transaction(session_factory, clinic_id) as session:
        updated = session.execute(
            text("UPDATE doctor SET working_intervals = :wi WHERE id = :d "
                 "RETURNING id"),
            {"wi": json.dumps(schedule), "d": doctor_id},
        ).scalar_one_or_none()
    if updated is None:
        raise ValueError(f"врач {doctor_id} не найден в клинике {clinic_id}")


def add_admin(session_factory, clinic_id: uuid.UUID, chat_id: int) -> None:
    """Добавить админ-чат клинике (M4): получатель алертов + право на
    команды /stats /release /dayoff /dayopen /forget. Идемпотентно."""
    with tenant_transaction(session_factory, clinic_id) as session:
        if session.execute(
            text("SELECT 1 FROM clinic WHERE id = :id"), {"id": clinic_id}
        ).scalar_one_or_none() is None:
            raise ValueError(f"клиника {clinic_id} не найдена")
        session.execute(
            text("UPDATE clinic SET tg_admin_chat_ids = "
                 "array_append(tg_admin_chat_ids, CAST(:chat AS bigint)) "
                 "WHERE id = :id AND NOT (CAST(:chat AS bigint) = ANY(tg_admin_chat_ids))"),
            {"chat": chat_id, "id": clinic_id},
        )


def remove_admin(session_factory, clinic_id: uuid.UUID, chat_id: int) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        if session.execute(
            text("SELECT 1 FROM clinic WHERE id = :id"), {"id": clinic_id}
        ).scalar_one_or_none() is None:
            raise ValueError(f"клиника {clinic_id} не найдена")
        session.execute(
            text("UPDATE clinic SET tg_admin_chat_ids = "
                 "array_remove(tg_admin_chat_ids, CAST(:chat AS bigint)) "
                 "WHERE id = :id"),
            {"chat": chat_id, "id": clinic_id},
        )


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


def import_calendar(session_factory, clinic_id: uuid.UUID, sync) -> int:
    """Импорт существующих событий GCal для всех врачей с календарём (E.2).

    sync — CalendarSync (или фейк в тестах); ручные события станут
    gcal_import-записями, слоты под ними закроются.
    """
    with tenant_transaction(session_factory, clinic_id) as session:
        doctor_ids = session.execute(
            text("SELECT id FROM doctor WHERE gcal_calendar_id IS NOT NULL "
                 "ORDER BY id")
        ).scalars().all()
    for doctor_id in doctor_ids:
        sync.sync_doctor(doctor_id)
    return len(doctor_ids)


def upload_prompt(session_factory, body: str, note: str | None = None) -> int:
    """Новая версия NLU-промпта (глобальный каталог флота, не per-clinic)."""
    with session_factory() as session:
        version = session.execute(
            text("INSERT INTO nlu_prompt (body, note) VALUES (:body, :note) "
                 "RETURNING version"),
            {"body": body, "note": note},
        ).scalar_one()
        session.commit()
    return version


def pin_prompt(session_factory, clinic_id: uuid.UUID, version_arg: str) -> None:
    """Пин версии промпта клинике; «file» — откат на встроенный файл."""
    if version_arg == "file":
        version = None
    else:
        try:
            version = int(version_arg)
        except ValueError:
            sys.exit(f"[FAIL] --prompt-pin: номер версии или «file», "
                     f"не {version_arg!r}")
    with tenant_transaction(session_factory, clinic_id) as session:
        if version is not None:
            exists = session.execute(
                text("SELECT 1 FROM nlu_prompt WHERE version = :v"),
                {"v": version},
            ).scalar_one_or_none()
            if exists is None:
                sys.exit(f"[FAIL] промпт версии {version} не найден — "
                         f"сначала --prompt-upload")
        session.execute(
            text("UPDATE clinic SET nlu_prompt_version = :v WHERE id = :c"),
            {"v": version, "c": clinic_id},
        )
    label = f"версия {version}" if version else "встроенный файл"
    print(f"[OK] клиника {clinic_id}: NLU-промпт — {label}")


def set_telegram(session_factory, clinic_id: uuid.UUID, token: str,
                 admin_chat: int | None) -> None:
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :token, "
                 "tg_admin_chat_id = COALESCE(:admin, tg_admin_chat_id), "
                 # --admin-chat задаёт стартовый админ-чат (M4: список из одного);
                 # дальше добавлять/убирать через --add-admin/--remove-admin
                 "tg_admin_chat_ids = CASE WHEN :admin IS NOT NULL "
                 "    THEN ARRAY[:admin]::bigint[] ELSE tg_admin_chat_ids END, "
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
                 "tg_admin_chat_ids, gcal_refresh_token_encrypted IS NOT NULL AS has_gcal, "
                 "nlu_prompt_version "
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
    admins = ", ".join(str(c) for c in clinic.tg_admin_chat_ids) or "НЕТ"
    print(f"  TG-токен: {'есть' if clinic.has_token else 'НЕТ'}; "
          f"админ-чаты: {admins}; "
          f"GCal: {'есть' if clinic.has_gcal else 'НЕТ'}")
    prompt_label = (f"версия {clinic.nlu_prompt_version}"
                    if clinic.nlu_prompt_version else "встроенный файл")
    print(f"  NLU-промпт: {prompt_label}")
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
    parser.add_argument("--new-clinic", metavar="NAME", help="создать клинику")
    parser.add_argument("--tz", default="Asia/Tashkent", help="таймзона новой клиники")
    parser.add_argument("--add-doctor", metavar="NAME", help="добавить врача в --clinic")
    parser.add_argument("--buffer", type=int, default=10, help="буфер врача, мин")
    parser.add_argument("--schedule-json", metavar="JSON",
                        help="график {день:[[HH:MM,HH:MM]]} для --add-doctor/--set-schedule")
    parser.add_argument("--add-service", metavar="KEY",
                        help="добавить услугу (канонический ключ) в --clinic")
    parser.add_argument("--duration", type=int, help="длительность услуги, мин")
    parser.add_argument("--price", type=int, help="цена услуги, сум")
    parser.add_argument("--set-price", metavar="KEY", help="изменить цену услуги")
    parser.add_argument("--set-schedule", metavar="DOCTOR_UUID", type=uuid.UUID,
                        help="заменить график врача (с --schedule-json)")
    parser.add_argument("--add-admin", type=int, metavar="CHAT_ID",
                        help="добавить админ-чат клинике (алерты + команды)")
    parser.add_argument("--remove-admin", type=int, metavar="CHAT_ID",
                        help="убрать админ-чат клиники")
    parser.add_argument("--tg-token")
    parser.add_argument("--admin-chat", type=int)
    parser.add_argument("--doctor", type=uuid.UUID)
    parser.add_argument("--calendar")
    parser.add_argument("--prompt-upload", metavar="FILE",
                        help="загрузить новую версию NLU-промпта")
    parser.add_argument("--note", help="комментарий к версии промпта")
    parser.add_argument("--prompt-pin", metavar="N|file",
                        help="пин версии промпта клинике (file — встроенный)")
    parser.add_argument("--import-calendar", action="store_true",
                        help="импортировать существующие события GCal")
    parser.add_argument("--list", action="store_true", help="показать клинику")
    args = parser.parse_args()

    load_env_file()
    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    session_factory = make_session_factory(make_app_engine())

    if args.prompt_upload:
        body = Path(args.prompt_upload).read_text(encoding="utf-8")
        version = upload_prompt(session_factory, body, args.note)
        print(f"[OK] промпт загружен: версия {version}")
        return 0
    if args.demo:
        if args.prompt_pin:  # staging: пин новой версии на демо-клинику
            pin_prompt(session_factory, DEMO_CLINIC_ID, args.prompt_pin)
            return 0
        seed_demo_clinic(session_factory)
        print(f"[OK] демо-клиника: {DEMO_CLINIC_ID}")
        # токен из .env: восстановление после pytest — одна команда
        token = args.tg_token or os.environ.get("NAVBAT_TG_TOKEN")
        admin = args.admin_chat or os.environ.get("NAVBAT_TG_ADMIN_CHAT")
        if token:
            set_telegram(session_factory, DEMO_CLINIC_ID, token,
                         int(admin) if admin else None)
        else:
            print(f"Дальше: python -m navbat.onboard --clinic {DEMO_CLINIC_ID} "
                  f"--tg-token <токен от @BotFather> --admin-chat <ваш chat id>")
        return 0
    if args.new_clinic:
        clinic_id = create_clinic(session_factory, args.new_clinic, args.tz)
        print(f"[OK] клиника создана: {clinic_id}")
        print(f"Дальше: --clinic {clinic_id} --add-doctor <имя> [--schedule-json ...]; "
              f"--add-service <ключ> --duration <мин> [--price <сум>]; "
              f"--tg-token <токен> --admin-chat <id>")
        return 0
    if not args.clinic:
        parser.error("нужен --clinic (или --demo / --new-clinic)")
    if args.add_doctor:
        intervals = json.loads(args.schedule_json) if args.schedule_json else None
        try:
            doctor_id = add_doctor(session_factory, args.clinic, args.add_doctor,
                                   intervals, args.buffer)
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] врач добавлен: {doctor_id}")
        return 0
    if args.add_service:
        if args.duration is None:
            sys.exit("[FAIL] --add-service требует --duration")
        try:
            service_id = add_service(session_factory, args.clinic, args.add_service,
                                     args.duration, args.price)
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] услуга добавлена: {args.add_service} ({service_id})")
        return 0
    if args.set_price:
        if args.price is None:
            sys.exit("[FAIL] --set-price требует --price")
        try:
            set_service_price(session_factory, args.clinic, args.set_price, args.price)
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] цена {args.set_price}: {args.price} сум")
        return 0
    if args.set_schedule:
        if not args.schedule_json:
            sys.exit("[FAIL] --set-schedule требует --schedule-json")
        try:
            set_doctor_schedule(session_factory, args.clinic, args.set_schedule,
                                json.loads(args.schedule_json))
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] график врача {args.set_schedule} обновлён")
        return 0
    if args.add_admin is not None:
        try:
            add_admin(session_factory, args.clinic, args.add_admin)
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] админ-чат {args.add_admin} добавлен")
        return 0
    if args.remove_admin is not None:
        try:
            remove_admin(session_factory, args.clinic, args.remove_admin)
        except ValueError as exc:
            sys.exit(f"[FAIL] {exc}")
        print(f"[OK] админ-чат {args.remove_admin} убран")
        return 0
    if args.tg_token:
        set_telegram(session_factory, args.clinic, args.tg_token, args.admin_chat)
        return 0
    if args.doctor and args.calendar:
        bind_calendar(session_factory, args.clinic, args.doctor, args.calendar)
        return 0
    if args.prompt_pin:
        pin_prompt(session_factory, args.clinic, args.prompt_pin)
        return 0
    if args.import_calendar:
        from navbat.calendar.api import GoogleCalendarAPI
        from navbat.calendar.sync import CalendarSync
        from navbat.crypto import decrypt_text

        with tenant_transaction(session_factory, args.clinic) as session:
            token = session.execute(
                text("SELECT gcal_refresh_token_encrypted FROM clinic "
                     "WHERE id = :id"), {"id": args.clinic},
            ).scalar_one_or_none()
        if not token:
            sys.exit("[FAIL] Google Calendar не авторизован — сначала "
                     "python -m navbat.calendar.auth")
        # tg_api/notifier не нужны: ботовских записей на онбординге ещё нет,
        # конфликт-уведомления слать некому
        sync = CalendarSync(session_factory, args.clinic,
                            api=GoogleCalendarAPI(decrypt_text(token)))
        synced = import_calendar(session_factory, args.clinic, sync)
        print(f"[OK] импорт календаря: синк прогнан для {synced} врач(ей)")
        return 0
    if args.list:
        show_clinic(session_factory, args.clinic)
        return 0
    parser.error("укажите действие: --add-doctor | --add-service | --set-price "
                 "| --set-schedule | --tg-token | --doctor+--calendar "
                 "| --prompt-pin | --import-calendar | --list")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
