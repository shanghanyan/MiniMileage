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


def great_circle_miles(
    a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Great-circle distance in statute miles between two [lat, lon] points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * 3958.7613 * math.asin(min(1.0, math.sqrt(h)))


def lookup_award_miles(
    program: str,
    program_chart: dict,
    route: Route,
    region_map: dict[str, str],
    *,
    airport_coords: Optional[dict[str, tuple[float, float]]] = None,
) -> Optional[ChartHit]:
    """Resolve `route` against one program's chart. None if unresolvable.

    `program_chart` shape (from charts.yaml / parsed rows):
        {
          "bands": [
            {"regions": ["north_america", "europe"],
             "roundtrip": false,
             "miles": {"economy": 30000, "business": 45000}},
            # distance-banded (Aeroplan): the band also carries a [lo, hi] mile
            # range; it matches only when the route's great-circle distance falls
            # inside it (§A.4). Needs `airport_coords`.
            {"regions": ["north_america", "europe"],
             "roundtrip": false,
             "distance": [4001, 6000],
             "miles": {"business": 70000}},
            ...
          ]
        }
    """
    r_o = region_of(route.origin, region_map)
    r_d = region_of(route.dest, region_map)
    if r_o is None or r_d is None:
        return None

    cabin_key = route.cabin.value
    gcm: Optional[float] = None
    for band in program_chart.get("bands", []):
        regions = band.get("regions", [])
        if len(regions) != 2 or not _bands_match(regions, r_o, r_d):
            continue
        # Distance-banded charts: the geography matched, but the band only
        # applies to a great-circle range. Compute the route distance once and
        # skip bands whose [lo, hi] the route falls outside.
        dist = band.get("distance")
        if dist:
            if airport_coords is None:
                continue
            co = airport_coords.get(route.origin.upper())
            cd = airport_coords.get(route.dest.upper())
            if not (co and cd):
                continue
            if gcm is None:
                gcm = great_circle_miles(co, cd)
            lo, hi = float(dist[0]), float(dist[1])
            if not (lo <= gcm <= hi):
                continue
        miles_map = band.get("miles", {})
        raw = miles_map.get(cabin_key)
        if raw is None:
            # Geography (and distance) matched but not this cabin: keep scanning;
            # another band for the same pair may carry it (distance charts split
            # one zone pair across many cabin/distance rows).
            continue
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
