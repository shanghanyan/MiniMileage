"""Bypass layer 1 — block_type classification tests."""

from __future__ import annotations

import os as _os

_os.environ.setdefault("MILEAGE_OFFLINE", "1")

from mileage.providers.aggregator.block_detect import (
    BLOCK_AKAMAI,
    BLOCK_CAPTCHA,
    BLOCK_CF,
    BLOCK_HTTP_403,
    BLOCK_NONE,
    BLOCK_RATE_LIMIT,
    BLOCK_SHORT_SHELL,
    BLOCK_UNDECODED,
    classify_block,
)
from mileage.providers.aggregator.fetch import FetchResult, Fetcher


def test_classify_clean_200() -> None:
    bt, signals = classify_block(
        status=200,
        body="<html><table><tr><td>LAX</td></tr></table></html>" * 5,
    )
    assert bt == BLOCK_NONE
    assert signals == []


def test_classify_cloudflare_headers_and_body() -> None:
    bt, _ = classify_block(
        status=403,
        headers={"server": "cloudflare", "cf-ray": "abc-SJC"},
        body="<html>Attention Required! | Cloudflare</html>",
    )
    assert bt == BLOCK_CF


def test_classify_cloudflare_challenge_on_200() -> None:
    bt, signals = classify_block(
        status=200,
        body=(
            "<html><title>Just a moment...</title>"
            "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script>"
            "</html>"
        ),
    )
    assert bt == BLOCK_CF
    assert any("cloudflare" in s or "cf" in s for s in signals)


def test_classify_429() -> None:
    bt, _ = classify_block(status=429, body="slow down")
    assert bt == BLOCK_RATE_LIMIT


def test_classify_plain_403() -> None:
    bt, _ = classify_block(status=403, body="forbidden")
    assert bt == BLOCK_HTTP_403


def test_classify_akamai_cookie() -> None:
    bt, _ = classify_block(
        status=403,
        headers={"set-cookie": "_abck=1; path=/"},
        body="Access Denied",
    )
    assert bt == BLOCK_AKAMAI


def test_classify_captcha_body() -> None:
    bt, _ = classify_block(
        status=200,
        body='<div class="g-recaptcha" data-sitekey="x"></div>' + ("pad" * 100),
    )
    assert bt == BLOCK_CAPTCHA


def test_classify_short_shell() -> None:
    bt, _ = classify_block(status=200, body="<html></html>")
    assert bt == BLOCK_SHORT_SHELL


def test_classify_undecoded() -> None:
    # Mostly non-printable bytes decoded as latin-1 look like binary noise.
    noise = "".join(chr(i % 256) for i in range(500))
    bt, _ = classify_block(status=200, body=noise)
    assert bt == BLOCK_UNDECODED


def test_annotate_ok_sets_block_type_on_challenge() -> None:
    fetcher = Fetcher(offline=True)
    result = FetchResult(
        url="https://example.com",
        text="<html><title>Just a moment...</title>cdn-cgi/challenge</html>" * 3,
        status=200,
        final_url="https://example.com",
        via="httpx",
    )
    out = fetcher._annotate_ok(result)
    assert out.block_type == BLOCK_CF


def test_failed_helper_classifies_403() -> None:
    fetcher = Fetcher(offline=True)
    fail = fetcher._failed(
        "https://example.com",
        status=403,
        via="httpx",
        text="forbidden",
        headers={"server": "nginx"},
    )
    assert not fail.ok
    assert fail.block_type == BLOCK_HTTP_403
