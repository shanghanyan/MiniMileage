"""The aggregator's local extractor (§6.2).

Turns a *document* (an email body, a blog article, a transcript) into
schema-valid, number-grounded `RawChartRow[]` — the SAME row shape the
deterministic URL parsers emit, so discovered rows drop straight into
`_build_charts -> verify -> graph` and the core can't tell them apart.

Design contract (matches Cursor-Mileage-Plan §6.2):
  - The backend sits behind the `LLMExtractor` interface so it is swappable
    (a local Qwen/Ollama model is a drop-in later; nothing here is hardwired).
  - The default backend is **deterministic and keyless** — no Anthropic, no
    cloud, no model download — so extraction runs and is testable offline.
  - **Verbatim-number grounding is mandatory**: a row whose `miles` integer does
    not appear literally in the source text is dropped. We never invent a number.
"""

from .base import Extractor, LLMExtractor
from .deterministic import DeterministicExtractor
from .factory import build_extractor
from .grounding import number_is_grounded

__all__ = [
    "Extractor",
    "LLMExtractor",
    "DeterministicExtractor",
    "build_extractor",
    "number_is_grounded",
]
