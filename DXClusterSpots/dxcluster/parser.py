"""Parser for raw DXCluster telnet lines.

DXCluster nodes communicate over plain TCP (telnet) and send spot
announcements as a continuous stream of human-readable text lines.  This
module converts those raw ASCII lines into structured DXSpot objects.

The two public entry points are:
    parse_spot(line)  — main parser, returns DXSpot or None
    parse_mode(comment) — mode classifier used internally and by tests
"""

import re
from typing import Optional

from .bands import frequency_to_band
from .dxcc import cq_zone_for
from .models import DXSpot

# ---------------------------------------------------------------------------
# Spot line regex
# ---------------------------------------------------------------------------
# A canonical DXCluster spot line looks like:
#
#   DX de VK3IO:      14025.0  VK3TDX       CW 599 GD DX                0527Z
#   DX de EA5/G3SXW:  7013.0   G3BJ         CW                           1803Z
#
# The regex is anchored to the "DX de" token rather than the start of the
# line because cluster nodes often prepend ANSI colour escape codes, beep
# characters, or other decorations before the spot text.  Using .search()
# rather than .match() finds the token wherever it appears in the line.
#
# Capture group breakdown:
#   Group 1  (\S+?)      — spotter callsign (non-greedy, stops before ":")
#   Group 2  (\d+(?:\.\d+)?)  — frequency in kHz, integer or decimal
#   Group 3  (\S+)       — DX callsign (no spaces)
#   Group 4  (.*?)       — free-text comment (non-greedy, stops before time)
#   Group 5  (\d{4}Z)   — UTC time in HHMM + "Z" format, e.g. "1234Z"
#
# Why re.IGNORECASE?
#   Most clusters send "DX de" in mixed case, but some legacy nodes use
#   all-caps ("DX DE") or inconsistent capitalisation.  Case-insensitive
#   matching handles all variants without additional pre-processing.
#
# Why non-greedy (.*?) for the comment group?
#   If we used .* (greedy), the regex engine would consume the entire rest of
#   the line and then backtrack, potentially matching the time string inside
#   the comment (e.g. if the comment contains "QSY to 1415Z").  Non-greedy
#   stops as soon as it finds the first valid \d{4}Z sequence, which is
#   almost always the correct cluster time at the end of the line.
_SPOT_RE = re.compile(
    r"DX\s+de\s+"
    r"(\S+?):\s+"          # group 1: spotter callsign (up to but not including ":")
    r"(\d+(?:\.\d+)?)\s+"  # group 2: frequency kHz (integer or decimal)
    r"(\S+)\s*"            # group 3: DX callsign
    r"(.*?)\s*"            # group 4: comment (non-greedy — see note above)
    r"(\d{4}Z)",           # group 5: time e.g. "1234Z"
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Mode keyword table
# ---------------------------------------------------------------------------
# Maps the exact keyword that might appear in a spotter's comment to the
# canonical mode name used throughout the application.  Multiple keywords
# can map to the same canonical name (e.g. "USB", "LSB", and "SSB" all
# normalise to "SSB") so that filters only need to check one value.
#
# WHY a dict rather than a list of (keyword, canonical) pairs?
#   The dict makes it easy to see which keyword maps to which canonical name,
#   and lets us look up a canonical name by keyword in O(1) if needed.
#   The dict is then converted to a sorted list of compiled patterns below.
_MODE_MAP: dict[str, str] = {
    # --- Weak-signal digital (WSJT-X family) ---
    "FT8":    "FT8",    # Franke-Taylor 8-FSK; dominant HF digital mode
    "FT4":    "FT4",    # Faster variant of FT8 for contests
    "FST4":   "FST4",   # For 60/80/160m where propagation is poor
    "FST4W":  "FST4W",  # WSPR-like FST4 variant for propagation beacons
    "JT65":   "JT65",   # Original weak-signal mode; still used for EME
    "JT9":    "JT9",    # More efficient than JT65 on HF
    "JS8":    "JS8",    # JS8Call conversational mode based on FT8
    "MSK144": "MSK144", # Meteor-scatter mode (2 m band)
    "WSPR":   "WSPR",   # Propagation beacon mode; very narrow bandwidth

    # --- Traditional digital ---
    "PSK31":  "PSK",    # Phase-shift keying at 31 baud; normalised to PSK
    "PSK63":  "PSK",    # Faster PSK variant; normalised to PSK
    "PSK":    "PSK",    # Generic PSK keyword
    "RTTY":   "RTTY",   # Radio teletype; common in HF contests
    "DIGI":   "DIGI",   # Generic "digital" tag used when mode is unspecified
    "OPERA":  "OPERA",  # Very-low-power beacon mode
    "SSTV":   "SSTV",   # Slow-scan TV (image transmission)

    # --- Voice ---
    "SSB":    "SSB",    # Single-sideband (generic)
    "USB":    "SSB",    # Upper-sideband — normalised to SSB for filtering
    "LSB":    "SSB",    # Lower-sideband — normalised to SSB for filtering
    "AM":     "AM",     # Amplitude modulation; rare on HF DX
    "FM":     "FM",     # Frequency modulation; mainly VHF/UHF

    # --- CW ---
    "CW":     "CW",     # Morse code; still very active, especially in contests
}

# ---------------------------------------------------------------------------
# Pre-compiled mode patterns — sorted longest keyword first
# ---------------------------------------------------------------------------
# We compile the patterns once at module import time rather than inside
# parse_mode() to avoid re-compiling on every call.  Module-level compilation
# is a standard Python optimisation for regexes used in hot paths.
#
# WHY sort by descending keyword length?
#   This prevents a shorter keyword from matching inside a longer one before
#   the longer one gets a chance.  For example, "FST4W" must be checked before
#   "FST4" so that a comment containing "FST4W" is classified as FST4W rather
#   than FST4.  Similarly, "PSK31" should match before "PSK".
#
#   sorted(..., key=lambda x: -len(x[0])) produces descending length order.
#
# WHY word-boundary anchors (\b)?
#   Without \b, searching for "CW" in "WSPR" would match the "W" — no wait,
#   but searching for "AM" in "RTTYAM" or "FM" in "IFM" could produce false
#   positives.  \b ensures the keyword is surrounded by non-word characters
#   (spaces, punctuation, start/end of string), so "AM" only matches when
#   "AM" appears as a standalone token.
_MODE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + kw + r"\b"), mode)
    for kw, mode in sorted(_MODE_MAP.items(), key=lambda x: -len(x[0]))
]


def parse_mode(comment: str) -> Optional[str]:
    """Extract the operating mode from a spot comment string, or None.

    The function upper-cases the comment once and then runs each pre-compiled
    word-boundary pattern against it.  The first match wins and is returned
    immediately — this "first match" policy combined with the longest-first
    sort order (established when _MODE_PATTERNS is built) ensures that more
    specific keywords (e.g. "FST4W") are preferred over shorter substrings
    (e.g. "FST4") when both would technically match.

    WHY upper-case the comment once at the start?
        All patterns were compiled with upper-case keywords (from _MODE_MAP).
        Upper-casing the comment once at entry is cheaper than making every
        pattern case-insensitive (re.IGNORECASE adds overhead per pattern
        check) or compiling separate lower/mixed-case pattern variants.

    Args:
        comment: The free-text comment field from a DX spot (e.g. "ft8 -12db").

    Returns:
        A canonical mode string such as "FT8", "CW", "SSB", etc., or None
        if no recognisable mode keyword appears in the comment.
    """
    upper = comment.upper()
    for pattern, mode in _MODE_PATTERNS:
        if pattern.search(upper):
            # Return immediately on the first match.  Because patterns are
            # sorted longest-first, this is always the most specific match.
            return mode
    return None


def parse_spot(line: str) -> Optional[DXSpot]:
    """Parse a raw cluster telnet line and return a DXSpot, or None.

    This is the primary entry point for the parser.  It applies the spot
    regex, extracts and cleans each field, then constructs a DXSpot with
    both the raw parsed data and derived enrichments (band, mode, CQ zones).

    WHY return None rather than raising an exception on no-match?
        A DXCluster telnet stream contains many non-spot lines: login banners,
        "connected to" messages, WWV propagation bulletins, DX bulletins,
        operator announcements, and KEEP-ALIVE traffic.  None-on-no-match lets
        callers simply discard unrecognised lines with ``if spot := parse_spot(line)``
        rather than wrapping every call in try/except.

    Field cleaning rationale:
        spotter.rstrip(":")  — The regex captures the spotter field up to but
            NOT including the colon (the "?" in \S+? stops early).  However,
            some cluster nodes emit the callsign WITH the trailing colon
            still attached (e.g. when the telnet stream is slightly malformed).
            rstrip(":") defensively strips it in either case.

        dx_callsign.upper()  — Callsigns are case-insensitive by convention
            but the DXCC lookup and all filters use upper-case.  Normalising
            here means no other module needs to remember to upper-case.

        time_str.upper()  — The time field is typically already upper-case
            ("1234Z") but some nodes send it lower-case ("1234z").
            Normalising makes display and comparisons consistent.

        comment.strip()  — The non-greedy regex group may capture leading or
            trailing whitespace from variable-width columns in the cluster
            line.  strip() ensures the stored comment is clean.

        line.rstrip()  — Telnet lines end with CR+LF or just LF; rstrip()
            removes all trailing whitespace from the raw line so the stored
            ``raw`` field doesn't silently contain invisible characters.

    Args:
        line: A single line from the DXCluster telnet stream (with or without
              trailing newline / CR).

    Returns:
        A DXSpot instance if the line matches the spot pattern, else None.
    """
    m = _SPOT_RE.search(line)
    if not m:
        # Line is not a DX spot announcement — it's a banner, bulletin, or
        # other cluster message.  Return None so the caller can skip it.
        return None

    spotter, freq_str, dx_call, comment, time_str = m.groups()

    # Convert frequency string to float immediately.  The regex guarantees
    # the string is a valid decimal number, so float() cannot raise here.
    frequency = float(freq_str)

    comment = comment.strip()

    # Normalise callsigns to upper-case for consistent DXCC lookup and
    # filtering — amateur callsigns are case-insensitive by ITU convention.
    dx_callsign = dx_call.upper()
    spotter_call = spotter.rstrip(":")

    return DXSpot(
        spotter=spotter_call,
        frequency=frequency,
        dx_callsign=dx_callsign,
        comment=comment,
        time_str=time_str.upper(),
        # Enrich with band name — may be None for out-of-band spots.
        band=frequency_to_band(frequency),
        # Enrich with mode — may be None if comment contains no mode keyword.
        mode=parse_mode(comment),
        # Enrich with CQ zones for both DX station and spotter.  Each may be
        # None if the callsign prefix is not in the DXCC database (e.g. new
        # allocations, portable callsigns with unusual prefixes, or
        # experiment/training callsigns).
        zone=cq_zone_for(dx_callsign),
        spotter_zone=cq_zone_for(spotter_call),
        # Preserve the original line (sans trailing whitespace) for debugging.
        raw=line.rstrip(),
    )
