"""Persistent configuration for DXClusterSpots.

Stored as JSON in the platform-appropriate user config directory:
  Windows : %APPDATA%\\DXClusterSpots\\config.json
  macOS   : ~/Library/Application Support/DXClusterSpots/config.json
  Linux   : ~/.config/DXClusterSpots/config.json

Saved automatically whenever a setting changes in the TUI.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform-appropriate config directory
# ---------------------------------------------------------------------------

def _config_dir() -> str:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "DXClusterSpots")


def config_path() -> str:
    """Return the full path to the config file."""
    return os.path.join(_config_dir(), "config.json")


def history_path() -> str:
    """Return the full path to the command history file, creating the dir if needed."""
    path = os.path.join(_config_dir(), "history.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    return path


def log_path() -> str:
    """Return the full path to the 24-hour spot log file."""
    return os.path.join(_config_dir(), "spots.log")


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ConnectionConfig:
    """Last-used cluster connection settings."""
    node: str = ""          # known node name, e.g. "pi4cc"
    host: str = ""          # resolved hostname
    port: int = 7300
    callsign: str = "NOCALL"


@dataclass
class FilterConfig:
    """Active spot filters – persisted between sessions."""
    bands: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    # include_prefixes: if non-empty, show ONLY spots from these entities
    include_prefixes: list[str] = field(default_factory=list)
    # exclude_prefixes: hide spots from these entities (worked list)
    exclude_prefixes: list[str] = field(default_factory=list)
    # cq_zones: None = all zones accepted; [] = all closed; [14,15] = whitelist
    cq_zones: object = None          # Optional[list[int]] – filter on DX station zone
    spotter_cq_zones: object = None  # Optional[list[int]] – filter on spotter zone


@dataclass
class AppConfig:
    """Top-level application configuration."""
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    json_mode: bool = False
    auto_stream: bool = True   # reconnect and start streaming on launch

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "connection": asdict(self.connection),
            "filters": asdict(self.filters),
            "json_mode": self.json_mode,
            "auto_stream": self.auto_stream,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        conn_d = d.get("connection", {})
        filt_d = d.get("filters", {})
        return cls(
            connection=ConnectionConfig(
                node=conn_d.get("node", ""),
                host=conn_d.get("host", ""),
                port=conn_d.get("port", 7300),
                callsign=conn_d.get("callsign", "NOCALL"),
            ),
            filters=FilterConfig(
                bands=filt_d.get("bands", []),
                modes=filt_d.get("modes", []),
                include_prefixes=filt_d.get("include_prefixes", []),
                exclude_prefixes=filt_d.get("exclude_prefixes", []),
                cq_zones=filt_d.get("cq_zones", None),
                spotter_cq_zones=filt_d.get("spotter_cq_zones", None),
            ),
            json_mode=d.get("json_mode", False),
            auto_stream=d.get("auto_stream", True),
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def has_connection(self) -> bool:
        return bool(self.connection.host) and self.connection.callsign != "NOCALL"

    def add_exclude(self, prefix: str) -> None:
        if prefix not in self.filters.exclude_prefixes:
            self.filters.exclude_prefixes.append(prefix)
        # Remove from include if present
        if prefix in self.filters.include_prefixes:
            self.filters.include_prefixes.remove(prefix)

    def add_include(self, prefix: str) -> None:
        if prefix not in self.filters.include_prefixes:
            self.filters.include_prefixes.append(prefix)
        # Remove from exclude if present
        if prefix in self.filters.exclude_prefixes:
            self.filters.exclude_prefixes.remove(prefix)


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_config() -> AppConfig:
    """Load config from disk, returning a default config on any error."""
    path = config_path()
    if not os.path.exists(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = AppConfig.from_dict(data)
        logger.debug("Config loaded from %s", path)
        return cfg
    except Exception as exc:
        logger.warning("Could not load config (%s): %s – using defaults", path, exc)
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    """Persist config to disk."""
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg.to_dict(), fh, indent=2)
        logger.debug("Config saved to %s", path)
    except Exception as exc:
        logger.error("Could not save config: %s", exc)
