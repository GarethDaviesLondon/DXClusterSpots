"""Core data model for a DX spot."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DXSpot:
    """A single DX spot as received from a DXCluster node.

    Designed to be serialisable to JSON so it can be passed directly
    to web service responses without transformation.
    """

    spotter: str          # callsign of the reporting station
    frequency: float      # spot frequency in kHz
    dx_callsign: str      # callsign of the spotted station
    comment: str          # free-text comment from the spotter
    time_str: str         # UTC time string from the cluster, e.g. "1234Z"
    band: Optional[str] = None   # derived band name, e.g. "20m"
    raw: str = ""                # original unparsed line
    received_at: datetime = field(default_factory=datetime.utcnow)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict (suitable for REST API responses)."""
        return {
            "spotter": self.spotter,
            "frequency": self.frequency,
            "dx_callsign": self.dx_callsign,
            "comment": self.comment,
            "time_str": self.time_str,
            "band": self.band,
            "received_at": self.received_at.isoformat(),
        }

    def to_json(self) -> str:
        """Return the spot as a JSON string (one per line, NDJSON compatible)."""
        return json.dumps(self.to_dict())

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        band_tag = f"[{self.band}]" if self.band else "[?]  "
        return (
            f"{band_tag:<7} "
            f"DX de {self.spotter:<12} "
            f"{self.frequency:>9.1f} kHz  "
            f"{self.dx_callsign:<12} "
            f"{self.comment:<33} "
            f"{self.time_str}"
        )
