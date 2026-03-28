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
    band: Optional[str] = None         # derived band name, e.g. "20m"
    mode: Optional[str] = None         # derived operating mode, e.g. "CW", "FT8"
    zone: Optional[int] = None         # CQ zone of the DX station
    spotter_zone: Optional[int] = None # CQ zone of the spotter
    raw: str = ""                      # original unparsed line
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
            "mode": self.mode,
            "zone": self.zone,
            "spotter_zone": self.spotter_zone,
            "received_at": self.received_at.isoformat(),
        }

    def to_json(self) -> str:
        """Return the spot as a JSON string (one per line, NDJSON compatible)."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "DXSpot":
        """Reconstruct a DXSpot from a dict (e.g. loaded from the log file)."""
        raw_ts = d.get("received_at", "")
        try:
            received_at = datetime.fromisoformat(raw_ts) if raw_ts else datetime.utcnow()
        except ValueError:
            received_at = datetime.utcnow()
        return cls(
            spotter=d.get("spotter", ""),
            frequency=float(d.get("frequency", 0.0)),
            dx_callsign=d.get("dx_callsign", ""),
            comment=d.get("comment", ""),
            time_str=d.get("time_str", ""),
            band=d.get("band"),
            mode=d.get("mode"),
            zone=d.get("zone"),
            spotter_zone=d.get("spotter_zone"),
            raw=d.get("raw", ""),
            received_at=received_at,
        )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        band_tag = f"[{self.band}]" if self.band else "[?]  "
        mode_tag = f" {self.mode}" if self.mode else ""
        zone_tag = f" Z{self.zone}" if self.zone else ""
        return (
            f"{band_tag:<7}"
            f"{mode_tag:<6} "
            f"DX de {self.spotter:<12} "
            f"{self.frequency:>9.1f} kHz  "
            f"{self.dx_callsign:<12} "
            f"{self.comment:<33} "
            f"{self.time_str}{zone_tag}"
        )
