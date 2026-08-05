"""Модель ответа бота + шаблоны uz/ru.

Кнопки — чистая модель (label + машинный action); рендер в Telegram —
инкремент 3, консоль показывает их нумерованным списком.
Узбекские строки — черновик, проверить носителем до пилота (BRIEF, разд. «Язык»).
"""
from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class Button:
    label: str
    action: str


@dataclass(frozen=True)
class Reply:
    """contact_request, buttons/button_rows и menu взаимоисключающие:
    в Telegram reply_markup один.

    contact_request — label кнопки «Поделиться контактом» (ReplyKeyboardMarkup,
    request_contact=True); menu — ряды label'ов постоянной reply-клавиатуры
    главного меню. button_rows — многорядная inline-клавиатура (П-4, сетка
    календаря); flat buttons — одна колонка, как раньше. edit — адаптер
    редактирует сообщение-источник callback'а вместо отправки нового;
    toast — текст answerCallbackQuery (text=='' → сообщение не шлётся).
    """
    text: str
    buttons: tuple[Button, ...] = ()
    contact_request: str | None = None
    menu: tuple[tuple[str, ...], ...] | None = None
    button_rows: tuple[tuple[Button, ...], ...] = ()
    edit: bool = False
    toast: str | None = None


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
    "whitening": {"ru": "Отбеливание", "uz": "Tish oqartirish"},
    "braces": {"ru": "Брекеты", "uz": "Breket"},
    "checkup": {"ru": "Осмотр", "uz": "Ko'rik"},
    "xray": {"ru": "Снимок", "uz": "Rentgen"},
}

# эмодзи — ТОЛЬКО в кнопках выбора услуги (П-7); в текстах label чистый,
# иначе карточка записи получает двойной эмодзи («🦷 ✨ Чистка»)
SERVICE_EMOJI = {
    "cleaning": "✨", "filling": "🩹", "extraction": "🦷", "implant": "🔩",
    "crown": "👑", "whitening": "🌟", "braces": "😁", "checkup": "🔍",
    "xray": "🩻",
}

TEMPLATES = {
    "ask_service": {
        "ru": "🦷 <b>На какую услугу вас записать?</b>",
        "uz": "🦷 <b>Qaysi xizmatga yozib qo'yay?</b>",
    },
    "ask_date": {
        "ru": "📅 <b>На какой день вам удобно?</b>",
        "uz": "📅 <b>Qaysi kun sizga qulay?</b>",
    },
    "offer_slots": {
        "ru": "📅 <b>Свободное время на {date}</b>\nВыберите удобное 👇",
        "uz": "📅 <b>{date} kuni bo'sh vaqtlar</b>\nQulayini tanlang 👇",
    },
    "offer_slots_other_day": {
        "ru": "На {asked} свободного времени нет.\n"
              "📅 <b>Ближайшее — {date}</b> 👇",
        "uz": "{asked} kuni bo'sh vaqt yo'q.\n"
              "📅 <b>Eng yaqini — {date}</b> 👇",
    },
    # запрос «на сегодня» вне рабочего окна: не врать «всё занято» (P0)
    "closed_now_slots": {
        "ru": "🌙 Сейчас клиника закрыта.\n"
              "📅 <b>Ближайшее свободное время — {date}</b> 👇",
        "uz": "🌙 Hozir klinika yopiq.\n"
              "📅 <b>Eng yaqin bo'sh vaqt — {date}</b> 👇",
    },
    "no_slots_calendar": {
        "ru": "В ближайшие две недели свободного времени нет — "
              "вот более дальние даты:",
        "uz": "Yaqin ikki haftada bo'sh vaqt yo'q — mana uzoqroq sanalar:",
    },
    "no_slots_horizon": {
        "ru": "Свободного времени не видно даже на три месяца вперёд — "
              "загляните позже или напишите «позовите администратора».",
        "uz": "Uch oy oldinga ham bo'sh vaqt ko'rinmayapti — keyinroq "
              "urinib ko'ring yoki «administratorni chaqiring» deb yozing.",
    },
    "btn_pick_date": {"ru": "📅 Выбрать дату", "uz": "📅 Sanani tanlash"},
    "btn_back_calendar": {"ru": "◀ К датам", "uz": "◀ Sanalarga"},
    "btn_more_dates": {"ru": "Ещё даты ▶", "uz": "Yana sanalar ▶"},
    "btn_first_dates": {"ru": "◀ Ближайшие", "uz": "◀ Eng yaqinlari"},
    "cal_no_slots": {
        "ru": "Свободного времени нет",
        "uz": "Bo'sh vaqt yo'q",
    },
    "cal_past_day": {
        "ru": "Этот день уже прошёл",
        "uz": "Bu kun o'tib ketdi",
    },
    "doctor_not_found": {
        "ru": "Врача с таким именем не нашёл, показываю всё свободное время.",
        "uz": "Bunday ismli shifokor topilmadi, barcha bo'sh vaqtlarni ko'rsataman.",
    },
    "ask_name": {
        "ru": "👤 Как вас зовут?",
        "uz": "👤 Ismingiz nima?",
    },
    "ask_phone": {
        "ru": "📱 Остался один шаг: нажмите кнопку ниже — она отправит "
              "ваш номер телефона.",
        "uz": "📱 Bitta qadam qoldi: pastdagi tugmani bosing — u telefon "
              "raqamingizni yuboradi.",
    },
    "press_contact_button": {
        "ru": "📱 Чтобы оставить номер, нажмите кнопку ниже:",
        "uz": "📱 Raqam qoldirish uchun pastdagi tugmani bosing:",
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
    # {doctor} — либо пустой, либо готовая строка «\n👨‍⚕️ Имя» (booking_flow)
    "booked": {
        "ru": "✅ <b>ЗАПИСЬ ПОДТВЕРЖДЕНА</b>\n\n"
              "🦷 {service}\n📅 {when}{doctor}\n\n"
              "🔔 Напомним заранее. Ждём вас!",
        "uz": "✅ <b>YOZILDINGIZ</b>\n\n"
              "🦷 {service}\n📅 {when}{doctor}\n\n"
              "🔔 Oldindan eslatamiz. Sizni kutamiz!",
    },
    "hold_expired": {
        "ru": "Бронь на выбранное время истекла. Вот свежие варианты:",
        "uz": "Tanlangan vaqtni band qilish muddati tugadi. "
              "Mana yangi variantlar:",
    },
    "slot_taken": {
        "ru": "Это время только что заняли. Вот свежие варианты:",
        "uz": "Bu vaqt hozirgina band bo'ldi. Mana yangi variantlar:",
    },
    "reask": {
        "ru": "Не понял вас. Напишите, пожалуйста, иначе — например: "
              "«запись на чистку завтра».",
        "uz": "Kechirasiz, tushunmadim. Boshqacha yozib ko'ring — masalan: "
              "«ertaga tish tozalashga yozilmoqchiman».",
    },
    "llm_off_menu": {
        "ru": "Сейчас запись принимается через кнопки меню — выберите "
              "нужное действие.",
        "uz": "Hozir yozilish menyu tugmalari orqali qabul qilinadi — "
              "kerakli amalni tanlang.",
    },
    "bot_paused": {
        "ru": "Запись через бота временно приостановлена. Позвоните в клинику "
              "или загляните позже.",
        "uz": "Bot orqali yozilish vaqtincha to'xtatildi. Klinikaga qo'ng'iroq "
              "qiling yoki keyinroq urinib ko'ring.",
    },
    "escalated": {
        "ru": "👤 Передаю администратору — он ответит вам здесь "
              "в ближайшее время.",
        "uz": "👤 Administratorga ulab berdim — u tez orada shu yerda "
              "javob beradi.",
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
    "outside_hours": {
        "ru": "Клиника работает с {open} до {close}.",
        "uz": "Klinika {open} dan {close} gacha ishlaydi.",
    },
    "hours_today": {
        "ru": "🕐 Сегодня клиника работает с {open} до {close}.",
        "uz": "🕐 Bugun klinika {open} dan {close} gacha ishlaydi.",
    },
    "hours_next": {
        "ru": "🕐 Сегодня клиника не работает. Ближайший рабочий день — "
              "{date}: с {open} до {close}.",
        "uz": "🕐 Bugun klinika ishlamaydi. Eng yaqin ish kuni — {date}: "
              "{open} dan {close} gacha.",
    },
    "clinic_address": {
        "ru": "📍 Наш адрес: {address}",
        "uz": "📍 Manzilimiz: {address}",
    },
    "not_understood": {
        "ru": "🤔 Я не понял. Помогу записаться, перенести или отменить "
              "приём — выберите действие в меню. Нужен человек — напишите "
              "«позовите администратора».",
        "uz": "🤔 Tushunmadim. Qabulga yozilish, uni ko'chirish yoki bekor "
              "qilishda yordam beraman — menyudan amalni tanlang. "
              "Administrator kerak bo'lsa — «administratorni chaqiring» "
              "deb yozing.",
    },
    "escalated_closed": {
        "ru": "👤 Передаю администратору. Клиника сейчас закрыта — он ответит "
              "вам здесь утром.",
        "uz": "👤 Administratorga uzataman. Klinika hozir yopiq — u sizga "
              "ertalab shu yerda javob beradi.",
    },
    "confirm_retry": {
        "ru": "Техническая заминка — подтвердить запись не получилось. "
              "Пожалуйста, выберите время ещё раз:",
        "uz": "Texnik nosozlik — qabulni tasdiqlab bo'lmadi. Iltimos, "
              "vaqtni yana tanlang:",
    },
    "cancel_confirm_q": {
        "ru": "❌ Отменить вашу запись на {when}?",
        "uz": "❌ {when} dagi qabulni bekor qilaymi?",
    },
    "cancel_done": {
        "ru": "✅ Запись отменена. Будем рады записать вас снова.",
        "uz": "✅ Qabul bekor qilindi. Sizni yana kutib qolamiz.",
    },
    "cancel_kept": {
        "ru": "Хорошо, запись остаётся в силе.",
        "uz": "Yaxshi, qabul o'z kuchida qoladi.",
    },
    "cancel_none": {
        "ru": "Активной записи не нашёл. Хотите записаться?",
        "uz": "Faol qabul topilmadi. Yozilishni xohlaysizmi?",
    },
    "resched_none": {
        "ru": "Активной записи для переноса не нашёл. Хотите записаться?",
        "uz": "Boshqa vaqtga ko'chirish uchun faol qabul topilmadi. "
              "Yozilishni xohlaysizmi?",
    },
    "resched_done": {
        "ru": "✅ <b>ПЕРЕНЕСЕНО</b>\n\n📅 {when}\n\nЖдём вас!",
        "uz": "✅ <b>KO'CHIRILDI</b>\n\n📅 {when}\n\nSizni kutamiz!",
    },
    "btn_other_time": {"ru": "Другое время", "uz": "Boshqa vaqt"},
    "btn_today": {"ru": "Сегодня", "uz": "Bugun"},
    "btn_tomorrow": {"ru": "Завтра", "uz": "Ertaga"},
    "btn_after_tomorrow": {"ru": "Послезавтра", "uz": "Indinga"},
    "btn_yes": {"ru": "Да, отменить", "uz": "Ha, bekor qilish"},
    "btn_no": {"ru": "Нет, оставить", "uz": "Yo'q, qoldirish"},
    "reminder": {
        "ru": "🔔 <b>Напоминание</b>\n\n🦷 {service}\n📅 {when}\n\nЖдём вас!",
        "uz": "🔔 <b>Eslatma</b>\n\n🦷 {service}\n📅 {when}\n\nSizni kutamiz!",
    },
    "btn_attend": {"ru": "✓ Приду", "uz": "✓ Kelaman"},
    "btn_remind_cancel": {"ru": "Отменить запись", "uz": "Qabulni bekor qilish"},
    "attend_ok": {
        "ru": "👍 Отлично, ждём вас!",
        "uz": "👍 Ajoyib, sizni kutamiz!",
    },
    "rate_limited": {
        "ru": "Слишком много сообщений подряд — сделайте небольшую паузу, "
              "и я отвечу.",
        "uz": "Juda ko'p xabar yubordingiz — biroz kuting, javob beraman.",
    },
    # подзаголовок «виртуальный администратор» — честность P0 BRIEF:
    # пациент должен понимать, что говорит с ботом
    "greeting": {
        "ru": "🦷 <b>{clinic}</b>\nВиртуальный администратор · запись 24/7\n\n"
              "Помогу записаться, перенести или отменить приём — и напомню "
              "накануне визита. По медицинским вопросам ответит врач.\n\n"
              "Начнём? 👇",
        "uz": "🦷 <b>{clinic}</b>\nVirtual administrator · yozilish 24/7\n\n"
              "Qabulga yozilish, uni boshqa vaqtga ko'chirish yoki bekor "
              "qilishda yordam beraman — tashrif oldidan eslatib qo'yaman. "
              "Tibbiy savollarga shifokor javob beradi.\n\n"
              "Boshlaymizmi? 👇",
    },
    "stale_button": {
        "ru": "Эта кнопка устарела.",
        "uz": "Bu tugma endi faol emas.",
    },
    "conflict_moved": {
        "ru": "К сожалению, время {old} стало недоступно — перенёс вашу запись "
              "на {new}. Если не подходит, выберите другое:",
        "uz": "Afsuski, {old} vaqti band bo'lib qoldi — qabulni {new} ga "
              "ko'chirdim. To'g'ri kelmasa, boshqasini tanlang:",
    },
    "conflict_cancelled": {
        "ru": "К сожалению, время {old} стало недоступно, а свободного времени "
              "в ближайшие дни нет — запись отменена. Напишите, и подберём новое.",
        "uz": "Afsuski, {old} vaqti band bo'lib qoldi, yaqin kunlarda bo'sh vaqt "
              "yo'q — qabul bekor qilindi. Yozing, boshqa vaqt topamiz.",
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
        "ru": "Выберите действие или напишите своими словами 👇",
        "uz": "Amalni tanlang yoki o'z so'zlaringiz bilan yozing 👇",
    },
    "lang_changed": {
        "ru": "Язык переключён на русский.",
        "uz": "Til o'zbek tiliga o'zgartirildi.",
    },
    "price_header": {"ru": "💰 <b>Наши цены</b>", "uz": "💰 <b>Narxlarimiz</b>"},
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
    """Шаблон по ключу и языку; mixed заранее сведён к ru на уровне FSM.

    Подстановки экранируются (П-7): пациентские ответы уходят с
    parse_mode=HTML — имя клиники/врача/адрес с «<&>» не должны ломать
    парсер Telegram. Эмодзи и \\n экранирование не трогает."""
    safe = {k: html.escape(str(v), quote=False) for k, v in kwargs.items()}
    return TEMPLATES[key][lang].format(**safe)


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
