"""Observability — Arize AX tracing for the redemption pipeline (§10, Phase 5).

The "agent" here is `cli.run_quote`: query -> providers.fetch -> verify -> graph
-> conclude. There are no LLM calls, so we emit **manual OpenInference spans** —
a CHAIN span per quote run, with a TOOL/RETRIEVER child span per provider fetch
(each scrape / API call shows up with its input query and output quote count).

Design rules (from the arize-instrumentation skill):
  - Purely additive: importing this module never changes business logic.
  - Optional + graceful: `arize-otel` / `opentelemetry` are an extra. If they
    are not installed, or credentials are absent, every span helper is a no-op
    and the app runs unchanged.
  - Credentials come only from the environment (populated by .env), never code.

Enable by setting ARIZE_SPACE_ID + ARIZE_API_KEY (real traces -> Arize AX), or
MILEAGE_TRACE_CONSOLE=1 for a local console exporter (verification without an
account). MILEAGE_TRACE=0 force-disables.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

log = logging.getLogger("mileage.obs")

try:
    from opentelemetry import trace as _otel_trace
    from openinference.semconv.trace import (
        OpenInferenceSpanKindValues,
        SpanAttributes,
    )

    _HAS_OTEL = True
except ImportError:  # tracing extra not installed -> all helpers no-op
    _HAS_OTEL = False

_SPAN_KIND = "openinference.span.kind"
_INPUT = "input.value"
_OUTPUT = "output.value"
_METADATA = "metadata"

if _HAS_OTEL:
    _SPAN_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
    _INPUT = SpanAttributes.INPUT_VALUE
    _OUTPUT = SpanAttributes.OUTPUT_VALUE
    _METADATA = SpanAttributes.METADATA
    KIND_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
    KIND_TOOL = OpenInferenceSpanKindValues.TOOL.value
    KIND_RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value
else:
    KIND_CHAIN, KIND_TOOL, KIND_RETRIEVER = "CHAIN", "TOOL", "RETRIEVER"


_provider: Any = None
_enabled: bool = False
_TRACER_NAME = "mileage"


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def setup_tracing(project_name: Optional[str] = None) -> bool:
    """Initialize Arize AX tracing once, before any pipeline run. Idempotent.

    Returns True if tracing is active. Never raises — missing deps or creds
    degrade to a no-op so the app keeps working.
    """
    global _provider, _enabled

    if _enabled:
        return True
    if os.getenv("MILEAGE_TRACE", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    if not _HAS_OTEL:
        log.info(
            "tracing requested but arize-otel/opentelemetry not installed; "
            'install with: pip install -e ".[observability]"'
        )
        return False

    project = (
        project_name
        or os.getenv("ARIZE_PROJECT_NAME")
        or os.getenv("ARIZE_MODEL_ID")
        or "mileage"
    )
    space_id = os.getenv("ARIZE_SPACE_ID") or os.getenv("ARIZE_SPACE") or ""
    api_key = os.getenv("ARIZE_API_KEY") or ""
    console = _truthy("MILEAGE_TRACE_CONSOLE")

    try:
        if space_id and api_key:
            from arize.otel import register

            _provider = register(
                space_id=space_id,
                api_key=api_key,
                project_name=project,
                log_to_console=console,
            )
            log.info("Arize AX tracing enabled (project=%s)", project)
        elif console:
            # Local-only verification path: no Arize account needed.
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                ConsoleSpanExporter,
                SimpleSpanProcessor,
            )

            _provider = TracerProvider()
            _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            _otel_trace.set_tracer_provider(_provider)
            log.info("tracing enabled with console exporter (no Arize creds)")
        else:
            log.info(
                "tracing inactive: set ARIZE_SPACE_ID + ARIZE_API_KEY (or "
                "MILEAGE_TRACE_CONSOLE=1) to emit spans."
            )
            return False
    except Exception as exc:  # never let observability break the app
        log.warning("tracing setup failed (continuing without it): %s", exc)
        _provider = None
        return False

    _enabled = True
    return True


def shutdown_tracing() -> None:
    """Flush + shut down the exporter. Critical for short-lived CLI runs so
    async OTLP exports are not dropped on exit."""
    global _enabled
    if _provider is None:
        return
    try:
        if hasattr(_provider, "force_flush"):
            _provider.force_flush()
        if hasattr(_provider, "shutdown"):
            _provider.shutdown()
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("tracing shutdown error: %s", exc)
    finally:
        _enabled = False


def _tracer():
    if not (_enabled and _HAS_OTEL):
        return None
    return _otel_trace.get_tracer(_TRACER_NAME)


@contextmanager
def span(
    name: str,
    kind: str,
    *,
    input_value: Optional[Any] = None,
    metadata: Optional[str] = None,
) -> Iterator[Any]:
    """Start an OpenInference span. No-op (yields None) when tracing is off."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as s:
        s.set_attribute(_SPAN_KIND, kind)
        if input_value is not None:
            s.set_attribute(_INPUT, str(input_value))
        if metadata is not None:
            s.set_attribute(_METADATA, metadata)
        yield s


def set_output(s: Any, value: Any) -> None:
    if s is not None:
        s.set_attribute(_OUTPUT, str(value))


def set_attr(s: Any, key: str, value: Any) -> None:
    if s is not None:
        s.set_attribute(key, value)
