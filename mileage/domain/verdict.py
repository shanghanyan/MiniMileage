"""conclude_winner — the honesty engine (Cursor-Mileage-Plan.md §7).

Rules:
  - No verified, non-stale transfer path        -> portal_only
  - Best transfer within 20% of portal          -> comparable
  - Best transfer beats portal by >= 20%        -> best
  - ...but if the winner carries a data-quality warning flag -> tentative_best

`no_live_space` and `single_source` are EXPECTED Phase-0 caveats (we only have
curated charts, no live availability yet), so they are surfaced but do not by
themselves downgrade a `best` to `tentative_best`. `hardcoded_fallback` here
tags the cash *price-to-beat* baseline (when no live fare API is configured),
not the recommended award path, so it is likewise surfaced but non-downgrading.
Genuine data-quality / correctness problems on the winning path DO downgrade.
"""

from __future__ import annotations

from typing import Optional

from .models import PathOption, Route, Verdict, VerdictLabel

DEFAULT_THRESHOLD = 0.20

# Flags that turn a `best` into `tentative_best` (real data-quality concerns on
# the winning redemption itself).
WARNING_FLAGS: frozenset[str] = frozenset(
    {"sources_disagree", "stale", "bounds_violation"}
)


def _has_warning(option: PathOption) -> bool:
    return any(
        f in WARNING_FLAGS or f.startswith("sources_disagree")
        for f in option.flags
    )


def conclude_winner(
    route: Route,
    portal: PathOption,
    transfers: list[PathOption],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Verdict:
    """Compare the portal floor against the best affordable transfer path."""
    affordable = [t for t in transfers if t.affordable and t.cpp > 0]
    all_options = sorted([portal, *transfers], key=lambda o: o.cpp, reverse=True)

    best_transfer: Optional[PathOption] = (
        max(affordable, key=lambda t: t.cpp) if affordable else None
    )

    if best_transfer is None:
        rationale = (
            "No verified transfer path beats your portal floor "
            f"({portal.cpp:.2f}c/pt). Just use your portal."
        )
        return Verdict(
            label=VerdictLabel.PORTAL_ONLY,
            route=route,
            portal=portal,
            best_transfer=None,
            options=all_options,
            rationale=rationale,
            flags=sorted(set(portal.flags)),
        )

    ratio = best_transfer.cpp / portal.cpp if portal.cpp > 0 else float("inf")
    flags = sorted(set(best_transfer.flags))

    if ratio >= 1.0 + threshold:
        label = (
            VerdictLabel.TENTATIVE_BEST
            if _has_warning(best_transfer)
            else VerdictLabel.BEST
        )
        rationale = (
            f"{best_transfer.label} returns {best_transfer.cpp:.2f}c/pt vs the "
            f"{portal.cpp:.2f}c/pt portal floor "
            f"({(ratio - 1) * 100:.0f}% better). Transfer wins."
        )
    elif ratio >= 1.0 - threshold:
        label = VerdictLabel.COMPARABLE
        rationale = (
            f"{best_transfer.label} ({best_transfer.cpp:.2f}c/pt) is within "
            f"{threshold * 100:.0f}% of the portal floor "
            f"({portal.cpp:.2f}c/pt). Weigh availability and fees."
        )
    else:
        label = VerdictLabel.PORTAL_ONLY
        rationale = (
            f"Best transfer ({best_transfer.cpp:.2f}c/pt) is well below the "
            f"portal floor ({portal.cpp:.2f}c/pt). Portal is your floor."
        )
        # Portal wins outright; best_transfer is kept for informational display.

    return Verdict(
        label=label,
        route=route,
        portal=portal,
        best_transfer=best_transfer,
        options=all_options,
        rationale=rationale,
        flags=flags,
    )
