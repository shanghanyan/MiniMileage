"""Tests for loyalty-program name canonicalization."""

from __future__ import annotations

import pytest

from mileage.providers.aggregator.extract.programs import canonicalize_program


@pytest.mark.parametrize(
    "text,expected",
    [
        ("turkish", "turkish"),
        ("Turkish Miles&Smiles", "turkish"),
        ("turkish miles&smiles", "turkish"),
        ("avianca lifemiles", "lifemiles"),
        ("Avianca LifeMiles", "lifemiles"),
        ("lifemiles", "lifemiles"),
        ("ana mileage club", "ana"),
        ("ANA Mileage Club", "ana"),
        ("krisflyer", "krisflyer"),
        ("Singapore KrisFlyer", "krisflyer"),
        ("air canada aeroplan", "aeroplan"),
        ("", None),
        ("united mileageplus", None),
    ],
)
def test_canonicalize_program(text: str, expected: str | None) -> None:
    assert canonicalize_program(text) == expected
