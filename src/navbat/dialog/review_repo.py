"""Журнал оценок приёма (review) из диалогового слоя — тонкий слой данных, как
recall_repo: в сценариях нет сырого SQL. Просьбу об оценке рассылает фон
(reminders), сюда приходит только ответ пациента. Функции работают ВНУТРИ
переданной session (tenant_transaction открывает вызывающий, RLS по clinic_id).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Просьба об оценке живёт неделю. Строка журнала хранится полгода (ретеншен),
# а сообщение со звёздами не исчезает из чата вообще: без срока тап через три
# месяца записал бы оценку давно забытому приёму и при 1–3 поднял бы владельца
# алертом о том, чего он уже не исправит. Оценка осмысленна по свежим следам;
# просроченная кнопка уходит stale-путём — rated_at у неё пуст, и is_rated
# по-прежнему отличает «уже оценено» от «протухло».
REVIEW_RATE_WINDOW_DAYS = 7


def rate(session: Session, appointment_id: str, tg_chat_id: int,
         rating: int) -> bool:
    """Поставить оценку приёму; False — ставить некуда, уже стоит или поздно.

    Субъект приходит в самой кнопке (она висит в чате), поэтому проверяется
    здесь же: callback_data — вход от клиента, и чужой id внутри той же
    клиники ставил бы оценку чужому приёму (RLS изолирует клиники, но не
    пациентов). Свежесть — вторая половина того же инварианта:
    REVIEW_RATE_WINDOW_DAYS отсекает тап по кнопке, пролежавшей в чате
    месяцами. `rated_at IS NULL` — оценка одна на приём: кнопки гаснут
    edit'ом, но старое сообщение переживает и правку (клиент офлайн), и второй
    тап переписывал бы владельцу не то, что пациент нажал первым.
    """
    return session.execute(
        text("UPDATE review SET rating = :rating, rated_at = now() "
             "WHERE appointment_id = CAST(:src AS uuid) AND tg_chat_id = :chat "
             "AND rated_at IS NULL "
             "AND requested_at > now() - make_interval(days => :fresh)"),
        {"rating": rating, "src": appointment_id, "chat": tg_chat_id,
         "fresh": REVIEW_RATE_WINDOW_DAYS},
    ).rowcount > 0


def is_rated(session: Session, appointment_id: str, tg_chat_id: int) -> bool:
    """Оценка ЭТОГО чата по приёму уже стоит.

    Отличает повторный тап («уже учтено») от мёртвой кнопки: у обоих rate()
    вернул False, но пациенту это два разных ответа.
    """
    return session.execute(
        text("SELECT 1 FROM review "
             "WHERE appointment_id = CAST(:src AS uuid) AND tg_chat_id = :chat "
             "AND rated_at IS NOT NULL"),
        {"src": appointment_id, "chat": tg_chat_id},
    ).scalar_one_or_none() is not None
