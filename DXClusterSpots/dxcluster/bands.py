"""Ham radio band plan with frequency-to-band conversion utilities.

This module is the single source of truth for frequency-to-band mappings used
throughout the application.  Every other module that needs to classify a spot
by band imports from here, so any future change to the band plan (e.g. adding
a new allocation or correcting an edge frequency) only needs to happen in one
place.

Design note — why a module-level constant rather than a class?
    The band plan is immutable data.  Wrapping it in a class would add
    boilerplate (instantiation, self references) without any benefit.  A plain
    module-level dict is the simplest, most readable representation.
"""

from typing import Optional

# ---------------------------------------------------------------------------
# ITU Region 1 band plan — all frequencies in kHz
# ---------------------------------------------------------------------------
#
# WHY Region 1?
#   Most DX cluster nodes and operators are based in Europe, which falls under
#   ITU Region 1.  Regions 2 (Americas) and 3 (Asia-Pacific) have slightly
#   different allocations at certain band edges, but the differences are
#   narrow enough that mis-classification is rare in practice.  Using the
#   widest common allocation across regions is a pragmatic compromise.
#
# WHY kilohertz (kHz) rather than megahertz (MHz)?
#   DXCluster telnet lines report frequency in kHz (e.g. "14225.0").  Storing
#   the plan in the same unit avoids a multiply-by-1000 / divide-by-1000
#   conversion at every parse and lookup, keeping the hot path minimal.
#
# DATA STRUCTURE — dict[str, tuple[float, float]]:
#   Key   : human-readable band name used throughout the UI (e.g. "20m")
#   Value : (lower_edge_kHz, upper_edge_kHz) — both bounds are INCLUSIVE so
#           that a spot sitting exactly on a band edge is still matched.
#
# WHY a plain dict rather than a sorted structure?
#   The plan has only ~15 entries.  A linear scan is O(n) but n is so small
#   that it is faster in practice than the overhead of a bisect or interval
#   tree.  Clarity and simplicity matter more at this scale.
#
# ORDER:
#   Entries are ordered from lowest to highest frequency for human
#   readability.  Python 3.7+ preserves insertion order, but the lookup
#   algorithm does NOT depend on any particular order — it simply stops at
#   the first match.  Because amateur bands never overlap, one match is
#   always sufficient.
BAND_PLAN: dict[str, tuple[float, float]] = {
    # ---- MF (Medium Frequency) ----------------------------------------
    "160m": (1800, 2000),       # "Top Band" — long-range skywave, active mainly
                                #   at night; deep QSB and heavy QRM from
                                #   broadcast stations make DX challenging

    # ---- HF (High Frequency) ------------------------------------------
    "80m":  (3500, 4000),       # Workhorse evening/night DX band; regional
                                #   by day, intercontinental at night
    "60m":  (5330, 5410),       # Channelised in most administrations;
                                #   stored as a continuous range here for
                                #   simplicity — the parser doesn't need to
                                #   know which exact channel is in use
    "40m":  (7000, 7300),       # Reliable DX at night; Region 1 upper edge
                                #   is 7.200 for telephony but 7.300 is the
                                #   full allocation boundary
    "30m":  (10100, 10150),     # WARC band (no contests permitted); CW and
                                #   digital modes only in most countries
    "20m":  (14000, 14350),     # The "king" of HF DX — often open 24 h,
                                #   first choice for a new DX entity
    "17m":  (18068, 18168),     # WARC band — generally less crowded than the
                                #   contest bands; excellent propagation when
                                #   solar flux is elevated
    "15m":  (21000, 21450),     # Primarily daytime DX; outstanding at solar
                                #   maximum, nearly silent at solar minimum
    "12m":  (24890, 24990),     # WARC band — mirrors 10 m propagation but
                                #   slightly more reliable at cycle minimum
    "10m":  (28000, 29700),     # Very wide band (1.7 MHz); essentially dead
                                #   at solar minimum, can support worldwide
                                #   contacts with milliwatts at maximum

    # ---- VHF (Very High Frequency) ------------------------------------
    "6m":   (50000, 54000),     # "Magic band" — spectacular sporadic-E
                                #   openings; upper edge varies by country
    "4m":   (70000, 70500),     # Region 1 only; not allocated in most of
                                #   the Americas or Asia-Pacific
    "2m":   (144000, 148000),   # Primary VHF band; supports EME, meteor
                                #   scatter, and tropo DX

    # ---- UHF / Microwave ----------------------------------------------
    "70cm": (430000, 440000),   # Primary UHF band; exact allocation varies
                                #   by country (some only have 432–438 MHz)
    "23cm": (1240000, 1300000), # Entry point for microwave DX; EME and
                                #   local weak-signal work
}


def frequency_to_band(freq_khz: float) -> Optional[str]:
    """Return the amateur band name for a given frequency in kHz, or None.

    This is the most-called function in the module — every parsed spot goes
    through it.  The implementation is intentionally kept simple:

      1. Iterate BAND_PLAN in insertion order.
      2. Return the first band whose [low, high] range contains freq_khz.
      3. If no band matches, return None.

    Why return None instead of raising an exception?
        A frequency outside the amateur allocations is not an error — it is an
        ordinary occurrence.  Spotters sometimes report commercial or broadcast
        frequencies by mistake, and garbled telnet lines can produce nonsense
        numbers (including 0.0).  Returning None lets the caller decide what to
        do (e.g. display "[?]", skip the spot, or log a warning) without
        requiring every call site to wrap this in a try/except.

    Why inclusive bounds (<=) on both sides?
        A spot at exactly 14000.0 kHz or 14350.0 kHz should be classified as
        "20m", not fall through to None.  Using strict inequality (<) would
        silently reject valid edge-frequency spots.

    Args:
        freq_khz: Frequency in kilohertz.  May be a float with a decimal
                  component (e.g. 14225.5 for 14.2255 MHz).  Negative values
                  and zero are tolerated but will return None because no band
                  range includes them.

    Returns:
        A string key from BAND_PLAN such as "20m", "40m", "2m", etc., or
        None if freq_khz does not fall within any defined amateur allocation.
    """
    for band, (low, high) in BAND_PLAN.items():
        if low <= freq_khz <= high:
            # Amateur bands never overlap, so the first match is the only
            # possible match — return immediately rather than scanning further.
            return band
    # No match found.  This is expected for out-of-band spots (e.g. a spot on
    # a broadcast frequency) or for zero/negative values from parse errors.
    return None


def band_to_range(band: str) -> Optional[tuple[float, float]]:
    """Return the (low_khz, high_khz) tuple for a named band, or None.

    This is the inverse of frequency_to_band(): given a band name you get the
    frequency boundaries back.  It is used by filtering code that wants to
    constrain searches to a specific band without hard-coding the numbers.

    Why case-insensitive lookup (.lower())?
        Band names arrive from multiple sources — config files, user input, URL
        query strings, and internal constants — each with potentially different
        casing ("20m", "20M", "20m").  Normalising to lowercase before the
        lookup means callers never need to worry about case, which eliminates
        a whole class of subtle bugs.

    Why use dict.get() rather than BAND_PLAN[band]?
        dict.get() returns None for a missing key without raising a KeyError.
        An unknown band name is not an exceptional condition — it just means the
        caller asked for a band we don't have.  Returning None is cleaner than
        forcing every call site to handle KeyError.

    Args:
        band: Band name string, case-insensitive (e.g. "20m", "20M", "2m").

    Returns:
        A (lower_edge_kHz, upper_edge_kHz) tuple, or None if the band name
        is not present in BAND_PLAN.
    """
    return BAND_PLAN.get(band.lower())
