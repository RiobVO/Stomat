"""JSON-формат логов (C-3): контейнер пишет машинно-разбираемые строки."""
from __future__ import annotations

import json
import logging

from navbat.logging_setup import make_formatter


def _record(level=logging.INFO, msg="hello %s", args=("world",), exc=None):
    return logging.LogRecord("navbat.test", level, __file__, 1, msg, args, exc)


def test_json_formatter_emits_parseable_line():
    line = make_formatter("json").format(_record())
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["logger"] == "navbat.test"
    assert data["message"] == "hello world"
    assert "ts" in data


def test_json_formatter_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        line = make_formatter("json").format(_record(exc=sys.exc_info()))
    data = json.loads(line)
    assert "ValueError: boom" in data["exc"]


def test_plain_formatter_keeps_legacy_format():
    line = make_formatter("plain").format(_record())
    assert line == "INFO navbat.test: hello world"
