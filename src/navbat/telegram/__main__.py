"""Точка входа: python -m navbat.telegram --clinic <uuid> [...]"""
import logging
import sys

from navbat.telegram.app import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows не-UTF8 по умолчанию
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx печатает полный URL запроса — в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(main())
