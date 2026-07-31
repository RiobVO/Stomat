"""--check как защита показа, а не как справка о погоде.

Живая находка 27.07.2026 (карта docs/SALES_READINESS.md, №1 и №2):
чеклист печатал все [OK] при полностью мёртвом Google Calendar (токена нет,
календарь не привязан ни одному врачу) и вообще не смотрел на рубильники
bot_paused / llm_enabled — забытая с прошлой сессии пауза давала зелёный
чек и «запись временно приостановлена» первому же сообщению покупателя.
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.supervisor import run_check


def _line(captured: str, marker: str) -> str:
    """Строка вывода чеклиста, содержащая marker (иначе — понятный провал)."""
    for line in captured.splitlines():
        if marker in line:
            return line
    raise AssertionError(f"нет строки с {marker!r} в выводе:\n{captured}")


def _set_clinic(admin_engine, clinic_id, column: str, value) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text(f"UPDATE clinic SET {column} = :v WHERE id = :id"),
                     {"v": value, "id": clinic_id})


def test_paused_bot_fails_the_checklist(app_session_factory, clinic_a,
                                        admin_engine, capsys):
    _set_clinic(admin_engine, clinic_a, "bot_paused", True)

    code = run_check(app_session_factory, clinic_a, use_real=False)

    line = _line(capsys.readouterr().out, "пауза")
    assert line.startswith("[FAIL]"), line
    assert "/resume" in line, "чек обязан подсказать, чем вернуть бота"
    assert code == 1


def test_llm_switch_off_fails_the_checklist(app_session_factory, clinic_a,
                                            admin_engine, capsys):
    _set_clinic(admin_engine, clinic_a, "llm_enabled", False)

    run_check(app_session_factory, clinic_a, use_real=False)

    line = _line(capsys.readouterr().out, "LLM-рубильник")
    assert line.startswith("[FAIL]"), line
    assert "/llm on" in line


def test_running_bot_reports_switches_green(app_session_factory, clinic_a,
                                            capsys):
    run_check(app_session_factory, clinic_a, use_real=False)

    out = capsys.readouterr().out
    assert _line(out, "пауза").startswith("[OK]")
    assert _line(out, "LLM-рубильник").startswith("[OK]")


def test_calendar_line_names_what_will_not_work(app_session_factory, clinic_a,
                                                capsys):
    """Токена нет — это законный режим (клиника без календаря), но строка
    обязана назвать цену: шаг показа с событием в Google не сработает."""
    run_check(app_session_factory, clinic_a, use_real=False)

    line = _line(capsys.readouterr().out, "Google Calendar")
    assert line.startswith("[OK]"), "клиника без календаря — не провал"
    assert "не будет" in line, f"чек молчит о последствиях: {line}"


def test_token_without_bound_doctor_fails(app_session_factory, clinic_a,
                                          doctor_a, admin_engine, capsys):
    """Главная дыра: токен есть, а календарь не привязан ни одному врачу —
    записи никуда не уедут. Проверяется ДО обращения к Google (без сети)."""
    _set_clinic(admin_engine, clinic_a, "gcal_refresh_token_encrypted",
                "ciphertext-placeholder")

    code = run_check(app_session_factory, clinic_a, use_real=False)

    line = _line(capsys.readouterr().out, "Google Calendar")
    assert line.startswith("[FAIL]"), line
    assert "--calendar" in line, "нужна подсказка, чем привязать"
    assert code == 1


def test_binding_only_on_hidden_doctor_fails(app_session_factory, clinic_a,
                                             doctor_a, admin_engine, capsys):
    """Календарь привязан, но врач скрыт: записи к нему не идут, значит шаг
    показа с событием в Google мёртв — и причина обязана отличаться от
    «не привязан ни одному врачу» (находка ревью: синк выбирает врачей БЕЗ
    is_active — sync_loop.py:48, watch.py:41 — и продолжает ходить в этот
    календарь). Сети здесь тоже нет: проверять токен незачем."""
    _set_clinic(admin_engine, clinic_a, "gcal_refresh_token_encrypted",
                "ciphertext-placeholder")
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET gcal_calendar_id = 'primary', "
                          "is_active = false WHERE id = :d"), {"d": doctor_a})

    code = run_check(app_session_factory, clinic_a, use_real=False)

    line = _line(capsys.readouterr().out, "Google Calendar")
    assert line.startswith("[FAIL]"), line
    assert "скрыт" in line, f"причина должна быть названа точно: {line}"
    assert code == 1


def test_check_skips_showcase_for_regular_clinic(app_session_factory, clinic_a,
                                                 capsys):
    """Витрина /stats — атрибут демо-клиники; живой клинике сеять показную
    историю нельзя, и строка про неё только путала бы."""
    run_check(app_session_factory, clinic_a, use_real=False)
    assert "витрина" not in capsys.readouterr().out


def test_check_fails_empty_demo_showcase(app_session_factory, capsys):
    """pytest стирает демо-историю, финализатор возвращает клинику БЕЗ неё:
    31.07 сводка показа однажды вышла с «записей: 2» — заметили глазами.
    Чек обязан ловить пустую витрину ДО встречи и называть команду починки."""
    from navbat.onboard import DEMO_CLINIC_ID, seed_demo_clinic
    seed_demo_clinic(app_session_factory)

    run_check(app_session_factory, DEMO_CLINIC_ID, use_real=False)

    line = _line(capsys.readouterr().out, "витрина")
    assert line.startswith("[FAIL]"), line
    assert "--demo-history" in line, "чек обязан подсказать команду починки"


def test_check_passes_seeded_demo_showcase(app_session_factory, capsys):
    from navbat.demo_history import seed_demo_history
    from navbat.onboard import DEMO_CLINIC_ID, seed_demo_clinic
    seed_demo_clinic(app_session_factory)
    seed_demo_history(app_session_factory, DEMO_CLINIC_ID)

    run_check(app_session_factory, DEMO_CLINIC_ID, use_real=False)

    line = _line(capsys.readouterr().out, "витрина")
    assert line.startswith("[OK]"), line
