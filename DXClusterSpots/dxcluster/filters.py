"""Composable spot filters.

Filters chain naturally and are web-service friendly (query-string
parameters map directly to filter methods).

Example::

    f = (SpotFilter()
         .band("20m", "40m")
         .mode("CW", "FT8")
         .dx_exclude("G", "ON"))   # hide worked England + Belgium
    spots = [s for s in raw_spots if f(s)]
"""

from typing import Callable, Optional

from .models import DXSpot

SpotPredicate = Callable[[DXSpot], bool]


class SpotFilter:
    """Chainable, callable filter for DXSpot objects.

    All predicates must pass for a spot to be accepted (logical AND).
    """

    def __init__(self) -> None:
        self._predicates: list[SpotPredicate] = []

    # ------------------------------------------------------------------
    # Basic filters
    # ------------------------------------------------------------------

    def band(self, *bands: str) -> "SpotFilter":
        """Accept only spots on the named band(s), e.g. "20m", "40m"."""
        band_set = {b.lower() for b in bands}
        self._predicates.append(
            lambda s: s.band is not None and s.band.lower() in band_set
        )
        return self

    def mode(self, *modes: str) -> "SpotFilter":
        """Accept only spots matching the given mode(s), e.g. "CW", "FT8"."""
        mode_set = {m.upper() for m in modes}
        self._predicates.append(
            lambda s: s.mode is not None and s.mode.upper() in mode_set
        )
        return self

    def frequency_range(self, low_khz: float, high_khz: float) -> "SpotFilter":
        """Accept spots within a specific frequency range (kHz)."""
        self._predicates.append(lambda s: low_khz <= s.frequency <= high_khz)
        return self

    def min_frequency(self, freq_khz: float) -> "SpotFilter":
        self._predicates.append(lambda s: s.frequency >= freq_khz)
        return self

    def max_frequency(self, freq_khz: float) -> "SpotFilter":
        self._predicates.append(lambda s: s.frequency <= freq_khz)
        return self

    def comment_contains(self, *keywords: str) -> "SpotFilter":
        """Accept spots whose comment contains at least one keyword."""
        lower = [k.lower() for k in keywords]
        self._predicates.append(
            lambda s: any(k in s.comment.lower() for k in lower)
        )
        return self

    # ------------------------------------------------------------------
    # Simple callsign-prefix filters (raw string matching)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # DXCC entity-aware filters
    # ------------------------------------------------------------------

    def dx_include(self, *callsigns_or_prefixes: str) -> "SpotFilter":
        """Show ONLY DX spots from these DXCC entities (whitelist).

        Each argument is resolved to its full entity prefix set:
            .dx_include("G")   → accepts G, M, 2E callsigns (all England)
            .dx_include("ON")  → accepts ON, OO, OP, OQ, OR, OS, OT (Belgium)

        If you supply multiple arguments they are OR'd:
            .dx_include("G", "DL")  → England OR Germany
        """
        prefix_set = _expand_entity_prefixes(callsigns_or_prefixes)
        self._predicates.append(
            lambda s, ps=prefix_set: _match_any_prefix(s.dx_callsign, ps)
        )
        return self

    def dx_exclude(self, *callsigns_or_prefixes: str) -> "SpotFilter":
        """Hide DX spots from these DXCC entities (blacklist / worked list).

        Same entity expansion as dx_include:
            .dx_exclude("G")   → hides G, M, 2E callsigns
        """
        prefix_set = _expand_entity_prefixes(callsigns_or_prefixes)
        self._predicates.append(
            lambda s, ps=prefix_set: not _match_any_prefix(s.dx_callsign, ps)
        )
        return self

    def spotter_include(self, *callsigns_or_prefixes: str) -> "SpotFilter":
        """Accept only spots from spotters in these DXCC entities."""
        prefix_set = _expand_entity_prefixes(callsigns_or_prefixes)
        self._predicates.append(
            lambda s, ps=prefix_set: _match_any_prefix(s.spotter, ps)
        )
        return self

    def spotter_exclude(self, *callsigns_or_prefixes: str) -> "SpotFilter":
        """Reject spots from spotters in these DXCC entities."""
        prefix_set = _expand_entity_prefixes(callsigns_or_prefixes)
        self._predicates.append(
            lambda s, ps=prefix_set: not _match_any_prefix(s.spotter, ps)
        )
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expand_entity_prefixes(inputs: tuple[str, ...]) -> frozenset[str]:
    """Resolve each input to its full entity prefix set and union them."""
    from .dxcc import all_prefixes_for
    result: set[str] = set()
    for item in inputs:
        result.update(all_prefixes_for(item))
    return frozenset(result)


def _match_any_prefix(callsign: str, prefix_set: frozenset[str]) -> bool:
    """Return True if the callsign's extracted prefix is in *prefix_set*."""
    from .dxcc import callsign_prefix
    pfx = callsign_prefix(callsign)
    return pfx in prefix_set


def build_filter_from_config(cfg_filters) -> Optional[SpotFilter]:
    """Build a SpotFilter from a FilterConfig object, or None if no filters."""
    f = cfg_filters
    has_filter = any([f.bands, f.modes, f.include_prefixes, f.exclude_prefixes])
    if not has_filter:
        return None
    sf = SpotFilter()
    if f.bands:
        sf.band(*f.bands)
    if f.modes:
        sf.mode(*f.modes)
    if f.include_prefixes:
        sf.dx_include(*f.include_prefixes)
    if f.exclude_prefixes:
        sf.dx_exclude(*f.exclude_prefixes)
    return sf
