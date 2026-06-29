"""Provider federation config loader (Phase 2, §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_DEFAULTS = {
    "cache_ttl_days": 2,
    "health_check_days": 30,
    "quota_warn_below": 10,
}


@dataclass
class ProviderSpec:
    name: str
    trust: float = 0.5
    monthly_quota: Optional[int] = None
    layers: list[str] = field(default_factory=list)
    layer_trust: dict[str, float] = field(default_factory=dict)

    def trust_for(self, layer: str) -> float:
        return self.layer_trust.get(layer, self.trust)


@dataclass
class FederationConfig:
    cache_ttl_seconds: float
    health_check_days: int
    quota_warn_below: int
    providers: dict[str, ProviderSpec]

    def spec(self, name: str) -> Optional[ProviderSpec]:
        return self.providers.get(name)

    def monthly_quota(self, name: str) -> Optional[int]:
        spec = self.spec(name)
        return spec.monthly_quota if spec else None


def load_federation_config(path: Path) -> FederationConfig:
    path = Path(path)
    if not path.exists():
        return FederationConfig(
            cache_ttl_seconds=_DEFAULTS["cache_ttl_days"] * 86400,
            health_check_days=_DEFAULTS["health_check_days"],
            quota_warn_below=_DEFAULTS["quota_warn_below"],
            providers={},
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = {**_DEFAULTS, **(data.get("defaults") or {})}
    providers: dict[str, ProviderSpec] = {}
    for name, raw in (data.get("providers") or {}).items():
        mq = raw.get("monthly_quota")
        providers[name] = ProviderSpec(
            name=name,
            trust=float(raw.get("trust", 0.5)),
            monthly_quota=int(mq) if mq is not None else None,
            layers=list(raw.get("layers") or []),
            layer_trust={
                str(k): float(v) for k, v in (raw.get("layer_trust") or {}).items()
            },
        )
    return FederationConfig(
        cache_ttl_seconds=float(defaults["cache_ttl_days"]) * 86400,
        health_check_days=int(defaults["health_check_days"]),
        quota_warn_below=int(defaults["quota_warn_below"]),
        providers=providers,
    )
