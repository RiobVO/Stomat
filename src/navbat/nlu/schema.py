"""Схема извлечения NLU — прод-перенос конвенций спайка (spike_nlu/eval.py).

Источник истины по значениям: CLAUDE.md «Принятые решения». intent строго 5,
услуги строго 9, date_ref/time_ref — закрытые словари через regex (union
в Literal structured outputs не умеет — кривое значение ловит валидатор).
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

DATE_REF_RE = re.compile(
    r"^(today|tomorrow|after_tomorrow|next_week"
    r"|weekday_(mon|tue|wed|thu|fri|sat|sun)|explicit_\d{2}\.\d{2})$"
)
TIME_REF_RE = re.compile(r"^(([01]\d|2[0-3]):[0-5]\d|morning|afternoon|evening)$")

ServiceKey = Literal[
    "cleaning", "filling", "extraction", "implant", "crown",
    "whitening", "braces", "checkup", "xray",
]


class Extraction(BaseModel):
    intent: Literal["book", "reschedule", "cancel", "question", "other"]
    service: Optional[ServiceKey]
    doctor: Optional[str]
    date_ref: Optional[str]
    time_ref: Optional[str]
    language: Literal["uz", "ru", "mixed"]
    is_medical: bool  # флаг для дисклеймера код-слоем, не интент

    @field_validator("date_ref")
    @classmethod
    def check_date_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not DATE_REF_RE.match(v):
            raise ValueError(f"date_ref вне словаря: {v!r}")
        return v

    @field_validator("time_ref")
    @classmethod
    def check_time_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not TIME_REF_RE.match(v):
            raise ValueError(f"time_ref вне словаря: {v!r}")
        return v
