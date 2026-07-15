"""Ollama-served local LLM extractor (§6.2).

Exercised live against Ollama + qwen2.5:7b-instruct (2026-07-09). Structured
output via Ollama's `format` JSON Schema is grammar-masked constrained decoding
(see scripts/verify_ollama_constrained.py). Program names from model output
are normalized via `programs.canonicalize_program()` before rows are scored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

import httpx

from ..parse import RawChartRow, _CABINS
from ..regions import canonicalize_region
from .grounding import number_is_grounded
from .programs import canonicalize_program

log = logging.getLogger("mileage.aggregator.extract.ollama")

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct"

# Mirrors grammar.gbnf (see that file's docstring for why this is inlined
# rather than loaded from disk: Ollama's /api/generate takes a JSON Schema via
# `format`, not a raw .gbnf grammar string). Keep the two in sync by hand.
_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "program": {"type": "string"},
            "from": {"type": "string"},
            "to": {"type": "string"},
            "cabin": {
                "type": "string",
                "enum": ["economy", "premium_economy", "business", "first"],
            },
            "miles": {"type": "integer"},
            "roundtrip": {"type": "boolean"},
        },
        "required": ["program", "from", "to", "cabin", "miles", "roundtrip"],
    },
}

_PROMPT_TEMPLATE = (
    Path(__file__).with_name("prompt_template.txt").read_text(encoding="utf-8")
)

class OllamaExtractor:
    """`LLMExtractor` backend served by a local Ollama instance.

    Config is passed explicitly (not read from env here) so tests can point
    this at a fake server; `extract/factory.py` is what reads
    `OLLAMA_HOST`/`OLLAMA_MODEL`/`MILEAGE_EXTRACTOR_BACKEND` from the
    environment and constructs this class.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        host: str = DEFAULT_OLLAMA_HOST,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = 60.0,
        prompt_template: str | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._prompt_template = prompt_template or _PROMPT_TEMPLATE

    def extract(self, document: str, *, source_hint: str = "") -> List[RawChartRow]:
        raw_rows = self._generate(document, source_hint=source_hint)
        if not raw_rows:
            return []
        out: List[RawChartRow] = []
        for raw in raw_rows:
            row = self._to_chart_row(raw, document)
            if row is not None:
                out.append(row)
        return out

    # --- model call ---------------------------------------------------- #
    def _generate(self, document: str, *, source_hint: str) -> Optional[list]:
        prompt = self._prompt_template.format(
            source_hint=source_hint or "(none given)", document=document
        )
        payload = {
            "model": self._model,
            "prompt": prompt,
            "format": _JSON_SCHEMA,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        try:
            resp = httpx.post(
                f"{self._host}/api/generate", json=payload, timeout=self._timeout
            )
            resp.raise_for_status()
        except Exception as exc:  # Ollama not running / unreachable / timeout
            log.info("ollama extractor unavailable (%s); returning no rows", exc)
            return None
        try:
            body = resp.json()
            text = body.get("response", "")
            parsed = json.loads(text)
        except Exception as exc:
            log.info("ollama response was not valid JSON (%s); dropping", exc)
            return None
        if not isinstance(parsed, list):
            log.info("ollama response was not a JSON array; dropping")
            return None
        return parsed

    # --- post-generation guards (never trust the model alone) ---------- #
    def _to_chart_row(self, raw: dict, document: str) -> Optional[RawChartRow]:
        if not isinstance(raw, dict):
            return None
        program = canonicalize_program(str(raw.get("program", "")))
        if not program:
            return None

        cabin = str(raw.get("cabin", "")).strip().lower()
        if cabin not in _CABINS:
            return None

        try:
            miles = int(raw.get("miles"))
        except (TypeError, ValueError):
            return None
        # Hard guard: reject any number not literally present in the source,
        # regardless of how confident the model sounded (§6.2, §2 rule 1).
        if not number_is_grounded(miles, document):
            log.info(
                "ollama extractor: rejected ungrounded miles=%s for program=%s",
                miles,
                program,
            )
            return None

        region_a = canonicalize_region(str(raw.get("from", "")))
        region_b = canonicalize_region(str(raw.get("to", "")))
        if region_a is None or region_b is None:
            return None  # unrecognized region -> drop, never guess (§A)

        roundtrip = bool(raw.get("roundtrip", False))
        return RawChartRow(
            program=program,
            region_a=region_a,
            region_b=region_b,
            cabin=cabin,
            miles=miles,
            roundtrip=roundtrip,
        )
