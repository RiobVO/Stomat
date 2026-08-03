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

# значки состояния подтверждения перед временем строки
_CONFIRMED = "✅ "
_SILENT = "⏳ "


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
    silent = 0
    for card in cards:
        mark = _mark(card)
        if mark == _SILENT:
            silent += 1
        lines.append(mark + at(
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
    if silent:
        # сразу под шапкой, а не в конце: список дня длинный, и главное
        # действие владельца («позвонить троим») не должно ждать прокрутки
        lines.insert(1, at("today_call_hint", lang, count=silent))
    return "\n".join(lines)


def _mark(card) -> str:
    """Значок состояния перед временем: подтвердил / молчит / не спрашивали.

    Пустой значок — напоминание ещё не уходило: звонить такому пациенту
    незачем, он просто не получал вопроса."""
    if card.confirm_status == "confirmed":
        return _CONFIRMED
    return _SILENT if card.reminder_sent else ""
