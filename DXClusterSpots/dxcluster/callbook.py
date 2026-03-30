"""Callsign lookup via HamQTH and QRZ.com XML APIs.

Both services use a session-token model:
  1. Authenticate once with username + password to receive a session key.
  2. Use the session key for every subsequent callsign lookup.

Session keys are cached at module level so repeated lookups within the same
process reuse the existing session.  If a lookup returns an auth/session error
the cached key is discarded and authentication is retried once automatically.

Service summary
---------------
HamQTH  (https://www.hamqth.com)
    Free registration.  Provides name, QTH, grid, CQ/ITU zone, QSL info,
    LoTW/eQSL flags, website, email.  No subscription required.
    XML API documented at: https://www.hamqth.com/developers.php

QRZ.com (https://www.qrz.com)
    XML Data API requires a paid subscription (Standard or higher).
    Free accounts receive a session key but callsign lookups return an
    error saying the subscription is required.
    XML API documented at: https://www.qrz.com/page/xml_data.html

Both clients are tolerant of network errors and missing fields: they return
a CallbookEntry with a non-empty .error field rather than raising exceptions.
This keeps the TUI responsive even when a callbook service is unreachable.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level session-key cache keyed by service name ("hamqth", "qrz").
# Using a module-level dict rather than instance variables means the cache
# survives across multiple CallbookEntry lookups without needing a persistent
# object.
_SESSION_CACHE: dict[str, str] = {}

# HTTP timeout for all callbook requests.  15 seconds is generous for typical
# internet latency; callbook lookups are one-off user actions, not hot-path
# streaming, so a slightly longer timeout is acceptable.
_TIMEOUT = 15.0


@dataclass
class CallbookEntry:
    """Structured data returned by a single callsign lookup.

    All string fields default to "" so callers can always display them without
    None-checking.  Boolean QSL flags default to False (unknown = assume no).
    """
    callsign: str               # The callsign that was looked up (upper-cased)
    name: str = ""              # Licensee's full name
    qth: str = ""               # City / QTH description
    country: str = ""           # Country name
    grid: str = ""              # Maidenhead grid square, e.g. "IO91pm"
    cq_zone: Optional[int] = None   # CQ zone (1–40)
    itu_zone: Optional[int] = None  # ITU zone (1–90)
    email: str = ""
    web: str = ""               # Operator's personal website
    lotw: bool = False          # Participates in ARRL Logbook of the World
    eqsl: bool = False          # Uses eQSL.cc
    qsl_direct: bool = False    # Accepts direct (paper) QSL cards
    qsl_bureau: bool = False    # Accepts bureau QSL cards
    source: str = ""            # Which service provided this data
    error: str = ""             # Non-empty string if the lookup failed


# ---------------------------------------------------------------------------
# Shared HTTP helper
# ---------------------------------------------------------------------------

async def _fetch_url(url: str) -> str:
    """GET a URL and return the response body as a string.

    Runs urllib.request.urlopen() in the default thread-pool executor so the
    event loop is never blocked.  urllib is chosen over aiohttp to avoid an
    external dependency; for the one-off request rate of callbook lookups the
    overhead of thread dispatch is negligible.
    """
    loop = asyncio.get_event_loop()

    def _do() -> str:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")

    return await loop.run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# HamQTH
# ---------------------------------------------------------------------------

async def _hamqth_auth(username: str, password: str) -> str:
    """Authenticate with HamQTH and return a session key string.

    Raises ValueError if the server returns an error (wrong password, account
    not found, etc.).
    """
    url = (
        "https://www.hamqth.com/xml.php"
        f"?u={urllib.parse.quote(username)}"
        f"&p={urllib.parse.quote(password)}"
    )
    xml_text = await _fetch_url(url)
    root = ET.fromstring(xml_text)
    ns = {"h": "https://www.hamqth.com"}

    err_el = root.find(".//h:error", ns)
    if err_el is not None and err_el.text:
        raise ValueError(f"HamQTH: {err_el.text.strip()}")

    key_el = root.find(".//h:session_id", ns)
    if key_el is None or not key_el.text:
        raise ValueError("HamQTH: no session_id in auth response")
    return key_el.text.strip()


async def lookup_hamqth(
    callsign: str,
    username: str,
    password: str,
) -> CallbookEntry:
    """Look up *callsign* via the HamQTH XML API.

    Authenticates automatically (or reuses a cached session key) and retries
    once if the session has expired.

    Args:
        callsign: The amateur callsign to look up.
        username: HamQTH account username.
        password: HamQTH account password.

    Returns:
        A CallbookEntry populated with whatever HamQTH returned, or with
        .error set if the lookup failed.
    """
    entry = CallbookEntry(callsign=callsign.upper(), source="HamQTH")

    # Ensure we have an active session key.
    if "hamqth" not in _SESSION_CACHE:
        try:
            _SESSION_CACHE["hamqth"] = await _hamqth_auth(username, password)
        except Exception as exc:
            entry.error = str(exc)
            return entry

    # Retry loop: attempt the lookup, and if we get a session error, re-auth
    # once and try again.
    for attempt in range(2):
        session_key = _SESSION_CACHE.get("hamqth", "")
        url = (
            "https://www.hamqth.com/xml.php"
            f"?id={urllib.parse.quote(session_key)}"
            f"&callsign={urllib.parse.quote(callsign.upper())}"
            f"&prg=DXClusterSpots"
        )
        try:
            xml_text = await _fetch_url(url)
        except Exception as exc:
            entry.error = f"Network error: {exc}"
            return entry

        root = ET.fromstring(xml_text)
        ns = {"h": "https://www.hamqth.com"}

        err_el = root.find(".//h:error", ns)
        if err_el is not None and err_el.text:
            err_msg = err_el.text.strip().lower()
            if attempt == 0 and ("session" in err_msg or "expired" in err_msg):
                # Session expired – clear cache, re-authenticate, retry.
                _SESSION_CACHE.pop("hamqth", None)
                try:
                    _SESSION_CACHE["hamqth"] = await _hamqth_auth(username, password)
                    continue
                except Exception as exc:
                    entry.error = str(exc)
                    return entry
            entry.error = err_el.text.strip()
            return entry

        search_el = root.find(".//h:search", ns)
        if search_el is None:
            entry.error = "Callsign not found in HamQTH database"
            return entry

        # Helper to extract text from a child element, returning "" if absent.
        def _t(tag: str) -> str:
            el = search_el.find(f"h:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        # Prefer adr_name (full name); fall back to nick (handle/first name).
        entry.name       = _t("adr_name") or _t("nick")
        entry.qth        = _t("qth")
        entry.country    = _t("country")
        entry.grid       = _t("grid")
        entry.email      = _t("email")
        entry.web        = _t("web")
        entry.lotw       = _t("lotw").upper() == "Y"
        entry.eqsl       = _t("eqsl").upper() == "Y"
        entry.qsl_direct = _t("qsldirect").upper() == "Y"
        entry.qsl_bureau = _t("qsl").upper() == "Y"

        cq  = _t("cq");  entry.cq_zone  = int(cq)  if cq.isdigit()  else None
        itu = _t("itu"); entry.itu_zone = int(itu) if itu.isdigit() else None

        return entry

    entry.error = "Authentication failed after retry"
    return entry


# ---------------------------------------------------------------------------
# QRZ.com
# ---------------------------------------------------------------------------

async def _qrz_auth(username: str, password: str) -> str:
    """Authenticate with QRZ.com and return a session key string.

    Note: QRZ returns a session key even for free accounts, but callsign
    lookups will fail with a subscription-required error unless the account
    has a paid XML Data subscription.
    """
    url = (
        "https://xmldata.qrz.com/xml/current/"
        f"?username={urllib.parse.quote(username)}"
        f"&password={urllib.parse.quote(password)}"
        f"&agent=DXClusterSpots"
    )
    xml_text = await _fetch_url(url)
    root = ET.fromstring(xml_text)
    ns = {"q": "http://xmldata.qrz.com"}

    err_el = root.find(".//q:Error", ns)
    if err_el is not None and err_el.text:
        raise ValueError(f"QRZ: {err_el.text.strip()}")

    key_el = root.find(".//q:Key", ns)
    if key_el is None or not key_el.text:
        raise ValueError("QRZ: no session Key in auth response")
    return key_el.text.strip()


async def lookup_qrz(
    callsign: str,
    username: str,
    password: str,
) -> CallbookEntry:
    """Look up *callsign* via the QRZ.com XML API.

    Requires a paid QRZ.com XML Data subscription.  Free accounts receive a
    session key but lookups return a subscription-required error message which
    is surfaced in CallbookEntry.error.

    Args:
        callsign: The amateur callsign to look up.
        username: QRZ.com account username (callsign).
        password: QRZ.com account password.

    Returns:
        A CallbookEntry populated with QRZ data, or with .error set on failure.
    """
    entry = CallbookEntry(callsign=callsign.upper(), source="QRZ.com")

    if "qrz" not in _SESSION_CACHE:
        try:
            _SESSION_CACHE["qrz"] = await _qrz_auth(username, password)
        except Exception as exc:
            entry.error = str(exc)
            return entry

    for attempt in range(2):
        session_key = _SESSION_CACHE.get("qrz", "")
        url = (
            "https://xmldata.qrz.com/xml/current/"
            f"?s={urllib.parse.quote(session_key)}"
            f"&callsign={urllib.parse.quote(callsign.upper())}"
        )
        try:
            xml_text = await _fetch_url(url)
        except Exception as exc:
            entry.error = f"Network error: {exc}"
            return entry

        root = ET.fromstring(xml_text)
        ns = {"q": "http://xmldata.qrz.com"}

        err_el = root.find(".//q:Error", ns)
        if err_el is not None and err_el.text:
            if attempt == 0:
                _SESSION_CACHE.pop("qrz", None)
                try:
                    _SESSION_CACHE["qrz"] = await _qrz_auth(username, password)
                    continue
                except Exception as exc:
                    entry.error = str(exc)
                    return entry
            entry.error = err_el.text.strip()
            return entry

        call_el = root.find(".//q:Callsign", ns)
        if call_el is None:
            entry.error = "Callsign not found in QRZ database"
            return entry

        def _t(tag: str) -> str:
            el = call_el.find(f"q:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        fname = _t("fname")
        lname = _t("name")
        # QRZ stores first name in <fname> and surname in <name>.
        # <name_fmt> is the pre-formatted combined name when available.
        entry.name       = f"{fname} {lname}".strip() or _t("name_fmt")
        entry.qth        = _t("addr2") or _t("addr1")
        entry.country    = _t("country")
        entry.grid       = _t("grid")
        entry.email      = _t("email")
        entry.web        = _t("url")
        entry.lotw       = _t("lotw") == "1"
        entry.eqsl       = _t("eqsl") == "1"
        entry.qsl_bureau = _t("mqsl") == "1"

        cq  = _t("cqzone");  entry.cq_zone  = int(cq)  if cq.isdigit()  else None
        itu = _t("ituzone"); entry.itu_zone = int(itu) if itu.isdigit() else None

        return entry

    entry.error = "Authentication failed after retry"
    return entry
