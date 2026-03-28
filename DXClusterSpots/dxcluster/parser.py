"""Parser for raw DXCluster telnet lines."""

import re
from typing import Optional

from .bands import frequency_to_band
from .dxcc import cq_zone_for
from .models import DXSpot

# Standard DX spot line format:
#   DX de VK3IO:      14025.0  VK3TDX       CW 599 GD DX                0527Z
#   DX de EA5/G3SXW:  7013.0   G3BJ         CW                           1803Z
_SPOT_RE = re.compile(
    r"DX\s+de\s+"
    r"(\S+?):\s+"          # group 1: spotter callsign
    r"(\d+(?:\.\d+)?)\s+"  # group 2: frequency kHz
    r"(\S+)\s*"            # group 3: DX callsign
    r"(.*?)\s*"            # group 4: comment (non-greedy)
    r"(\d{4}Z)",           # group 5: time e.g. 1234Z
    re.IGNORECASE,
)

# Ordered from most specific to least so FT8 matches before F, etc.
# Maps keyword (upper-case) → canonical mode name
_MODE_MAP: dict[str, str] = {
    "FT8":    "FT8",
    "FT4":    "FT4",
    "FST4":   "FST4",
    "FST4W":  "FST4W",
    "JT65":   "JT65",
    "JT9":    "JT9",
    "JS8":    "JS8",
    "MSK144": "MSK144",
    "WSPR":   "WSPR",
    "PSK31":  "PSK",
    "PSK63":  "PSK",
    "PSK":    "PSK",
    "RTTY":   "RTTY",
    "DIGI":   "DIGI",
    "OPERA":  "OPERA",
    "SSTV":   "SSTV",
    "SSB":    "SSB",
    "USB":    "SSB",
    "LSB":    "SSB",
    "AM":     "AM",
    "FM":     "FM",
    "CW":     "CW",
}

# Pre-compiled word-boundary patterns ordered longest keyword first
_MODE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + kw + r"\b"), mode)
    for kw, mode in sorted(_MODE_MAP.items(), key=lambda x: -len(x[0]))
]


def parse_mode(comment: str) -> Optional[str]:
    """Extract the operating mode from a spot comment, or None."""
    upper = comment.upper()
    for pattern, mode in _MODE_PATTERNS:
        if pattern.search(upper):
            return mode
    return None


def parse_spot(line: str) -> Optional[DXSpot]:
    """Parse a raw cluster line and return a DXSpot, or None if not a spot."""
    m = _SPOT_RE.search(line)
    if not m:
        return None

    spotter, freq_str, dx_call, comment, time_str = m.groups()
    frequency = float(freq_str)
    comment = comment.strip()

    dx_callsign = dx_call.upper()
    spotter_call = spotter.rstrip(":")
    return DXSpot(
        spotter=spotter_call,
        frequency=frequency,
        dx_callsign=dx_callsign,
        comment=comment,
        time_str=time_str.upper(),
        band=frequency_to_band(frequency),
        mode=parse_mode(comment),
        zone=cq_zone_for(dx_callsign),
        spotter_zone=cq_zone_for(spotter_call),
        raw=line.rstrip(),
    )
