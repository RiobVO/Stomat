"""Точка входа всей системы: python -m navbat [--clinic ...] [--real] [--check]"""
import sys

from navbat.logging_setup import setup_logging
from navbat.supervisor import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows не-UTF8 по умолчанию
    setup_logging()
    sys.exit(main())
