"""Обвязка реального NLU: деидентификация PII и дневной token cap.

BRIEF: перед отправкой в LLM текст деидентифицируется; per-clinic лимит
токенов + алерт — защита кошелька от спама (карта в USD у разработчика).
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.nlu.extractor import ExtractionError, Extractor
from navbat.nlu.schema import Extraction

log = logging.getLogger("navbat.nlu")

DEFAULT_DAILY_TOKEN_CAP = 200_000  # ≈$0.1/день на gpt-4o-mini — потолок аномалии

# телефоноподобное: 7+ цифр с разделителями; даты (20.06) и время (15:00)
# короче и не задеваются
_PHONE_RE = re.compile(r"\+?\d(?:[\s\-()]?\d){6,14}")


class BudgetExceededError(ExtractionError):
    """Дневной token cap исчерпан — LLM не дёргаем (FSM мягко эскалирует)."""


class DeidentifyingExtractor:
    """Маскирует телефоны до отправки текста в LLM."""

    def __init__(self, inner: Extractor) -> None:
        self._inner = inner

    def extract(self, message: str) -> Extraction:
        return self._inner.extract(_PHONE_RE.sub("[phone]", message))


class UsageRecorder:
    """Дневной учёт токенов клиники (llm_usage) + проверка cap."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        daily_cap: int | None = None,
        notifier: EscalationNotifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._cap = daily_cap or int(
            os.environ.get("NAVBAT_DAILY_TOKEN_CAP", DEFAULT_DAILY_TOKEN_CAP))
        self._notifier = notifier or LoggingEscalation()
        self._alerted_on: date | None = None  # алерт раз в день (в памяти — ок)

    def record(self, in_tokens: int, out_tokens: int) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text("""
                    INSERT INTO llm_usage (clinic_id, day, requests, in_tokens, out_tokens)
                    VALUES (current_setting('app.clinic_id')::uuid, current_date,
                            1, :input, :output)
                    ON CONFLICT (clinic_id, day) DO UPDATE
                    SET requests = llm_usage.requests + 1,
                        in_tokens = llm_usage.in_tokens + EXCLUDED.in_tokens,
                        out_tokens = llm_usage.out_tokens + EXCLUDED.out_tokens
                """),
                {"input": in_tokens, "output": out_tokens},
            )

    def cap_exceeded(self) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            total = session.execute(
                text("SELECT COALESCE(in_tokens + out_tokens, 0) FROM llm_usage "
                     "WHERE day = current_date")
            ).scalar_one_or_none() or 0
        return total >= self._cap

    def alert_once(self) -> None:
        today = date.today()
        if self._alerted_on == today:
            return
        self._alerted_on = today
        self._notifier.notify(
            0, f"дневной лимит LLM-токенов ({self._cap}) исчерпан — "
               f"бот эскалирует диалоги до конца дня", {"cap": self._cap})


class BudgetedExtractor:
    """Гейт перед LLM: cap исчерпан → BudgetExceededError, вызова нет."""

    def __init__(self, inner: Extractor, recorder: UsageRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def extract(self, message: str) -> Extraction:
        if self._recorder.cap_exceeded():
            self._recorder.alert_once()
            raise BudgetExceededError("дневной token cap исчерпан")
        return self._inner.extract(message)
