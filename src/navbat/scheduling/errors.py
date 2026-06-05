"""Доменные ошибки scheduling engine."""


class SchedulingError(Exception):
    """Базовая ошибка движка."""


class SlotTakenError(SchedulingError):
    """Слот занят: exclusion constraint отклонил вставку/перенос."""


class HoldExpiredError(SchedulingError):
    """Hold протух — подтверждение невозможно."""


class InvalidSlotError(SchedulingError):
    """Запрошенное время не является валидным свободным слотом."""


class AppointmentNotFoundError(SchedulingError):
    """Запись не найдена (или не видна в текущем тенант-контексте)."""


class DuplicateMessageError(SchedulingError):
    """Дубль Telegram-сообщения: (tg_chat_id, tg_message_id) уже обработан."""
