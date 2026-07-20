"""NLU за адаптером: протокол + фейковый экстрактор для тестов и демо.

FakeExtractor работает от двух источников (в порядке приоритета):
1) scripted-очередь — детерминированные сценарии юнит-тестов FSM,
   элементом может быть и исключение (имитация кривого JSON от модели);
2) фикстуры спайка (messages.jsonl) — реальные формулировки без API.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from navbat.nlu.schema import Extraction

_WS_RE = re.compile(r"\s+")


class ExtractionError(Exception):
    """Невалидный/пустой ответ NLU — после repair-попыток или вне фикстур."""


class Extractor(Protocol):
    def extract(self, text: str) -> Extraction: ...


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.strip().casefold())


class FakeExtractor:
    def __init__(
        self,
        fixtures: dict[str, Extraction] | None = None,
        script: list[Extraction | Exception] | None = None,
    ) -> None:
        self._fixtures = fixtures or {}
        self._script = list(script or [])

    @classmethod
    def from_fixtures(cls, path: Path) -> "FakeExtractor":
        fixtures: dict[str, Extraction] = {}
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rec = json.loads(line)
            gold = dict(rec["gold"])
            # сет v2 размечен по is_medical не полностью — для FSM пропуск = False
            if gold.get("is_medical") is None:
                gold["is_medical"] = False
            try:
                fixtures[_normalize(rec["text"])] = Extraction.model_validate(gold)
            except ValidationError as e:
                raise ValueError(f"{path}:{lineno}: кривая gold-разметка: {e}") from e
        return cls(fixtures=fixtures)

    def __len__(self) -> int:
        return len(self._fixtures)

    def extract(self, text: str) -> Extraction:
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        try:
            return self._fixtures[_normalize(text)]
        except KeyError:
            raise ExtractionError(f"текста нет в фикстурах: {text!r}") from None
