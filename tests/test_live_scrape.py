import os, sys
if os.environ.get("MILEAGE_OFFLINE", "1") not in ("0", "false", "no"):
    print("SKIP: set MILEAGE_OFFLINE=0 to run live scrape test")
    # Hard-exit only when run as a standalone script. Under pytest (conftest pins
    # MILEAGE_OFFLINE=1) a top-level sys.exit() raises SystemExit during
    # collection and aborts the whole suite; there pytest just finds no test_*
    # functions here, so the file is a silent no-op as intended.
    if "pytest" not in sys.modules:
        sys.exit(0)
    else:
        import pytest
        pytest.skip("set MILEAGE_OFFLINE=0 to run live scrape test", allow_module_level=True)

"""Live scrape smoke test — fetch -> parse -> resolve, per target (§6).

Walks every target in `knowledge/sources.yaml` through the REAL production
fetch/parse/resolve stack (no fakes) and prints a role-aware scoreboard. It is
intentionally NOT a pytest test: under a normal `pytest` run `MILEAGE_OFFLINE=1`
(conftest default) makes the guard above exit 0 immediately. To hit the network:

    MILEAGE_OFFLINE=0 python tests/test_live_scrape.py

The per-target check, the zero-row diagnosis, and the role reclassification all
live in `mileage.providers.aggregator.live_scrape` — the SAME module the
`GET /scrape/live` endpoint uses — so the CLI and the UI can never disagree
about what was scraped or why something failed. That module also parses through
`AggregatorProvider`'s own dispatch, so this harness cannot drift from
production the way a hand-maintained parser switch used to.

Exit code is 0 unless a PRIMARY source failed (i.e. a program lost its only
working source). A fallback failing is a warning, never a red build — a
redundant mirror/PDF/blog going down is expected and must not read as an outage.
"""

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.providers.aggregator.live_scrape import run_live_scrape

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"

_STATUS_LABEL = {"ok": "ok", "warn": "WARN", "fail": "FAIL"}


def main() -> int:
    report = run_live_scrape(knowledge_dir=_KNOWLEDGE, offline=False)

    for t in report.targets:
        label = _STATUS_LABEL.get(t.status, t.status)
        print(f"[{label:^5}]  {t.role:<8}  {t.name:<24}  {t.url[:52]:<52}  {t.detail}")

    print("\nPrograms (chart coverage — a working PRIMARY is what counts):")
    for p in report.programs:
        mark = "green" if p.has_working_primary else "RED"
        prims = ", ".join(f"{x['name']}={_STATUS_LABEL.get(x['status'], x['status'])}"
                          for x in p.primaries) or "(none)"
        line = f"  [{mark:^5}]  {p.program:<10}  primary: {prims}"
        if p.fallbacks:
            fbs = ", ".join(f"{x['name']}={_STATUS_LABEL.get(x['status'], x['status'])}"
                            for x in p.fallbacks)
            line += f"   fallback: {fbs}"
        print(line)

    s = report.summary
    print(
        f"\n{s['primary_ok']}/{s['primary_ok'] + s['primary_warn'] + s['primary_fail']} "
        f"primaries ok · {s['fallback_warn']} fallback warning(s) · "
        f"{s['programs']} programs"
    )
    if s["all_primaries_ok"]:
        print("EVERY program has a working primary. ✅")
        return 0
    broken = [p.program for p in report.programs if not p.has_working_primary]
    print(f"MISSING working primary for: {', '.join(broken)} ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
