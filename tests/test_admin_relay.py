"""Канал «админ ↔ замороженный пациент» через бота: якоря, карточки, ответы.

Админ отвечает пациенту реплаем (Telegram reply) на алерт эскалации, а Bot API
отдаёт только id сообщения, на которое ответили. Связь «сообщение в админ-чате
→ пациент» держит якорь: парсинг chat_id из текста алерта отвергнут (шаблоны
двуязычные и меняются).
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Reply, t
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram import relay_repo
from navbat.telegram.admin_texts import at
from navbat.telegram.escalation import TelegramEscalation, build_escalation
from navbat.telegram.queue import enqueue
from navbat.telegram.worker import UpdateWorker

from test_tg_worker import UPDATE_SEQ, FakeTelegramAPI, put_callback, put_message

ADMIN_A, ADMIN_B = 7001, 7002
PATIENT = 5555


def anchors(app_session_factory, clinic_id) -> list:
    with tenant_transaction(app_session_factory, clinic_id) as session:
        return list(session.execute(text(
            "SELECT admin_chat_id, message_id, patient_chat_id "
            "FROM admin_relay ORDER BY id")).all())


def make_dialog(app_session_factory, clinic_id, api, admin_chats) -> DialogEngine:
    """Диалог с НАСТОЯЩИМ телеграм-нотификатором: карточка 💬 и якорь живут
    в нём, фейк-нотификатор соседних тестов их не проверяет."""
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=[]),
                        notifier=build_escalation(api, app_session_factory,
                                                  clinic_id, admin_chats))


def make_relay_worker(app_session_factory, clinic_id, api, admin_chats
                      ) -> UpdateWorker:
    notifier = build_escalation(api, app_session_factory, clinic_id, admin_chats)
    dialog = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=[]), notifier=notifier)
    return UpdateWorker(app_session_factory, clinic_id, dialog=dialog, api=api,
                        notifier=notifier, admin_chat_id=admin_chats)


def put_admin_reply(app_session_factory, clinic_id, admin_chat, text_in,
                    reply_to: int) -> None:
    """Свайп-ответ админа: обычное текстовое сообщение + reply_to_message."""
    update_id = next(UPDATE_SEQ)
    payload = {"update_id": update_id,
               "message": {"chat": {"id": admin_chat}, "text": text_in,
                           "reply_to_message": {"message_id": reply_to}}}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, admin_chat, payload)


def escalate_patient(app_session_factory, clinic_id, worker, api,
                     lang="ru") -> int:
    """Пациент просит человека → алерт в админ-чат. Возвращает message_id
    алерта — позицию (1-based) последнего сообщения админ-чату в фейке
    (фейк нумерует отправки подряд). На него админ и реплаит; ответ самому
    пациенту уходит ПОСЛЕ алерта, поэтому «последняя отправка» не годится."""
    if lang == "uz":
        # язык чата — только явным выбором пациента: /start → экран языка,
        # первая кнопка (a:1) узбекская
        put_message(app_session_factory, clinic_id, "/start", chat_id=PATIENT)
        worker.process_one()
        put_callback(app_session_factory, clinic_id, "a:1", chat_id=PATIENT)
        worker.process_one()
    put_message(app_session_factory, clinic_id, "позовите администратора",
                chat_id=PATIENT)
    worker.process_one()
    return max(i + 1 for i, (chat, _, _) in enumerate(api.sent)
               if chat == ADMIN_A)


# ── Запись якорей при алертах ───────────────────────────────────────────────

def test_escalation_alert_leaves_an_anchor_in_every_admin_chat(
        app_session_factory, clinic_a):
    """Реплаить на алерт может ЛЮБОЙ админ-чат, значит якорь нужен на каждое
    отправленное сообщение — со своим message_id: он уникален внутри чата,
    и один общий увёл бы ответ не тому пациенту."""
    api = FakeTelegramAPI()
    escalation = build_escalation(api, app_session_factory, clinic_a,
                                  [ADMIN_A, ADMIN_B])

    assert escalation.notify(PATIENT, "пациент просит человека", {}) is True

    assert [chat for chat, _, _ in api.sent] == [ADMIN_A, ADMIN_B]
    rows = anchors(app_session_factory, clinic_a)
    assert [(row.admin_chat_id, row.patient_chat_id) for row in rows] == \
        [(ADMIN_A, PATIENT), (ADMIN_B, PATIENT)]
    assert len({row.message_id for row in rows}) == 2, \
        "у каждой отправки свой message_id — якорь обязан взять его из ответа API"
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, rows[0].admin_chat_id,
                                      rows[0].message_id) == PATIENT


def test_notify_without_writer_still_delivers(app_session_factory, clinic_a):
    """CLI и тесты без БД собирают нотификатор напрямую — поведение прежнее:
    якорить некуда, но алерт обязан уйти всем админ-чатам."""
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_A, ADMIN_B])

    assert escalation.notify(PATIENT, "пациент просит человека", {}) is True

    assert [chat for chat, _, _ in api.sent] == [ADMIN_A, ADMIN_B]
    assert anchors(app_session_factory, clinic_a) == []


def test_anchor_failure_does_not_swallow_the_alert(caplog):
    """Сигнал важнее якоря: эскалация — сигнал, а не транзакция. Потерять
    «пациент просит человека» хуже, чем потерять возможность ответить свайпом
    (у админа остаются /release и прямой чат), но след в логах обязателен."""
    api = FakeTelegramAPI()

    def broken_writer(admin_chat_id, message_id, patient_chat_id):
        raise RuntimeError("база недоступна")

    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_A, ADMIN_B],
                                    anchor_writer=broken_writer)

    with caplog.at_level("ERROR", logger="navbat.escalation"):
        assert escalation.notify(PATIENT, "просит человека", {}) is True

    assert len(api.sent) == 2, "сбой якоря не должен гасить рассылку"
    assert caplog.records, "сбой якоря обязан оставить след"


def test_only_escalation_alerts_are_anchored(app_session_factory, clinic_a):
    """🟡 FYI, операционные и системные алерты — не пациентские каналы:
    реплай на них адресовать некому."""
    api = FakeTelegramAPI()
    escalation = build_escalation(api, app_session_factory, clinic_a, [ADMIN_A])

    escalation.notify_fyi(PATIENT, "нет слотов", {})
    escalation.notify_ops("синхронизация не работает", {})
    escalation.notify_system("бэкапы БД не снимаются", {})

    assert anchors(app_session_factory, clinic_a) == []


# ── Репозиторий якорей ──────────────────────────────────────────────────────

def test_anchor_round_trip(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) == PATIENT
        assert relay_repo.patient_for(session, ADMIN_A, 43) is None, \
            "реплай на сообщение без якоря не должен уйти случайному пациенту"
        assert relay_repo.patient_for(session, ADMIN_B, 42) is None, \
            "message_id уникален только внутри чата — искать надо по паре"


def test_repeated_anchor_is_not_an_error(app_session_factory, clinic_a):
    """Повтор отправки того же алерта (ретрай транспорта) не должен ронять
    рассылку на UNIQUE: адресат тот же."""
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
        assert relay_repo.patient_for(session, ADMIN_A, 42) == PATIENT


def test_cleanup_removes_only_stale_anchors(app_session_factory, admin_engine,
                                            clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE admin_relay SET created_at = now() - "
                          "interval '8 days' WHERE message_id = 42"))
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 43, PATIENT)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        deleted = relay_repo.cleanup_old(session, days=7)

    assert deleted == 1
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) is None
        assert relay_repo.patient_for(session, ADMIN_A, 43) == PATIENT


def test_anchors_are_isolated_by_clinic(app_session_factory, clinic_a, clinic_b):
    """Якорь — пара chat_id: утечка из чужой клиники отправила бы ответ
    администратора пациенту другой клиники."""
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)

    with tenant_transaction(app_session_factory, clinic_b) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) is None


# ── Пациент → админ: карточки 💬 из заморозки ───────────────────────────────

def test_escalated_patient_text_reaches_every_admin_chat(app_session_factory,
                                                         clinic_a):
    """Замороженный пациент продолжает писать — эти реплики админ обязан
    видеть, иначе он «отвечает» человеку, не зная, о чём тот просит.
    Якорь на КАЖДУЮ карточку: реплаить админ будет именно на неё."""
    api = FakeTelegramAPI()
    dialog = make_dialog(app_session_factory, clinic_a, api, [ADMIN_A, ADMIN_B])
    dialog.handle_text(PATIENT, "позовите администратора")
    before = len(api.sent)

    dialog.handle_text(PATIENT, "мне бы сегодня успеть, зуб ноет")

    cards = api.sent[before:]
    assert [chat for chat, _, _ in cards] == [ADMIN_A, ADMIN_B]
    assert all("мне бы сегодня успеть, зуб ноет" in body
               for _, body, _ in cards), "админ читает саму реплику пациента"
    assert all(body.startswith("💬") for _, body, _ in cards), \
        "карточка ответа, а не второй алерт эскалации"
    rows = anchors(app_session_factory, clinic_a)
    assert [(row.admin_chat_id, row.patient_chat_id) for row in rows[2:]] == \
        [(ADMIN_A, PATIENT), (ADMIN_B, PATIENT)]
    assert {row.message_id for row in rows[2:]} \
        .isdisjoint({row.message_id for row in rows[:2]}), \
        "у карточки свой message_id — якорь алерта его не подменяет"


def test_relay_does_not_change_the_reply_to_the_patient(app_session_factory,
                                                        clinic_a):
    """Пересылка админу — побочный канал: пациент видит ровно тот же ответ
    заморозки с кнопкой выхода, что и раньше."""
    api = FakeTelegramAPI()
    dialog = make_dialog(app_session_factory, clinic_a, api, [ADMIN_A])
    dialog.handle_text(PATIENT, "позовите администратора")
    before = len(api.sent)

    reply = dialog.handle_text(PATIENT, "ну сколько ещё ждать")

    assert len(api.sent) == before + 1, "реплика админу ушла"
    assert reply.text == t("escalated", "ru")
    assert [b.action for b in reply.buttons] == ["unfreeze"]


def test_stale_button_tap_is_not_relayed(app_session_factory, clinic_a):
    """Тап по устаревшей кнопке — не сообщение админу: пересылать «stale»
    значит будить человека на каждый случайный клик."""
    api = FakeTelegramAPI()
    dialog = make_dialog(app_session_factory, clinic_a, api, [ADMIN_A])
    dialog.handle_text(PATIENT, "позовите администратора")
    before = len(api.sent)

    dialog.handle_action(PATIENT, "stale")

    assert api.sent[before:] == []
    dialog.handle_text(PATIENT, "алло")
    assert len(api.sent) == before + 1, "а текст — шлёт"


# ── Админ → пациент: свайп-ответ ────────────────────────────────────────────

def test_admin_swipe_reply_reaches_the_patient_in_his_language(
        app_session_factory, clinic_a):
    """Полный круг: пациент позвал человека → админ ответил реплаем на алерт.
    Языки разные и не путаются: пациенту — его, админу — язык консоли."""
    api = FakeTelegramAPI()
    worker = make_relay_worker(app_session_factory, clinic_a, api, [ADMIN_A])
    alert_id = escalate_patient(app_session_factory, clinic_a, worker, api,
                                lang="uz")
    put_admin_reply(app_session_factory, clinic_a, ADMIN_A,
                    "Перезвоню через 10 минут", reply_to=alert_id)

    worker.process_one()

    to_patient, to_admin = api.sent[-2], api.sent[-1]
    assert to_patient[0] == PATIENT
    assert to_patient[1] == t("relay_from_admin", "uz",
                              text="Перезвоню через 10 минут")
    assert to_admin == (ADMIN_A, at("relay_delivered", "ru"), ())


def test_reply_without_anchor_goes_nowhere(app_session_factory, clinic_a):
    """Реплай на консольное сообщение или на алерт старше ретеншена: адресата
    нет. Угадывать пациента нельзя, и в консоль такой текст пускать тоже —
    она приняла бы его за ввод шага (цена, имя врача)."""
    api = FakeTelegramAPI()
    worker = make_relay_worker(app_session_factory, clinic_a, api, [ADMIN_A])
    console: list[str] = []
    worker._admin.handle_text = lambda chat, body: (console.append(body)
                                                    or Reply("консоль"))
    put_admin_reply(app_session_factory, clinic_a, ADMIN_A, "ответ в пустоту",
                    reply_to=404)

    worker.process_one()

    assert api.sent == [(ADMIN_A, at("relay_no_anchor", "ru"), ())]
    assert console == [], "текст свайп-ответа не должен уходить в консоль"


def test_failed_delivery_is_reported_to_the_admin(app_session_factory, clinic_a):
    """Пациент заблокировал бота или сеть легла: админ обязан узнать, что его
    ответ НЕ дошёл, — иначе он ждёт реакции на неотправленное."""
    api = FakeTelegramAPI()
    worker = make_relay_worker(app_session_factory, clinic_a, api, [ADMIN_A])
    alert_id = escalate_patient(app_session_factory, clinic_a, worker, api)
    put_admin_reply(app_session_factory, clinic_a, ADMIN_A, "уже иду",
                    reply_to=alert_id)
    api.send_failures = 1  # падает ближайшая отправка — она пациенту

    worker.process_one()

    assert api.sent[-1][0] == ADMIN_A
    assert api.sent[-1][1] == at("relay_failed", "ru",
                                 error="эмуляция падения сети")


def test_reply_to_a_relay_card_also_reaches_the_patient(app_session_factory,
                                                        clinic_a):
    """Переписка идёт репликами: админ отвечает на последнюю 💬-карточку,
    а не отматывает чат до первого алерта."""
    api = FakeTelegramAPI()
    worker = make_relay_worker(app_session_factory, clinic_a, api, [ADMIN_A])
    escalate_patient(app_session_factory, clinic_a, worker, api)
    put_message(app_session_factory, clinic_a, "я на остановке уже",
                chat_id=PATIENT)
    worker.process_one()
    # именно карточка, а не алерт: фейк нумерует отправки подряд, и позиция
    # в списке — это message_id, на который админ жмёт «ответить»
    card_id = next(i for i, (chat, body, _) in enumerate(api.sent, start=1)
                   if chat == ADMIN_A and body.startswith("💬"))

    put_admin_reply(app_session_factory, clinic_a, ADMIN_A, "ждём вас",
                    reply_to=card_id)
    worker.process_one()

    assert api.sent[-2] == (PATIENT, t("relay_from_admin", "ru",
                                       text="ждём вас"), ())
    assert api.sent[-1] == (ADMIN_A, at("relay_delivered", "ru"), ())
