"""DXCC entity prefix database.

Maps DXCC entity keys to their name and every associated callsign prefix,
so that a user typing "G" as a filter also matches M and 2E (all England),
and "ON" also matches OO, OP, OR, OS, OT (all Belgium), etc.

Sources: ARRL DXCC Country List, Big CTY.dat (AD1C), ITU Radio Regulations.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Entity database
# Key   : canonical short name (what the user types, usually the primary pfx)
# Value : (display_name, [all_valid_prefixes_for_this_entity])
# ---------------------------------------------------------------------------

_ENTITIES: dict[str, tuple[str, list[str]]] = {

    # ── United Kingdom ──────────────────────────────────────────────────────
    # England: G, M (post-reform), 2E (foundation/intermediate)
    "G":    ("England",          ["G", "M", "2E"]),
    "GM":   ("Scotland",         ["GM", "MM", "2M"]),
    "GW":   ("Wales",            ["GW", "MW", "2W"]),
    "GI":   ("N. Ireland",       ["GI", "MI", "2I"]),
    "GD":   ("Isle of Man",      ["GD", "MD", "2D"]),
    "GJ":   ("Jersey",           ["GJ", "MJ", "2J"]),
    "GU":   ("Guernsey",         ["GU", "MU", "2U"]),

    # ── Benelux ─────────────────────────────────────────────────────────────
    "ON":   ("Belgium",          ["ON", "OO", "OP", "OQ", "OR", "OS", "OT"]),
    "LX":   ("Luxembourg",       ["LX"]),
    "PA":   ("Netherlands",      ["PA", "PB", "PC", "PD", "PE", "PF", "PG", "PH", "PI"]),

    # ── Germany ─────────────────────────────────────────────────────────────
    "DL":   ("Germany",          ["DA", "DB", "DC", "DD", "DE", "DF", "DG", "DH", "DI",
                                   "DJ", "DK", "DL", "DM", "DN", "DO", "DP", "DQ", "DR",
                                   "Y2", "Y3", "Y4", "Y5", "Y6", "Y7", "Y8", "Y9"]),

    # ── France ──────────────────────────────────────────────────────────────
    "F":    ("France",           ["F", "TM"]),
    "TK":   ("Corsica",          ["TK"]),

    # ── Iberian Peninsula ───────────────────────────────────────────────────
    "EA":   ("Spain",            ["EA", "EB", "EC", "ED", "EE", "EF", "EG", "EH", "AM"]),
    "EA6":  ("Balearic Is.",     ["EA6", "EB6", "EC6", "EE6", "EF6", "EG6", "EH6"]),
    "EA8":  ("Canary Is.",       ["EA8", "EB8", "EC8", "EE8", "EF8", "EG8", "EH8"]),
    "EA9":  ("Ceuta & Melilla",  ["EA9", "EB9"]),
    "CT":   ("Portugal",         ["CR", "CS", "CT"]),
    "CU":   ("Azores",           ["CU"]),
    "CT3":  ("Madeira",          ["CR3", "CS3", "CT3", "CQ3"]),

    # ── Italy ───────────────────────────────────────────────────────────────
    "I":    ("Italy",            ["I", "IA", "IB", "IC", "ID", "IE", "IF", "IG", "IH",
                                   "II", "IJ", "IK", "IL", "IM", "IN", "IO", "IP", "IQ",
                                   "IR", "IS", "IT", "IU", "IV", "IW", "IX", "IY", "IZ"]),
    "IS0":  ("Sardinia",         ["IS0", "IM0"]),
    "IT9":  ("Sicily",           ["IT9"]),

    # ── Alps / Central Europe ───────────────────────────────────────────────
    "OE":   ("Austria",          ["OE"]),
    "HB":   ("Switzerland",      ["HB", "HE"]),
    "HB0":  ("Liechtenstein",    ["HB0"]),

    # ── Scandinavia ─────────────────────────────────────────────────────────
    "SM":   ("Sweden",           ["SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SI",
                                   "SJ", "SK", "SL", "SM", "7S", "8S"]),
    "LA":   ("Norway",           ["LA", "LB", "LC", "LD", "LE", "LF", "LG", "LH", "LI",
                                   "LJ", "LK", "LL", "LM", "LN"]),
    "JW":   ("Svalbard",         ["JW"]),
    "JX":   ("Jan Mayen",        ["JX"]),
    "OZ":   ("Denmark",          ["OZ", "5P", "5Q", "OU", "OV", "OW"]),
    "OX":   ("Greenland",        ["OX", "XP"]),
    "TF":   ("Iceland",          ["TF"]),
    "OH":   ("Finland",          ["OF", "OG", "OH", "OI", "OJ"]),
    "OH0":  ("Åland Is.",        ["OH0", "OF0", "OG0", "OI0"]),
    "OJ0":  ("Market Reef",      ["OJ0"]),

    # ── Eastern Europe ──────────────────────────────────────────────────────
    "OK":   ("Czech Rep.",       ["OK", "OL"]),
    "OM":   ("Slovakia",         ["OM"]),
    "SP":   ("Poland",           ["HF", "SO", "SP", "SQ", "SR"]),
    "HA":   ("Hungary",          ["HA", "HG"]),
    "YO":   ("Romania",          ["YO", "YP", "YQ", "YR"]),
    "LZ":   ("Bulgaria",         ["LZ"]),
    "SV":   ("Greece",           ["SV", "SW", "SX", "SY", "SZ", "J4"]),
    "SV5":  ("Dodecanese",       ["SV5", "SW5", "SX5"]),
    "SV9":  ("Crete",            ["SV9", "SW9", "SX9"]),
    "EI":   ("Ireland",          ["EI", "EJ"]),
    "5B":   ("Cyprus",           ["5B", "H2", "P3"]),

    # ── Baltic States ───────────────────────────────────────────────────────
    "ES":   ("Estonia",          ["ES"]),
    "LY":   ("Lithuania",        ["LY"]),
    "YL":   ("Latvia",           ["YL"]),

    # ── Former Soviet Union ─────────────────────────────────────────────────
    "UA":   ("Russia Europe",    ["R", "RA", "RB", "RC", "RD", "RE", "RF", "RG", "RH",
                                   "RI", "RJ", "RK", "RL", "RM", "RN", "RO", "RP", "RQ",
                                   "RR", "RS", "RT", "RU", "RV", "RW", "RX", "RY", "RZ",
                                   "UA", "UB", "UC", "UD", "UE", "UF", "UG", "UI"]),
    "UA9":  ("Russia Asia",      ["R0", "R8", "R9", "UA0", "UA8", "UA9"]),
    "UR":   ("Ukraine",          ["EM", "EN", "EO", "UR", "US", "UT", "UU", "UV",
                                   "UW", "UX", "UY", "UZ"]),
    "EU":   ("Belarus",          ["EU", "EV", "EW"]),
    "UN":   ("Kazakhstan",       ["UN", "UO", "UP", "UQ"]),

    # ── Balkans / South-East Europe ─────────────────────────────────────────
    "9A":   ("Croatia",          ["9A"]),
    "S5":   ("Slovenia",         ["S5"]),
    "YU":   ("Serbia",           ["YT", "YU"]),
    "4O":   ("Montenegro",       ["4O"]),
    "T9":   ("Bosnia-Herz.",     ["T9", "E7"]),
    "Z3":   ("N. Macedonia",     ["Z3"]),
    "ZA":   ("Albania",          ["ZA"]),
    "TA":   ("Turkey",           ["TA", "TB", "TC", "YM"]),

    # ── Middle East ─────────────────────────────────────────────────────────
    "4X":   ("Israel",           ["4X", "4Z"]),
    "OD":   ("Lebanon",          ["OD"]),
    "A4":   ("Oman",             ["A4"]),
    "A6":   ("UAE",              ["A6"]),
    "HZ":   ("Saudi Arabia",     ["7Z", "HZ"]),

    # ── Africa ──────────────────────────────────────────────────────────────
    "ZS":   ("South Africa",     ["ZR", "ZS", "ZT", "ZU"]),
    "5H":   ("Tanzania",         ["5H"]),
    "5N":   ("Nigeria",          ["5N"]),
    "5Z":   ("Kenya",            ["5Y", "5Z"]),
    "9J":   ("Zambia",           ["9J"]),
    "9Q":   ("DR Congo",         ["9O", "9P", "9Q", "9R", "9S", "9T"]),
    "D2":   ("Angola",           ["D2", "D3"]),
    "3B8":  ("Mauritius",        ["3B8"]),
    "ZD8":  ("Ascension Is.",    ["ZD8"]),
    "ZD7":  ("St. Helena",       ["ZD7"]),

    # ── North America ───────────────────────────────────────────────────────
    "W":    ("USA",              ["A", "AA", "AB", "AC", "AD", "AE", "AF", "AG", "AH",
                                   "AI", "AJ", "AK", "AL", "K", "N", "W"]),
    "VE":   ("Canada",           ["CF", "CG", "CH", "CI", "CJ", "CK", "CY", "CZ",
                                   "VA", "VB", "VC", "VD", "VE", "VF", "VG", "VY",
                                   "XJ", "XK", "XL", "XM", "XN", "XO"]),
    "XE":   ("Mexico",           ["4A", "4B", "4C", "XA", "XB", "XC", "XD", "XE",
                                   "XF", "XG", "XH", "XI"]),

    # ── Caribbean / Central America ─────────────────────────────────────────
    "PJ2":  ("Curaçao",          ["PJ2"]),
    "PJ4":  ("Bonaire",          ["PJ4"]),
    "FG":   ("Guadeloupe",       ["FG"]),
    "FM":   ("Martinique",       ["FM"]),
    "FY":   ("French Guiana",    ["FY"]),
    "8P":   ("Barbados",         ["8P"]),
    "VP9":  ("Bermuda",          ["VP9"]),
    "KG4":  ("Guantanamo",       ["KG4"]),

    # ── South America ───────────────────────────────────────────────────────
    "PY":   ("Brazil",           ["PP", "PQ", "PR", "PS", "PT", "PU", "PV", "PW",
                                   "PX", "PY", "ZV", "ZW", "ZX", "ZY", "ZZ"]),
    "LU":   ("Argentina",        ["AY", "AZ", "LO", "LP", "LQ", "LR", "LS", "LT",
                                   "LU", "LV", "LW"]),
    "CE":   ("Chile",            ["3G", "CA", "CB", "CC", "CD", "CE", "XQ", "XR"]),
    "CX":   ("Uruguay",          ["CV", "CW", "CX"]),
    "HC":   ("Ecuador",          ["HC", "HD"]),
    "OA":   ("Peru",             ["OA", "OB", "OC"]),
    "CP":   ("Bolivia",          ["CP"]),

    # ── Asia-Pacific ────────────────────────────────────────────────────────
    "JA":   ("Japan",            ["JA", "JB", "JC", "JD", "JE", "JF", "JG", "JH",
                                   "JI", "JJ", "JK", "JL", "JM", "JN", "JO", "JP",
                                   "JQ", "JR", "JS", "7J", "7K", "7L", "7M", "7N"]),
    "HL":   ("South Korea",      ["DS", "DT", "HL", "6K", "6L", "6M", "6N"]),
    "BY":   ("China",            ["B", "BA", "BD", "BG", "BH", "BI", "BJ", "BK", "BL",
                                   "BM", "BN", "BO", "BP", "BQ", "BR", "BS", "BT", "BU",
                                   "BV", "BW", "BX", "BY", "BZ"]),
    "BV":   ("Taiwan",           ["BV", "BU", "BX"]),
    "VK":   ("Australia",        ["AX", "VH", "VI", "VJ", "VK", "VL", "VM", "VN", "VZ"]),
    "ZL":   ("New Zealand",      ["ZK", "ZL", "ZM"]),
    "VU":   ("India",            ["AT", "AU", "AV", "AW", "VT", "VU", "VW"]),
    "9V":   ("Singapore",        ["9V"]),
    "HS":   ("Thailand",         ["E2", "HS"]),
    "DU":   ("Philippines",      ["4D", "4E", "4F", "4G", "4H", "4I", "DU", "DV",
                                   "DW", "DX", "DY", "DZ"]),
    "YB":   ("Indonesia",        ["7A", "7B", "7C", "7D", "7E", "7F", "7G", "7H",
                                   "7I", "JZ", "PK", "PL", "PM", "PN", "PO",
                                   "YB", "YC", "YD", "YE", "YF", "YG", "YH"]),

    # ── Pacific ─────────────────────────────────────────────────────────────
    "KH6":  ("Hawaii",           ["AH6", "KH6", "NH6", "WH6"]),
    "KL7":  ("Alaska",           ["AL7", "KL7", "NL7", "WL7"]),
    "ZK2":  ("Niue",             ["ZK2"]),
    "A3":   ("Tonga",            ["A3"]),
    "T2":   ("Tuvalu",           ["T2"]),
}

# ---------------------------------------------------------------------------
# Build reverse lookup: prefix → entity_key
# Longer prefixes take priority (e.g. "EA8" wins over "EA")
# ---------------------------------------------------------------------------

_PREFIX_TO_ENTITY: dict[str, str] = {}


def _build_reverse_lookup() -> None:
    pairs: list[tuple[str, str]] = []
    for entity_key, (_name, prefixes) in _ENTITIES.items():
        for pfx in prefixes:
            pairs.append((pfx, entity_key))
    # Longest prefix first so it gets inserted first and wins
    pairs.sort(key=lambda x: -len(x[0]))
    for pfx, entity_key in pairs:
        if pfx:
            _PREFIX_TO_ENTITY.setdefault(pfx, entity_key)


_build_reverse_lookup()

# ---------------------------------------------------------------------------
# CQ zone table (entity_key → CQ zone number)
# Sources: ARRL/CQ Magazine zone map, cty.dat (AD1C)
# ---------------------------------------------------------------------------

_ENTITY_CQ_ZONE: dict[str, int] = {
    # ── United Kingdom ──────────────────────────────────────────────────────
    "G": 14,  "GM": 14, "GW": 14, "GI": 14,
    "GD": 14, "GJ": 14, "GU": 14,
    "EI": 14,

    # ── Benelux ─────────────────────────────────────────────────────────────
    "ON": 14, "LX": 14, "PA": 14,

    # ── Germany / Central Europe ─────────────────────────────────────────────
    "DL": 14, "OE": 15, "HB": 14, "HB0": 14,

    # ── France / Iberia ──────────────────────────────────────────────────────
    "F": 14, "TK": 15,
    "EA": 14, "EA6": 14, "EA8": 33, "EA9": 33,
    "CT": 14, "CU": 14, "CT3": 33,

    # ── Scandinavia / North ──────────────────────────────────────────────────
    "SM": 14, "LA": 14, "OZ": 14, "TF": 40,
    "OH": 18, "OH0": 18, "OJ0": 18,
    "JW": 40, "JX": 40, "OX": 40,

    # ── Italy ────────────────────────────────────────────────────────────────
    "I": 15, "IS0": 15, "IT9": 15,

    # ── Eastern Europe ───────────────────────────────────────────────────────
    "OK": 15, "OM": 15, "SP": 15, "HA": 15,
    "YO": 20, "LZ": 20, "SV": 20, "SV5": 20, "SV9": 20,
    "5B": 20,

    # ── Baltic / FSU ─────────────────────────────────────────────────────────
    "ES": 15, "LY": 15, "YL": 15,
    "UA": 16, "UR": 16, "EU": 16,
    "UA9": 17, "UN": 17,

    # ── Balkans ───────────────────────────────────────────────────────────────
    "9A": 15, "S5": 15, "YU": 15, "4O": 15,
    "T9": 15, "Z3": 15, "ZA": 15,

    # ── Turkey / Middle East ─────────────────────────────────────────────────
    "TA": 20, "4X": 20, "OD": 20,
    "A4": 21, "A6": 21, "HZ": 21,

    # ── Africa ───────────────────────────────────────────────────────────────
    "ZS": 38, "ZD7": 38,
    "5H": 37, "5Z": 37, "9J": 37,
    "9Q": 36, "D2": 36, "ZD8": 36,
    "5N": 35,
    "3B8": 39,
    "EA8": 33,  # already above; repeated for clarity

    # ── North America ────────────────────────────────────────────────────────
    "W": 5, "VE": 4, "XE": 6,
    "KH6": 31, "KL7": 1,
    "VP9": 5,

    # ── Caribbean ────────────────────────────────────────────────────────────
    "PJ2": 9, "PJ4": 9,
    "FG": 8, "FM": 8, "FY": 9,
    "8P": 8, "KG4": 8,

    # ── South America ────────────────────────────────────────────────────────
    "PY": 11, "LU": 13, "CE": 12, "CX": 13,
    "HC": 10, "OA": 10, "CP": 10,

    # ── Asia-Pacific ─────────────────────────────────────────────────────────
    "JA": 25, "HL": 25,
    "BY": 24, "BV": 24,
    "VU": 26, "HS": 26, "9V": 28,
    "DU": 27, "YB": 28,
    "VK": 29, "ZL": 32,

    # ── Pacific islands ──────────────────────────────────────────────────────
    "A3": 32, "T2": 31, "ZK2": 32,
}


def cq_zone_for(callsign_or_prefix: str) -> Optional[int]:
    """Return the CQ zone for a callsign or prefix, or None if unknown."""
    key = resolve_entity(callsign_or_prefix)
    if key is None:
        return None
    return _ENTITY_CQ_ZONE.get(key)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

_CLEAN_RE = re.compile(r"[^A-Z0-9]")


def callsign_prefix(callsign: str) -> str:
    """Extract the ITU prefix portion from a callsign.

    Examples::

        "G3SXW"  → "G"
        "ON4KST" → "ON"
        "DL9GTB" → "DL"
        "2E0ABC" → "2E"
        "M0ABC"  → "M"
        "GM3KMA" → "GM"
        "VK3IO"  → "VK"
        "4X1ABC" → "4X"
    """
    # Drop portable/beacon suffix: G3SXW/P → G3SXW
    call = callsign.upper().split("/")[0]
    call = _CLEAN_RE.sub("", call)
    if not call:
        return ""

    if call[0].isdigit():
        # Starts with digit: consume digit + following letters (e.g. 2E, 4X, 9A)
        i = 1
        while i < len(call) and call[i].isalpha():
            i += 1
        return call[:i]
    else:
        # Normal: letters up to the first digit (e.g. G, ON, DL, GM, VK)
        i = 0
        while i < len(call) and call[i].isalpha():
            i += 1
        return call[:i]


def resolve_entity(callsign_or_prefix: str) -> Optional[str]:
    """Return the entity key for a callsign or prefix, or None if unknown.

    Tries progressively shorter prefix strings until a match is found.
    """
    pfx = callsign_prefix(callsign_or_prefix)
    for length in range(len(pfx), 0, -1):
        candidate = pfx[:length]
        if candidate in _PREFIX_TO_ENTITY:
            return _PREFIX_TO_ENTITY[candidate]
    return None


def entity_name(entity_key: str) -> str:
    """Return the human-readable country name for an entity key."""
    entry = _ENTITIES.get(entity_key)
    return entry[0] if entry else entity_key


def entity_prefixes(entity_key: str) -> list[str]:
    """Return every callsign prefix that belongs to this entity."""
    entry = _ENTITIES.get(entity_key)
    return list(entry[1]) if entry else []


def all_prefixes_for(callsign_or_prefix: str) -> list[str]:
    """Return all prefixes for the same DXCC entity as the given input.

    Examples::

        "G"     → ["G", "M", "2E"]
        "M0ABC" → ["G", "M", "2E"]
        "ON"    → ["ON", "OO", "OP", "OQ", "OR", "OS", "OT"]
    """
    key = resolve_entity(callsign_or_prefix)
    if key is None:
        return [callsign_prefix(callsign_or_prefix.upper())]
    return entity_prefixes(key)


def describe_entity(callsign_or_prefix: str) -> str:
    """Return a short human-readable description.

    Example: "G" → "England (G, M, 2E)"
    """
    key = resolve_entity(callsign_or_prefix)
    if key is None:
        pfx = callsign_prefix(callsign_or_prefix.upper())
        return pfx or callsign_or_prefix.upper()
    name = entity_name(key)
    prefixes = entity_prefixes(key)
    pfx_str = ", ".join(prefixes[:6])
    if len(prefixes) > 6:
        pfx_str += f", … (+{len(prefixes) - 6})"
    return f"{name} ({pfx_str})"
