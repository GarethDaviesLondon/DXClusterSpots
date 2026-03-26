"""Parser for raw DXCluster telnet lines."""

import re
from typing import Optional

from .bands import frequency_to_band
from .models import DXSpot

# Standard DX spot line format:
#   DX de VK3IO:      14025.0  VK3TDX       CW 599 GD DX                0527Z
#   DX de EA5/G3SXW:  7013.0   G3BJ         CW                           1803Z
_SPOT_RE = re.compile(
    r"DX\s+de\s+"
    r"(\S+?):\s+"          # group 1: spotter callsign (strip trailing colon later)
    r"(\d+(?:\.\d+)?)\s+"  # group 2: frequency kHz
    r"(\S+)\s*"            # group 3: DX callsign
    r"(.*?)\s*"            # group 4: comment (non-greedy)
    r"(\d{4}Z)",           # group 5: time e.g. 1234Z
    re.IGNORECASE,
)


def parse_spot(line: str) -> Optional[DXSpot]:
    """Parse a raw cluster line and return a DXSpot, or None if not a spot."""
    m = _SPOT_RE.search(line)
    if not m:
        return None

    spotter, freq_str, dx_call, comment, time_str = m.groups()
    frequency = float(freq_str)

    return DXSpot(
        spotter=spotter.rstrip(":"),
        frequency=frequency,
        dx_callsign=dx_call.upper(),
        comment=comment.strip(),
        time_str=time_str.upper(),
        band=frequency_to_band(frequency),
        raw=line.rstrip(),
    )
