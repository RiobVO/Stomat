"""Загрузка локального .env: KEY=VALUE в os.environ, окружение главнее файла."""
from __future__ import annotations

import os

from navbat.envfile import load_env_file


def test_parses_values_and_skips_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# комментарий\n"
        "\n"
        "NAVBAT_TEST_TOKEN=123:abc-def\n"
        "NAVBAT_TEST_QUOTED=\"в кавычках\"\n"
        "строка без знака равно\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NAVBAT_TEST_TOKEN", raising=False)
    monkeypatch.delenv("NAVBAT_TEST_QUOTED", raising=False)

    assert load_env_file(env) == 2
    assert os.environ["NAVBAT_TEST_TOKEN"] == "123:abc-def"
    assert os.environ["NAVBAT_TEST_QUOTED"] == "в кавычках"


def test_does_not_override_existing_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("NAVBAT_TEST_TOKEN=из_файла\n", encoding="utf-8")
    monkeypatch.setenv("NAVBAT_TEST_TOKEN", "из_окружения")

    assert load_env_file(env) == 0
    assert os.environ["NAVBAT_TEST_TOKEN"] == "из_окружения"


def test_missing_file_is_noop(tmp_path):
    assert load_env_file(tmp_path / "нет_такого.env") == 0


def test_bom_from_windows_editor_is_stripped(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes("NAVBAT_TEST_TOKEN=123\n".encode("utf-8-sig"))
    monkeypatch.delenv("NAVBAT_TEST_TOKEN", raising=False)

    assert load_env_file(env) == 1
    assert os.environ["NAVBAT_TEST_TOKEN"] == "123"
