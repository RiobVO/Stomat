"""Модель ответа бота + шаблоны uz/ru.

Кнопки — чистая модель (label + машинный action); рендер в Telegram —
инкремент 3, консоль показывает их нумерованным списком.
Узбекские строки — черновик, проверить носителем до пилота (BRIEF, разд. «Язык»).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Button:
    label: str
    action: str


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: tuple[Button, ...] = ()


MEDICAL_DISCLAIMER = {
    "ru": "Я виртуальный администратор и не даю медицинских советов — "
          "точный ответ даст врач на приёме.",
    "uz": "Men virtual administratorman, tibbiy maslahat bera olmayman — "
          "aniq javobni shifokor qabulda beradi.",
}

SERVICE_LABELS = {
    "cleaning": {"ru": "Чистка", "uz": "Tish tozalash"},
    "filling": {"ru": "Пломба", "uz": "Plomba"},
    "extraction": {"ru": "Удаление", "uz": "Tish oldirish"},
    "implant": {"ru": "Имплант", "uz": "Implant"},
    "crown": {"ru": "Коронка", "uz": "Koronka"},
    "whitening": {"ru": "Отбеливание", "uz": "Oqartirish"},
    "braces": {"ru": "Брекеты", "uz": "Breket"},
    "checkup": {"ru": "Осмотр", "uz": "Ko'rik"},
    "xray": {"ru": "Снимок", "uz": "Rentgen"},
}

TEMPLATES = {
    "ask_service": {
        "ru": "На какую услугу вас записать?",
        "uz": "Qaysi xizmatga yozib qo'yay?",
    },
    "ask_date": {
        "ru": "На какой день вам удобно?",
        "uz": "Qaysi kun sizga qulay?",
    },
    "offer_slots": {
        "ru": "Свободное время на {date}:",
        "uz": "{date} kuni bo'sh vaqtlar:",
    },
    "offer_slots_other_day": {
        "ru": "На {asked} свободного времени нет. Ближайшее — {date}:",
        "uz": "{asked} kuni bo'sh vaqt yo'q. Eng yaqini — {date}:",
    },
    "no_slots_at_all": {
        "ru": "В ближайшие две недели свободного времени нет — передаю администратору.",
        "uz": "Yaqin ikki haftada bo'sh vaqt yo'q — administratorga uzataman.",
    },
    "doctor_not_found": {
        "ru": "Врача с таким именем не нашёл, показываю всё свободное время.",
        "uz": "Bunday ismli shifokor topilmadi, barcha bo'sh vaqtlarni ko'rsataman.",
    },
    "ask_name": {
        "ru": "Как вас зовут?",
        "uz": "Ismingiz nima?",
    },
    "ask_phone": {
        "ru": "Оставьте номер телефона (например, 90 123-45-67):",
        "uz": "Telefon raqamingizni qoldiring (masalan, 90 123-45-67):",
    },
    "bad_phone": {
        "ru": "Не разобрал номер. Напишите в формате 90 123-45-67.",
        "uz": "Raqamni tushunmadim. 90 123-45-67 ko'rinishida yozing.",
    },
    "booked": {
        "ru": "Записал: {service}, {when}{doctor}. Ждём вас!",
        "uz": "Yozib qo'ydim: {service}, {when}{doctor}. Sizni kutamiz!",
    },
    "hold_expired": {
        "ru": "Бронь на выбранное время истекла. Вот свежие варианты:",
        "uz": "Tanlangan vaqt broni tugadi. Mana yangi variantlar:",
    },
    "slot_taken": {
        "ru": "Это время только что заняли. Вот свежие варианты:",
        "uz": "Bu vaqt hozirgina band bo'ldi. Mana yangi variantlar:",
    },
    "reask": {
        "ru": "Не понял вас. Напишите, пожалуйста, иначе — например: "
              "«запись на чистку завтра».",
        "uz": "Tushunmadim. Boshqacha yozib ko'ring — masalan: "
              "«ertaga tish tozalashga yozilmoqchiman».",
    },
    "escalated": {
        "ru": "Передаю администратору — он ответит вам здесь в ближайшее время.",
        "uz": "Administratorga uzatdim — u tez orada shu yerda javob beradi.",
    },
    "other_fallback": {
        "ru": "Я помогу записаться на приём: напишите услугу и удобный день.",
        "uz": "Qabulga yozilishga yordam beraman: xizmat va qulay kunni yozing.",
    },
    "btn_other_time": {"ru": "Другое время", "uz": "Boshqa vaqt"},
    "btn_today": {"ru": "Сегодня", "uz": "Bugun"},
    "btn_tomorrow": {"ru": "Завтра", "uz": "Ertaga"},
    "btn_after_tomorrow": {"ru": "Послезавтра", "uz": "Indinga"},
}


def t(key: str, lang: str, **kwargs) -> str:
    """Шаблон по ключу и языку; mixed заранее сведён к ru на уровне FSM."""
    return TEMPLATES[key][lang].format(**kwargs)


def service_label(key: str, lang: str) -> str:
    labels = SERVICE_LABELS.get(key)
    return labels[lang] if labels else key
