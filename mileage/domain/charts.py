"""Partner award-chart logic (region resolution + band lookup).

Pure logic only: the chart *data* lives in knowledge/charts.yaml and is loaded
by providers/curated.py. This module resolves a `Route` against a parsed chart
spec for one program and returns the one-way miles, handling round-trip charts
(e.g. ANA) by normalizing to one-way and flagging it (§6 carried-over fixes).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .models import Cabin, Route


@dataclass
class ChartHit:
    program: str
    miles: int
    flags: list[str] = field(default_factory=list)


def region_of(airport: str, region_map: dict[str, str]) -> Optional[str]:
    return region_map.get(airport.upper())


def _bands_match(band_regions: list[str], a: str, b: str) -> bool:
    """A band matches a route if its unordered region pair equals {a, b}."""
    return sorted(x.lower() for x in band_regions) == sorted([a, b])


def lookup_award_miles(
    program: str,
    program_chart: dict,
    route: Route,
    region_map: dict[str, str],
) -> Optional[ChartHit]:
    """Resolve `route` against one program's chart. None if unresolvable.

    `program_chart` shape (from charts.yaml):
        {
          "bands": [
            {"regions": ["north_america", "europe"],
             "roundtrip": false,
             "miles": {"economy": 30000, "business": 45000}},
            ...
          ]
        }
    """
    r_o = region_of(route.origin, region_map)
    r_d = region_of(route.dest, region_map)
    if r_o is None or r_d is None:
        return None

    cabin_key = route.cabin.value
    for band in program_chart.get("bands", []):
        regions = band.get("regions", [])
        if len(regions) != 2 or not _bands_match(regions, r_o, r_d):
            continue
        miles_map = band.get("miles", {})
        raw = miles_map.get(cabin_key)
        if raw is None:
            # Band matches the geography but not this cabin: no usable datum.
            return None
        flags: list[str] = []
        miles = int(raw)
        if band.get("roundtrip", False):
            miles = math.ceil(miles / 2)
            flags.append("rt_to_ow_normalized")
        return ChartHit(program=program, miles=miles, flags=flags)
    return None


def cabins_available(program_chart: dict) -> set[Cabin]:
    out: set[Cabin] = set()
    for band in program_chart.get("bands", []):
        for c in band.get("miles", {}):
            try:
                out.add(Cabin(c))
            except ValueError:
                continue
    return out
