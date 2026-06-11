"""Узбекские строки replies.py: правки ревью (раунд 1, кросс-проверка
LLM 07.06.2026) + инварианты шаблонов.

Решения раунда зафиксированы в docs/UZ_STRINGS.md: терминология «qabul»
вместо «yozuv», аффикс «dagi» вместо «kungi» при дате-времени,
смягчённый reask; ASCII-апостроф оставлен намеренно.
"""
from __future__ import annotations

import string

import pytest

from navbat.dialog.replies import TEMPLATES, t

# точные значения, принятые по итогам ревью; ключ -> новый uz-текст
REVIEWED_UZ = {
    # П-7: hero-обвязка; ревью-формулировки сохранены внутри
    "greeting": (
        "🦷 <b>{clinic}</b>\nVirtual administrator · yozilish 24/7\n\n"
        "Qabulga yozilish, uni boshqa vaqtga ko'chirish yoki bekor "
        "qilishda yordam beraman — tashrif oldidan eslatib qo'yaman. "
        "Tibbiy savollarga shifokor javob beradi.\n\n"
        "Boshlaymizmi? 👇"
    ),
    "hold_expired": (
        "Tanlangan vaqtni band qilish muddati tugadi. Mana yangi variantlar:"
    ),
    "cancel_confirm_q": "❌ {when} dagi qabulni bekor qilaymi?",
    "cancel_done": "✅ Qabul bekor qilindi. Sizni yana kutib qolamiz.",
    "cancel_kept": "Yaxshi, qabul o'z kuchida qoladi.",
    "cancel_none": "Faol qabul topilmadi. Yozilishni xohlaysizmi?",
    "resched_none": (
        "Boshqa vaqtga ko'chirish uchun faol qabul topilmadi. "
        "Yozilishni xohlaysizmi?"
    ),
    "resched_done": "✅ <b>KO'CHIRILDI</b>\n\n📅 {when}\n\nSizni kutamiz!",
    "conflict_moved": (
        "Afsuski, {old} vaqti band bo'lib qoldi — qabulni {new} ga "
        "ko'chirdim. To'g'ri kelmasa, boshqasini tanlang:"
    ),
    "conflict_cancelled": (
        "Afsuski, {old} vaqti band bo'lib qoldi, yaqin kunlarda bo'sh vaqt "
        "yo'q — qabul bekor qilindi. Yozing, boshqa vaqt topamiz."
    ),
    "reask": (
        "Kechirasiz, tushunmadim. Boshqacha yozib ko'ring — masalan: "
        "«ertaga tish tozalashga yozilmoqchiman»."
    ),
    "escalated": (
        "👤 Administratorga ulab berdim — u tez orada shu yerda javob beradi."
    ),
    "stale_button": "Bu tugma endi faol emas.",
    "btn_remind_cancel": "Qabulni bekor qilish",
}

# сэмплы всех подстановок: str.format игнорирует лишние именованные аргументы
SAMPLE_VALUES = {
    "date": "08.06",
    "asked": "08.06",
    "when": "08.06 15:30",
    "old": "08.06 15:30",
    "new": "09.06 11:00",
    "service": "Tish tozalash",
    "doctor": ", Dilshod Karimov",
    "price": "150 000",
    "clinic": "Shifo Dent",
    "open": "09:00",
    "close": "18:00",
    "address": "Toshkent, Navoiy ko'chasi 10",
}


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template)
            if field}


@pytest.mark.parametrize("key", sorted(REVIEWED_UZ))
def test_reviewed_uzbek_text_applied(key):
    assert TEMPLATES[key]["uz"] == REVIEWED_UZ[key]


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_placeholders_match_between_languages(key):
    """Набор подстановок в ru и uz обязан совпадать — иначе format() упадёт
    в одном языке и промолчит в другом."""
    assert _placeholders(TEMPLATES[key]["ru"]) == \
        _placeholders(TEMPLATES[key]["uz"])


@pytest.mark.parametrize("key", sorted(TEMPLATES))
@pytest.mark.parametrize("lang", ["ru", "uz"])
def test_every_template_renders_with_samples(key, lang):
    """Каждый шаблон рендерится с реальными форматами дат/цен:
    ловит кривые скобки и опечатки в именах подстановок."""
    rendered = t(key, lang, **SAMPLE_VALUES)
    assert "{" not in rendered and "}" not in rendered
