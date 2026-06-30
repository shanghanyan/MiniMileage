"""The `LLMExtractor` interface (§6.2).

A backend takes a document and returns number-grounded `RawChartRow`s. The
interface is deliberately tiny so the backend is a config swap: the
deterministic, keyless extractor ships today; a local Qwen2.5-Instruct served
via Ollama/llama.cpp with GBNF-constrained decoding is a drop-in replacement
that satisfies the exact same contract (and is gated by the same grounding
guard, so the safety guarantee does not depend on the backend).
"""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from ..parse import RawChartRow


@runtime_checkable
class LLMExtractor(Protocol):
    def extract(self, document: str, *, source_hint: str = "") -> List[RawChartRow]:
        """Prose / HTML / transcript -> schema-valid, number-grounded rows."""
        ...


# Alias: the default backend is deterministic, so "LLM" is a misnomer in this
# build. Callers may depend on the neutral name.
Extractor = LLMExtractor
