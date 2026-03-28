"""Rolling 24-hour spot log with frequency and callsign search.

Spots are appended to a plain NDJSON file (one JSON object per line).
On startup the file is read and trimmed to the last 24 hours in-place,
so the file never grows beyond one day of traffic.

Search methods return spots ordered chronologically (oldest first).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import List

from .models import DXSpot

logger = logging.getLogger(__name__)

_24H = timedelta(hours=24)


class SpotLog:
    """Append-only rolling log of DX spots covering the last 24 hours."""

    def __init__(self, log_file: str) -> None:
        self._path = log_file
        self._spots: list[DXSpot] = []
        self._load()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, spot: DXSpot) -> None:
        """Record a new spot in memory and append it to the log file."""
        self._spots.append(spot)
        self._write_line(spot.to_json())

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_frequency(
        self,
        freq_khz: float,
        window_khz: float = 5.0,
        hours: float = 24,
    ) -> List[DXSpot]:
        """Return all spots within *window_khz* of *freq_khz* in the last *hours*.

        Results are ordered chronologically (oldest first).
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return sorted(
            [
                s for s in self._spots
                if s.received_at >= cutoff
                and abs(s.frequency - freq_khz) <= window_khz
            ],
            key=lambda s: s.received_at,
        )

    def search_callsign(
        self,
        pattern: str,
        hours: float = 24,
    ) -> List[DXSpot]:
        """Return all spots where the DX callsign or spotter matches *pattern*.

        The match is case-insensitive substring search.
        Results are ordered chronologically (oldest first).
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        upper = pattern.upper()
        return sorted(
            [
                s for s in self._spots
                if s.received_at >= cutoff
                and (upper in s.dx_callsign.upper() or upper in s.spotter.upper())
            ],
            key=lambda s: s.received_at,
        )

    def size(self) -> int:
        """Return the number of spots currently in the log."""
        return len(self._spots)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cutoff(self) -> datetime:
        return datetime.utcnow() - _24H

    def _write_line(self, line: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:
            logger.warning("SpotLog write error: %s", exc)

    def _load(self) -> None:
        """Load the last 24 hours from the log file, then rewrite it trimmed."""
        if not os.path.exists(self._path):
            return
        cutoff = self._cutoff()
        kept: list[DXSpot] = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                        spot = DXSpot.from_dict(d)
                        if spot.received_at >= cutoff:
                            kept.append(spot)
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("SpotLog load error: %s", exc)
            return

        self._spots = sorted(kept, key=lambda s: s.received_at)

        # Rewrite the file with only the kept lines (trim old entries)
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                for spot in self._spots:
                    fh.write(spot.to_json() + "\n")
        except Exception as exc:
            logger.warning("SpotLog trim error: %s", exc)
