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

# A handful of well-known public DXCluster nodes.
# (host, port) – extend this dict as needed.
KNOWN_CLUSTERS: dict[str, tuple[str, int]] = {
    "gb7mbc":    ("gb7mbc.gb7.me.uk",   7300),
    "ve7cc":     ("dx.ve7cc.net",          23),
    "k4zr":      ("k4zr.no-ip.org",      7300),
    "w6cua":     ("w6cua.no-ip.com",     7300),
    "ar-cluster": ("ar.k3lr.com",         7373),
    "db0sue":    ("db0sue.de",            8000),
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
