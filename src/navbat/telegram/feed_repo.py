"""Данные карточки ленты записей — по appointment_id и списком на день.

Лента шлёт владельцу карточку в момент события (запись, отмена, перенос),
экран «Сегодня» показывает те же карточки за день. Собирать их обязан ОДИН
слой: точки события живут в диалоге, напоминаниях и календарном синке, и
знать про шифрование имён, рабочие часы клиники и статус привязки пациента
им незачем. Функции работают внутри tenant_transaction (RLS по clinic_id).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.crypto import decrypt_text
from navbat.dialog import clinic_repo, doctors_repo
from navbat.scheduling.calendar_rules import outside_open_hours


@dataclass(frozen=True)
class Card:
    """Запись глазами владельца: кто, к кому, когда и в каком состоянии."""

    start: datetime
    service_key: str | None      # canonical-ключ услуги; NULL у записи без услуги
    doctor_name: str | None
    patient_name: str | None     # None — пациент не назвался (запись с улицы, /forget)
    patient_phone: str | None
    tg_chat_id: int | None
    created_at: datetime
    status: str
    confirm_status: str | None  # 'confirmed' — пациент нажал «Приду»
    # напоминание по записи уже ушло: «молчит» = отправлено, но не подтверждено
    reminder_sent: bool


# Проекция карточки: одна на обоих потребителей (событие ленты и день
# целиком). Копия SELECT'а разъехалась бы на первой же правке — карточка
# события и строка дня обязаны показывать владельцу одно и то же.
# Пациент ищется по patient_id, а при пустом — по чату записи: привязка
# пациента к записи идёт ОТДЕЛЬНОЙ транзакцией уже после confirm
# (booking_flow: транзакция движка держит ту же строку), и первая запись
# нового пациента иначе уходила бы владельцу «без имени».
# У чата НЕТ UNIQUE на пациента (записывают себя, потом ребёнка), поэтому
# фолбэк сортирует кандидатов по времени последней записи ЭТОГО чата под
# ними: «первый по uuid» показывал владельцу чужое имя и чужой телефон.
# Один LATERAL, а не COALESCE двух: карточка обязана взять имя и телефон
# ОДНОЙ строки, поколоночная склейка дала бы имя одного пациента с номером
# другого, когда у первого номера нет. Записей у чата ещё нет (самая первая,
# привязка не случилась) — max() пуст, NULLS LAST возвращает прежний порядок
# по pt.id, и он верен: пациент у чата ровно один, только что созданный.
_CARD_QUERY = """
    SELECT lower(a.time_range) AS start, a.created_at, a.status,
           a.tg_chat_id, s.name AS service, a.confirm_status,
           -- «молчит» не хранится, а выводится: напоминание ушло, отметки нет.
           -- Отдельное поле состояния пришлось бы поддерживать фоном, и оно
           -- расходилось бы с фактом отправки
           EXISTS (SELECT 1 FROM reminder r
                   WHERE r.appointment_id = a.id AND r.status = 'sent'
                  ) AS reminder_sent,
           d.name_encrypted AS doctor_encrypted,
           p.name_encrypted AS patient_encrypted, p.phone_encrypted
    FROM appointment a
    LEFT JOIN service s ON s.id = a.service_id
    LEFT JOIN doctor d ON d.id = a.doctor_id
    LEFT JOIN LATERAL (
        SELECT pt.name_encrypted, pt.phone_encrypted
        FROM patient pt
        WHERE pt.id = a.patient_id
           OR (a.patient_id IS NULL
               AND pt.tg_chat_id = a.tg_chat_id)
        ORDER BY (SELECT max(a2.created_at)
                  FROM appointment a2
                  WHERE a2.patient_id = pt.id
                    AND a2.tg_chat_id = a.tg_chat_id) DESC NULLS LAST,
                 pt.id
        LIMIT 1
    ) p ON true
"""


def appointment_card(session: Session, appointment_id: uuid.UUID | str
                     ) -> Card | None:
    """Данные карточки по записи; None — записи уже нет.

    Имя врача, имя и телефон пациента расшифровываются здесь же: админ-чат —
    тот же доверенный канал, что событие календаря врача (docs/PRIVACY.md),
    и владельцу нужен не хэш, а человек, которому можно позвонить.
    """
    row = session.execute(
        text(_CARD_QUERY + "WHERE a.id = CAST(:id AS uuid)"),
        {"id": str(appointment_id)},
    ).one_or_none()
    return _card(row) if row is not None else None


def day_cards(session: Session, day: date, tz: ZoneInfo) -> list[Card]:
    """Живые приёмы одного дня клиники по возрастанию времени (экран
    «Сегодня» и утренняя сводка).

    День считается в локали клиники, а не сервера: база живёт в UTC, и
    ташкентский вечер иначе уезжал бы в завтрашний список. Прошедшие часы
    из дня не вычёркиваются — владельцу нужен день целиком, включая тех,
    кто уже пришёл.
    """
    rows = session.execute(
        text(_CARD_QUERY + "WHERE a.status = 'booked' "
                           "  AND (lower(a.time_range) AT TIME ZONE :tz)::date "
                           "      = :day "
                           "ORDER BY lower(a.time_range)"),
        {"tz": tz.key, "day": day},
    ).all()
    return [_card(row) for row in rows]


def _card(row) -> Card:
    return Card(
        start=row.start,
        service_key=row.service,
        doctor_name=_decrypted(row.doctor_encrypted),
        patient_name=_decrypted(row.patient_encrypted),
        patient_phone=_decrypted(row.phone_encrypted),
        tg_chat_id=row.tg_chat_id,
        created_at=row.created_at,
        status=row.status,
        confirm_status=row.confirm_status,
        reminder_sent=row.reminder_sent,
    )


def is_after_hours(session: Session, moment: datetime, tz: ZoneInfo) -> bool:
    """Момент вне рабочего окна клиники — пометка 🌙 у карточки.

    Тот же предикат, что считает after_hours_booked в /stats: «бот записал,
    пока клиника спала» в ленте и в сводке обязано означать одно и то же.
    """
    day = moment.astimezone(tz).date()
    return outside_open_hours(moment, doctors_repo.working_intervals(session),
                              clinic_repo.holidays_on(session, day), tz)


def _decrypted(value: str | None) -> str | None:
    return decrypt_text(value) if value else None
