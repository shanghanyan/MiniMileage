"""JSON serialization for API + CLI output."""

from __future__ import annotations

from typing import Optional

from .domain.models import Route, Verdict
from .verify.crosscheck import VerifiedAward, VerifiedFare


def quote_result_to_dict(result: dict) -> dict:
    route: Route = result["route"]
    out: dict = {"route": route.key()}
    verdict: Optional[Verdict] = result.get("verdict")
    if verdict is None:
        out["error"] = result.get("error")
        out["message"] = result.get("message")
        return out

    fare: VerifiedFare = result["fare"]
    awards: list[VerifiedAward] = result.get("awards") or []
    live = [a for a in awards if a.seats_available is not None]

    out["verdict"] = verdict.label.value
    out["rationale"] = verdict.rationale
    out["flags"] = verdict.flags
    out["fare_cents"] = fare.cash_cents
    out["fare_flags"] = fare.flags
    out["fare_confidence"] = fare.confidence
    out["live_award_space"] = [
        {
            "program": a.program,
            "miles": a.miles,
            "seats_available": a.seats_available,
            "flags": a.flags,
        }
        for a in live
    ]
    out["options"] = [
        {
            "label": o.label,
            "kind": o.kind,
            "cpp": round(o.cpp, 2),
            "source_points": o.source_points,
            "affordable": o.affordable,
            "confidence": o.confidence,
            "flags": o.flags,
        }
        for o in verdict.options
    ]
    if verdict.best_transfer:
        out["best_transfer"] = {
            "label": verdict.best_transfer.label,
            "cpp": round(verdict.best_transfer.cpp, 2),
            "source_points": verdict.best_transfer.source_points,
            "flags": verdict.best_transfer.flags,
        }
    out["portal_cpp"] = round(verdict.portal.cpp, 2)
    return out
