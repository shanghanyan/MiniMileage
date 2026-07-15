"""Backend selection for the discovery intake's extractor (§6.2).

Every ingest call site (`ingest/email_source.py`, `ingest/creators.py`,
`ingest/transcripts.py`) used to hardcode `DeterministicExtractor()` directly.
That made the "swappable backend" promise in the plan aspirational — there
was nowhere to actually flip the switch. `build_extractor()` is that switch:
default stays the keyless deterministic extractor (so offline tests and a
fresh checkout with no local model still work unchanged); setting
`MILEAGE_EXTRACTOR_BACKEND=ollama` (plus `OLLAMA_HOST`/`OLLAMA_MODEL` if not
using the defaults) opts into `OllamaExtractor` wherever a call site asks this
factory instead of constructing `DeterministicExtractor` itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import LLMExtractor
from .deterministic import DeterministicExtractor

if TYPE_CHECKING:
    from ...config import Config


def build_extractor(config: Optional["Config"] = None) -> LLMExtractor:
    """Return the configured `LLMExtractor` backend (deterministic by default)."""
    if config is None or getattr(config, "extractor_backend", "deterministic") == "deterministic":
        return DeterministicExtractor()
    if config.extractor_backend == "ollama":
        # Imported lazily so `httpx`-less/no-Ollama installs never pay for
        # this module unless they actually opt in.
        from .local_extractor import OllamaExtractor

        return OllamaExtractor(
            host=config.ollama_host,
            model=config.ollama_model,
        )
    raise ValueError(f"unknown MILEAGE_EXTRACTOR_BACKEND: {config.extractor_backend!r}")
