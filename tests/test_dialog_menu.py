"""Кнопочный вход: /start, выбор языка, главное меню — всё до NLU.

Нажатие reply-кнопки приходит текстом; FSM матчит label (оба языка)
до экстрактора — CountingExtractor доказывает ноль вызовов NLU.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES, menu_rows
from navbat.nlu.extractor import ExtractionError
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    appt_status,
    explicit,
    extr,
    fsm_state,
    slot_buttons,
)
from test_dialog_contact import CountingExtractor


def counting_engine(app_session_factory, clinic_id, script=()):
    extractor = CountingExtractor(list(script))
    engine = DialogEngine(app_session_factory, clinic_id, extractor=extractor,
                          notifier=RecordingNotifier())
    return engine, extractor


# ── /start и язык ────────────────────────────────────────────────────────────

def test_start_first_time_offers_language_choice(app_session_factory, clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    reply = engine.handle_text(CHAT, "/start")
    assert [b.action for b in reply.buttons] == ["lang:uz", "lang:ru"]
    assert "Tilni tanlang" in reply.text
    assert "Clinic A" not in reply.text, "приветствие — после выбора языка"
    assert extractor.calls == [], "/start не должен уходить в NLU"


def test_lang_choice_shows_greeting_with_menu(app_session_factory, admin_engine,
                                              clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    reply = engine.handle_action(CHAT, "lang:uz")
    assert reply.menu == menu_rows("uz")
    assert "Clinic A" in reply.text, "приветствие-дисклеймер (P0 BRIEF)"
    assert extractor.calls == []
    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "uz"


def test_start_repeat_skips_language_choice(app_session_factory, clinic_a):
    engine, _ = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, "lang:ru")
    again = engine.handle_text(CHAT, "/start")
    assert again.menu == menu_rows("ru")
    assert not again.buttons, "язык уже выбран — сразу меню"


def test_explicit_lang_survives_nlu_misdetection(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a,
                                                 service_cleaning):
    # пациент явно выбрал узбекский кнопкой; NLU на узбекской кириллице
    # массово ошибается language='ru' (eval 12.06.2026) — явный выбор
    # пациента главнее самого слабого поля модели
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", language="ru")])  # ← ошибка модели
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, "lang:uz")
    reply = engine.handle_text(CHAT, "Тиш тозалашга ёзилмоқчиман")
    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "uz", "явный выбор языка не перебивается детектом NLU"
    assert "Qaysi kun" in reply.text, "ответ остаётся на узбекском"


def test_start_after_text_dialog_keeps_detected_lang(app_session_factory, clinic_a,
                                                     doctor_a, service_cleaning):
    # пациент начал текстом (язык детектнут NLU) — /start не переспрашивает язык
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()),
                     language="uz")])
    engine.handle_text(CHAT, "ertaga tish tozalashga yozilmoqchiman")
    reply = engine.handle_text(CHAT, "/start")
    assert reply.menu == menu_rows("uz")
    assert not reply.buttons


def test_uzbek_cyrillic_first_contact_overrides_nlu_ru(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # живой тест 12.06: «Тишим оғрияпти...» → NLU сказал ru → пациент,
    # пишущий уз-кириллицей, получил русский интерфейс. Буквы ўқғҳ не
    # существуют в русском — детерминированный код-слой главнее модели
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", language="ru")])  # ← ошибка модели
    reply = engine.handle_text(CHAT, "тиш тозалашга ёзилмоқчиман")

    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "uz", "уз-кириллица (ўқғҳ) перебивает детект NLU"
    assert "Qaysi kun" in reply.text


def test_russian_first_contact_keeps_nlu_detection(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # обычная кириллица без ўқғҳ — эвристика молчит, решает NLU
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", language="ru")])
    engine.handle_text(CHAT, "запишите на чистку")

    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "ru"


# ── Кнопки меню: запись, перенос, отмена ─────────────────────────────────────

def start_with_menu(engine, lang="ru"):
    """Доводит чат до состояния «меню показано, язык выбран»."""
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, f"lang:{lang}")


def test_menu_book_starts_booking_without_nlu(app_session_factory, admin_engine,
                                              clinic_a, doctor_a, service_cleaning):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert fsm_state(admin_engine) == "booking_collect"
    assert extractor.calls == [], "кнопка меню не должна уходить в NLU"


def test_menu_book_uz_label_matches_too(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine, lang="uz")
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["uz"])
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert extractor.calls == []


def test_menu_book_mid_booking_releases_hold(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    # пациент дошёл до шага имени (hold создан) и передумал — жмёт «Записаться»
    engine, extractor = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()))])
    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    assert fsm_state(admin_engine) == "awaiting_name"

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert appt_status(admin_engine) == "cancelled", "hold отпущен"
    assert "service:cleaning" in [b.action for b in reply.buttons]
    assert len(extractor.calls) == 1, "только исходная фраза, кнопка — нет"


def test_menu_resched_without_appointment(app_session_factory, clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_resched"]["ru"])
    assert reply.text == TEMPLATES["resched_none"]["ru"]
    assert extractor.calls == []


def test_menu_resched_with_booking_asks_date(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appt = sched.hold(doctor_a, service_cleaning,
                      at_tashkent(next_monday(), "09:00"), tg_chat_id=CHAT)
    sched.confirm(appt)

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_resched"]["ru"])
    assert any(b.action.startswith("date:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "resched_offer_slots"
    assert extractor.calls == []


def test_menu_cancel_confirms_active_booking(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    sched = SchedulingEngine(app_session_factory, clinic_a)
    appt = sched.hold(doctor_a, service_cleaning,
                      at_tashkent(next_monday(), "09:00"), tg_chat_id=CHAT)
    sched.confirm(appt)

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_cancel"]["ru"])
    assert [b.action for b in reply.buttons] == ["cancel_yes", "cancel_no"]
    assert extractor.calls == []

    done = engine.handle_action(CHAT, "cancel_yes")
    assert done.text == TEMPLATES["cancel_done"]["ru"]
    assert appt_status(admin_engine) == "cancelled"



def test_menu_cancel_mid_booking_cancels_hold_directly(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # «Отменить» посреди оформления = отказ от него: без вопроса-подтверждения
    engine, extractor = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()))])
    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    assert fsm_state(admin_engine) == "awaiting_name"

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_cancel"]["ru"])
    assert reply.text == TEMPLATES["cancel_done"]["ru"]
    assert appt_status(admin_engine) == "cancelled"
    assert fsm_state(admin_engine) == "idle"
    assert len(extractor.calls) == 1, "кнопка в NLU не уходит"

def test_menu_in_escalated_state_stays_blocked(app_session_factory, admin_engine,
                                               clinic_a):
    # эскалация — по прямой просьбе человека (П-2а), NLU не дёргается
    engine, _ = counting_engine(app_session_factory, clinic_a, script=[])
    engine.handle_text(CHAT, "позовите администратора")
    assert fsm_state(admin_engine) == "escalated"

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])
    assert reply.text == TEMPLATES["escalated"]["ru"], \
        "кнопки не обходят стоп-состояние"


# ── Прайс и язык ─────────────────────────────────────────────────────────────

def test_menu_prices_lists_catalog(app_session_factory, admin_engine, clinic_a,
                                   service_cleaning):
    # услуга с ценой и услуга без цены — обе в списке
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET price = 200000 WHERE name = 'cleaning'"))
    make_service(admin_engine, clinic_a, "checkup", 30)  # без цены

    engine, extractor = counting_engine(app_session_factory, clinic_a)
    start_with_menu(engine)
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_prices"]["ru"])
    assert "Чистка — 200 000 сум" in reply.text
    assert "Осмотр — цену уточнит администратор" in reply.text
    assert extractor.calls == []


def test_menu_prices_mid_booking_reprompts_step(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    # вопрос цены посреди записи — ответ + повтор шага, сценарий не сброшен
    engine, _ = counting_engine(app_session_factory, clinic_a,
                                script=[extr(service="cleaning")])
    engine.handle_text(CHAT, "хочу чистку")          # booking_collect, спросил дату
    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_prices"]["ru"])
    assert TEMPLATES["price_header"]["ru"] in reply.text
    assert TEMPLATES["ask_date"]["ru"] in reply.text, "шаг повторён"
    assert any(b.action.startswith("date:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "booking_collect"


def test_menu_lang_switch_mid_booking_reprompts_in_new_lang(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    engine, _ = counting_engine(app_session_factory, clinic_a,
                                script=[extr(service="cleaning")])
    engine.handle_text(CHAT, "хочу чистку")          # booking_collect (ru)
    screen = engine.handle_text(CHAT, TEMPLATES["btn_menu_lang"]["ru"])
    assert [b.action for b in screen.buttons] == ["lang:uz", "lang:ru"]

    reply = engine.handle_action(CHAT, "lang:uz")
    assert TEMPLATES["lang_changed"]["uz"] in reply.text
    assert TEMPLATES["ask_date"]["uz"] in reply.text, "повтор шага на новом языке"
    assert fsm_state(admin_engine) == "booking_collect"


def test_other_intent_first_contact_keeps_menu(app_session_factory, clinic_a):
    # off-topic на первом контакте: приветствие не должно стирать кнопки меню
    # (M7 + фикс greeting-wrap, который раньше терял menu/contact_request)
    engine, _ = counting_engine(app_session_factory, clinic_a, [extr(intent="other")])
    reply = engine.handle_text(CHAT, "просто поболтать")
    assert reply.menu == menu_rows("ru")
