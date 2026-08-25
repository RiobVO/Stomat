"""Экран «Сегодня»: приёмы дня клиники одним списком.

Клиника без Google Calendar видела только события ленты по одному —
собирать из них день приходилось в голове. Здесь тот же приём, что в
dialog/calendar_view: чистый рендер, данные приходят готовыми (feed_repo —
единственный, кто знает про шифрование и локаль клиники), своего SQL тут нет.
Экран читают двое: /today и кнопка консоли (по запросу владельца) и утренняя
сводка (сама в 08:30) — текст обязан быть один и тот же.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from navbat.dialog.replies import service_label
from navbat.telegram import feed_repo
from navbat.telegram.admin_texts import at


def render_today(session: Session, lang: str, tz: ZoneInfo) -> str:
    """День клиники на СЕГОДНЯ: шапка с датой и числом приёмов + строки."""
    today = datetime.now(tz).date()
    return render_day(feed_repo.day_cards(session, today, tz), today, lang, tz)


def render_day(cards, day: date, lang: str, tz: ZoneInfo) -> str:
    """Тот же экран из УЖЕ собранных карточек.

    Утренняя сводка идёт веером по админ-чатам с разными языками: список дня
    для неё один, а рендеров столько, сколько языков у получателей."""
    if not cards:
        return at("today_empty", lang)
    lines = [at("today_header", lang, date=f"{day:%d.%m}", count=len(cards))]
    for card in cards:
        lines.append(at(
            "today_line", lang,
            time=f"{card.start.astimezone(tz):%H:%M}",
            # пациента может не быть (приём с улицы, /forget), телефона —
            # тоже: строка дня от этого не исчезает, врач всё равно ждёт
            patient=card.patient_name or at("feed_no_name", lang),
            phone=card.patient_phone or at("today_no_phone", lang),
            service=(service_label(card.service_key, lang) if card.service_key
                     else at("feed_no_service", lang)),
            doctor=card.doctor_name or at("doc_noname", lang),
        ))
    return "\n".join(lines)
