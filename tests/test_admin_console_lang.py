"""Язык админ-консоли (карта готовности, №16).

Бот говорит с пациентом по-узбекски, а владелец клиники управлял им только
по-русски. Язык переключается кнопкой верхнего меню и живёт в диалоге
админ-чата: у каждого администратора он свой.
"""
from __future__ import annotations

import ast
import pathlib
import re

from navbat.telegram import admin_console as ac
from navbat.telegram import admin_texts as at
from test_admin_console import (ADMIN_CHAT, click, flat, last_menu, last_to,
                                row_labels, send_admin)
from test_tg_worker import make_worker

CONSOLE_SRC = pathlib.Path(ac.__file__)


# ── словарь ───────────────────────────────────────────────────────────────

def test_templates_have_both_languages():
    for key, langs in at.TEMPLATES.items():
        assert set(langs) == set(at.LANGS), f"{key}: языки {set(langs)}"
        for lang, value in langs.items():
            assert value.strip(), f"{key}/{lang}: пустая строка"


def test_templates_placeholders_match_across_languages():
    """Разъехавшиеся плейсхолдеры — KeyError у узбекского администратора."""
    holes = re.compile(r"\{(\w+)\}")
    for key, langs in at.TEMPLATES.items():
        sets = {lang: set(holes.findall(value)) for lang, value in langs.items()}
        assert len(set(map(frozenset, sets.values()))) == 1, f"{key}: {sets}"


def test_at_escapes_substitutions():
    out = at.at("dname_saved", "ru", name="<b>Иванов</b>")
    assert "&lt;b&gt;" in out and "<b>Иванов</b>" not in out


def test_menu_key_recognises_both_languages():
    assert at.menu_key(at.TEMPLATES["btn_services"]["ru"]) == "btn_services"
    assert at.menu_key(at.TEMPLATES["btn_services"]["uz"]) == "btn_services"
    assert at.menu_key("что-то ещё") is None


# ── style-lock: строк консоли не осталось в коде ──────────────────────────

def test_console_has_no_hardcoded_user_strings():
    """Пропущенный литерал остаётся русским при uz — ловим это машинно."""
    tree = ast.parse(CONSOLE_SRC.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    # аргументы log.* — диагностика для разработчика, она по-русски
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"):
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    docstrings.add(id(arg))
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    leftovers = [
        f"{node.lineno}: {node.value[:60]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and cyrillic.search(node.value)
    ]
    assert not leftovers, "строки мимо словаря:\n" + "\n".join(leftovers)


# ── поведение ─────────────────────────────────────────────────────────────

def test_language_switch_changes_menu_and_persists(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "/start")
    assert at.TEMPLATES["btn_services"]["ru"] in flat(last_menu(api))

    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])
    assert at.TEMPLATES["btn_services"]["uz"] in flat(last_menu(api))

    # язык переживает следующее сообщение — он в диалоге, не в памяти процесса
    send_admin(worker, app_session_factory, clinic_a, "/start")
    labels = flat(last_menu(api))
    assert at.TEMPLATES["btn_services"]["uz"] in labels
    assert at.TEMPLATES["btn_lang"]["uz"] in labels, "тумблер предлагает русский"


def test_language_switch_back_to_russian(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["uz"])

    assert at.TEMPLATES["btn_services"]["ru"] in flat(last_menu(api))


def test_uzbek_console_renders_sections(app_session_factory, clinic_a,
                                        service_cleaning, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])

    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_services"]["uz"])
    assert "Xizmatlar" in last_to(api, ADMIN_CHAT)

    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")
    card = api.edited[-1][2] if api.edited else last_to(api, ADMIN_CHAT)
    assert "Narxi" in card and "Davomiyligi" in card

    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}")
    card = api.edited[-1][2]
    assert "Bufer" in card and "Ish jadvali" in card


def test_uzbek_console_service_label_is_uzbek(app_session_factory, clinic_a,
                                              service_cleaning):
    """Услуга «Чистка» в узбекской консоли — «Tish tozalash», не по-русски."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_services"]["uz"])

    assert any("Tish tozalash" in label for label in row_labels(api))


def test_russian_label_still_works_after_switch(app_session_factory, clinic_a,
                                                service_cleaning):
    """Старая клавиатура остаётся на экране: её кнопки обязаны работать."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_services"]["ru"])

    assert "Xizmatlar" in last_to(api, ADMIN_CHAT)


def test_uzbek_cancel_word_clears_pending(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    from test_tg_worker import context_of
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_lang"]["ru"])
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    send_admin(worker, app_session_factory, clinic_a, "bekor")

    assert "adm_pending" not in (context_of(admin_engine, ADMIN_CHAT) or {})


# ── язык доходит до всех ответов админу (ревью волны C) ───────────────────

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _switch_to_uz(worker, sf, clinic):
    send_admin(worker, sf, clinic, at.TEMPLATES["btn_lang"]["ru"])


def test_stats_button_answers_in_uzbek(app_session_factory, clinic_a,
                                       service_cleaning):
    """«📊 Statistika» отвечала русской сводкой — узбекский обрывался ровно
    на самом ценном экране владельца."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    _switch_to_uz(worker, app_session_factory, clinic_a)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_stats"]["uz"])

    body = last_to(api, ADMIN_CHAT)
    assert not CYRILLIC.search(body), f"сводка по-русски: {body}"


def test_pause_and_resume_answer_in_uzbek(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    _switch_to_uz(worker, app_session_factory, clinic_a)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_pause"]["uz"])
    paused_body = last_to(api, ADMIN_CHAT)

    assert not CYRILLIC.search(paused_body), f"пауза по-русски: {paused_body}"
    assert at.TEMPLATES["btn_resume"]["uz"] in flat(last_menu(api))

    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_resume"]["uz"])
    assert not CYRILLIC.search(last_to(api, ADMIN_CHAT))


def test_slash_commands_answer_in_uzbek(app_session_factory, clinic_a,
                                        doctor_a, service_cleaning):
    """Слэш-команды — аварийный выход из консоли, и он тоже на языке чата."""
    from datetime import date, timedelta
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    _switch_to_uz(worker, app_session_factory, clinic_a)
    target = date.today() + timedelta(days=7)
    for command in (f"/dayoff {target:%d.%m}", f"/dayopen {target:%d.%m}",
                    "/pause", "/resume", "/llm off", "/llm on", "/stats 7"):
        send_admin(worker, app_session_factory, clinic_a, command)
        body = last_to(api, ADMIN_CHAT)
        assert not CYRILLIC.search(body), f"{command} → по-русски: {body}"


def test_onboard_error_reaches_admin_in_uzbek(app_session_factory,
                                              admin_engine, clinic_a,
                                              service_cleaning):
    """Ошибки слоя данных приходили русским текстом внутрь узбекской консоли."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    _switch_to_uz(worker, app_session_factory, clinic_a)
    # услуга уже есть в клинике — add_service поднимет ValueError
    click(worker, app_session_factory, clinic_a, "adm:svcadd:cleaning")
    send_admin(worker, app_session_factory, clinic_a, "30")

    # ответ на текстовый ввод всегда уходит новым сообщением, не правкой
    body = last_to(api, ADMIN_CHAT)
    assert not CYRILLIC.search(body), f"ошибка по-русски: {body}"


def test_digest_renders_in_chat_language(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    """Вечерняя сводка идёт веером по админ-чатам — каждому на его языке."""
    from navbat.stats import DailyStats, render_digest_short

    sample = DailyStats(booked=3, cancelled=0, escalated=0, reminders_sent=0,
                        llm_requests=0, llm_tokens=0, nlu_failures=0,
                        nlu_repairs=0, prevented_noshows=1, saved_revenue=350000)
    ru = render_digest_short(sample, "ru")
    uz = render_digest_short(sample, "uz")
    assert CYRILLIC.search(ru) and not CYRILLIC.search(uz), uz
