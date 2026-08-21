"""Приёмочный тест BRIEF: ручное событие поверх записи бота.

Приоритет у ручного (человек физически в кресле): бот переносит свою
запись на ближайший слот, уведомляет пациента (с кнопками альтернатив)
и админа. Не молчаливое отклонение.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, make_doctor, next_monday
from navbat.calendar.sync import CalendarSync
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Button, Reply
from navbat.nlu.extractor import FakeExtractor
from navbat.scheduling.engine import SchedulingEngine
from navbat.telegram.worker import UpdateWorker, send_reply
from test_dialog_booking import CHAT, RecordingNotifier
from test_gcal_export import CAL, FakeCalendarAPI, bind_calendar, book
from test_tg_worker import FakeTelegramAPI, put_callback


def make_conflict_sync(app_session_factory, clinic_id):
    api = FakeCalendarAPI()
    notifier = RecordingNotifier()
    tg_api = FakeTelegramAPI()
    sync = CalendarSync(app_session_factory, clinic_id, api=api,
                        notifier=notifier, tg_api=tg_api)
    return sync, api, notifier, tg_api


def bot_rows(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT status, lower(time_range) AS start FROM appointment "
            "WHERE source = 'bot' ORDER BY created_at"
        )).all()


def import_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM appointment "
            "WHERE source = 'gcal_import' AND status = 'booked'"
        )).scalar_one()


def test_manual_event_over_booking_moves_it_and_notifies(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)  # событие бота уехало в календарь

    # админ руками вписал пациента с улицы прямо поверх
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    # ручной блок встал
    assert import_count(admin_engine) == 1
    # ботовская переехала на 10:00 (09:30 не подходит: буфер 10 мин после
    # ручного приёма) — той же записью, а не «отмена + новая»
    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["booked"]
    assert rows[0].start == at_tashkent(day, "10:00")

    # пациент уведомлён, с кнопками альтернатив
    assert tg_api.sent, "пациент должен получить уведомление о переносе"
    chat_id, message_text, buttons = tg_api.sent[-1]
    assert chat_id == CHAT
    assert "10:00" in message_text
    assert buttons, "кнопки альтернативных слотов"
    with admin_engine.begin() as conn:
        conv = conn.execute(text(
            "SELECT fsm_state, context FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).one()
    assert conv.fsm_state == "resched_offer_slots"
    # кнопка несёт слот сама: сообщение висит до приёма, а карта кнопок чата
    # перезаписывается любой следующей отправкой
    assert buttons[0].action.startswith("reslot:")
    assert "tg_actions" not in conv.context

    # админ уведомлён
    assert notifier.calls

    # календарь согласован: событие бота сдвинуто на 10:00 (patch, не пересоздание)
    confirmed = [e for e in api.events(CAL).values()
                 if e["status"] == "confirmed" and "extendedProperties" in e]
    assert len(confirmed) == 1
    assert confirmed[0]["start"]["dateTime"] == at_tashkent(day, "10:00").isoformat()
    assert api.insert_calls == 1, "перенос жертвы — patch события, не новое"


def test_no_alternative_slot_cancels_and_notifies(app_session_factory, admin_engine,
                                                  clinic_a, service_cleaning):
    # врач работает один слот в неделю: пн 09:00–09:30
    doctor = make_doctor(admin_engine, clinic_a,
                         intervals={"mon": [["09:00", "09:30"]]})
    bind_calendar(admin_engine, doctor)
    day = next_monday()
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)

    # будущие понедельники заняты заранее (скан переноса — 14 дней)
    from datetime import timedelta
    api.seed_manual_event(day=day + timedelta(days=7))
    api.seed_manual_event(day=day + timedelta(days=14))
    sync.sync_doctor(doctor)

    book(app_session_factory, clinic_a, doctor, service_cleaning, day, "09:00",
         chat_id=CHAT)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor)

    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["cancelled"], "переносить некуда — отмена"
    assert import_count(admin_engine) == 3
    assert tg_api.sent and not tg_api.sent[-1][2], "уведомление без кнопок"
    assert notifier.calls


def test_relocation_reserves_replacement_before_cancelling_victim(
        monkeypatch, app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """H2 crash-window: отмена жертвы раньше замены теряла запись пациента.

    Перенос шёл тремя транзакциями (cancel → hold → confirm): падение
    процесса между ними оставляло пациента без записи навсегда — жертва
    отменена, замены нет, следующий цикл синка её уже не увидит.
    Инвариант: пока замена не существует, запись пациента не отменяется."""
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)  # событие бота уехало в календарь

    from navbat.scheduling.engine import SchedulingEngine

    original_cancel = SchedulingEngine.cancel

    def dying_cancel(self, appointment_id, actor=None):
        # процесс умирает сразу ПОСЛЕ коммита отмены — худший момент краша
        original_cancel(self, appointment_id, actor)
        raise RuntimeError("процесс убит сразу после отмены жертвы")

    monkeypatch.setattr(SchedulingEngine, "cancel", dying_cancel)

    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    # запись пациента цела и переехала — одной строкой, без промежуточной отмены
    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["booked"], "запись пациента потеряна"
    assert rows[0].start == at_tashkent(day, "10:00")
    assert tg_api.sent, "пациент должен узнать о переносе"


def test_relocate_failure_does_not_silently_lose_patient(
        monkeypatch, app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    # Слот, выбранный для переноса, перехвачен конкурентной бронью между
    # find_free_slots и переносом → SlotTakenError. Пациента НЕЛЬЗЯ терять
    # молча: он и админ обязаны быть уведомлены, а sync_doctor не падать.
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)  # событие бота уехало в календарь

    from navbat.scheduling.engine import SchedulingEngine
    from navbat.scheduling.errors import SlotTakenError

    def failing_reschedule(self, *args, **kwargs):
        raise SlotTakenError("слот перехвачен конкурентной записью")

    monkeypatch.setattr(SchedulingEngine, "reschedule", failing_reschedule)

    # админ вписал пациента с улицы прямо поверх записи бота
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)  # не бросает, не теряет пациента молча

    # жертва отменена (её слот занял ручной приём), новой брони нет
    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["cancelled"]
    # но пациент и админ уведомлены — деградация в «перенести некуда», не тишина
    assert tg_api.sent, "пациент должен быть уведомлён при сорванном переносе"
    assert notifier.calls, "админ должен быть уведомлён при сорванном переносе"


def test_victim_gone_before_relocation_is_not_reported_as_cancelled(
        monkeypatch, app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Пациент сам отменил запись между сканом жертв и переносом.

    Переносить нечего: синк не падает и не рассказывает пациенту про
    отмену, которой бот не делал."""
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)

    from navbat.scheduling.engine import SchedulingEngine

    original_reschedule = SchedulingEngine.reschedule

    def cancel_then_reschedule(self, appt_id, new_start, **kwargs):
        # гонка: запись ушла из-под переноса ровно перед ним
        sched.cancel(appt_id)
        return original_reschedule(self, appt_id, new_start, **kwargs)

    monkeypatch.setattr(SchedulingEngine, "reschedule", cancel_then_reschedule)

    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)  # не бросает

    assert [r.status for r in bot_rows(admin_engine)] == ["cancelled"]
    assert not tg_api.sent, "пациенту нечего сообщать: он отменил запись сам"


def test_conflict_with_live_hold_waits_for_expiry(app_session_factory, admin_engine,
                                                  clinic_a, doctor_a, service_cleaning):
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    sched = SchedulingEngine(app_session_factory, clinic_a)
    sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"), tg_chat_id=CHAT)

    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    # живой hold не трогаем — пациент прямо сейчас выбирает; импорт отложен
    assert import_count(admin_engine) == 0
    with admin_engine.begin() as conn:
        status = conn.execute(text(
            "SELECT status FROM appointment WHERE source = 'bot'")).scalar_one()
    assert status == "hold"

    # hold протух — следующий цикл дотащил импорт
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE appointment SET hold_expires_at = "
                          "now() - interval '1 minute' WHERE status = 'hold'"))
    sync.sync_doctor(doctor_a)
    assert import_count(admin_engine) == 1


def test_partial_overlap_relocates_into_slot_freed_by_the_move(
        app_session_factory, admin_engine, clinic_a, service_cleaning):
    """Ручное событие перекрывает запись частично.

    Соседний слот занят только самой жертвой — переезжая, она его и
    освобождает. Резерв замены до отмены не должен стоить пациенту дня
    ожидания: слот дня остаётся в выборе."""
    # окно врача — ровно два слота: 09:00 и 09:30
    doctor = make_doctor(admin_engine, clinic_a,
                         intervals={"mon": [["09:00", "10:00"]]})
    bind_calendar(admin_engine, doctor)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor, service_cleaning, day, "09:30",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor)

    # приём с улицы на 09:50–10:20 — хвост записи бота задет, начало свободно
    api.seed_manual_event(start=at_tashkent(day, "09:50"),
                          end=at_tashkent(day, "10:20"))
    sync.sync_doctor(doctor)

    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["booked"]
    assert rows[0].start == at_tashkent(day, "09:00"), \
        "пациента унесло дальше, хотя слот 09:00 освобождается самим переносом"
    assert import_count(admin_engine) == 1


def test_patient_reschedule_wins_over_stale_relocation(
        monkeypatch, app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Пациент перенёс запись сам за миг до переноса синком.

    Синк действует по снимку, снятому до этого: применять свой перенос к
    изменившейся записи он не вправе — иначе выбор пациента молча затирается,
    а сам он получает «вас вытеснили» на ровном месте."""
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    sync, api, notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)

    original_reschedule = SchedulingEngine.reschedule
    patient_choice = at_tashkent(day, "12:00")

    def racing_reschedule(self, appt_id, new_start, **kwargs):
        # пациент успел первым — снимок синка устарел
        original_reschedule(self, appt_id, patient_choice)
        return original_reschedule(self, appt_id, new_start, **kwargs)

    monkeypatch.setattr(SchedulingEngine, "reschedule", racing_reschedule)

    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    rows = bot_rows(admin_engine)
    assert [r.status for r in rows] == ["booked"]
    assert rows[0].start == patient_choice, "выбор пациента затёрт переносом синка"
    assert not tg_api.sent, "пациента никто не вытеснял — извиняться не за что"


def test_alternative_button_survives_a_later_button_send(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Предложение альтернатив висит в чате часами — до самого приёма.

    Карта кнопок одна на чат и перезаписывается ЦЕЛИКОМ любой следующей
    отправкой (инвариант кнопок, CLAUDE.md): номерная кнопка после этого
    уводит в чужое действие. Альтернатива обязана нести слот сама."""
    bind_calendar(admin_engine, doctor_a)
    day = next_monday()
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a,
                             service_cleaning, day, "09:00", chat_id=CHAT)
    sync, api, _notifier, tg_api = make_conflict_sync(app_session_factory, clinic_a)
    sync.sync_doctor(doctor_a)
    api.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_a)

    _chat, _text, buttons = tg_api.sent[-1]
    offered_action = buttons[0].action  # ровно то, что ушло в Telegram
    offered_at = at_tashkent(day, buttons[0].label[-5:])

    # пока предложение висело, пациент начал новую запись — в тот же чат
    # ушли другие кнопки и перезаписали карту
    send_reply(tg_api, app_session_factory, clinic_a, CHAT,
               Reply("Свободное время:", buttons=(
                   Button("12:00",
                          f"slot:{doctor_a}:{at_tashkent(day, '12:00').isoformat()}"),
               )))

    worker = UpdateWorker(
        app_session_factory, clinic_a,
        dialog=DialogEngine(app_session_factory, clinic_a,
                            extractor=FakeExtractor(script=[])),
        api=tg_api, notifier=RecordingNotifier())
    put_callback(app_session_factory, clinic_a, offered_action)
    worker.process_one()

    with admin_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT status, lower(time_range) AS start FROM appointment "
            "WHERE id = :id"), {"id": appointment_id}).one()
    assert (row.status, row.start) == ("booked", offered_at), \
        "тап по альтернативе ушёл в чужое действие: карту кнопок перезаписали"
