"""Composable spot filters.

Filters are designed to chain naturally and to be passed as objects to
web service endpoints (e.g. query-string parameters map to filter methods).

Example::

    f = SpotFilter().band("20m", "40m").dx_callsign_prefix("VK", "ZL")
    spots = [s for s in raw_spots if f(s)]
"""

from typing import Callable

from .models import DXSpot

SpotPredicate = Callable[[DXSpot], bool]


class SpotFilter:
    """Chainable, callable filter for DXSpot objects.

    Each method adds a predicate and returns *self* for chaining.
    All predicates must pass for a spot to be accepted (logical AND).
    """

    def __init__(self) -> None:
        self._predicates: list[SpotPredicate] = []

    # ------------------------------------------------------------------
    # Filter builders
    # ------------------------------------------------------------------

    def band(self, *bands: str) -> "SpotFilter":
        """Accept only spots on the named band(s), e.g. "20m", "40m"."""
        band_set = {b.lower() for b in bands}
        self._predicates.append(
            lambda s: s.band is not None and s.band.lower() in band_set
        )
        return self

    def frequency_range(self, low_khz: float, high_khz: float) -> "SpotFilter":
        """Accept spots within a specific frequency range (kHz)."""
        self._predicates.append(lambda s: low_khz <= s.frequency <= high_khz)
        return self

    def dx_callsign_prefix(self, *prefixes: str) -> "SpotFilter":
        """Accept spots where the DX callsign starts with one of the prefixes."""
        upper = [p.upper() for p in prefixes]
        self._predicates.append(
            lambda s: any(s.dx_callsign.upper().startswith(p) for p in upper)
        )
        return self

    def spotter_prefix(self, *prefixes: str) -> "SpotFilter":
        """Accept spots where the spotter callsign starts with one of the prefixes."""
        upper = [p.upper() for p in prefixes]
        self._predicates.append(
            lambda s: any(s.spotter.upper().startswith(p) for p in upper)
        )
        return self

    def comment_contains(self, *keywords: str) -> "SpotFilter":
        """Accept spots whose comment contains at least one keyword (case-insensitive)."""
        lower = [k.lower() for k in keywords]
        self._predicates.append(
            lambda s: any(k in s.comment.lower() for k in lower)
        )
        return self

    def min_frequency(self, freq_khz: float) -> "SpotFilter":
        self._predicates.append(lambda s: s.frequency >= freq_khz)
        return self

    def max_frequency(self, freq_khz: float) -> "SpotFilter":
        self._predicates.append(lambda s: s.frequency <= freq_khz)
        return self

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def matches(self, spot: DXSpot) -> bool:
        """Return True if *spot* passes all predicates."""
        return all(p(spot) for p in self._predicates)

    def __call__(self, spot: DXSpot) -> bool:
        return self.matches(spot)

    def __repr__(self) -> str:
        return f"SpotFilter({len(self._predicates)} predicates)"
