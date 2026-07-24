"""Загрузка локального .env в os.environ — секреты не переписывать руками.

Переменные из реального окружения главнее файла. python-dotenv не нужен:
формат KEY=VALUE с комментариями покрывается stdlib.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("navbat.env")

ENV_FILE = Path(__file__).parents[2] / ".env"


def load_env_file(path: Path = ENV_FILE) -> int:
    """Читает KEY=VALUE построчно; возвращает число применённых переменных."""
    if not path.exists():
        return 0
    applied = 0
    # utf-8-sig: редакторы Windows любят дописывать BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            applied += 1
    if applied:
        log.info("из %s загружено переменных: %d", path.name, applied)
    return applied
