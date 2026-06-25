"""Data sources behind ONE interface (Cursor-Mileage-Plan.md §4-5).

Each provider declares the layers it serves and its quota/health; the registry
federates a query by capability -> health -> cache-first -> remaining quota ->
trust order. Any provider (or a whole layer) can return nothing without
crashing the run.
"""

from .base import Provider, Query, ProviderHealth
from .registry import ProviderRegistry

__all__ = ["Provider", "Query", "ProviderHealth", "ProviderRegistry"]
