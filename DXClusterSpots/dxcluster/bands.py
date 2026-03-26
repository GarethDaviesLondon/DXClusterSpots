"""Ham radio band plan with frequency-to-band conversion utilities."""

from typing import Optional

# ITU Region 1 band plan (frequencies in kHz)
BAND_PLAN: dict[str, tuple[float, float]] = {
    "160m": (1800, 2000),
    "80m": (3500, 4000),
    "60m": (5330, 5410),
    "40m": (7000, 7300),
    "30m": (10100, 10150),
    "20m": (14000, 14350),
    "17m": (18068, 18168),
    "15m": (21000, 21450),
    "12m": (24890, 24990),
    "10m": (28000, 29700),
    "6m": (50000, 54000),
    "4m": (70000, 70500),
    "2m": (144000, 148000),
    "70cm": (430000, 440000),
    "23cm": (1240000, 1300000),
}


def frequency_to_band(freq_khz: float) -> Optional[str]:
    """Return the amateur band name for a given frequency in kHz, or None."""
    for band, (low, high) in BAND_PLAN.items():
        if low <= freq_khz <= high:
            return band
    return None


def band_to_range(band: str) -> Optional[tuple[float, float]]:
    """Return the (low_khz, high_khz) tuple for a named band, or None."""
    return BAND_PLAN.get(band.lower())
