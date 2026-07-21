"""Email inbox auto-delete — messages get moved to Trash after they're scraped.

Runs standalone (`python tests/test_email_autodelete.py`) and is pytest-
discoverable. Everything is OFFLINE with respect to the network: IMAP itself
is faked (`imaplib.IMAP4_SSL` is monkeypatched), so this never touches a real
mailbox, but it exercises the real UID-search -> UID-fetch -> UID-copy/store
-> expunge code path in `email_source.py`.

Context: previously (see mileage-project-state memory) there was no message-
level cleanup at all — `mileage discover` re-polled and re-extracted every
UNSEEN message forever. This proves the fix: a live-polled message is moved
to [Gmail]/Trash once it's actually been run through the extractor, and that
behavior is off for fixtures/offline runs and toggleable via
`gmail_auto_delete` / `GMAIL_AUTO_DELETE`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config
from mileage.providers.aggregator.ingest import email_source as es

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"

_RAW_BODIES = {
    b"101": b"From: promo@example.com\r\nSubject: hello\r\n\r\nJust a promo, no chart data.",
    b"102": b"From: promo2@example.com\r\nSubject: hi\r\n\r\nAnother promo, no chart data.",
}


class _FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: two UNSEEN messages, UID-addressable."""

    calls: list = []  # class-level so both the poll AND delete connections share it

    def __init__(self, *_a, **_kw) -> None:
        self.readonly = None

    def login(self, *_a):
        return "OK", [b"ok"]

    def select(self, _mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return "OK", [b"101 102"]
        if command == "FETCH":
            key = args[0]
            return "OK", [(b"101 (BODY[] {1}", _RAW_BODIES[key]), b")"]
        if command == "COPY":
            _FakeIMAP.calls.append(("copy", args[0]))
            return "OK", [b"done"]
        if command == "STORE":
            assert self.readonly is False, "STORE must happen on a read-write connection"
            _FakeIMAP.calls.append(("store", args[0]))
            return "OK", [b"done"]
        return "NO", [b"unknown command"]

    def expunge(self):
        _FakeIMAP.calls.append(("expunge", None))
        return "OK", [b""]

    def close(self):
        pass

    def logout(self):
        pass


def _patch_imap():
    _FakeIMAP.calls = []
    orig = es.imaplib.IMAP4_SSL
    es.imaplib.IMAP4_SSL = _FakeIMAP
    return orig


def _unpatch_imap(orig) -> None:
    es.imaplib.IMAP4_SSL = orig


def test_imap_documents_capture_uid() -> None:
    orig = _patch_imap()
    try:
        docs = es._imap_documents("occulosequor@gmail.com", "fake-pw", limit=50)
    finally:
        _unpatch_imap(orig)
    assert {d.uid for d in docs} == {"101", "102"}
    assert {d.subject for d in docs} == {"hello", "hi"}


def test_delete_processed_moves_to_trash_and_expunges() -> None:
    orig = _patch_imap()
    try:
        moved = es._delete_processed("occulosequor@gmail.com", "fake-pw", ["101", "102"])
    finally:
        _unpatch_imap(orig)
    assert moved == 2
    kinds = [c[0] for c in _FakeIMAP.calls]
    assert kinds.count("copy") == 2
    assert kinds.count("store") == 2
    assert kinds.count("expunge") == 1
    assert {c[1] for c in _FakeIMAP.calls if c[0] == "store"} == {"101", "102"}


def test_run_discovery_auto_deletes_live_polled_mail() -> None:
    orig = _patch_imap()
    try:
        config = Config(
            offline=False,
            knowledge_dir=_KNOWLEDGE,
            gmail_address="occulosequor@gmail.com",
            gmail_app_password="fake-pw",
        )
        result = es.run_discovery(config, fixture_dir=_FIXTURES)
    finally:
        _unpatch_imap(orig)

    assert result.used_fixtures is False
    assert len(result.documents) == 2
    assert result.deleted_count == 2
    stores = {c[1] for c in _FakeIMAP.calls if c[0] == "store"}
    assert stores == {"101", "102"}


def test_run_discovery_skips_delete_when_disabled() -> None:
    orig = _patch_imap()
    try:
        config = Config(
            offline=False,
            knowledge_dir=_KNOWLEDGE,
            gmail_address="occulosequor@gmail.com",
            gmail_app_password="fake-pw",
            gmail_auto_delete=False,
        )
        result = es.run_discovery(config, fixture_dir=_FIXTURES)
    finally:
        _unpatch_imap(orig)

    assert result.deleted_count == 0
    assert not any(c[0] in ("copy", "store") for c in _FakeIMAP.calls)


def test_fixtures_never_trigger_delete() -> None:
    # offline=True (or no creds) -> .eml fixtures -> used_fixtures=True ->
    # the delete path must never even try to touch imaplib.
    orig = es.imaplib.IMAP4_SSL

    def _boom(*_a, **_kw):
        raise AssertionError("fixtures path must never open a real IMAP connection")

    es.imaplib.IMAP4_SSL = _boom
    try:
        config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
        result = es.run_discovery(config, fixture_dir=_FIXTURES)
    finally:
        es.imaplib.IMAP4_SSL = orig
    assert result.used_fixtures is True
    assert result.deleted_count == 0


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  XX  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  XX  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
