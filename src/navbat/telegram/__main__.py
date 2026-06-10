"""Точка входа: python -m navbat.telegram --clinic <uuid> [...]"""
import sys

from navbat.logging_setup import setup_logging
from navbat.telegram.app import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows не-UTF8 по умолчанию
    setup_logging()
    sys.exit(main())
