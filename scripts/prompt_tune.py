#!/usr/bin/env python3
"""Interactive prompt-tuning loop for the Ollama extractor.

Edit a prompt template, re-run fixtures, and watch per-call latency plus
miss/extra diffs against expected rows — without touching source code.

Examples:
  python scripts/prompt_tune.py
  python scripts/prompt_tune.py --fixture fixture_01_turkish_business_sweet_spot
  python scripts/prompt_tune.py --only-misses
  python scripts/prompt_tune.py --prompt-file my_prompt.txt --watch

The default prompt file is ``mileage/providers/aggregator/extract/prompt_template.txt``.
It must contain ``{source_hint}`` and ``{document}`` placeholders (same as
``OllamaExtractor``'s built-in template).

Requires a running Ollama server and the configured model pulled locally.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "extraction"
_DEFAULT_PROMPT = (
    _REPO_ROOT / "mileage" / "providers" / "aggregator" / "extract" / "prompt_template.txt"
)

# Allow `python scripts/prompt_tune.py` without installing the package.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mileage.config import Config
from mileage.extraction_eval import (  # noqa: E402
    _chartrow_dict,
    _chartrow_key,
    _expected_key,
    validate_fixture,
)
from mileage.providers.aggregator.extract.local_extractor import (  # noqa: E402
    DEFAULT_OLLAMA_MODEL,
    OllamaExtractor,
    _PROMPT_TEMPLATE,
)


@dataclass
class TuneResult:
    name: str
    latency_ms: float
    expected: int
    produced: int
    matched: int
    missed: List[dict]
    extra: List[dict]
    raw_rows: List[dict]


def _load_prompt(path: Path) -> str:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_PROMPT_TEMPLATE, encoding="utf-8")
        print(f"Created default prompt at {path}")
    text = path.read_text(encoding="utf-8")
    for placeholder in ("{source_hint}", "{document}"):
        if placeholder not in text:
            raise SystemExit(f"Prompt file missing required placeholder {placeholder}: {path}")
    return text


def _load_fixtures(fixtures_dir: Path, names: Optional[List[str]]) -> list[tuple[str, dict]]:
    if names:
        paths = [fixtures_dir / (n if n.endswith(".json") else f"{n}.json") for n in names]
    else:
        paths = sorted(fixtures_dir.glob("*.json"))
    out: list[tuple[str, dict]] = []
    for path in paths:
        if path.name == "schema.json":
            continue
        if not path.is_file():
            raise SystemExit(f"Fixture not found: {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_fixture(doc, path.stem)
        if errors:
            raise SystemExit(f"Invalid fixture {path.name}:\n  " + "\n  ".join(errors))
        out.append((path.stem, doc))
    return out


def _run_fixture(extractor: OllamaExtractor, name: str, doc: dict) -> TuneResult:
    document = doc["document"]
    hint = doc.get("source_hint", "")
    expected_rows = doc.get("expected_rows", [])

    t0 = time.perf_counter()
    produced_rows = extractor.extract(document, source_hint=hint)
    latency_ms = (time.perf_counter() - t0) * 1000

    expected_keys = {_expected_key(r) for r in expected_rows}
    produced_keys = [_chartrow_key(r) for r in produced_rows]
    matched = sum(1 for k in produced_keys if k in expected_keys)
    found_keys = set(produced_keys)
    missed = [r for r in expected_rows if _expected_key(r) not in found_keys]
    extra = [
        _chartrow_dict(r)
        for r, k in zip(produced_rows, produced_keys)
        if k not in expected_keys
    ]
    return TuneResult(
        name=name,
        latency_ms=latency_ms,
        expected=len(expected_rows),
        produced=len(produced_rows),
        matched=matched,
        missed=missed,
        extra=extra,
        raw_rows=[_chartrow_dict(r) for r in produced_rows],
    )


def _print_result(r: TuneResult) -> None:
    ok = not r.missed and not r.extra
    mark = "OK" if ok else "MISS"
    print(
        f"  [{mark}] {r.name}: {r.matched}/{r.expected} matched, "
        f"produced={r.produced}, {r.latency_ms:.0f}ms"
    )
    for m in r.missed:
        print(f"    MISSED: {m}")
    for e in r.extra:
        print(f"    EXTRA:  {e}")


def _summarize(results: List[TuneResult]) -> None:
    total_expected = sum(r.expected for r in results)
    total_produced = sum(r.produced for r in results)
    total_matched = sum(r.matched for r in results)
    total_ms = sum(r.latency_ms for r in results)
    precision = total_matched / total_produced if total_produced else 1.0
    recall = total_matched / total_expected if total_expected else 1.0
    misses = sum(1 for r in results if r.missed or r.extra)
    print("=" * 60)
    print(
        f"TOTAL fixtures={len(results)} misses={misses} "
        f"precision={precision:.2f} recall={recall:.2f} "
        f"latency={total_ms:.0f}ms ({total_ms / len(results):.0f}ms/fixture)"
    )


def _run_once(
    extractor: OllamaExtractor,
    fixtures: list[tuple[str, dict]],
    *,
    only_misses: bool,
    baseline: Optional[List[TuneResult]] = None,
) -> List[TuneResult]:
    results: List[TuneResult] = []
    for name, doc in fixtures:
        if only_misses and baseline is not None:
            base = next((b for b in baseline if b.name == name), None)
            if base is not None and not base.missed and not base.extra:
                continue
        r = _run_fixture(extractor, name, doc)
        results.append(r)
        _print_result(r)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        action="append",
        dest="fixtures",
        metavar="NAME",
        help="Run one fixture (stem or filename); repeat for multiple. Default: all.",
    )
    parser.add_argument(
        "--fixtures-dir",
        default=str(_DEFAULT_FIXTURES),
        help=f"Fixture directory (default: {_DEFAULT_FIXTURES})",
    )
    parser.add_argument(
        "--prompt-file",
        default=str(_DEFAULT_PROMPT),
        help=f"Prompt template with {{source_hint}} and {{document}} (default: {_DEFAULT_PROMPT})",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Ollama model tag (default: $OLLAMA_MODEL or {DEFAULT_OLLAMA_MODEL})",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Ollama host (default: $OLLAMA_HOST or http://localhost:11434)",
    )
    parser.add_argument(
        "--only-misses",
        action="store_true",
        help="Second pass: re-run only fixtures that missed on the first full pass.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Re-run whenever the prompt file's mtime changes (Ctrl-C to stop).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable summary JSON on stdout after the human report.",
    )
    args = parser.parse_args()

    config = Config.from_env()
    host = args.host or config.ollama_host
    model = args.model or config.ollama_model
    prompt_path = Path(args.prompt_file)
    fixtures_dir = Path(args.fixtures_dir)

    fixtures = _load_fixtures(fixtures_dir, args.fixtures)
    prompt = _load_prompt(prompt_path)

    print(f"Prompt: {prompt_path}")
    print(f"Model:  {model} @ {host}")
    print(f"Fixtures: {len(fixtures)}")
    print("=" * 60)

    def make_extractor(template: str) -> OllamaExtractor:
        return OllamaExtractor(host=host, model=model, prompt_template=template)

    baseline: Optional[List[TuneResult]] = None
    if args.only_misses and not args.fixtures:
        print("(baseline pass for --only-misses)")
        baseline = _run_once(make_extractor(_PROMPT_TEMPLATE), fixtures, only_misses=False)
        print("=" * 60)
        print("(re-run with custom prompt, misses only)")
        print("=" * 60)

    last_mtime = prompt_path.stat().st_mtime if prompt_path.is_file() else 0.0

    while True:
        prompt = _load_prompt(prompt_path)
        extractor = make_extractor(prompt)
        results = _run_once(
            extractor,
            fixtures,
            only_misses=args.only_misses and baseline is not None,
            baseline=baseline,
        )
        _summarize(results)

        if args.json:
            payload = {
                "model": model,
                "host": host,
                "prompt_file": str(prompt_path),
                "results": [
                    {
                        "name": r.name,
                        "latency_ms": round(r.latency_ms, 1),
                        "expected": r.expected,
                        "produced": r.produced,
                        "matched": r.matched,
                        "missed": r.missed,
                        "extra": r.extra,
                    }
                    for r in results
                ],
            }
            print(json.dumps(payload, indent=2))

        if not args.watch:
            break

        print(f"\nWatching {prompt_path} — edit and save to re-run (Ctrl-C to stop)\n")
        try:
            while True:
                time.sleep(0.5)
                mtime = prompt_path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    print("=" * 60)
                    print("Prompt file changed — re-running")
                    print("=" * 60)
                    break
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
