"""dxcluster – Python library for consuming DXCluster spots.

Public API::

    from dxcluster import DXSpot, DXClusterClient, SpotFeed, SpotFilter
    from dxcluster import KNOWN_CLUSTERS, BAND_PLAN, frequency_to_band
"""

from .bands import BAND_PLAN, band_to_range, frequency_to_band
from .client import DXClusterClient
from .feed import CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed
from .filters import SpotFilter
from .models import DXSpot
from .parser import parse_spot

__all__ = [
    "DXSpot",
    "DXClusterClient",
    "SpotFeed",
    "SpotFilter",
    "parse_spot",
    "KNOWN_CLUSTERS",
    "CLUSTER_DESCRIPTIONS",
    "BAND_PLAN",
    "frequency_to_band",
    "band_to_range",
]

__version__ = "0.1.0"
