"""Amadeus for Developers (Self-Service) — L1 schedules + L2 cash fares (§5).

PRIMARY source for the cash "price-to-beat". Free tier, real data. Requires
AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET in the environment; when absent the
provider reports DOWN and the registry falls back to curated fares (graceful
degradation, §2.4). Phase 0 therefore runs end-to-end with zero API keys.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from ..domain.models import FareQuote, Layer, Provenance, Route
from .base import ProviderHealth, Query, Quote

log = logging.getLogger("mileage.amadeus")

_TEST_BASE = "https://test.api.amadeus.com"


class AmadeusProvider:
    name = "amadeus"
    trust = 0.9  # live API, above curated fallback

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: str = _TEST_BASE,
        timeout: float = 10.0,
    ) -> None:
        self.client_id = client_id or os.getenv("AMADEUS_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("AMADEUS_CLIENT_SECRET")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token: Optional[str] = None

    def capabilities(self) -> set[Layer]:
        return {Layer.SCHEDULES, Layer.FARES}

    def health(self) -> ProviderHealth:
        if not (self.client_id and self.client_secret):
            return ProviderHealth.DOWN  # no creds -> registry skips us
        return ProviderHealth.HEALTHY

    def remaining_quota(self) -> Optional[int]:
        return None

    def fetch(self, q: Query) -> list[Quote]:
        if q.layer != Layer.FARES or self.health() == ProviderHealth.DOWN:
            return []
        try:
            return self._fetch_fares(q.route)
        except Exception as exc:  # never crash the run
            log.warning("amadeus fetch failed: %s", exc)
            return []

    # --- internals --------------------------------------------------------- #
    def _auth(self) -> str:
        if self._token:
            return self._token
        resp = httpx.post(
            f"{self.base_url}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _fetch_fares(self, route: Route) -> list[FareQuote]:
        from datetime import date, timedelta

        depart = (date.today() + timedelta(days=30)).isoformat()
        travel_class = {
            "economy": "ECONOMY",
            "premium_economy": "PREMIUM_ECONOMY",
            "business": "BUSINESS",
            "first": "FIRST",
        }[route.cabin.value]
        resp = httpx.get(
            f"{self.base_url}/v2/shopping/flight-offers",
            params={
                "originLocationCode": route.origin,
                "destinationLocationCode": route.dest,
                "departureDate": depart,
                "adults": 1,
                "travelClass": travel_class,
                "currencyCode": "USD",
                "max": 5,
                "nonStop": "false",
            },
            headers={"Authorization": f"Bearer {self._auth()}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        offers = resp.json().get("data", [])
        prices = [
            float(o["price"]["grandTotal"])
            for o in offers
            if o.get("price", {}).get("grandTotal")
        ]
        if not prices:
            return []
        cheapest_cents = int(round(min(prices) * 100))
        prov = Provenance(
            source_name="Amadeus Flight Offers Search",
            source_url=f"{self.base_url}/v2/shopping/flight-offers",
            trust=self.trust,
        )
        return [
            FareQuote(
                route=route,
                cash_cents=cheapest_cents,
                provenance=prov,
                confidence=self.trust,
                flags=[],
            )
        ]
