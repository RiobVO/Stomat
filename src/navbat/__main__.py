"""Точка входа всей системы: python -m navbat [--clinic ...] [--real] [--check]"""
import logging
import sys

from navbat.supervisor import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows не-UTF8 по умолчанию
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
