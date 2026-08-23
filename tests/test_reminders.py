"""Напоминания 24ч/2ч: reconciliation из БД (не таймеры в памяти), retry, кнопки.

Требование BRIEF: вычисляются запросом к appointment — переживают рестарт;
трекинг доставки; backoff → dead letter → алерт.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.reminders import ReminderService
from test_dialog_booking import CHAT, RecordingNotifier
from test_gcal_export import book
from test_tg_worker import FakeTelegramAPI

# запись далеко в будущем: оба дефолтных офсета (24ч/2ч) гарантированно впереди
def far_monday():
    return next_monday() + timedelta(days=7)


def make_service_obj(app_session_factory, clinic_id, **kwargs):
    api = FakeTelegramAPI()
    notifier = RecordingNotifier()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=notifier, **kwargs)
    return service, api, notifier


def reminder_rows(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT kind, status, send_at FROM reminder ORDER BY send_at"
        )).all()


def ripen_all(admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE reminder SET send_at = now() - interval '1 minute' "
                          "WHERE status = 'pending'"))


def test_reconcile_creates_reminders_per_offset(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    day = far_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert [r.status for r in rows] == ["pending", "pending"]
    starts = {r.kind: r.send_at for r in rows}
    appointment_start = at_tashkent(day, "09:00")
    assert starts["1440m"] == appointment_start - timedelta(hours=24)
    assert starts["120m"] == appointment_start - timedelta(hours=2)

    service.reconcile()  # идемпотентность
    assert len(reminder_rows(admin_engine)) == 2


def test_past_offset_is_not_created(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    # офсет 60 дней — send_at в прошлом, такое напоминание бессмысленно
    service, _, _ = make_service_obj(
        app_session_factory, clinic_a,
        offsets=(timedelta(days=60), timedelta(hours=2)))
    service.reconcile()
    assert [r.kind for r in reminder_rows(admin_engine)] == ["120m"]


def test_reschedule_moves_send_at(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    day = far_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    sched.reschedule(appointment_id, at_tashkent(day, "11:00"))
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert len(rows) == 2
    starts = {r.kind: r.send_at for r in rows}
    assert starts["120m"] == at_tashkent(day, "11:00") - timedelta(hours=2)


def test_cancelled_appointment_cancels_pending(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, far_monday(), "09:00",
                                 chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    sched.cancel(appointment_id)
    service.reconcile()
    assert all(r.status == "cancelled" for r in reminder_rows(admin_engine))


def test_send_due_sends_only_ripe_with_buttons(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    assert service.send_due() == 0, "оба напоминания ещё не созрели"
    ripen_all(admin_engine)
    assert service.send_due() == 2

    chat_id, message_text, buttons = api.sent[0]
    assert chat_id == CHAT
    assert "09:00" in message_text
    assert len(buttons) == 2
    # кнопки напоминания несут запись сами (сырой callback) и номер в карте не
    # занимают — сообщение висит до приёма и переживает любую следующую отправку
    assert buttons[0].action.startswith("attend:")
    assert buttons[1].action.startswith("remind_cancel:")
    with admin_engine.begin() as conn:
        dialogs = conn.execute(text(
            "SELECT count(*) FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).scalar_one()
    assert dialogs == 0, \
        "напоминание перезаписывает диалог пациента из фонового потока"

    assert service.send_due() == 0, "sent не переотправляются"
    statuses = {r.status for r in reminder_rows(admin_engine)}
    assert statuses == {"sent"}


def test_reminder_about_passed_appointment_is_not_sent(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a, service_cleaning):
    """Приём уже прошёл, а напоминание о нём осталось pending.

    Так бывает после простоя процесса (обновление, упавший хост) и после
    переноса записи ближе, чем офсет: строка остаётся с прежним send_at.
    Статус приёма после визита никто не меняет — он вечно 'booked', поэтому
    сама по себе такая строка не гаснет. Отправлять её нельзя: пациент получит
    напоминание о визите, который состоялся, с живой кнопкой «Отменить
    запись» — а отмена из напоминания идёт в сводку владельца как
    предотвращённая неявка с деньгами."""
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()
    with admin_engine.begin() as conn:  # приём состоялся, пока процесс стоял
        conn.execute(text(
            "UPDATE appointment SET time_range = tstzrange("
            "now() - interval '3 hours', now() - interval '2 hours', '[)')"))
    ripen_all(admin_engine)

    service.reconcile()
    assert service.send_due() == 0, "напоминание о прошедшем приёме ушло пациенту"
    assert api.sent == []
    assert {r.status for r in reminder_rows(admin_engine)} == {"cancelled"}, \
        "просроченная строка осталась pending и будет тянуться вечно"


def test_appointment_moved_back_to_future_rearms_reminder(app_session_factory,
                                                          admin_engine, clinic_a,
                                                          doctor_a,
                                                          service_cleaning):
    """Запись переехала из прошлого в будущее — напоминание обязано вернуться.

    Гашение напоминаний о начавшихся приёмах не должно становиться билетом
    в один конец: приём можно перенести и после его начала (правка события
    в Google, ручной перенос), и тогда пациент остался бы без напоминания
    о новом времени."""
    day = far_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()
    with admin_engine.begin() as conn:  # приём начался, пока процесс стоял
        conn.execute(text(
            "UPDATE appointment SET time_range = tstzrange("
            "now() - interval '1 hour', now() - interval '30 minutes', '[)')"))
    service.reconcile()
    assert {r.status for r in reminder_rows(admin_engine)} == {"cancelled"}

    new_start = at_tashkent(day, "15:00")
    sched.reschedule(appointment_id, new_start)
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert [r.status for r in rows] == ["pending", "pending"], \
        "погашенное напоминание не вернулось — о новом времени никто не скажет"
    assert {r.kind: r.send_at for r in rows}["120m"] == \
        new_start - timedelta(hours=2)


def test_reminder_buttons_belong_to_their_own_appointment(app_session_factory,
                                                          admin_engine, clinic_a,
                                                          doctor_a,
                                                          service_cleaning):
    """Напоминание висит в чате часами, а карта кнопок одна на чат.

    Пронумерованная кнопка (a:N) относится к ПОСЛЕДНЕЙ отправке: следующий
    фоновой пуш (напоминание о другой записи, предложение из листа ожидания,
    любой список слотов) перетирает map, и «Отменить запись» под старым
    сообщением начинает указывать на чужую запись. Кнопки напоминания обязаны
    нести свою запись сами — сырым callback'ом, как cal:/wl:/unfreeze."""
    day = far_monday()
    first_id, _ = book(app_session_factory, clinic_a, doctor_a, service_cleaning,
                       day, "09:00", chat_id=CHAT)
    second_id, _ = book(app_session_factory, clinic_a, doctor_a, service_cleaning,
                        day, "11:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a,
                                       offsets=(timedelta(hours=2),))
    service.reconcile()
    ripen_all(admin_engine)
    assert service.send_due() == 2, "по напоминанию на каждую запись"

    early_text, early_buttons = api.sent[0][1], api.sent[0][2]
    assert "09:00" in early_text, "первым уходит напоминание о ранней записи"
    actions = {b.action for b in early_buttons}
    assert actions == {f"attend:{first_id}", f"remind_cancel:{first_id}"}, \
        "кнопки раннего напоминания указывают на чужую запись"


def test_tap_on_early_reminder_cancels_that_appointment(app_session_factory,
                                                        admin_engine, clinic_a,
                                                        doctor_a,
                                                        service_cleaning):
    """Сквозь адаптер: пациент возвращается к раннему напоминанию и отменяет.

    Отменена обязана быть та запись, о которой сообщение, а не последняя,
    занявшая номер в карте кнопок."""
    from test_tg_worker import make_worker, put_callback

    day = far_monday()
    first_id, _ = book(app_session_factory, clinic_a, doctor_a, service_cleaning,
                       day, "09:00", chat_id=CHAT)
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         day, "11:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a,
                                       offsets=(timedelta(hours=2),))
    service.reconcile()
    ripen_all(admin_engine)
    assert service.send_due() == 2

    # ровно то, что Telegram пришлёт обратно при тапе под ранним сообщением
    early_cancel = api.sent[0][2][1].action
    worker, _, _ = make_worker(app_session_factory, clinic_a, [], api=api)
    put_callback(app_session_factory, clinic_a, early_cancel)
    assert worker.process_one()

    with admin_engine.begin() as conn:
        actions = conn.execute(text(
            "SELECT context -> 'tg_actions' FROM conversation "
            "WHERE tg_chat_id = :c"), {"c": CHAT}).scalar_one() or {}
    yes = next(f"a:{i}" for i, action in actions.items() if action == "cancel_yes")
    put_callback(app_session_factory, clinic_a, yes)
    assert worker.process_one()

    with admin_engine.begin() as conn:
        cancelled = conn.execute(text(
            "SELECT id FROM appointment WHERE status = 'cancelled'")).scalars().all()
    assert cancelled == [first_id], \
        "отменена не та запись, о которой было напоминание"


def test_send_failures_go_to_dead_letter_with_alert(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, notifier = make_service_obj(
        app_session_factory, clinic_a, offsets=(timedelta(hours=2),))
    service.reconcile()
    ripen_all(admin_engine)

    api.send_failures = 3
    for _ in range(3):
        service.send_due()

    assert [r.status for r in reminder_rows(admin_engine)] == ["failed"]
    assert notifier.calls, "dead letter напоминания — алерт админу"
    assert service.send_due() == 0


def test_reschedule_after_send_rearms_reminder(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    """Запись перенесли после того, как напоминание уже ушло.

    Пациент обязан получить новое — на новое время. Раньше перенос
    вытесненной записи создавал новую строку appointment, и напоминание
    заводилось само собой; атомарный перенос сохраняет id, а ON CONFLICT
    обновлял только pending — второго напоминания не было бы вовсе."""
    day = far_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()
    ripen_all(admin_engine)
    assert service.send_due() == 2
    assert [r.status for r in reminder_rows(admin_engine)] == ["sent", "sent"]

    new_start = at_tashkent(day, "15:00")
    sched.reschedule(appointment_id, new_start)
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert [r.status for r in rows] == ["pending", "pending"], \
        "напоминание не перевзведено — пациент не узнает о новом времени"
    send_at = {r.kind: r.send_at for r in rows}
    assert send_at["1440m"] == new_start - timedelta(hours=24)
    assert send_at["120m"] == new_start - timedelta(hours=2)


def test_delivery_does_not_bury_rearmed_reminder(monkeypatch, app_session_factory,
                                                 admin_engine, clinic_a, doctor_a,
                                                 service_cleaning):
    """Запись переехала, пока напоминание отправлялось.

    Отметка «отправлено» относится к той версии напоминания, которую взяли
    в работу. Если за время отправки его перевзвели на новое время, гасить
    его нельзя — иначе о переносе пациенту никто не напомнит."""
    import navbat.reminders as reminders_module

    day = far_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    service, _, _ = make_service_obj(
        app_session_factory, clinic_a, offsets=(timedelta(hours=24),))
    service.reconcile()
    ripen_all(admin_engine)

    new_start = at_tashkent(day, "15:00")
    original_send_reply = reminders_module.send_reply

    def send_then_reschedule(*args, **kwargs):
        result = original_send_reply(*args, **kwargs)
        # запись переносят ровно между отправкой и отметкой «sent»
        sched.reschedule(appointment_id, new_start)
        service.reconcile()
        return result

    monkeypatch.setattr(reminders_module, "send_reply", send_then_reschedule)
    service.send_due()

    rows = reminder_rows(admin_engine)
    assert [r.status for r in rows] == ["pending"], \
        "перевзведённое напоминание погашено отметкой об отправке"
    assert rows[0].send_at == new_start - timedelta(hours=24)


def test_reminder_card_shows_doctor_and_address(app_session_factory, admin_engine,
                                                clinic_a, service_cleaning):
    """Пациент идёт на приём по напоминанию — «к кому и куда» обязано быть
    в нём самом, а не в глубине чата."""
    from conftest import make_doctor
    from navbat.onboard import set_clinic_address

    doctor = make_doctor(admin_engine, clinic_a, name="Алиев")
    set_clinic_address(app_session_factory, clinic_a, "Ташкент, ул. Навои, 10")
    book(app_session_factory, clinic_a, doctor, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a,
                                       offsets=(timedelta(hours=2),))
    service.reconcile()
    ripen_all(admin_engine)
    assert service.send_due() == 1

    sent_text = api.sent[0][1]
    assert "\n👨‍⚕️ Алиев" in sent_text, "врач — отдельной строкой напоминания"
    assert "\n📍 Ташкент, ул. Навои, 10" in sent_text, \
        "адрес — отдельной строкой напоминания"
