"""Core data model for a DX spot.

A DX spot is the fundamental unit of information on a DXCluster network: one
amateur radio operator ("spotter") reports hearing another ("DX station") on a
particular frequency.  This module defines the dataclass that represents that
single observation throughout the application.

Why a dataclass rather than a plain dict or namedtuple?
    - Dataclasses give us typed fields, default values, and auto-generated
      __init__/__repr__/__eq__ for free.
    - Unlike namedtuple, dataclass instances are mutable, so enrichment fields
      (band, mode, zone) can be filled in after initial construction if needed.
    - Unlike a plain dict, fields are discoverable by IDE tools and static
      type checkers, which catches attribute-name typos at analysis time
      rather than at runtime.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class DXSpot:
    """A single DX spot as received from a DXCluster node.

    Fields are divided into two groups:

    *Raw / parsed fields* — extracted directly from the telnet line:
        spotter, frequency, dx_callsign, comment, time_str, raw

    *Derived / enriched fields* — computed after parsing:
        band, mode, zone, spotter_zone, received_at

    The separation matters because raw fields are guaranteed to be present
    whenever a spot is successfully parsed, while derived fields may be None
    if enrichment data (band plan, DXCC database) does not cover the value.

    The dataclass is intentionally mutable so that enrichment can be applied
    after construction (e.g. zone lookup can be deferred or updated).
    """

    # ------------------------------------------------------------------
    # Raw fields — populated directly by the parser
    # ------------------------------------------------------------------

    spotter: str
    # Callsign of the station that heard and reported the DX.  May include
    # a portable suffix (e.g. "EA5/G3SXW") exactly as received.

    frequency: float
    # Spot frequency in kHz.  Stored as a float because DXCluster lines can
    # carry decimal kHz values (e.g. "14025.5"), and float arithmetic is fast
    # enough for all the range comparisons we do.

    dx_callsign: str
    # Callsign of the station being spotted, normalised to upper-case.

    comment: str
    # Free-text comment appended by the spotter, e.g. "CW 599 GD DX".
    # This is the primary source for mode detection (see parser.parse_mode).

    time_str: str
    # UTC time string exactly as it appears in the cluster line, e.g. "1234Z".
    # We deliberately do *not* parse this into a datetime because the cluster
    # omits the date — we cannot reliably reconstruct a full timestamp from
    # it alone, so received_at (below) is used for all time-based operations.

    # ------------------------------------------------------------------
    # Derived / enriched fields — may be None
    # ------------------------------------------------------------------

    band: Optional[str] = None
    # Human-readable band label derived from frequency via the BAND_PLAN,
    # e.g. "20m".  None if the frequency falls outside known allocations.

    mode: Optional[str] = None
    # Canonical operating mode extracted from the comment field,
    # e.g. "CW", "FT8", "SSB".  None if no recognisable mode keyword found.

    zone: Optional[int] = None
    # CQ zone of the DX station, looked up from the DXCC database via the
    # callsign prefix.  None if the prefix is unrecognised.

    spotter_zone: Optional[int] = None
    # CQ zone of the spotter, by the same lookup mechanism.  Useful for
    # "show me spots from my region" filters.

    raw: str = ""
    # The original unparsed telnet line, stripped of trailing whitespace.
    # Retained so that diagnostics and bug reports can always quote the
    # exact input that was processed.

    received_at: datetime = field(default_factory=datetime.utcnow)
    # Wall-clock UTC timestamp at which *this process* received the spot.
    #
    # WHY default_factory rather than a plain default?
    #   If we wrote ``received_at: datetime = datetime.utcnow()`` Python would
    #   evaluate utcnow() exactly ONCE at class-definition time, and every
    #   instance would share that same frozen datetime object.  Using
    #   default_factory=datetime.utcnow means utcnow() is called fresh for
    #   each new DXSpot instance, so each spot gets an independent timestamp.
    #   This is a subtle but critical distinction — the bug it prevents is
    #   particularly insidious because it only manifests when spots are created
    #   after a long-running process has been up for hours.
    #
    # WHY use received_at instead of time_str for time-window operations?
    #   time_str carries only HHMM (e.g. "1423Z") with no date component.
    #   There is no safe way to infer the date: if the application runs across
    #   midnight the same HHMM value could refer to two different calendar days.
    #   received_at is a full datetime that avoids this ambiguity entirely.

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict (suitable for REST API responses).

        The ``raw`` field is intentionally excluded from the output: it is a
        debugging aid and can contain unpredictable whitespace/control chars
        that would bloat responses and complicate client parsing.

        ``received_at`` is serialised as an ISO 8601 string so that it round-
        trips cleanly through JSON without a custom encoder/decoder.
        """
        return {
            "spotter":      self.spotter,
            "frequency":    self.frequency,
            "dx_callsign":  self.dx_callsign,
            "comment":      self.comment,
            "time_str":     self.time_str,
            "band":         self.band,
            "mode":         self.mode,
            "zone":         self.zone,
            "spotter_zone": self.spotter_zone,
            # isoformat() produces a standard, unambiguous string like
            # "2024-06-01T14:23:00" that sorts lexicographically by time —
            # a useful property when the NDJSON log is processed by external
            # tools (grep, jq, awk) without custom date-aware comparators.
            "received_at":  self.received_at.isoformat(),
        }

    def to_json(self) -> str:
        """Return the spot as a compact JSON string (one line, NDJSON-compatible).

        NDJSON (Newline-Delimited JSON) stores one JSON object per line with
        no trailing comma, making it trivial to append new records and to
        stream-parse a file line by line without loading the whole file into
        memory.  SpotLog relies on this format.

        The compact form (no indentation, no extra spaces between keys/values)
        is intentional: adding indentation would spread one spot across
        multiple lines and break the "one record per line" contract that makes
        NDJSON easy to tail, grep, and stream-parse.
        """
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "DXSpot":
        """Reconstruct a DXSpot from a dict (e.g. loaded from the log file).

        This is the inverse of to_dict().  It is deliberately lenient:
        - Missing keys fall back to empty strings / 0.0 / None rather than
          raising KeyError, so log files written by older versions of the
          code (which may lack new fields) can still be read.
        - An unparseable received_at string falls back to utcnow() rather
          than propagating a ValueError, because a spot with an imprecise
          timestamp is still useful; it is better to surface it than to drop
          it entirely.

        WHY d.get() everywhere instead of d[key]?
            dict.get(key, default) never raises KeyError.  This forward- and
            backward-compatibility guarantee is essential for a log file that
            may span application versions: if a new field is added in v2, v1
            log entries simply won't have it and from_dict() will silently use
            the default rather than crashing on startup.

        Args:
            d: A plain dict, typically from json.loads() on a log line.

        Returns:
            A fully constructed DXSpot instance.
        """
        raw_ts = d.get("received_at", "")
        try:
            # datetime.fromisoformat handles the output of isoformat() reliably.
            # We guard against an empty string first because fromisoformat("")
            # raises ValueError in Python < 3.11.
            received_at = datetime.fromisoformat(raw_ts) if raw_ts else datetime.utcnow()
        except ValueError:
            # The timestamp field existed but could not be parsed — use now as
            # a safe fallback rather than crashing or discarding the whole spot.
            # A spot with a slightly wrong timestamp is far more useful than
            # a spot that was silently dropped from the log.
            received_at = datetime.utcnow()
        return cls(
            spotter=d.get("spotter", ""),
            # Explicit float() cast: JSON numbers without a decimal point are
            # parsed as int by json.loads (e.g. 14025 → int(14025)).  The rest
            # of the code always works with float frequencies, so we normalise
            # here rather than adding float() calls at every downstream usage.
            frequency=float(d.get("frequency", 0.0)),
            dx_callsign=d.get("dx_callsign", ""),
            comment=d.get("comment", ""),
            time_str=d.get("time_str", ""),
            # Optional fields: dict.get() returns None when the key is absent,
            # which is exactly what we want for these Optional fields — no
            # explicit default needed.
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
        """Return a fixed-width, human-readable one-line summary of the spot.

        The layout mimics the traditional DXCluster telnet display format so
        that users familiar with cluster software immediately recognise it.
        Column widths are chosen so that a typical terminal (80+ chars) shows
        all fields without wrapping.

        Example output:
            [20m]  CW     DX de VK3IO       14025.0 kHz  VK3TDX       CW 599 GD DX          0527Z Z29
        """
        # Build optional tag strings first so the f-string alignment below
        # works correctly even when fields are absent.

        # Band tag: always shown, but uses "[?]  " (with trailing spaces) when
        # the band is unknown so the column width stays constant.
        # The trailing spaces in "[?]  " ensure that when the :<7 format
        # specifier is applied, the field still aligns with "[20m]  " (5 chars
        # + 2 spaces of padding) rather than collapsing to 3 chars.
        band_tag = f"[{self.band}]" if self.band else "[?]  "

        # Mode and zone tags are empty strings when absent; the format spec
        # on the f-string handles alignment for both the present and absent case.
        mode_tag = f" {self.mode}" if self.mode else ""
        zone_tag = f" Z{self.zone}" if self.zone else ""

        return (
            f"{band_tag:<7}"        # 7 chars: "[160m] " or "[20m]  "
            f"{mode_tag:<6} "       # 6 chars for mode + 1 space separator
            f"DX de {self.spotter:<12} "   # spotter callsign, left-aligned in 12 chars
            f"{self.frequency:>9.1f} kHz  " # frequency right-aligned with one decimal
            f"{self.dx_callsign:<12} "      # DX callsign, left-aligned in 12 chars
            f"{self.comment:<33} "          # comment truncated/padded to 33 chars
            f"{self.time_str}{zone_tag}"    # time always last; zone appended if known
        )
