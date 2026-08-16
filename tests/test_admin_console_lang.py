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
