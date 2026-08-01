"""Обвязка реального NLU: деидентификация PII, дневной token cap, дрифт.

BRIEF: перед отправкой в LLM текст деидентифицируется; per-clinic лимит
токенов + алерт — защита кошелька от спама (карта в USD у разработчика).
Метрика дрифта (Ф1.5 B.3): доля ExtractionError за день — деградацию
модели/промпта должен первым видеть админ, а не пациент эскалациями.
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
from navbat.dialog.escalation import (
    EscalationNotifier,
    LoggingEscalation,
    system_alert,
)
from navbat.nlu.extractor import ExtractionError, Extractor
from navbat.nlu.schema import Extraction

log = logging.getLogger("navbat.nlu")

DEFAULT_DAILY_TOKEN_CAP = 200_000  # ≈$0.1/день на gpt-4o-mini — потолок аномалии
DEFAULT_DRIFT_THRESHOLD = 0.2  # доля сбоев за день, выше — алерт о дрифте
DRIFT_MIN_REQUESTS = 20        # меньше запросов — статистики нет, не алертим

# телефоноподобное: 7+ цифр с разделителями; даты (20.06) и время (15:00)
# короче и не задеваются
_PHONE_RE = re.compile(r"\+?\d(?:[\s\-()]?\d){6,14}")

# «день» учёта — локальные сутки клиники, не UTC: вокруг полуночи запись
# и чтение сводки иначе расходятся на день (поймано тестом при смене даты)
_CLINIC_TODAY_SQL = ("(now() AT TIME ZONE (SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid))::date")


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
        drift_threshold: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._cap = daily_cap or int(
            os.environ.get("NAVBAT_DAILY_TOKEN_CAP", DEFAULT_DAILY_TOKEN_CAP))
        self._notifier = notifier or LoggingEscalation()
        self._alerted_on: date | None = None  # алерт раз в день (в памяти — ок)
        self._drift_threshold = drift_threshold or float(
            os.environ.get("NAVBAT_NLU_DRIFT_THRESHOLD", DEFAULT_DRIFT_THRESHOLD))
        self._drift_alerted_on: date | None = None

    def record(self, in_tokens: int, out_tokens: int) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text(f"""
                    INSERT INTO llm_usage (clinic_id, day, requests, in_tokens, out_tokens)
                    VALUES (current_setting('app.clinic_id')::uuid,
                            {_CLINIC_TODAY_SQL}, 1, :input, :output)
                    ON CONFLICT (clinic_id, day) DO UPDATE
                    SET requests = llm_usage.requests + 1,
                        in_tokens = llm_usage.in_tokens + EXCLUDED.in_tokens,
                        out_tokens = llm_usage.out_tokens + EXCLUDED.out_tokens
                """),
                {"input": in_tokens, "output": out_tokens},
            )

    def _bump(self, column: str) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(
                text(f"""
                    INSERT INTO llm_usage (clinic_id, day, {column})
                    VALUES (current_setting('app.clinic_id')::uuid,
                            {_CLINIC_TODAY_SQL}, 1)
                    ON CONFLICT (clinic_id, day) DO UPDATE
                    SET {column} = llm_usage.{column} + 1
                """))

    def record_failure(self) -> None:
        """ExtractionError после repair — сигнал деградации модели/промпта."""
        self._bump("failures")

    def record_repair(self) -> None:
        """Повторная попытка из-за невалидного JSON (вызов удался со 2-го раза)."""
        self._bump("repairs")

    def maybe_alert_drift(self) -> None:
        """Доля сбоев за день выше порога → алерт админу, раз в день."""
        today = date.today()
        if self._drift_alerted_on == today:
            return
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text(f"SELECT requests, failures FROM llm_usage "
                     f"WHERE day = {_CLINIC_TODAY_SQL}")
            ).one_or_none()
        if row is None or row.requests < DRIFT_MIN_REQUESTS:
            return
        share = row.failures / row.requests
        if share <= self._drift_threshold:
            return
        self._drift_alerted_on = today
        system_alert(
            self._notifier,
            f"NLU-дрифт: {row.failures} сбоев из {row.requests} запросов "
            f"за сегодня ({share:.0%}) — проверить промпт/модель",
            {"failures": row.failures, "requests": row.requests})

    def cap_exceeded(self) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            total = session.execute(
                text(f"SELECT COALESCE(in_tokens + out_tokens, 0) FROM llm_usage "
                     f"WHERE day = {_CLINIC_TODAY_SQL}")
            ).scalar_one_or_none() or 0
        return total >= self._cap

    def alert_once(self) -> None:
        today = date.today()
        if self._alerted_on == today:
            return
        self._alerted_on = today
        system_alert(
            self._notifier,
            f"дневной лимит LLM-токенов ({self._cap}) исчерпан — "
            f"бот эскалирует диалоги до конца дня", {"cap": self._cap})


class DriftTrackingExtractor:
    """Учёт сбоев NLU: ExtractionError → failures+1 + проверка дрифта.

    Бюджет (BudgetExceededError) и аутэйдж провайдера (ProviderDownError) —
    не дрифт качества: первый — про деньги, второй — про инфраструктуру.
    """

    def __init__(self, inner: Extractor, recorder: "UsageRecorder") -> None:
        self._inner = inner
        self._recorder = recorder

    def extract(self, message: str) -> Extraction:
        try:
            return self._inner.extract(message)
        except BudgetExceededError:
            raise
        except ExtractionError:
            self._recorder.record_failure()
            self._recorder.maybe_alert_drift()
            raise


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
