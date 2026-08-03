"""Строки админ-консоли на двух языках (карта готовности, №16).

Бот говорит с пациентом по-узбекски, а владелец клиники управлял им только
по-русски — узбекоязычный администратор оставался без консоли. Здесь тот же
приём, что в dialog/replies.py: шаблон по ключу, язык вторым ключом, `at()`
экранирует подстановки (сообщения консоли уходят с parse_mode=HTML).

Узбекские строки — черновик, как и пациентские до вычитки носителем
(BRIEF разд. 14.D, карта №20): латиница, апостроф `'` как в replies.py.
"""
from __future__ import annotations

import html
import logging
from string import Formatter

LANGS = ("ru", "uz")
log = logging.getLogger("navbat.admin_texts")

DEFAULT_LANG = "ru"

# слова отмены ввода — распознаём на любом языке независимо от языка консоли:
# админ мог переключить язык посреди ввода
CANCEL_WORDS = {"отмена", "cancel", "bekor", "bekor qilish"}

TEMPLATES: dict[str, dict[str, str]] = {
    # ── верхнее меню ─────────────────────────────────────────────────────
    "btn_services": {"ru": "💊 Услуги", "uz": "💊 Xizmatlar"},
    "btn_doctors": {"ru": "🧑‍⚕️ Врачи", "uz": "🧑‍⚕️ Shifokorlar"},
    "btn_about": {"ru": "🏥 О клинике", "uz": "🏥 Klinika haqida"},
    "btn_dayoff": {"ru": "📅 Выходные", "uz": "📅 Dam olish kunlari"},
    "btn_stats": {"ru": "📊 Статистика", "uz": "📊 Statistika"},
    "btn_pause": {"ru": "⏸ Пауза", "uz": "⏸ Pauza"},
    "btn_resume": {"ru": "▶️ Возобновить", "uz": "▶️ Davom ettirish"},
    # кнопка языка предлагает ДРУГОЙ язык — тумблер без лишнего экрана
    "btn_lang": {"ru": "🌐 O'zbekcha", "uz": "🌐 Русский"},
    "btn_preview": {"ru": "👁 Глазами пациента", "uz": "👁 Bemor ko'zi bilan"},
    "preview_head": {
        "ru": "👁 <b>Так вас видит пациент</b>\nЯзык пациента: {language}\n"
              "<i>Это картинка: записи не создаются.</i>\n\n",
        "uz": "👁 <b>Bemor sizni shunday ko'radi</b>\nBemor tili: {language}\n"
              "<i>Bu ko'rinish: yozuv yaratilmaydi.</i>\n\n",
    },
    "preview_menu": {
        "ru": "\n\n<i>Кнопки пациента:</i> {buttons}",
        "uz": "\n\n<i>Bemor tugmalari:</i> {buttons}",
    },
    "preview_lang_ru": {"ru": "русский", "uz": "rus tili"},
    "preview_lang_uz": {"ru": "узбекский", "uz": "o'zbek tili"},
    "btn_preview_ru": {"ru": "🇷🇺 По-русски", "uz": "🇷🇺 Rus tilida"},
    "btn_preview_uz": {"ru": "🇺🇿 По-узбекски", "uz": "🇺🇿 O'zbek tilida"},
    "console_title": {
        "ru": "🛠 <b>Админ-консоль</b>\nВыберите раздел 👇",
        "uz": "🛠 <b>Admin-konsol</b>\nBo'limni tanlang 👇",
    },
    "console_paused": {
        "ru": "⏸ <i>Бот на паузе.</i>\n\n",
        "uz": "⏸ <i>Bot pauzada.</i>\n\n",
    },
    "lang_switched": {
        "ru": "✅ Язык консоли: русский",
        "uz": "✅ Konsol tili: o'zbekcha",
    },

    # ── общие кнопки и бейджи ────────────────────────────────────────────
    "btn_cancel": {"ru": "✖ Отмена", "uz": "✖ Bekor qilish"},
    "btn_back": {"ru": "◀ Назад", "uz": "◀ Orqaga"},
    "btn_home": {"ru": "◀ Меню", "uz": "◀ Menyu"},
    "btn_hide": {"ru": "⛔ Скрыть", "uz": "⛔ Yashirish"},
    "btn_show": {"ru": "✅ Показать", "uz": "✅ Ko'rsatish"},
    "btn_delete": {"ru": "🗑 Удалить совсем", "uz": "🗑 Butunlay o'chirish"},
    "btn_delete_yes": {"ru": "🗑 Да, удалить", "uz": "🗑 Ha, o'chirish"},
    "badge_active_f": {"ru": "🟢 Активна", "uz": "🟢 Faol"},
    "badge_active_m": {"ru": "🟢 Активен", "uz": "🟢 Faol"},
    "badge_hidden_f": {"ru": "⚪ Скрыта", "uz": "⚪ Yashirilgan"},
    "badge_hidden_m": {"ru": "⚪ Скрыт", "uz": "⚪ Yashirilgan"},

    # ── услуги ───────────────────────────────────────────────────────────
    "services_title": {
        "ru": "💊 <b>Услуги</b>\nВыберите услугу 👇",
        "uz": "💊 <b>Xizmatlar</b>\nXizmatni tanlang 👇",
    },
    "btn_services_back": {"ru": "◀ Услуги", "uz": "◀ Xizmatlar"},
    "btn_svc_add": {"ru": "+ Добавить услугу", "uz": "+ Xizmat qo'shish"},
    "svc_hidden_item": {"ru": "⚪ {name} (скрыта)", "uz": "⚪ {name} (yashirilgan)"},
    "svc_card": {
        "ru": "{emoji} <b>{name}</b>\n\n💰 Цена: {price}\n"
              "⏱ Длительность: {duration}\n{badge}",
        "uz": "{emoji} <b>{name}</b>\n\n💰 Narxi: {price}\n"
              "⏱ Davomiyligi: {duration}\n{badge}",
    },
    "price_unset": {"ru": "не задана", "uz": "kiritilmagan"},
    "sum": {"ru": "{value} сум", "uz": "{value} so'm"},
    "minutes": {"ru": "{value} мин", "uz": "{value} daqiqa"},
    "btn_price_edit": {"ru": "💰 Изм. цену", "uz": "💰 Narxi"},
    "btn_dur_edit": {"ru": "⏱ Изм. длит.", "uz": "⏱ Davomiyligi"},
    "svc_hidden_notice": {"ru": "⛔ Услуга скрыта", "uz": "⛔ Xizmat yashirildi"},
    "svc_shown_notice": {
        "ru": "✅ Услуга снова доступна",
        "uz": "✅ Xizmat yana mavjud",
    },
    "svc_delete_confirm": {
        "ru": "🗑 Удалить услугу навсегда? Отменить это будет нельзя.",
        "uz": "🗑 Xizmat butunlay o'chirilsinmi? Buni qaytarib bo'lmaydi.",
    },
    "svc_deleted": {"ru": "✅ Услуга удалена", "uz": "✅ Xizmat o'chirildi"},
    "dur_prompt": {
        "ru": "⏱ <b>{label}</b>\nТекущая длительность: {current}\n\n"
              "Введите длительность в минутах ({lo}–{hi}), например 30.",
        "uz": "⏱ <b>{label}</b>\nHozirgi davomiyligi: {current}\n\n"
              "Davomiyligini daqiqada kiriting ({lo}–{hi}), masalan 30.",
    },
    "dur_unknown": {"ru": "неизвестно", "uz": "noma'lum"},
    "dur_invalid": {
        "ru": "⚠️ Длительность — целое число от {lo} до {hi} минут.",
        "uz": "⚠️ Davomiyligi — {lo} dan {hi} gacha butun son (daqiqa).",
    },
    "dur_saved": {
        "ru": "✅ Длительность «{label}»: {duration}",
        "uz": "✅ «{label}» davomiyligi: {duration}",
    },
    "svcadd_all_present": {
        "ru": "✅ Все услуги из каталога уже добавлены.",
        "uz": "✅ Katalogdagi barcha xizmatlar allaqachon qo'shilgan.",
    },
    "svcadd_catalog": {
        "ru": "Добавить услугу из каталога:",
        "uz": "Katalogdan xizmat qo'shish:",
    },
    "svcadd_prompt": {
        "ru": "➕ <b>{label}</b>\nВведите длительность приёма в минутах "
              "({lo}–{hi}), например 30.",
        "uz": "➕ <b>{label}</b>\nQabul davomiyligini daqiqada kiriting "
              "({lo}–{hi}), masalan 30.",
    },
    "svcadd_retry": {
        "ru": "Введите длительность в минутах ({lo}–{hi}):",
        "uz": "Davomiyligini daqiqada kiriting ({lo}–{hi}):",
    },
    "svcadd_done": {
        "ru": "✅ Услуга «{label}» добавлена, {duration}",
        "uz": "✅ «{label}» xizmati qo'shildi, {duration}",
    },
    "price_prompt": {
        "ru": "💰 <b>{label}</b>\nТекущая цена: {current}\n\n"
              "Введите новую цену в сумах, например 400000.",
        "uz": "💰 <b>{label}</b>\nHozirgi narxi: {current}\n\n"
              "Yangi narxni so'mda kiriting, masalan 400000.",
    },
    "price_invalid": {
        "ru": "⚠️ Цена — целое число сум больше нуля, например 400000.\n"
              "Введите ещё раз или нажмите «Отмена».",
        "uz": "⚠️ Narx — noldan katta butun son (so'm), masalan 400000.\n"
              "Qaytadan kiriting yoki «Bekor qilish» tugmasini bosing.",
    },
    "price_saved": {
        "ru": "✅ Цена «{label}»: {price}",
        "uz": "✅ «{label}» narxi: {price}",
    },

    # ── о клинике (FAQ) ──────────────────────────────────────────────────
    "faq_title": {
        "ru": "🏥 <b>О клинике</b>\nВыберите поле 👇",
        "uz": "🏥 <b>Klinika haqida</b>\nMaydonni tanlang 👇",
    },
    "faq_btn_address": {"ru": "📍 Адрес", "uz": "📍 Manzil"},
    "faq_btn_payment": {"ru": "💳 Оплата", "uz": "💳 To'lov"},
    "faq_btn_phone": {"ru": "📞 Телефон", "uz": "📞 Telefon"},
    "faq_name_address": {"ru": "Адрес", "uz": "Manzil"},
    "faq_name_payment": {"ru": "Условия оплаты", "uz": "To'lov shartlari"},
    "faq_name_phone": {"ru": "Телефон", "uz": "Telefon"},
    "faq_unset": {"ru": "не задано", "uz": "kiritilmagan"},
    "faq_prompt": {
        "ru": "🏥 <b>{title}</b>\nТекущее значение: {current}\n\n"
              "Введите новое значение или нажмите «Отмена».",
        "uz": "🏥 <b>{title}</b>\nHozirgi qiymati: {current}\n\n"
              "Yangi qiymatni kiriting yoki «Bekor qilish» tugmasini bosing.",
    },
    "faq_invalid": {
        "ru": "⚠️ Введите непустой текст до {limit} символов.",
        "uz": "⚠️ {limit} belgigacha bo'sh bo'lmagan matn kiriting.",
    },
    "faq_saved": {"ru": "✅ {title} обновлено", "uz": "✅ {title} yangilandi"},

    # ── врачи ────────────────────────────────────────────────────────────
    "doctors_title": {
        "ru": "🧑‍⚕️ <b>Врачи</b>\nВыберите врача 👇",
        "uz": "🧑‍⚕️ <b>Shifokorlar</b>\nShifokorni tanlang 👇",
    },
    "btn_doctors_back": {"ru": "◀ Врачи", "uz": "◀ Shifokorlar"},
    "btn_doc_add": {"ru": "+ Добавить врача", "uz": "+ Shifokor qo'shish"},
    "doc_noname": {"ru": "(без имени)", "uz": "(ismsiz)"},
    "doc_placeholder": {"ru": "[врач {short_id}]", "uz": "[shifokor {short_id}]"},
    "doc_hidden_item": {"ru": "⚪ {name} (скрыт)", "uz": "⚪ {name} (yashirilgan)"},
    "doc_card": {
        "ru": "🧑‍⚕️ <b>{name}</b>\n\n⏲ Буфер: {buffer}\n📆 Календарь: {calendar}\n"
              "{badge}\n\n📅 <b>График</b>\n{schedule}",
        "uz": "🧑‍⚕️ <b>{name}</b>\n\n⏲ Bufer: {buffer}\n📆 Kalendar: {calendar}\n"
              "{badge}\n\n📅 <b>Ish jadvali</b>\n{schedule}",
    },
    "cal_linked": {"ru": "привязан", "uz": "ulangan"},
    "cal_unlinked": {"ru": "не привязан", "uz": "ulanmagan"},
    "btn_doc_name": {"ru": "👤 Имя", "uz": "👤 Ismi"},
    "btn_doc_buffer": {"ru": "⏲ Буфер", "uz": "⏲ Bufer"},
    "btn_doc_sched": {"ru": "📅 Расписание", "uz": "📅 Ish jadvali"},
    "dname_prompt": {
        "ru": "👤 <b>Имя врача</b>\n\nВведите имя (до {limit} символов):",
        "uz": "👤 <b>Shifokor ismi</b>\n\nIsmni kiriting ({limit} belgigacha):",
    },
    "dname_invalid": {
        "ru": "Имя — непустая строка до {limit} символов.",
        "uz": "Ism — {limit} belgigacha bo'sh bo'lmagan matn.",
    },
    "dname_saved": {"ru": "✅ Имя обновлено: {name}", "uz": "✅ Ism yangilandi: {name}"},
    "dbuf_prompt": {
        "ru": "⏲ <b>Буфер</b>\n\nВведите буфер в минутах ({lo}–{hi}), например 10:",
        "uz": "⏲ <b>Bufer</b>\n\nBuferni daqiqada kiriting ({lo}–{hi}), masalan 10:",
    },
    "dbuf_invalid": {
        "ru": "Буфер — целое число от {lo} до {hi}.",
        "uz": "Bufer — {lo} dan {hi} gacha butun son.",
    },
    "dbuf_saved": {"ru": "✅ Буфер: {buffer}", "uz": "✅ Bufer: {buffer}"},
    "docadd_prompt": {
        "ru": "🧑‍⚕️ <b>Новый врач</b>\n\nВведите имя:",
        "uz": "🧑‍⚕️ <b>Yangi shifokor</b>\n\nIsmni kiriting:",
    },
    "doc_added": {
        "ru": "✅ Врач «{name}» добавлен",
        "uz": "✅ «{name}» shifokor qo'shildi",
    },
    "doc_hidden_notice": {"ru": "⛔ Врач скрыт", "uz": "⛔ Shifokor yashirildi"},
    "doc_hidden_bookings": {
        "ru": "\n⚠️ Будущих записей к нему: {count} — они остаются в силе, "
              "перенесите их сами",
        "uz": "\n⚠️ Unga kelgusi yozuvlar: {count} — ular bekor qilinmadi, "
              "o'zingiz ko'chiring",
    },
    "doc_shown_notice": {
        "ru": "✅ Врач снова в записи",
        "uz": "✅ Shifokor yana yozuvda",
    },
    "doc_delete_confirm": {
        "ru": "🗑 Удалить врача навсегда? Отменить это будет нельзя.",
        "uz": "🗑 Shifokor butunlay o'chirilsinmi? Buni qaytarib bo'lmaydi.",
    },
    "doc_deleted": {"ru": "✅ Врач удалён", "uz": "✅ Shifokor o'chirildi"},

    # ── расписание ───────────────────────────────────────────────────────
    "sched_title": {
        "ru": "📅 <b>Расписание</b>\nВыберите шаблон или задайте свой 👇\n\n"
              "Шаблон задаёт неделю целиком, «Свой график» меняет только "
              "выбранные дни.",
        "uz": "📅 <b>Ish jadvali</b>\nShablonni tanlang yoki o'zingiznikini "
              "kiriting 👇\n\nShablon butun haftani belgilaydi, «O'z jadvali» "
              "faqat tanlangan kunlarni o'zgartiradi.",
    },
    "btn_sched_custom": {"ru": "📝 Свой график", "uz": "📝 O'z jadvali"},
    "sched_tpl_workweek": {"ru": "Пн–Пт 09–18", "uz": "Du–Ju 09–18"},
    "sched_tpl_six_days": {"ru": "Пн–Сб 09–13 / 14–18", "uz": "Du–Sh 09–13 / 14–18"},
    "sched_tpl_late": {"ru": "Пн–Пт 10:00–19:00", "uz": "Du–Ju 10:00–19:00"},
    "sched_saved": {"ru": "✅ Расписание задано", "uz": "✅ Ish jadvali belgilandi"},
    "sched_days_title": {
        "ru": "📅 <b>Свой график</b>\nОтметьте дни, которые меняем 👇\n\n"
              "Остальные дни останутся как есть.",
        "uz": "📅 <b>O'z jadvali</b>\nO'zgartiriladigan kunlarni belgilang 👇\n\n"
              "Qolgan kunlar o'z holicha qoladi.",
    },
    "btn_sched_next": {"ru": "Далее →", "uz": "Keyingi →"},
    "sched_pick_day": {
        "ru": "Выберите хотя бы один рабочий день.",
        "uz": "Kamida bitta ish kunini tanlang.",
    },
    "sched_pick_any_day": {
        "ru": "Выберите хотя бы один день.",
        "uz": "Kamida bitta kunni tanlang.",
    },
    "sched_shifts_prompt": {
        "ru": "Дни: {days}\n\nВведите смены через запятую, например:\n"
              "<code>09:00-13:00, 14:00-18:00</code>\n\n"
              "Остальные дни недели останутся как есть.",
        "uz": "Kunlar: {days}\n\nSmenalarni vergul bilan kiriting, masalan:\n"
              "<code>09:00-13:00, 14:00-18:00</code>\n\n"
              "Haftaning qolgan kunlari o'z holicha qoladi.",
    },
    "sched_shifts_invalid": {
        "ru": "⚠️ Формат: <code>09:00-13:00, 14:00-18:00</code>\nВведите ещё раз:",
        "uz": "⚠️ Format: <code>09:00-13:00, 14:00-18:00</code>\n"
              "Qaytadan kiriting:",
    },
    "btn_sched_dayoff": {
        "ru": "🚫 Сделать выходным",
        "uz": "🚫 Dam olish kuni qilish",
    },
    "sched_dayoff_saved": {
        "ru": "✅ Дни сделаны выходными",
        "uz": "✅ Kunlar dam olish kuni qilindi",
    },
    "sched_dayoff_last": {
        "ru": "⚠️ Так у врача не останется ни одного рабочего дня.\n\n"
              "Чтобы убрать врача из записи целиком, нажмите «⛔ Скрыть» "
              "в его карточке.",
        "uz": "⚠️ Unda shifokorda birorta ham ish kuni qolmaydi.\n\n"
              "Shifokorni yozuvdan butunlay olib tashlash uchun uning "
              "kartochkasida «⛔ Yashirish» tugmasini bosing.",
    },
    "day_off": {"ru": "выходной", "uz": "dam olish"},
    "week_off": {"ru": "выходной всю неделю", "uz": "butun hafta dam olish"},
    "weekday_mon": {"ru": "Пн", "uz": "Du"},
    "weekday_tue": {"ru": "Вт", "uz": "Se"},
    "weekday_wed": {"ru": "Ср", "uz": "Ch"},
    "weekday_thu": {"ru": "Чт", "uz": "Pa"},
    "weekday_fri": {"ru": "Пт", "uz": "Ju"},
    "weekday_sat": {"ru": "Сб", "uz": "Sh"},
    "weekday_sun": {"ru": "Вс", "uz": "Ya"},

    # ── выходные дни ─────────────────────────────────────────────────────
    "dayoff_title": {"ru": "📅 <b>Выходные</b>\n", "uz": "📅 <b>Dam olish kunlari</b>\n"},
    "dayoff_intro": {
        "ru": "Ближайшие закрытые дни (тап — снова открыть):",
        "uz": "Yaqin yopiq kunlar (bosing — yana ochiladi):",
    },
    "dayoff_empty": {
        "ru": "📅 Закрытых дней нет — клиника работает по графику.",
        "uz": "📅 Yopiq kunlar yo'q — klinika jadval bo'yicha ishlaydi.",
    },
    "btn_dayoff_add": {"ru": "➕ Закрыть день", "uz": "➕ Kunni yopish"},
    "dayoff_prompt": {
        "ru": "📅 Введите дату и (по желанию) причину:\n<code>21.03 Навруз</code>",
        "uz": "📅 Sanani va (xohlasangiz) sababni kiriting:\n"
              "<code>21.03 Navro'z</code>",
    },
    "dayoff_invalid": {
        "ru": "⚠️ Формат: <code>21.03 причина</code> (день.месяц). "
              "Повторите или нажмите «Отмена».",
        "uz": "⚠️ Format: <code>21.03 sabab</code> (kun.oy). "
              "Qaytaring yoki «Bekor qilish» tugmasini bosing.",
    },
    "dayoff_closed": {"ru": "✅ {date} — выходной", "uz": "✅ {date} — dam olish kuni"},
    "dayoff_reopened": {
        "ru": "✅ День снова рабочий",
        "uz": "✅ Kun yana ish kuni",
    },
    "dayoff_booked_warning": {
        "ru": "\n\n⚠️ На этот день уже есть записи: {count} ({times}).\n"
              "Бот их не отменяет и напоминания придут — перенесите или "
              "отмените их сами.",
        "uz": "\n\n⚠️ Bu kunga yozuvlar bor: {count} ({times}).\n"
              "Bot ularni bekor qilmaydi va eslatmalar boradi — o'zingiz "
              "ko'chiring yoki bekor qiling.",
    },
    "dayoff_warning_more": {"ru": " и ещё {count}", "uz": " va yana {count}"},

    # ── сводка владельца и вечерний дайджест ─────────────────────────────
    "stats_header_day": {
        "ru": "📊 <b>Сводка за {date}</b>",
        "uz": "📊 <b>{date} kunlik hisobot</b>",
    },
    "stats_header_range": {
        "ru": "📊 <b>Сводка за {days} дн. ({first}–{last})</b>",
        "uz": "📊 <b>{days} kunlik hisobot ({first}–{last})</b>",
    },
    "stats_value_title": {"ru": "💰 Ценность", "uz": "💰 Qiymat"},
    "stats_booked": {
        "ru": "• записей подтверждено: {count}{trend}{after}",
        "uz": "• tasdiqlangan yozuvlar: {count}{trend}{after}",
    },
    "stats_after_hours": {
        "ru": " (из них {count} — вне рабочих часов)",
        "uz": " (shundan {count} tasi — ish vaqtidan tashqari)",
    },
    "stats_prevented": {
        "ru": "• предотвращено неявок: {count} "
              "(слотов на ≈ {money} сум освобождено заранее)",
        "uz": "• oldi olingan kelmasliklar: {count} "
              "(≈ {money} so'mlik vaqt oldindan bo'shatildi)",
    },
    "stats_cancelled": {
        "ru": "• отмен: {count}{trend}",
        "uz": "• bekor qilingan: {count}{trend}",
    },
    "stats_escalated": {
        "ru": "• эскалаций к администратору: {count}",
        "uz": "• administratorga murojaatlar: {count}",
    },
    "stats_clients": {
        "ru": "👥 Клиенты\n• новых: {new} · вернувшихся: {returning}",
        "uz": "👥 Mijozlar\n• yangi: {new} · qaytgan: {returning}",
    },
    "stats_top_doctors": {"ru": "👨‍⚕️ Топ врачей", "uz": "👨‍⚕️ Eng band shifokorlar"},
    "stats_doctor_line": {
        "ru": "• {name} — {count} зап. (≈ {money} сум)",
        "uz": "• {name} — {count} ta yozuv (≈ {money} so'm)",
    },
    "stats_hit_service": {
        "ru": "✨ Хит-услуга\n• {service} — {count} зап.",
        "uz": "✨ Eng ommabop xizmat\n• {service} — {count} ta yozuv",
    },
    "stats_waitlist": {
        "ru": "🔔 Очередь ожидания\n• сейчас ждут слота: {count}",
        "uz": "🔔 Kutish navbati\n• hozir vaqt kutayotganlar: {count}",
    },
    "stats_tech": {
        "ru": "⚙️ Служебное\n• напоминаний: {reminders} · LLM: {requests} "
              "запросов, {tokens} токенов, сбоев: {failures}, repair: {repairs}",
        "uz": "⚙️ Texnik\n• eslatmalar: {reminders} · LLM: {requests} "
              "so'rov, {tokens} token, xato: {failures}, repair: {repairs}",
    },
    "stats_p95": {
        "ru": " · p95 ответа: {seconds} с (SLA &lt; 5 с)",
        "uz": " · javob p95: {seconds} s (SLA &lt; 5 s)",
    },
    "digest_title": {"ru": "📊 <b>Итог дня</b>", "uz": "📊 <b>Kun yakuni</b>"},
    "digest_booked": {
        "ru": "• записей: {count}{after}",
        "uz": "• yozuvlar: {count}{after}",
    },
    "digest_prevented": {
        "ru": "• предотвращено неявок: {count} (≈ {money} сум)",
        "uz": "• oldi olingan kelmasliklar: {count} (≈ {money} so'm)",
    },
    "digest_escalated": {
        "ru": "• эскалаций: {count}",
        "uz": "• murojaatlar: {count}",
    },
    "digest_waitlist": {
        "ru": "\n• 🔔 в очереди ожидания: {count}",
        "uz": "\n• 🔔 kutish navbatida: {count}",
    },
    "digest_more": {"ru": "📊 Подробнее", "uz": "📊 Batafsil"},

    # ── лента записей: карточка владельцу в момент события ───────────────
    # Клиника без Google Calendar не видит записи бота вообще — карточка
    # закрывает эту дыру. Уходит с parse_mode=HTML (at() экранирует имя
    # пациента и врача: они приходят из БД и могут нести «<&>»)
    "feed_booked": {
        "ru": "✅ Запись: {patient}, {service}, {when}, {doctor}{night}",
        "uz": "✅ Yozuv: {patient}, {service}, {when}, {doctor}{night}",
    },
    "feed_cancelled": {
        "ru": "❌ Отмена: {patient}, {service}, {when}, {doctor}{night}",
        "uz": "❌ Bekor qilindi: {patient}, {service}, {when}, {doctor}{night}",
    },
    "feed_resched": {
        "ru": "🔄 Перенос: {patient}, {service} → {when}, {doctor}{night}",
        "uz": "🔄 Ko'chirildi: {patient}, {service} → {when}, {doctor}{night}",
    },
    "feed_no_name": {"ru": "без имени", "uz": "ismsiz"},
    # услуги может не быть у записи с улицы — карточку это не отменяет
    "feed_no_service": {"ru": "приём", "uz": "qabul"},

    # ── экран «Сегодня» и утренняя сводка ────────────────────────────────
    "btn_today_list": {"ru": "📅 Сегодня", "uz": "📅 Bugun"},
    "btn_today_refresh": {"ru": "🔄 Обновить", "uz": "🔄 Yangilash"},
    # «приёмов: N», а не «N приёмов»: русское число требует трёх форм
    # («1 приём», «2 приёма», «5 приёмов»), а склонять в шаблоне нечем
    "today_header": {
        "ru": "📅 <b>Сегодня, {date}</b> — приёмов: {count}",
        "uz": "📅 <b>Bugun, {date}</b> — qabullar: {count}",
    },
    "today_line": {
        "ru": "{time} {patient} ({phone}) — {service}, {doctor}",
        "uz": "{time} {patient} ({phone}) — {service}, {doctor}",
    },
    "today_empty": {
        "ru": "📅 Сегодня записей нет",
        "uz": "📅 Bugun yozuvlar yo'q",
    },
    "today_no_phone": {"ru": "без телефона", "uz": "telefonsiz"},
    # ⏳-строки дня одним числом: владельцу нужно действие, а не подсчёт
    # значков глазами
    "today_call_hint": {
        "ru": "⚠️ Без ответа после напоминания: {count} — стоит позвонить",
        "uz": "⚠️ Eslatmaga javob bermaganlar: {count} — qo'ng'iroq qilgan ma'qul",
    },
    "morning_header": {
        "ru": "☀️ <b>Доброе утро!</b>",
        "uz": "☀️ <b>Xayrli tong!</b>",
    },
    "questions_title": {
        "ru": "❓ <b>Вопросы без ответа ({count})</b>",
        "uz": "❓ <b>Javobsiz savollar ({count})</b>",
    },
    "questions_more": {"ru": "\n… и ещё {count}", "uz": "\n… va yana {count}"},

    # ── ответы админ-команд (слэш-путь и кнопки) ─────────────────────────
    "paused_ok": {
        "ru": "[OK] бот на паузе. Пациентам отвечаем «запись временно "
              "по телефону». Вернуть: /resume",
        "uz": "[OK] bot pauzada. Bemorlarga «yozuv vaqtincha telefon "
              "orqali» deb javob beramiz. Qaytarish: /resume",
    },
    # причина отдельным шаблоном: вложенный at() экранировал бы её дважды
    "paused_ok_reason": {
        "ru": "[OK] бот на паузе ({reason}). Пациентам отвечаем «запись "
              "временно по телефону». Вернуть: /resume",
        "uz": "[OK] bot pauzada ({reason}). Bemorlarga «yozuv vaqtincha "
              "telefon orqali» deb javob beramiz. Qaytarish: /resume",
    },
    "resumed_ok": {
        "ru": "[OK] бот снова принимает запись",
        "uz": "[OK] bot yana yozuvni qabul qilmoqda",
    },
    "llm_off_ok": {
        "ru": "[OK] свободный текст выключен — работают только кнопки",
        "uz": "[OK] erkin matn o'chirildi — faqat tugmalar ishlaydi",
    },
    "llm_on_ok": {
        "ru": "[OK] свободный текст снова понимает NLU",
        "uz": "[OK] erkin matnni yana NLU tushunadi",
    },
    "llm_usage": {
        "ru": "Формат: /llm on|off",
        "uz": "Format: /llm on|off",
    },
    "dayoff_ok": {
        "ru": "[OK] {date} — выходной",
        "uz": "[OK] {date} — dam olish kuni",
    },
    # причина отдельным шаблоном: at() внутри at() экранировал бы её дважды
    "reason_suffix": {"ru": " ({reason})", "uz": " ({reason})"},
    "dayoff_already": {
        "ru": "{date} уже выходной.",
        "uz": "{date} allaqachon dam olish kuni.",
    },
    "dayopen_ok": {
        "ru": "[OK] {date} снова рабочий",
        "uz": "[OK] {date} yana ish kuni",
    },
    "dayopen_already": {
        "ru": "{date} и так рабочий.",
        "uz": "{date} allaqachon ish kuni.",
    },
    # без {upcoming}: список закрытых дней приклеивается склейкой — он уже
    # прошёл экранирование, второй проход дал бы «&amp;lt;» (ревью, дефект 5)
    "dayoff_usage": {
        "ru": "Формат: /dayoff DD.MM [причина] — закрыть день, "
              "/dayopen DD.MM — снова открыть.",
        "uz": "Format: /dayoff DD.MM [sabab] — kunni yopish, "
              "/dayopen DD.MM — yana ochish.",
    },
    "dayoff_upcoming": {
        "ru": "Ближайшие выходные: {days}",
        "uz": "Yaqin dam olish kunlari: {days}",
    },
    "dayoff_none_ahead": {
        "ru": "Закрытых дней впереди нет.",
        "uz": "Oldinda yopiq kunlar yo'q.",
    },
    "stats_usage": {
        "ru": "Формат: /stats — за сегодня, /stats 7 — за неделю "
              "или /stats 30",
        "uz": "Format: /stats — bugun uchun, /stats 7 — hafta uchun "
              "yoki /stats 30",
    },
    "btn_period": {"ru": "{days} дней", "uz": "{days} kun"},
    "btn_period_today": {"ru": "📅 День", "uz": "📅 Bugun"},

    # ошибки слоя данных: текст исключения технический и русский, владельцу
    # достаточно знать, что действие не выполнено
    "action_failed": {
        "ru": "⚠️ Не получилось: {reason}",
        "uz": "⚠️ Bajarilmadi: {reason}",
    },
    "svc_exists": {
        "ru": "⚠️ Такая услуга уже есть в клинике.",
        "uz": "⚠️ Bunday xizmat klinikada allaqachon bor.",
    },
    "svc_still_active": {
        "ru": "⚠️ Услуга ещё доступна пациентам — сначала скройте её.",
        "uz": "⚠️ Xizmat hali bemorlarga ochiq — avval uni yashiring.",
    },
    "doc_still_active": {
        "ru": "⚠️ Врач ещё доступен пациентам — сначала скройте его.",
        "uz": "⚠️ Shifokor hali bemorlarga ochiq — avval uni yashiring.",
    },
    "doc_missing": {
        "ru": "⚠️ Этого врача уже нет — возможно, его удалили в другом чате.",
        "uz": "⚠️ Bu shifokor endi yo'q — boshqa chatda o'chirilgan bo'lishi mumkin.",
    },
    "svc_missing": {
        "ru": "⚠️ Этой услуги уже нет — возможно, её удалили в другом чате.",
        "uz": "⚠️ Bu xizmat endi yo'q — boshqa chatda o'chirilgan bo'lishi mumkin.",
    },
    "svc_in_use": {
        "ru": "⚠️ Услугу нельзя удалить: на неё ссылаются записи.",
        "uz": "⚠️ Xizmatni o'chirib bo'lmaydi: unga yozuvlar bog'langan.",
    },
    "doc_in_use": {
        "ru": "⚠️ Врача нельзя удалить: на него ссылаются записи.",
        "uz": "⚠️ Shifokorni o'chirib bo'lmaydi: unga yozuvlar bog'langan.",
    },
    # каркас алертов админ-чату. Алерты уходят PLAIN (без parse_mode) —
    # подставляем через plain(), иначе «&» превратится в «&amp;»
    "alert_escalation": {
        "ru": "Эскалация: чат {chat}\n"
              "Причина: {reason}\n"
              "Что хотел пациент: {context}\n"
              "Снять: /release {chat}",
        "uz": "Murojaat: chat {chat}\n"
              "Sababi: {reason}\n"
              "Bemor nima xohlagan: {context}\n"
              "Yopish: /release {chat}",
    },
    "alert_fyi": {
        "ru": "🟡 К сведению: {reason}\nЧто хотел пациент: {context}",
        "uz": "🟡 Ma'lumot uchun: {reason}\nBemor nima xohlagan: {context}",
    },
    "alert_ops": {"ru": "⚠ {reason}", "uz": "⚠ {reason}"},
    "alert_system": {
        "ru": "⚠ Системный алерт\n{reason}",
        "uz": "⚠ Tizim ogohlantirishi\n{reason}",
    },
    # реплика замороженного пациента. Тем же plain-каналом, что алерты: текст
    # пациента чужой, экранировать его под HTML нечем — parse_mode здесь нет
    "relay_card": {
        "ru": "💬 Чат {chat}: {text}",
        "uz": "💬 Chat {chat}: {text}",
    },
    # «что хотел пациент» в алерте: подписи полей брони. Часть того же экрана,
    # что причина, — значит и язык тот же (остаток по №16)
    "ctx_service": {"ru": "услуга — {value}", "uz": "xizmat — {value}"},
    "ctx_day": {"ru": "день — {value}", "uz": "kun — {value}"},
    "ctx_time": {"ru": "время — {value}", "uz": "vaqt — {value}"},
    "ctx_slot": {"ru": "выбранный слот — {value}", "uz": "tanlangan vaqt — {value}"},
    "ctx_doctor": {"ru": "врач — {value}", "uz": "shifokor — {value}"},
    "ctx_cancel": {
        "ru": "отмена записи на — {value}",
        "uz": "yozuvni bekor qilish — {value}",
    },
    "ctx_empty": {
        "ru": "пациент ещё ничего не выбрал",
        "uz": "bemor hali hech narsa tanlamagan",
    },
    # причины ВНУТРИ алертов: их формируют служебные пути (календарь, синк,
    # напоминания, воркер, диалог), а читает владелец на своём языке. Каркас
    # перевели волной C, причины оставались русскими — остаток по №16
    "reason_wants_human": {
        "ru": "пациент просит администратора",
        "uz": "bemor administrator bilan gaplashmoqchi",
    },
    "reason_confirm_failed": {
        "ru": "сбой подтверждения записи",
        "uz": "yozuvni tasdiqlashda xatolik",
    },
    "reason_no_slots": {
        "ru": "нет слотов на 2 недели вперёд",
        "uz": "2 hafta oldinga bo'sh vaqt yo'q",
    },
    "reason_no_slots_reschedule": {
        "ru": "перенос: нет слотов на 2 недели вперёд",
        "uz": "ko'chirish: 2 hafta oldinga bo'sh vaqt yo'q",
    },
    "reason_event_restored": {
        "ru": "событие записи удалили в календаре — восстановил; "
              "правки записей — через бота",
        "uz": "kalendarda yozuv hodisasi o'chirilgan — tikladim; "
              "yozuvlarni bot orqali o'zgartiring",
    },
    "reason_event_moved_back": {
        "ru": "событие записи сдвинули в календаре — вернул; "
              "переносы — через бота",
        "uz": "kalendarda yozuv vaqti ko'chirilgan — qaytardim; "
              "ko'chirishni bot orqali qiling",
    },
    "reason_relocated": {
        "ru": "запись {old} вытеснена ручным событием — перенесена на {new}",
        "uz": "{old} yozuvi qo'lda kiritilgan hodisa tufayli {new} ga ko'chirildi",
    },
    "reason_unrelocatable": {
        "ru": "запись {old} вытеснена ручным событием, перенести некуда — отменена",
        "uz": "{old} yozuvi qo'lda kiritilgan hodisa tufayli bekor qilindi: "
              "ko'chirish uchun bo'sh vaqt yo'q",
    },
    "reason_double_booking": {
        "ru": "в календаре два приёма на одно время ({when}) — бот учитывает "
              "только первый, второй в записи не виден",
        "uz": "kalendarda bir vaqtga ikkita qabul ({when}) — bot faqat "
              "birinchisini hisobga oladi, ikkinchisi yozuvlarda ko'rinmaydi",
    },
    "reason_gcal_auth_dead": {
        "ru": "Google OAuth-токен мёртв — синхронизация календаря остановилась "
              "и сама не починится. Нужна переавторизация: "
              "python -m navbat.calendar.auth",
        "uz": "Google OAuth tokeni ishlamaydi — kalendar sinxronizatsiyasi "
              "to'xtadi va o'zi tuzalmaydi. Qayta ruxsat kerak: "
              "python -m navbat.calendar.auth",
    },
    "reason_sync_stuck": {
        "ru": "синхронизация Google Calendar не работает {cycles} циклов "
              "подряд — проверьте доступ Google (возможно, протух токен). Пока "
              "синк стоит, бот может записать пациента на занятое врачом время.",
        "uz": "Google Calendar sinxronizatsiyasi {cycles} tsikl ketma-ket "
              "ishlamadi — Google ruxsatini tekshiring (token muddati o'tgan "
              "bo'lishi mumkin). Sinxronizatsiya to'xtaganda bot bemorni "
              "shifokor band qilgan vaqtga yozib qo'yishi mumkin.",
    },
    "reason_sync_restored": {
        "ru": "синхронизация Google Calendar восстановлена.",
        "uz": "Google Calendar sinxronizatsiyasi tiklandi.",
    },
    "reason_llm_cap": {
        "ru": "дневной лимит LLM-токенов ({cap}) исчерпан — бот эскалирует "
              "диалоги до конца дня",
        "uz": "kunlik LLM token limiti ({cap}) tugadi — bot kun oxirigacha "
              "suhbatlarni administratorga uzatadi",
    },
    "reason_reminder_failed": {
        "ru": "напоминание пациенту (чат {chat}) не доставлено после "
              "{attempts} попыток — свяжитесь с ним сами",
        "uz": "bemorga eslatma ({chat} chat) {attempts} urinishdan keyin yetib "
              "bormadi — u bilan o'zingiz bog'laning",
    },
    "reason_update_failed": {
        "ru": "сообщение пациента (чат {chat}) не удалось обработать — "
              "свяжитесь с ним сами",
        "uz": "bemor xabarini ({chat} chat) qayta ishlab bo'lmadi — "
              "u bilan o'zingiz bog'laning",
    },
    "release_usage": {
        "ru": "Формат: /release <chat_id> (число из алерта эскалации)",
        "uz": "Format: /release <chat_id> (murojaat xabaridagi raqam)",
    },
    "release_not_found": {
        "ru": "Чат {chat} не найден.",
        "uz": "{chat} chat topilmadi.",
    },
    "release_not_escalated": {
        "ru": "Чат {chat} не в эскалации (состояние: {state}).",
        "uz": "{chat} chat murojaatda emas (holati: {state}).",
    },
    "release_ok": {
        "ru": "[OK] эскалация снята: чат {chat}",
        "uz": "[OK] murojaat yopildi: chat {chat}",
    },
    # свайп-ответ админа пациенту: подтверждения адресату ответа
    "relay_delivered": {"ru": "✅ Доставлено", "uz": "✅ Yetkazildi"},
    "relay_failed": {
        "ru": "⚠️ Не доставлено: {error}",
        "uz": "⚠️ Yetkazilmadi: {error}",
    },
    "relay_no_anchor": {
        "ru": "Отвечайте реплаем на алерт эскалации или карточку 💬",
        "uz": "Eskalatsiya ogohlantirishiga yoki 💬 kartochkaga javob (reply) "
              "qiling",
    },
    "forget_usage": {
        "ru": "Формат: /forget <chat_id> — анонимизировать пациента",
        "uz": "Format: /forget <chat_id> — bemor ma'lumotlarini o'chirish",
    },
    "forget_not_found": {
        "ru": "Чат {chat} не найден — данных нет.",
        "uz": "{chat} chat topilmadi — ma'lumot yo'q.",
    },
    "forget_ok": {
        "ru": "[OK] чат {chat}: пациент анонимизирован, диалог и сообщения "
              "удалены. Будущие записи не отменены — отмените отдельно, "
              "если пациент просил.",
        "uz": "[OK] chat {chat}: bemor ma'lumotlari o'chirildi, suhbat va "
              "xabarlar tozalandi. Kelgusi yozuvlar bekor qilinmadi — bemor "
              "so'ragan bo'lsa, alohida bekor qiling.",
    },
}


def at(key: str, lang: str, **kwargs) -> str:
    """Строка консоли по ключу и языку.

    Подстановки экранируются: консоль шлёт HTML, а имя врача или адрес
    с «<&>» иначе ломают парсер Telegram (урок бага «(SLA < 5 с)»)."""
    safe = {k: html.escape(str(v), quote=False) for k, v in kwargs.items()}
    template = TEMPLATES[key].get(lang) or TEMPLATES[key][DEFAULT_LANG]
    return template.format(**safe) if safe else template


def plain(key: str, lang: str, **kwargs) -> str:
    """То же, что at(), но БЕЗ экранирования: алерты админ-чату уходят
    без parse_mode, и html.escape превратил бы «&» в «&amp;»."""
    template = TEMPLATES[key].get(lang) or TEMPLATES[key][DEFAULT_LANG]
    return template.format(**kwargs) if kwargs else template


def render_reason(key: str, lang: str, params: dict) -> str:
    """Причина алерта на языке получателя, которая не роняет доставку.

    Сигнал важнее перевода: опечатка в ключе или в подстановке — в том числе
    внесённая вычиткой узбекских строк — отменяла бы алерт целиком, и
    администратор не узнал бы, что синк стоит. Поэтому сбой рендера уходит в
    лог, а получатель видит ключ с подстановками: некрасиво, но доходит."""
    try:
        # форматируем ВСЕГДА, даже без подстановок: plain() при пустых kwargs
        # отдаёт шаблон как есть, и забытый параметр уезжал владельцу
        # буквальным «{cycles}» — это не сообщение, а мусор
        template = TEMPLATES[key].get(lang) or TEMPLATES[key][DEFAULT_LANG]
        holes = {name.split("[")[0].split(".")[0]
                 for _, name, _, _ in Formatter().parse(template) if name}
        unused = set(params) - holes
        if unused:
            # format молча игнорирует лишние аргументы: убрали плейсхолдер из
            # шаблона (правка перевода) — и данные исчезли из алерта незаметно
            log.warning("причина %r (%s): подстановки не использованы — %s",
                        key, lang, ", ".join(sorted(unused)))
        return template.format(**params)
    # шаблон — данные, а не код: сломать его можно многими способами
    # («{cycles.typo}» даёт AttributeError, незакрытая скобка — ValueError), и
    # ни один не вправе отменить доставку сигнала
    except Exception as e:  # noqa: BLE001
        log.error("причина %r не отрендерилась (%s): %r", key, lang, e)
        if not params:
            return key
        return f"{key} " + ", ".join(f"{name}={value}"
                                     for name, value in params.items())


class Reason(str):
    """Причина алерта: готовая русская строка ПЛЮС ключ и подстановки к ней.

    Служебные пути (календарь, синк, напоминания, воркер, диалог) знают причину,
    но не знают языка получателя: у каждого админ-чата клиники он свой, и решить
    можно только при отправке. Поэтому причина остаётся обычной строкой — логи,
    фейковые нотификаторы и `in`-проверки работают как раньше, — а
    TelegramEscalation достаёт из неё ключ и рендерит на язык чата."""

    key: str
    params: dict

    def __new__(cls, key: str, **params):
        reason = super().__new__(cls, render_reason(key, DEFAULT_LANG, params))
        reason.key, reason.params = key, params
        return reason


def admin_lang_resolver(session_factory, clinic_id):
    """Функция «chat_id → язык консоли» для алертов (карта, №16).

    Отдельно от AdminConsole: нотификатор создаётся раньше воркера и
    не должен знать про консоль."""
    from navbat.db.base import tenant_transaction
    from navbat.dialog.conversation import load_conversation

    def resolve(chat_id: int) -> str:
        try:
            with tenant_transaction(session_factory, clinic_id) as session:
                conv = load_conversation(session, chat_id)
        except Exception as e:  # noqa: BLE001 — алерт важнее языка
            # но не молча: иначе узбекский владелец получает русский экран, и
            # причину деградации никто не видит (ревью)
            log.warning("язык админ-чата %s не определён (%r) — русский",
                        chat_id, e)
            return DEFAULT_LANG
        lang = conv.context.extras.get("adm_lang")
        return lang if lang in LANGS else DEFAULT_LANG

    return resolve


def menu_key(label: str) -> str | None:
    """Ключ кнопки верхнего меню по её подписи — на ЛЮБОМ языке.

    Reply-клавиатура остаётся у админа на экране и после смены языка:
    тап по старой кнопке обязан сработать, а не молча открыть меню."""
    for key in ("btn_services", "btn_doctors", "btn_about", "btn_dayoff",
                "btn_today_list", "btn_stats", "btn_pause", "btn_resume",
                "btn_lang", "btn_preview"):
        if any(TEMPLATES[key][lang] == label for lang in LANGS):
            return key
    return None
