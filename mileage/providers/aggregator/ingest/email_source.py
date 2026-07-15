"""Email intake for the discovery mode (§6.1) — `occulosequor@gmail.com`.

The mailbox is a *standing scraping feed*: any received newsletter is ingested
automatically. This module polls the inbox over IMAP (App Password only — no
Gmail API, no OAuth, no Pub/Sub), takes each unread message's HTML/plain body
as a document, runs it through the local extractor, and returns number-grounded
`RawChartRow`s plus any program a "devaluation" subject flagged stale.

Auth comes from the environment (`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`), never
from code. With no credentials — or in `MILEAGE_OFFLINE` mode — it falls back to
`.eml` fixtures under `knowledge/fixtures/`, so the whole pipeline runs and is
testable without touching the network.
"""

from __future__ import annotations

import email
import imaplib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

from ..parse import RawChartRow
from .devaluation import detect_devaluation

if TYPE_CHECKING:  # avoid a runtime import cycle with the package __init__
    from ...config import Config

log = logging.getLogger("mileage.aggregator.ingest.email")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


@dataclass
class EmailDocument:
    sender: str
    subject: str
    body: str
    received_at: Optional[str] = None

    @property
    def source_name(self) -> str:
        addr = email.utils.parseaddr(self.sender)[1] or self.sender
        return f"email:{addr.strip().lower() or 'unknown'}"


@dataclass
class DiscoveryResult:
    documents: List[EmailDocument] = field(default_factory=list)
    rows: List[dict] = field(default_factory=list)          # serialized RawChartRow + provenance
    stale_programs: set = field(default_factory=set)
    used_fixtures: bool = False
    email_links_followed: int = 0


# --------------------------------------------------------------------------- #
# Body extraction
# --------------------------------------------------------------------------- #
def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def message_body(msg: Message) -> str:
    """Prefer the HTML body; fall back to text/plain. Skip attachments."""
    html: Optional[str] = None
    text: Optional[str] = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            ctype = part.get_content_type()
            if ctype == "text/html" and html is None:
                html = _decode(part)
            elif ctype == "text/plain" and text is None:
                text = _decode(part)
    else:
        body = _decode(msg)
        if msg.get_content_type() == "text/html":
            html = body
        else:
            text = body
    return html or text or ""


def _to_document(msg: Message) -> EmailDocument:
    received = msg.get("Date")
    received_iso: Optional[str] = None
    if received:
        try:
            received_iso = email.utils.parsedate_to_datetime(received).astimezone(
                timezone.utc
            ).isoformat()
        except (TypeError, ValueError):
            received_iso = None
    return EmailDocument(
        sender=str(msg.get("From") or "unknown"),
        subject=str(msg.get("Subject") or ""),
        body=message_body(msg),
        received_at=received_iso,
    )


# --------------------------------------------------------------------------- #
# Sources: live IMAP, or fixtures when offline / unconfigured
# --------------------------------------------------------------------------- #
def _fixture_documents(fixture_dir: Path) -> List[EmailDocument]:
    docs: List[EmailDocument] = []
    for path in sorted(Path(fixture_dir).glob("*.eml")):
        try:
            msg = email.message_from_bytes(path.read_bytes())
        except Exception as exc:
            log.info("skip fixture %s: %s", path.name, exc)
            continue
        docs.append(_to_document(msg))
    return docs


def _imap_documents(
    address: str, app_password: str, *, limit: int
) -> List[EmailDocument]:
    """Poll unread Gmail over IMAP. PEEK keeps messages unread (idempotent)."""
    docs: List[EmailDocument] = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(address, app_password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return docs
        ids = data[0].split()[:limit] if data and data[0] else []
        for mid in ids:
            typ, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data:
                continue
            raw = next(
                (p[1] for p in msg_data if isinstance(p, tuple) and p[1]), None
            )
            if raw is None:
                continue
            docs.append(_to_document(email.message_from_bytes(raw)))
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass
    return docs


def fetch_email_documents(
    config: "Config",
    *,
    fixture_dir: Optional[Path] = None,
    limit: int = 50,
) -> tuple[List[EmailDocument], bool]:
    """Return (documents, used_fixtures). Never raises — degrades to fixtures/[]."""
    fixture_dir = Path(fixture_dir) if fixture_dir else config.knowledge_dir / "fixtures"
    address = getattr(config, "gmail_address", None)
    password = getattr(config, "gmail_app_password", None)

    if getattr(config, "offline", False) or not (address and password):
        if not (address and password):
            log.info(
                "GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set; using .eml fixtures."
            )
        return _fixture_documents(fixture_dir), True

    try:
        return _imap_documents(address, password, limit=limit), False
    except Exception as exc:  # auth/network failure -> honest degrade, no crash
        log.warning("IMAP poll failed (%s); falling back to .eml fixtures.", exc)
        return _fixture_documents(fixture_dir), True


# --------------------------------------------------------------------------- #
# The discovery run
# --------------------------------------------------------------------------- #
def run_discovery(
    config: "Config",
    *,
    extractor=None,
    fetcher=None,
    cache: Any = None,
    caption_fetcher=None,
    fixture_dir: Optional[Path] = None,
    limit: int = 50,
    link_limit: int = 5,
) -> DiscoveryResult:
    """Poll the mailbox, extract grounded rows, detect devaluations.

    When ``fetcher`` is provided, http(s)/file blog and YouTube links embedded
    in the email body are also fetched and extracted (same grounded extractor).

    Pure with respect to the store: it returns a `DiscoveryResult`; persisting
    to `discovered_charts.json` is the caller's job (the CLI), so the function
    is trivially testable.
    """
    if extractor is None:
        from ..extract import build_extractor

        extractor = build_extractor(config)

    documents, used_fixtures = fetch_email_documents(
        config, fixture_dir=fixture_dir, limit=limit
    )
    result = DiscoveryResult(documents=documents, used_fixtures=used_fixtures)

    from .email_links import follow_links_in_document

    for doc in documents:
        program = detect_devaluation(doc.subject)
        if program:
            result.stale_programs.add(program)
            log.info("devaluation: %s flagged stale via %s", program, doc.source_name)

        rows: List[RawChartRow] = extractor.extract(
            doc.body, source_hint=doc.source_name
        )
        for r in rows:
            result.rows.append(
                {
                    "program": r.program,
                    "region_a": r.region_a,
                    "region_b": r.region_b,
                    "cabin": r.cabin,
                    "miles": r.miles,
                    "roundtrip": r.roundtrip,
                    "source_name": doc.source_name,
                    "source_url": None,
                    "source_updated_at": doc.received_at,
                    "trust": 0.3,  # discovered rows are second-class until confirmed
                }
            )

        if fetcher is not None:
            link_rows, n_links = follow_links_in_document(
                doc,
                fetcher=fetcher,
                extractor=extractor,
                cache=cache,
                caption_fetcher=caption_fetcher,
                limit=link_limit,
            )
            result.rows.extend(link_rows)
            result.email_links_followed += n_links

    return result
