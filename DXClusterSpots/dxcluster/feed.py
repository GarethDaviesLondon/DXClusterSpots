"""SpotFeed – high-level ongoing DX spot stream.

This is the primary entry point for consuming spots, both from the CLI
and from future web service endpoints.

Architecture note
-----------------
SpotFeed deliberately uses an *async generator* pattern so it can be:

* consumed line-by-line in a CLI (``async for spot in feed.spots()``)
* iterated inside an SSE / WebSocket endpoint (FastAPI, aiohttp)
* wrapped by a background task that pushes to a message queue (Redis, etc.)

The callback list (``add_callback``) supports the *observer* pattern for
web service scenarios where multiple consumers need the same feed without
each opening a separate telnet connection.
"""

import asyncio
import logging
import socket
from typing import AsyncIterator, Callable, Optional

from .client import DXClusterClient
from .filters import SpotFilter
from .models import DXSpot
from .parser import parse_spot

logger = logging.getLogger(__name__)

# Known public DXCluster telnet nodes.
# Key   → short name used on the command line / in the shell
# Value → (hostname, port)
KNOWN_CLUSTERS: dict[str, tuple[str, int]] = {
    # ── Belgium ────────────────────────────────────────────────────────
    "on0nol":    ("nolcluster.on8ar.eu",        7300),  # NOL Radioamateur Club, JO21rd
    # ── Netherlands ────────────────────────────────────────────────────
    "pi4cc":     ("dxc.pi4cc.nl",               8000),  # PI4CC, Centrum voor Elektronica
    "pa6nl":     ("dxc.pa6.nl",                   23),  # PA6NL, Wassenaar JO22fd
    # ── Germany ────────────────────────────────────────────────────────
    "da0bcc":    ("dx.da0bcc.de",               7300),  # Bavarian Contest Club (replaced db0sue Sep 2024)
    # ── United Kingdom ─────────────────────────────────────────────────
    "g6nhu":     ("dxspider.co.uk",             7300),  # G6NHU-2, dedicated datacenter, ~99.998% uptime
    "gb7djk":    ("gb7djk.dxcluster.net",       7300),  # GB7DJK, East Dereham
    "gb7baa":    ("gb7baa.com",                 7300),  # GB7BAA, Worcester
    "gb7bux":    ("dxc.gb7bux.co.uk",           7373),  # GB7BUX, Buxton
    # ── France ─────────────────────────────────────────────────────────
    "f6kdf":     ("f6kdf.ath.cx",               7300),  # F6KDF, Lyon
    # ── Spain ──────────────────────────────────────────────────────────
    "ea7jxh":    ("dx.ea7jxh.eu",               7300),  # EA7JXH
    # ── North America ──────────────────────────────────────────────────
    "ve7cc":     ("dx.ve7cc.net",                 23),  # VE7CC, Vancouver BC
    "k4zr":      ("k4zr.no-ip.org",            7300),  # K4ZR
    "w6cua":     ("w6cua.no-ip.com",           7300),  # W6CUA
    "ar-cluster": ("ar.k3lr.com",              7373),  # AR-Cluster, K3LR
}

# Human-readable descriptions shown by the 'nodes' command.
CLUSTER_DESCRIPTIONS: dict[str, str] = {
    "on0nol":    "Belgium      – ON0NOL / NOL Radioamateur Club (JO21rd)",
    "pi4cc":     "Netherlands  – PI4CC, Centrum voor Elektronica",
    "pa6nl":     "Netherlands  – PA6NL, Wassenaar (JO22fd)",
    "da0bcc":    "Germany      – DA0BCC / Bavarian Contest Club",
    "g6nhu":     "UK           – G6NHU-2, datacenter hosted, ~99.998% uptime",
    "gb7djk":    "UK           – GB7DJK, East Dereham",
    "gb7baa":    "UK           – GB7BAA, Worcester",
    "gb7bux":    "UK           – GB7BUX, Buxton",
    "f6kdf":     "France       – F6KDF, Lyon",
    "ea7jxh":    "Spain        – EA7JXH",
    "ve7cc":     "Canada       – VE7CC, Vancouver BC",
    "k4zr":      "USA          – K4ZR",
    "w6cua":     "USA          – W6CUA",
    "ar-cluster": "USA         – AR-Cluster / K3LR",
}

_DEFAULT_RECONNECT_DELAY = 30.0  # seconds


class SpotFeed:
    """Manages a live stream of DX spots from a single cluster node.

    Parameters
    ----------
    host:
        Hostname of the DXCluster node.
    port:
        Telnet port (default 7300).
    callsign:
        Your callsign used for cluster login.
    spot_filter:
        Optional :class:`SpotFilter` – only matching spots are yielded.
    reconnect:
        If True (default), automatically reconnect on connection loss.
    reconnect_delay:
        Seconds to wait before reconnecting.
    """

    def __init__(
        self,
        host: str,
        port: int = 7300,
        callsign: str = "NOCALL",
        spot_filter: Optional[SpotFilter] = None,
        reconnect: bool = True,
        reconnect_delay: float = _DEFAULT_RECONNECT_DELAY,
    ) -> None:
        self.host = host
        self.port = port
        self.callsign = callsign
        self.spot_filter = spot_filter
        self.reconnect = reconnect
        self.reconnect_delay = reconnect_delay
        self._running = False
        self._callbacks: list[Callable[[DXSpot], None]] = []

    # ------------------------------------------------------------------
    # Observer / callback API (for web service fan-out)
    # ------------------------------------------------------------------

    def add_callback(self, callback: Callable[[DXSpot], None]) -> None:
        """Register a callback invoked for every accepted spot.

        Useful for pushing spots to a message broker or WebSocket clients
        without multiple open telnet connections.
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[DXSpot], None]) -> None:
        self._callbacks.remove(callback)

    # ------------------------------------------------------------------
    # Main async generator
    # ------------------------------------------------------------------

    async def spots(self) -> AsyncIterator[DXSpot]:
        """Async generator that yields :class:`DXSpot` objects indefinitely.

        Handles reconnection transparently when *reconnect* is True.
        """
        self._running = True
        while self._running:
            try:
                async with DXClusterClient(self.host, self.port, self.callsign) as client:
                    async for line in client.read_lines():
                        if not self._running:
                            return

                        spot = parse_spot(line)
                        if spot is None:
                            logger.debug("Non-spot line: %s", line[:80])
                            continue

                        if self.spot_filter and not self.spot_filter(spot):
                            continue

                        for cb in self._callbacks:
                            try:
                                cb(spot)
                            except Exception as exc:
                                logger.warning("Callback error: %s", exc)

                        yield spot

                # The async-with block exited cleanly (server closed the
                # connection without raising an exception).  Sleep before
                # reconnecting so we don't hammer the server.
                if self._running and self.reconnect:
                    logger.info(
                        "Connection to %s closed by server, reconnecting in %.0fs…",
                        self.host, self.reconnect_delay,
                    )
                    await asyncio.sleep(self.reconnect_delay)

            except asyncio.CancelledError:
                # Task was cancelled (e.g. stream stop, app shutdown) – exit cleanly.
                raise

            except socket.gaierror as exc:
                # DNS resolution failure – retrying won't help until the hostname
                # is corrected, so raise immediately as a ConnectionError so the
                # caller (e.g. the interactive shell) can surface a clear message.
                raise ConnectionError(
                    f"Cannot resolve hostname '{self.host}' – "
                    f"check the address is correct. ({exc})"
                ) from exc

            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Connection lost (%s): %s", self.host, exc)
                if not self.reconnect or not self._running:
                    break
                logger.info("Reconnecting in %.0fs…", self.reconnect_delay)
                try:
                    await asyncio.sleep(self.reconnect_delay)
                except asyncio.CancelledError:
                    raise

            except Exception as exc:
                logger.error("Unexpected error: %s", exc, exc_info=True)
                if not self.reconnect or not self._running:
                    break
                try:
                    await asyncio.sleep(self.reconnect_delay)
                except asyncio.CancelledError:
                    raise

    def stop(self) -> None:
        """Signal the feed to stop after the next spot."""
        self._running = False
