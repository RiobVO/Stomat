"""Конфигурация логов: plain для консоли разработчика, json для контейнера.

NAVBAT_LOG_FORMAT=json включается в прод-compose: stdout контейнера
становится потоком структурных событий (готов к Loki/grep без парсинга
свободного текста). Никаких сторонних пакетов — stdlib Formatter.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

PLAIN_FORMAT = "%(levelname)s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                  .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def make_formatter(kind: str) -> logging.Formatter:
    return JsonFormatter() if kind == "json" else logging.Formatter(PLAIN_FORMAT)


def setup_logging() -> None:
    """Точка входа процессов (supervisor/канал/календарь): формат из env."""
    handler = logging.StreamHandler()
    handler.setFormatter(make_formatter(os.environ.get("NAVBAT_LOG_FORMAT", "plain")))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx печатает полный URL запроса — в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
