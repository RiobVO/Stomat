"""Rate limit: >5 сообщений за 10 секунд на чат — NLU не дёргается (кошелёк).

Предупреждение пациенту — один раз (6-е сообщение), дальше молча:
не спамим в ответ на спам.
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.dialog.replies import TEMPLATES
from test_dialog_booking import CHAT, extr
from test_tg_worker import make_worker, put_message


def drain(worker) -> None:
    while worker.process_one():
        pass


def test_burst_is_throttled_with_single_warning(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    # script ровно на 5 обработок: если 6-е/7-е дёрнут NLU —
    # FakeExtractor бросит ExtractionError и в ответах появится reask
    worker, api, _ = make_worker(app_session_factory, clinic_a,
                                 [extr(service="cleaning")] * 5)
    for i in range(7):
        put_message(app_session_factory, clinic_a, f"сообщение {i}")
    drain(worker)

    texts = [message for _, message, _ in api.sent]
    assert len(texts) == 6, "5 ответов + 1 предупреждение, 7-е — молча"
    assert sum(TEMPLATES["rate_limited"]["ru"] in m for m in texts) == 1
    assert not any(TEMPLATES["reask"]["ru"] in m for m in texts), "NLU не дёргался"


def test_old_messages_outside_window_dont_count(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a,
                                 [extr(service="cleaning")] * 7)
    for i in range(6):
        put_message(app_session_factory, clinic_a, f"старое {i}")
    drain(worker)
    # история уезжает за окно 10 секунд
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE message_queue "
                          "SET created_at = now() - interval '1 minute'"))

    put_message(app_session_factory, clinic_a, "новое сообщение")
    drain(worker)
    assert TEMPLATES["rate_limited"]["ru"] not in api.sent[-1][1], \
        "после паузы лимит не действует"


def test_callbacks_are_not_throttled(app_session_factory, admin_engine, clinic_a,
                                     doctor_a, service_cleaning):
    from test_tg_worker import put_callback

    worker, api, _ = make_worker(app_session_factory, clinic_a, [])
    for _ in range(7):
        put_callback(app_session_factory, clinic_a, "a:99")  # устаревшая кнопка
    drain(worker)
    # каждый callback обработан (stale-ответ), лимит не вмешивался
    assert len(api.sent) == 7
