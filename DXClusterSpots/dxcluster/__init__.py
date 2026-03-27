"""dxcluster – Python library for consuming DXCluster spots.

Public API::

    from dxcluster import DXSpot, DXClusterClient, SpotFeed, SpotFilter
    from dxcluster import KNOWN_CLUSTERS, BAND_PLAN, frequency_to_band
"""

from .bands import BAND_PLAN, band_to_range, frequency_to_band
from .client import DXClusterClient
from .config import AppConfig, FilterConfig, load_config, save_config
from .dxcc import all_prefixes_for, callsign_prefix, describe_entity, resolve_entity
from .feed import CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed
from .filters import SpotFilter, build_filter_from_config
from .models import DXSpot
from .parser import parse_spot, parse_mode

__all__ = [
    "DXSpot",
    "DXClusterClient",
    "SpotFeed",
    "SpotFilter",
    "build_filter_from_config",
    "parse_spot",
    "parse_mode",
    "KNOWN_CLUSTERS",
    "CLUSTER_DESCRIPTIONS",
    "BAND_PLAN",
    "frequency_to_band",
    "band_to_range",
    "AppConfig",
    "FilterConfig",
    "load_config",
    "save_config",
    "resolve_entity",
    "all_prefixes_for",
    "callsign_prefix",
    "describe_entity",
]

__version__ = "0.1.0"
