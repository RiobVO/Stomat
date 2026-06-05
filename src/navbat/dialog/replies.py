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
    """contact_request, buttons и menu взаимоисключающие:
    в Telegram reply_markup один.

    contact_request — label кнопки «Поделиться контактом» (ReplyKeyboardMarkup,
    request_contact=True); menu — ряды label'ов постоянной reply-клавиатуры
    главного меню.
    """
    text: str
    buttons: tuple[Button, ...] = ()
    contact_request: str | None = None
    menu: tuple[tuple[str, ...], ...] | None = None


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
        "ru": "Нажмите кнопку ниже — она отправит ваш номер телефона:",
        "uz": "Pastdagi tugmani bosing — u telefon raqamingizni yuboradi:",
    },
    "press_contact_button": {
        "ru": "Чтобы оставить номер, нажмите кнопку ниже:",
        "uz": "Raqam qoldirish uchun pastdagi tugmani bosing:",
    },
    "foreign_contact": {
        "ru": "Это контакт другого человека. Нажмите кнопку — она отправит "
              "ваш собственный номер:",
        "uz": "Bu boshqa odamning kontakti. Tugmani bosing — u o'zingizning "
              "raqamingizni yuboradi:",
    },
    "btn_share_contact": {
        "ru": "📱 Отправить мой номер",
        "uz": "📱 Raqamimni yuborish",
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
    "price_answer": {
        "ru": "«{service}» — {price} сум.",
        "uz": "«{service}» — {price} so'm.",
    },
    "price_unknown": {
        "ru": "Цену на «{service}» уточнит администратор.",
        "uz": "«{service}» narxini administrator aniqlashtiradi.",
    },
    "faq_fallback": {
        "ru": "Это уточнит администратор — я передал ему ваш вопрос.",
        "uz": "Buni administrator aniqlashtiradi — savolingizni unga uzatdim.",
    },
    "cancel_confirm_q": {
        "ru": "Отменить вашу запись на {when}?",
        "uz": "{when} kungi yozuvingizni bekor qilaymi?",
    },
    "cancel_done": {
        "ru": "Запись отменена. Будем рады записать вас снова.",
        "uz": "Yozuv bekor qilindi. Sizni yana yozishdan xursand bo'lamiz.",
    },
    "cancel_kept": {
        "ru": "Хорошо, запись остаётся в силе.",
        "uz": "Yaxshi, yozuv o'z kuchida qoladi.",
    },
    "cancel_none": {
        "ru": "Активной записи не нашёл. Хотите записаться?",
        "uz": "Faol yozuv topilmadi. Yozilishni xohlaysizmi?",
    },
    "resched_none": {
        "ru": "Активной записи для переноса не нашёл. Хотите записаться?",
        "uz": "Ko'chirish uchun faol yozuv topilmadi. Yozilishni xohlaysizmi?",
    },
    "resched_done": {
        "ru": "Перенёс вашу запись на {when}. Ждём вас!",
        "uz": "Yozuvingizni {when} ga ko'chirdim. Sizni kutamiz!",
    },
    "btn_other_time": {"ru": "Другое время", "uz": "Boshqa vaqt"},
    "btn_today": {"ru": "Сегодня", "uz": "Bugun"},
    "btn_tomorrow": {"ru": "Завтра", "uz": "Ertaga"},
    "btn_after_tomorrow": {"ru": "Послезавтра", "uz": "Indinga"},
    "btn_yes": {"ru": "Да, отменить", "uz": "Ha, bekor qilish"},
    "btn_no": {"ru": "Нет, оставить", "uz": "Yo'q, qoldirish"},
    "reminder": {
        "ru": "Напоминаем: вы записаны на {service} {when}. Ждём вас!",
        "uz": "Eslatamiz: siz {service} uchun {when} ga yozilgansiz. "
              "Sizni kutamiz!",
    },
    "btn_attend": {"ru": "✓ Приду", "uz": "✓ Kelaman"},
    "btn_remind_cancel": {"ru": "Отменить запись", "uz": "Yozuvni bekor qilish"},
    "attend_ok": {
        "ru": "Отлично, ждём вас!",
        "uz": "Ajoyib, sizni kutamiz!",
    },
    "rate_limited": {
        "ru": "Слишком много сообщений подряд — сделайте небольшую паузу, "
              "и я отвечу.",
        "uz": "Juda ko'p xabar yubordingiz — biroz kuting, javob beraman.",
    },
    "greeting": {
        "ru": "Здравствуйте! Я виртуальный администратор клиники «{clinic}»: "
              "помогу записаться, перенести или отменить приём. "
              "По медицинским вопросам ответит врач.",
        "uz": "Assalomu alaykum! Men «{clinic}» klinikasining virtual "
              "administratoriman: qabulga yozilish, ko'chirish yoki bekor "
              "qilishda yordam beraman. Tibbiy savollarga shifokor javob beradi.",
    },
    "stale_button": {
        "ru": "Эта кнопка устарела.",
        "uz": "Bu tugma eskirgan.",
    },
    "conflict_moved": {
        "ru": "К сожалению, время {old} стало недоступно — перенёс вашу запись "
              "на {new}. Если не подходит, выберите другое:",
        "uz": "Afsuski, {old} vaqti band bo'lib qoldi — yozuvingizni {new} ga "
              "ko'chirdim. To'g'ri kelmasa, boshqasini tanlang:",
    },
    "conflict_cancelled": {
        "ru": "К сожалению, время {old} стало недоступно, а свободного времени "
              "в ближайшие дни нет — запись отменена. Напишите, и подберём новое.",
        "uz": "Afsuski, {old} vaqti band bo'lib qoldi, yaqin kunlarda bo'sh vaqt "
              "yo'q — yozuv bekor qilindi. Yozing, yangisini topamiz.",
    },
    "text_only": {
        "ru": "Пока я понимаю только текст — напишите, пожалуйста, словами.",
        "uz": "Hozircha faqat matnni tushunaman — iltimos, so'z bilan yozing.",
    },
    # намеренно двуязычный в обоих вариантах — экран выбора языка должен
    # читаться до того, как язык известен; не «локализовать»
    "choose_lang": {
        "ru": "Tilni tanlang / Выберите язык:",
        "uz": "Tilni tanlang / Выберите язык:",
    },
    "menu_hint": {
        "ru": "Выберите действие или напишите своими словами:",
        "uz": "Amalni tanlang yoki o'z so'zlaringiz bilan yozing:",
    },
    "lang_changed": {
        "ru": "Язык переключён на русский.",
        "uz": "Til o'zbek tiliga o'zgartirildi.",
    },
    "price_header": {"ru": "Наши цены:", "uz": "Narxlarimiz:"},
    "price_line": {
        "ru": "• {service} — {price} сум",
        "uz": "• {service} — {price} so'm",
    },
    "price_line_unknown": {
        "ru": "• {service} — цену уточнит администратор",
        "uz": "• {service} — narxini administrator aniqlashtiradi",
    },
    "price_empty": {
        "ru": "Прайс уточнит администратор.",
        "uz": "Narxlarni administrator aniqlashtiradi.",
    },
    "btn_menu_book": {"ru": "📅 Записаться", "uz": "📅 Yozilish"},
    "btn_menu_resched": {"ru": "🔄 Перенести", "uz": "🔄 Ko'chirish"},
    "btn_menu_cancel": {"ru": "❌ Отменить", "uz": "❌ Bekor qilish"},
    "btn_menu_prices": {"ru": "💰 Цены", "uz": "💰 Narxlar"},
    "btn_menu_lang": {"ru": "🌐 Til / Язык", "uz": "🌐 Til / Язык"},
    # кнопки экрана выбора языка (inline, не reply-меню)
    "btn_lang_uz": {"ru": "O'zbekcha", "uz": "O'zbekcha"},
    "btn_lang_ru": {"ru": "Русский", "uz": "Русский"},
}


def t(key: str, lang: str, **kwargs) -> str:
    """Шаблон по ключу и языку; mixed заранее сведён к ru на уровне FSM."""
    return TEMPLATES[key][lang].format(**kwargs)


def service_label(key: str, lang: str) -> str:
    labels = SERVICE_LABELS.get(key)
    return labels[lang] if labels else key


def menu_rows(lang: str) -> tuple[tuple[str, ...], ...]:
    """Ряды постоянной reply-клавиатуры главного меню."""
    return (
        (t("btn_menu_book", lang),),
        (t("btn_menu_resched", lang), t("btn_menu_cancel", lang)),
        (t("btn_menu_prices", lang), t("btn_menu_lang", lang)),
    )
