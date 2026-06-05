"""Медицинский дисклеймер — код-слой по флагу is_medical, один раз за диалог."""
from __future__ import annotations

from conftest import make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import MEDICAL_DISCLAIMER
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import CHAT, explicit, extr


def test_medical_flag_adds_disclaimer_once(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    day = next_monday()
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(service="checkup", date_ref=explicit(day), is_medical=True),
        extr(service="checkup", date_ref=explicit(day), time_ref="morning",
             is_medical=True),
    ]))

    first = engine.handle_text(CHAT, "болит зуб, можно в понедельник?")
    assert MEDICAL_DISCLAIMER["ru"] in first.text

    second = engine.handle_text(CHAT, "лучше утром, зуб ноет")
    assert MEDICAL_DISCLAIMER["ru"] not in second.text, "дисклеймер не повторяется"


def test_non_medical_has_no_disclaimer(app_session_factory, clinic_a, doctor_a,
                                       service_cleaning):
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(script=[
        extr(service="cleaning", date_ref=explicit(next_monday())),
    ]))
    reply = engine.handle_text(CHAT, "запишите на чистку в понедельник")
    assert MEDICAL_DISCLAIMER["ru"] not in reply.text
