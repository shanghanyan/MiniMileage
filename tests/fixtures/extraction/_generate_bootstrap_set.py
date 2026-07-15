"""One-off generator for fixtures 05-26 (kept in-repo so the provenance of
every fixture's exact wording is traceable to this script, not hand-edited
after the fact — see README.md; these are ALL synthetic bootstrap fixtures).

Run once: `python tests/fixtures/extraction/_generate_bootstrap_set.py`
Safe to re-run (overwrites its own output files only).
"""

import json
from pathlib import Path

HERE = Path(__file__).parent

FIXTURES = {
    "fixture_05_aeroplan_clean": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "clean",
        "notes": "Baseline aeroplan happy path, round-trip phrasing.",
        "document": (
            "Air Canada's Aeroplan program is pricing business class from "
            "North America to Europe at 60,000 miles round-trip this month, "
            "one of the more consistent redemptions out there."
        ),
        "expected_rows": [
            {"program": "aeroplan", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 60000, "roundtrip": True}
        ],
    },
    "fixture_06_eva_oceania_clean": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "clean",
        "notes": "EVA + oceania region coverage (oceania mapping is new as of the 2026-07-08 regions.py expansion).",
        "document": (
            "EVA Air's Infinity MileageLands chart still shows economy class "
            "from North America to Oceania priced at 100,000 miles round-trip "
            "via Star Alliance partners, covering both Australia and New Zealand."
        ),
        "expected_rows": [
            {"program": "eva", "region_a": "north_america", "region_b": "oceania",
             "cabin": "economy", "miles": 100000, "roundtrip": True}
        ],
    },
    "fixture_07_ana_first_clean": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "clean",
        "notes": "ANA first-class, a different route than fixture_04, round-trip phrasing.",
        "document": (
            "ANA Mileage Club continues to price first class from Europe to "
            "North Asia at 190,000 miles round-trip, among the most "
            "aspirational redemptions in the Star Alliance chart."
        ),
        "expected_rows": [
            {"program": "ana", "region_a": "europe", "region_b": "north_asia",
             "cabin": "first", "miles": 190000, "roundtrip": True}
        ],
    },
    "fixture_08_krisflyer_saver_advantage_clean": {
        "source_hint": "yt:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "clean",
        "notes": "KrisFlyer-specific Saver/Advantage tier vocabulary; south_asia region.",
        "document": (
            "Singapore KrisFlyer Saver awards from North America to South Asia "
            "in premium economy are pricing at 60,000 miles one-way, well "
            "below the Advantage tier."
        ),
        "expected_rows": [
            {"program": "krisflyer", "region_a": "north_america", "region_b": "south_asia",
             "cabin": "premium_economy", "miles": 60000, "roundtrip": False}
        ],
    },
    "fixture_09_bulleted_multi_program_roundup": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "Cross-item bleed stress test: four different programs/routes/cabins "
            "in one bulleted roundup. A window-based extractor anchored on the "
            "wrong program mention could blend adjacent bullets' fields."
        ),
        "document": (
            "This week's roundup:\n"
            "- Turkish: business class North America to Europe, 45,000 miles one-way.\n"
            "- LifeMiles: economy class North America to South America, 30,000 miles one-way.\n"
            "- Aeroplan: business class Europe to North Asia, 75,000 miles one-way.\n"
            "- KrisFlyer: first class North America to Southeast Asia, 140,000 miles one-way."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 45000, "roundtrip": False},
            {"program": "lifemiles", "region_a": "north_america", "region_b": "south_america",
             "cabin": "economy", "miles": 30000, "roundtrip": False},
            {"program": "aeroplan", "region_a": "europe", "region_b": "north_asia",
             "cabin": "business", "miles": 75000, "roundtrip": False},
            {"program": "krisflyer", "region_a": "north_america", "region_b": "southeast_asia",
             "cabin": "first", "miles": 140000, "roundtrip": False},
        ],
    },
    "fixture_10_implicit_origin": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "Only ONE explicit region (Istanbul -> europe); origin is implicit "
            "'from anywhere in the US'. Tests the deterministic extractor's "
            "single-hint fallback to (north_america, other) — this one is "
            "expected to actually PASS today; contrast with fixture_20 where "
            "the same fallback logic gives the WRONG answer."
        ),
        "document": (
            "Flying to Istanbul in business only costs 45,000 Turkish "
            "Miles&Smiles miles one-way — no need to overthink the origin."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 45000, "roundtrip": False}
        ],
    },
    "fixture_11_cash_price_distractor": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "Precision risk: a dollar cash fare ($4,150) falls inside the "
            "plausible award-miles range (3,000-400,000) and sits right next "
            "to the real award price in the same sentence."
        ),
        "document": (
            "A comparable cash fare runs about $4,150 round-trip, but Avianca "
            "LifeMiles prices the same business-class North America to Europe "
            "route at 63,000 miles one-way."
        ),
        "expected_rows": [
            {"program": "lifemiles", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 63000, "roundtrip": False}
        ],
    },
    "fixture_12_k_shorthand": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Number written as '27k' shorthand rather than '27,000'.",
        "document": (
            "Turkish's off-peak economy award from North America to Europe is "
            "holding steady at 27k miles one-way, still one of the cheapest "
            "ways across the pond."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "europe",
             "cabin": "economy", "miles": 27000, "roundtrip": False}
        ],
    },
    "fixture_13_space_separated_thousands": {
        "source_hint": "yt:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Number written with a space as the thousands separator ('95 000') instead of a comma.",
        "document": (
            "KrisFlyer business class from North America to Europe is priced "
            "at 95 000 miles one-way under the current chart."
        ),
        "expected_rows": [
            {"program": "krisflyer", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 95000, "roundtrip": False}
        ],
    },
    "fixture_14_cabin_disambiguation": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "A second cabin ('first') is name-dropped as an aside right next to the real business-class number.",
        "document": (
            "Book Aeroplan business (or first, if you can find it) from North "
            "America to Europe for 75,000 miles one-way in business; first is "
            "priced separately and much higher."
        ),
        "expected_rows": [
            {"program": "aeroplan", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 75000, "roundtrip": False}
        ],
    },
    "fixture_15_devaluation_rumor_zero_rows": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Pure devaluation chatter with no published number at all — correct output is zero rows, a precision test.",
        "document": (
            "Rumor has it Turkish Miles&Smiles will devalue its Star Alliance "
            "award chart sometime next year, though no official announcement "
            "or new chart has been published, and current pricing remains "
            "unchanged from last quarter."
        ),
        "expected_rows": [],
    },
    "fixture_16_hotel_points_distractor": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "A hotel-points promotion (not a modeled program) sits beside a real airline row; only the airline row should extract.",
        "document": (
            "Marriott Bonvoy is running a promotion at 35,000 points per night "
            "at several Category 4 hotels this month. Meanwhile, Aeroplan "
            "continues to price economy class from North America to Europe "
            "at 32,000 miles one-way."
        ),
        "expected_rows": [
            {"program": "aeroplan", "region_a": "north_america", "region_b": "europe",
             "cabin": "economy", "miles": 32000, "roundtrip": False}
        ],
    },
    "fixture_17_line_wrapped_whitespace": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Irregular line breaks/whitespace splitting the program name, cabin, and region across several lines — common in copy-pasted newsletter HTML.",
        "document": (
            "Turkish   Miles&Smiles\nbusiness class\nfrom North America\nto "
            "Europe\nis priced at 50,000 miles\none-way this week."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 50000, "roundtrip": False}
        ],
    },
    "fixture_18_percentage_and_date_distractors": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "A percentage ('15%') and a bare year ('2027') both sit below the plausible-miles floor (3,000) and should never be mistaken for the real price.",
        "document": (
            "Turkish increased Star Alliance pricing by roughly 15% starting "
            "January 2027, but as of today business class from North America "
            "to Europe still books for 45,000 miles one-way under the current "
            "chart."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 45000, "roundtrip": False}
        ],
    },
    "fixture_19_dense_four_combo_paragraph": {
        "source_hint": "yt:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Four (region, cabin, miles) combinations for the SAME program packed into one dense sentence — the densest recall stress test in this set.",
        "document": (
            "KrisFlyer's Star Alliance chart shows North America to Southeast "
            "Asia running 40,000 miles one-way in economy and 90,000 miles "
            "one-way in business, while North America to South Asia is 45,000 "
            "miles one-way in economy and 95,000 miles one-way in business."
        ),
        "expected_rows": [
            {"program": "krisflyer", "region_a": "north_america", "region_b": "southeast_asia",
             "cabin": "economy", "miles": 40000, "roundtrip": False},
            {"program": "krisflyer", "region_a": "north_america", "region_b": "southeast_asia",
             "cabin": "business", "miles": 90000, "roundtrip": False},
            {"program": "krisflyer", "region_a": "north_america", "region_b": "south_asia",
             "cabin": "economy", "miles": 45000, "roundtrip": False},
            {"program": "krisflyer", "region_a": "north_america", "region_b": "south_asia",
             "cabin": "business", "miles": 95000, "roundtrip": False},
        ],
    },
    "fixture_20_within_region_non_na_gap": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "KNOWN DETERMINISTIC-EXTRACTOR GAP (documented, not a bug to silently "
            "'fix' by guessing): 'within <region>' phrasing should produce a "
            "same-region pair (southeast_asia, southeast_asia), but "
            "deterministic.py's single-region fallback only special-cases "
            "north_america -> (north_america, north_america); any OTHER single "
            "region falls back to (north_america, other) instead. Expect this "
            "fixture to score 0/1 against the deterministic backend today — "
            "that's the point. Contrast with fixture_25, the north_america "
            "case, which the same code path gets right."
        ),
        "document": (
            "Flying within Southeast Asia? KrisFlyer prices intra-region "
            "economy awards at just 15,000 miles round-trip, one of the best "
            "short-haul deals going."
        ),
        "expected_rows": [
            {"program": "krisflyer", "region_a": "southeast_asia", "region_b": "southeast_asia",
             "cabin": "economy", "miles": 15000, "roundtrip": True}
        ],
    },
    "fixture_21_brand_plus_program_name": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "Brand name and program name combined in one phrase ('Air Canada's Aeroplan') rather than either alone.",
        "document": (
            "Air Canada's Aeroplan continues to price premium economy from "
            "Europe to North America at 45,000 miles one-way, a nice middle "
            "ground between economy and business."
        ),
        "expected_rows": [
            {"program": "aeroplan", "region_a": "europe", "region_b": "north_america",
             "cabin": "premium_economy", "miles": 45000, "roundtrip": False}
        ],
    },
    "fixture_22_spelled_out_number_zero_rows": {
        "source_hint": "yt:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "The award price is spelled out in words ('forty five thousand') "
            "with NO digit form anywhere in the document. This is a deliberate "
            "test of the grounding guard's honest blind spot: "
            "number_is_grounded() only matches digit patterns, so even a "
            "'perfect' semantic reading of this sentence must still be "
            "rejected as ungrounded. Correct behavior for ANY compliant "
            "extractor (deterministic or LLM) is zero rows here — this is a "
            "safety feature, not a recall bug to fix."
        ),
        "document": (
            "So basically, if you're doing Aeroplan business class from North "
            "America over to Europe, that's gonna run you about forty five "
            "thousand miles one way, roughly."
        ),
        "expected_rows": [],
    },
    "fixture_23_transactional_negative_control": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": "A routine booking-confirmation email with no chart data at all — realistic negative control for the live inbox intake.",
        "document": (
            "Your booking confirmation number is XK7P29Q4. Departure: 14 Jan "
            "2027, flight AC851, seat 14C. Please arrive at the airport at "
            "least 3 hours before departure for international flights."
        ),
        "expected_rows": [],
    },
    "fixture_24_middle_east_and_africa_multi_row": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "middle_east and africa region coverage (both new mappings as of "
            "the 2026-07-08 regions.py expansion) in a two-region, same-program "
            "sentence."
        ),
        "document": (
            "Turkish, as a Star Alliance carrier based in Istanbul, prices "
            "business class from North America to the Middle East at 55,000 "
            "miles one-way, and separately to Africa at 65,000 miles one-way "
            "for select routes."
        ),
        "expected_rows": [
            {"program": "turkish", "region_a": "north_america", "region_b": "middle_east",
             "cabin": "business", "miles": 55000, "roundtrip": False},
            {"program": "turkish", "region_a": "north_america", "region_b": "africa",
             "cabin": "business", "miles": 65000, "roundtrip": False},
        ],
    },
    "fixture_25_within_north_america_clean": {
        "source_hint": "blog:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "clean",
        "notes": (
            "'within North America' same-region phrasing — this IS handled "
            "correctly by deterministic.py's single-hint fallback special-case "
            "for north_america specifically. Contrast with fixture_20, the "
            "same 'within X' phrasing for a non-north_america region, which "
            "is NOT handled."
        ),
        "document": (
            "Aeroplan short-haul awards within North America start at just "
            "15,000 miles round-trip in economy for flights under 2,000 miles."
        ),
        "expected_rows": [
            {"program": "aeroplan", "region_a": "north_america", "region_b": "north_america",
             "cabin": "economy", "miles": 15000, "roundtrip": True}
        ],
    },
    "fixture_26_stale_price_distractor": {
        "source_hint": "email:synthetic_bootstrap",
        "synthetic": True,
        "difficulty": "messy",
        "notes": (
            "A historical/stale price (last year's 55,000) sits in the same "
            "sentence as the current price (60,000) for the same program/"
            "route/cabin. Deterministic.py has no temporal reasoning, so it "
            "may extract BOTH numbers as separate rows with an identical key "
            "except miles — expect a possible extra/false-positive row here, "
            "not necessarily a clean match."
        ),
        "document": (
            "LifeMiles currently prices business class from North America to "
            "Europe at 60,000 miles one-way; last year's chart had it at "
            "55,000 miles for comparison."
        ),
        "expected_rows": [
            {"program": "lifemiles", "region_a": "north_america", "region_b": "europe",
             "cabin": "business", "miles": 60000, "roundtrip": False}
        ],
    },
}


def main() -> None:
    for name, data in FIXTURES.items():
        path = HERE / f"{name}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.name}")
    print(f"\n{len(FIXTURES)} fixtures written.")


if __name__ == "__main__":
    main()
