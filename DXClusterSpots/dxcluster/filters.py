"""Composable spot filters.

Filters chain naturally and are web-service friendly (query-string
parameters map directly to filter methods).

Example::

    f = (SpotFilter()
         .band("20m", "40m")
         .mode("CW", "FT8")
         .dx_exclude("G", "ON"))   # hide worked England + Belgium
    spots = [s for s in raw_spots if f(s)]

Design overview — the predicate-chaining pattern
-------------------------------------------------
SpotFilter works by accumulating a list of *predicate functions* — callables
that accept a DXSpot and return bool.  Each builder method (band(), mode(),
cq_zone(), ...) creates one such predicate and appends it to the internal
list, then returns ``self`` so calls can be chained fluently.

At evaluation time, matches() runs ``all(p(spot) for p in self._predicates)``.
Using all() gives us short-circuit evaluation for free: if the first predicate
rejects a spot, Python does not call the remaining predicates at all.  This
is significant for expensive predicates like the DXCC entity lookups.

The logical composition is always AND (all predicates must pass).  OR
semantics across different filter dimensions don't make sense for the typical
use case ("I want spots on 20m OR CW" would flood the display).  OR within a
single dimension — e.g. "20m OR 40m" — is handled by passing multiple
arguments to a single builder method (band("20m", "40m") checks membership
in a set, which is inherently OR for that one predicate).
"""

from typing import Callable, Optional

from .models import DXSpot

# A SpotPredicate is any callable that takes a DXSpot and returns a bool.
# Naming this type alias makes method signatures self-documenting and lets
# static type checkers (mypy, pyright) verify that we're appending the
# right kind of callable to _predicates.
SpotPredicate = Callable[[DXSpot], bool]


class SpotFilter:
    """Chainable, callable filter for DXSpot objects.

    All predicates must pass for a spot to be accepted (logical AND).
    """

    def __init__(self) -> None:
        # Start with an empty predicate list.  An empty SpotFilter accepts
        # every spot (all() on an empty iterable returns True), which is the
        # correct "no filter applied" default — it lets callers always use
        # a SpotFilter instance without special-casing the "no filter" case.
        self._predicates: list[SpotPredicate] = []

    # ------------------------------------------------------------------
    # Basic filters
    # ------------------------------------------------------------------

    def band(self, *bands: str) -> "SpotFilter":
        """Accept only spots on the named band(s), e.g. "20m", "40m"."""
        # Convert to a set for O(1) membership testing.  A list scan would
        # be O(n) per spot — negligible for 2–3 bands but the set is
        # idiomatic and marginally faster at any scale.
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

    def cq_zone(self, *zones: int) -> "SpotFilter":
        """Accept only spots where the DX station is in one of the given CQ zones.

        An empty zone list rejects every spot (all-closed state).

        WHY capture zone_set with a default argument (zs=zone_set)?
            See the detailed explanation under _LAMBDA_CAPTURE_NOTE below.
            Short version: Python closures capture variables by reference,
            not by value.  By the time the lambda is called, the local
            variable ``zone_set`` in this method frame no longer exists.
            The default-argument trick binds the current value of zone_set
            into the lambda's own default parameter at definition time,
            giving us value semantics.
        """
        zone_set = set(zones)
        self._predicates.append(
            lambda s, zs=zone_set: s.zone is not None and s.zone in zs
        )
        return self

    def spotter_cq_zone(self, *zones: int) -> "SpotFilter":
        """Accept only spots filed by a spotter in one of the given CQ zones.

        An empty zone list rejects every spot (all-closed state).
        """
        zone_set = set(zones)
        self._predicates.append(
            lambda s, zs=zone_set: s.spotter_zone is not None and s.spotter_zone in zs
        )
        return self

    def frequency_range(self, low_khz: float, high_khz: float) -> "SpotFilter":
        """Accept spots within a specific frequency range (kHz).

        Note: low_khz and high_khz are captured by closure here rather than
        via the default-argument trick.  That is safe because they are
        function *parameters* (immutable floats), not a mutable local variable
        that could be rebound before the lambda runs.  The closure captures the
        parameter cell, which is stable for the lifetime of the lambda.
        """
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
        # Pre-compute lower-cased keywords so we don't re-lowercase on every spot.
        lower = [k.lower() for k in keywords]
        self._predicates.append(
            lambda s: any(k in s.comment.lower() for k in lower)
        )
        return self

    # ------------------------------------------------------------------
    # Simple callsign-prefix filters (raw string matching)
    # ------------------------------------------------------------------

    def dx_callsign_prefix(self, *prefixes: str) -> "SpotFilter":
        """Accept spots where the DX callsign starts with one of the prefixes.

        This is a simple string startswith() check — it does NOT perform DXCC
        entity expansion.  Use dx_include() / dx_exclude() when you want
        all callsigns belonging to a given DXCC entity (which may have multiple
        prefixes, e.g. England has G, M, 2E, etc.).
        """
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
    #
    # These four methods differ from the simple prefix methods above in one
    # crucial way: they expand a human-friendly entity identifier (e.g. "G"
    # for England) to the *full set* of callsign prefixes that belong to that
    # entity (G, M, 2E, 2U, ...) using the DXCC database.
    #
    # WHY is DXCC expansion deferred to method-call time rather than import time?
    #
    #   The _expand_entity_prefixes() helper imports from .dxcc *inside* the
    #   function body rather than at module level.  This defers the (relatively
    #   expensive) DXCC table loading until the first time a DXCC-aware filter
    #   is actually constructed.  If a caller only ever uses band/mode filters,
    #   the DXCC module is never loaded.  It also avoids circular import issues
    #   that can arise when the import graph is loaded during package
    #   initialisation.
    #
    #   The frozenset returned by _expand_entity_prefixes() is then captured
    #   by value in the lambda via the default-argument trick (ps=prefix_set).
    #   This means the DXCC lookup happens ONCE per filter construction and
    #   the resulting frozenset is reused for every spot evaluation — not once
    #   per spot.

    def dx_include(self, *callsigns_or_prefixes: str) -> "SpotFilter":
        """Show ONLY DX spots from these DXCC entities (whitelist).

        Each argument is resolved to its full entity prefix set:
            .dx_include("G")   → accepts G, M, 2E callsigns (all England)
            .dx_include("ON")  → accepts ON, OO, OP, OQ, OR, OS, OT (Belgium)

        If you supply multiple arguments they are OR'd:
            .dx_include("G", "DL")  → England OR Germany
        """
        # Expand entity identifiers to a frozen prefix set.  This happens once
        # at filter-construction time (not once per spot) and is captured by
        # value in the lambda via the ps=prefix_set default-argument trick.
        prefix_set = _expand_entity_prefixes(callsigns_or_prefixes)
        self._predicates.append(
            # ps=prefix_set — the default-argument trick: the current value of
            # prefix_set is bound as a default parameter of the lambda at the
            # moment this line executes.  Later rebinding of prefix_set (or the
            # method returning and its frame being destroyed) does not affect ps.
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
        """Return True if *spot* passes all predicates.

        Uses all() with a generator expression for two reasons:
          1. Short-circuit evaluation: all() stops calling predicates as soon
             as one returns False.  Expensive predicates (e.g. DXCC lookups
             inside _match_any_prefix) are skipped once a cheaper predicate
             has already rejected the spot.
          2. Memory efficiency: the generator expression does not build an
             intermediate list of boolean results; it yields one at a time.

        An empty predicate list (no filters applied) returns True for every
        spot because all() on an empty iterable is True by definition —
        "all zero constraints are satisfied."
        """
        return all(p(spot) for p in self._predicates)

    def __call__(self, spot: DXSpot) -> bool:
        # Making SpotFilter callable (via __call__) lets it be used anywhere
        # a plain function is expected — e.g. as the key argument to filter()
        # or as a predicate in a list comprehension — without the caller needing
        # to know it's an object rather than a function.
        return self.matches(spot)

    def __repr__(self) -> str:
        return f"SpotFilter({len(self._predicates)} predicates)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# _LAMBDA_CAPTURE_NOTE — why the default-argument trick is needed
# ---------------------------------------------------------------
# Python lambdas close over the *variable name* in their enclosing scope, not
# the *value* the variable held at lambda-creation time.  Consider:
#
#   predicates = []
#   for zone in [14, 25]:
#       zone_set = {zone}
#       predicates.append(lambda s: s.zone in zone_set)  # BUG: all lambdas
#                                                          # share the last zone_set
#
# By the time any lambda is called, zone_set refers to the final value from
# the loop ({25}).  Every predicate would check against {25}, not the intended
# {14} or {25}.
#
# The fix is the default-argument trick:
#
#   predicates.append(lambda s, zs=zone_set: s.zone in zs)
#
# Python evaluates default arguments at the moment the lambda (or def) is
# defined, binding the *current value* of zone_set into the parameter zs.
# Subsequent rebinding of zone_set does not affect zs.  This gives us
# genuine value-capture semantics.
#
# In SpotFilter the issue is less obvious because each builder method has its
# own stack frame, so zone_set / prefix_set cannot be rebound from outside.
# However, the default-argument trick is still used for the set-typed locals
# (zone_set, prefix_set) as defensive programming and as a self-documenting
# signal that the value is intentionally frozen at definition time.
# Float parameters (low_khz, high_khz, freq_khz) are immutable scalars passed
# directly as function arguments, so their closure capture is already safe.

def _expand_entity_prefixes(inputs: tuple[str, ...]) -> frozenset[str]:
    """Resolve each input to its full entity prefix set and union them.

    WHY the deferred import of .dxcc here rather than at module top level?
        1. Lazy loading: the DXCC table is only read from disk when a
           DXCC-aware filter is first constructed.  Callers that only use
           band/mode filters pay no I/O cost.
        2. Circular import safety: dxcc may import from filters in some
           configurations.  Placing the import inside the function body
           defers it until after all module-level code in both modules has
           finished executing, breaking the cycle.

    Returns a frozenset (immutable) rather than a set because it will be
    captured in a lambda.  frozenset signals to the reader that this
    collection is fixed at construction time and will not be mutated.
    """
    from .dxcc import all_prefixes_for
    result: set[str] = set()
    for item in inputs:
        # all_prefixes_for() returns every callsign prefix associated with
        # the same DXCC entity as ``item`` — e.g. passing "G" returns
        # {"G", "M", "2E", "2U", ...} for England.
        result.update(all_prefixes_for(item))
    return frozenset(result)


def _match_any_prefix(callsign: str, prefix_set: frozenset[str]) -> bool:
    """Return True if the callsign's extracted prefix is in *prefix_set*.

    WHY extract a prefix rather than testing startswith()?
        Callsigns can be portable (EA5/G3SXW) or have numeric suffixes
        (G3SXW/P, G3SXW/QRP).  callsign_prefix() strips those to the base
        prefix (G3) before lookup, so the DXCC database match is accurate
        even for portable operations.

    WHY another deferred import of .dxcc?
        Same reason as _expand_entity_prefixes: lazy loading and circular
        import safety.  callsign_prefix() is a pure function with no I/O,
        so the import overhead is negligible once the module is cached.
    """
    from .dxcc import callsign_prefix
    pfx = callsign_prefix(callsign)
    return pfx in prefix_set


def build_filter_from_config(cfg_filters) -> Optional[SpotFilter]:
    """Build a SpotFilter from a FilterConfig object, or None if no filters.

    Returns None rather than an empty SpotFilter when no filters are configured
    so that calling code can detect the "no filter" case cheaply — checking
    ``if spot_filter`` is O(1), whereas calling an empty SpotFilter on every
    spot would still iterate through all() on an empty list (O(1) but with
    function-call overhead).

    The any([...]) check uses a list (not a generator) intentionally: we want
    all conditions to be evaluated eagerly for the truthiness check, not
    short-circuit — though in practice either would work since none of the
    conditions have side effects.
    """
    f = cfg_filters
    # Check whether any filter dimension has been configured.  If none have,
    # there is no point building a SpotFilter object at all.
    has_filter = any([
        f.bands,
        f.modes,
        f.include_prefixes,
        f.exclude_prefixes,
        f.cq_zones is not None,         # cq_zones=[] (empty list) is still a filter
        f.spotter_cq_zones is not None, # that rejects all spots — treat as "set"
    ])
    if not has_filter:
        return None

    sf = SpotFilter()

    # Apply each configured filter dimension.  The "if f.x" guards skip empty
    # lists (falsy) so we don't append useless always-reject predicates.
    # Note: cq_zones and spotter_cq_zones use "is not None" rather than a
    # truthiness check because an *empty* list is a valid (if unusual) config
    # that means "reject all zones" — we want to honour that intent.
    if f.bands:
        sf.band(*f.bands)
    if f.modes:
        sf.mode(*f.modes)
    if f.include_prefixes:
        sf.dx_include(*f.include_prefixes)
    if f.exclude_prefixes:
        sf.dx_exclude(*f.exclude_prefixes)
    if f.cq_zones is not None:
        sf.cq_zone(*f.cq_zones)
    if f.spotter_cq_zones is not None:
        sf.spotter_cq_zone(*f.spotter_cq_zones)

    return sf
