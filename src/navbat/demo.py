"""Консольный диалог с ботом — демо без Telegram.

    python -m navbat.demo            # фейковый NLU: фразы из spike_nlu/data/messages.jsonl
    python -m navbat.demo --real     # ДЕНЬГИ: реальный gpt-4o-mini (нужен OPENAI_API_KEY)

Кнопки печатаются нумерованным списком: ввод числа = нажатие, текст = сообщение.
Команды: /reset — начать диалог заново, /exit — выход.
Требует поднятый postgres (docker compose up -d) и накатанные миграции
(они применяются тестами; вручную: alembic upgrade head).
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Reply
from navbat.nlu.extractor import FakeExtractor

log = logging.getLogger("navbat.demo")

# фиксированный tenant демо-клиники: сидинг идемпотентен от запуска к запуску
DEMO_CLINIC_ID = uuid.UUID("00000000-0000-4000-8000-000000000d31")
DEMO_CHAT_ID = 1
FIXTURES = Path(__file__).parents[2] / "spike_nlu" / "data" / "messages.jsonl"

WORKING_INTERVALS = {
    day: [["09:00", "13:00"], ["14:00", "18:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat")
}
DOCTORS = ("Akmal aka", "Dilnoza opa")
SERVICES = {  # (длительность мин, цена сум|None)
    "cleaning": (30, 350_000), "filling": (60, 400_000), "extraction": (30, None),
    "implant": (90, None), "crown": (60, None), "whitening": (60, 500_000),
    "braces": (60, None), "checkup": (30, 150_000), "xray": (15, 80_000),
}


def seed_demo_clinic(session_factory) -> None:
    """Создаёт демо-клинику с врачами и услугами, если её ещё нет."""
    import json

    from navbat.crypto import encrypt_text

    with tenant_transaction(session_factory, DEMO_CLINIC_ID) as session:
        exists = session.execute(
            text("SELECT 1 FROM clinic WHERE id = :id"), {"id": DEMO_CLINIC_ID}
        ).scalar_one_or_none()
        if exists:
            return
        session.execute(
            text("INSERT INTO clinic (id, name, salt, timezone) "
                 "VALUES (:id, 'Navbat Demo', 'demo-salt', 'Asia/Tashkent')"),
            {"id": DEMO_CLINIC_ID},
        )
        for doctor in DOCTORS:
            session.execute(
                text("INSERT INTO doctor (clinic_id, name_encrypted, working_intervals, "
                     "buffer_min) VALUES (:c, :name, :wi, 10)"),
                {"c": DEMO_CLINIC_ID, "name": encrypt_text(doctor),
                 "wi": json.dumps(WORKING_INTERVALS)},
            )
        for name, (duration, price) in SERVICES.items():
            session.execute(
                text("INSERT INTO service (clinic_id, name, duration_min, price) "
                     "VALUES (:c, :name, :dur, :price)"),
                {"c": DEMO_CLINIC_ID, "name": name, "dur": duration, "price": price},
            )
    log.info("демо-клиника создана: 2 врача, %d услуг", len(SERVICES))


def reset_conversation(session_factory) -> None:
    with tenant_transaction(session_factory, DEMO_CLINIC_ID) as session:
        session.execute(text("DELETE FROM conversation WHERE tg_chat_id = :chat"),
                        {"chat": DEMO_CHAT_ID})


def build_extractor(use_real: bool):
    if not use_real:
        if not FIXTURES.exists():
            sys.exit(f"[FAIL] нет фикстур {FIXTURES} — демо без --real требует spike_nlu")
        extractor = FakeExtractor.from_fixtures(FIXTURES)
        print(f"NLU: фейковый экстрактор, {len(extractor)} фраз из спайка "
              f"(незнакомый текст = «кривой JSON»)")
        return extractor
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("[FAIL] --real требует OPENAI_API_KEY в окружении")
    from navbat.nlu.openai_extractor import OpenAIExtractor

    print("NLU: gpt-4o-mini — КАЖДОЕ сообщение стоит денег")
    return OpenAIExtractor()


def render(reply: Reply) -> list[str]:
    """Печать ответа; возвращает actions кнопок для выбора цифрой."""
    print(f"\nБот: {reply.text}")
    for idx, button in enumerate(reply.buttons, 1):
        print(f"  {idx}. {button.label}")
    return [b.action for b in reply.buttons]


def main() -> int:
    parser = argparse.ArgumentParser(description="Консольное демо диалога Navbat")
    parser.add_argument("--real", action="store_true",
                        help="реальный gpt-4o-mini вместо фикстур (платно!)")
    args = parser.parse_args()

    # dev-ключ шифрования: только для локального демо, в проде ключ из секретов
    os.environ.setdefault(
        "NAVBAT_ENC_KEY", base64.b64encode(b"demo-key-32-bytes-padded-00000!!").decode()
    )

    extractor = build_extractor(args.real)
    session_factory = make_session_factory(make_app_engine())
    seed_demo_clinic(session_factory)
    engine = DialogEngine(session_factory, DEMO_CLINIC_ID, extractor=extractor)

    print("Диалог начат (/reset — заново, /exit — выход). Напишите боту:")
    actions: list[str] = []
    while True:
        try:
            # пайпы Windows любят подсовывать BOM первой строке
            user_input = input("\nВы: ").strip().lstrip("\ufeff").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not user_input:
            continue
        if user_input == "/exit":
            return 0
        if user_input == "/reset":
            reset_conversation(session_factory)
            actions = []
            print("Диалог сброшен.")
            continue
        if user_input.isdigit() and 1 <= int(user_input) <= len(actions):
            reply = engine.handle_action(DEMO_CHAT_ID, actions[int(user_input) - 1])
        else:
            reply = engine.handle_text(DEMO_CHAT_ID, user_input)
        actions = render(reply)


if __name__ == "__main__":
    # консоль Windows по умолчанию не-UTF8 — кириллица бьётся в обе стороны
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
