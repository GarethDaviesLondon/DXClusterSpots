"""Async telnet client for DXCluster nodes.

Uses asyncio.open_connection (the modern stdlib replacement for the
deprecated telnetlib) so this is web-service friendly and can be used
inside FastAPI/aiohttp without blocking.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

_LOGIN_KEYWORDS = ("login", "call", "enter your", "please enter")
_CONNECT_TIMEOUT = 10.0  # seconds to wait for the TCP connection to be established
_LOGIN_TIMEOUT = 30.0    # seconds to wait for a login prompt after connecting
_LINE_TIMEOUT = 120.0    # seconds before sending a keepalive
_KEEPALIVE_CMD = "sh/dx 1"  # ask for 1 recent spot as a keepalive


class DXClusterClient:
    """Async context-manager client for a single DXCluster node connection.

    Usage::

        async with DXClusterClient("gb7mbc.gb7.me.uk", 7300, "G0ABC") as client:
            async for line in client.read_lines():
                process(line)
    """

    def __init__(self, host: str, port: int = 7300, callsign: str = "NOCALL"):
        self.host = host
        self.port = port
        self.callsign = callsign
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        logger.info("Connecting to %s:%d as %s", self.host, self.port, self.callsign)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=_CONNECT_TIMEOUT,
        )
        self._connected = True
        await self._do_login()
        logger.info("Connected and logged in to %s", self.host)

    async def disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("Disconnected from %s", self.host)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _do_login(self) -> None:
        """Wait for a login prompt then send our callsign."""
        deadline = asyncio.get_event_loop().time() + _LOGIN_TIMEOUT
        sent = False

        while not sent and asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                text = line.decode("utf-8", errors="replace").lower()
                logger.debug("Server: %s", text.strip())
                if any(kw in text for kw in _LOGIN_KEYWORDS):
                    await self._send(self.callsign)
                    sent = True
            except asyncio.TimeoutError:
                # No prompt received – some clusters start sending spots immediately
                await self._send(self.callsign)
                sent = True

        if not sent:
            await self._send(self.callsign)

    # ------------------------------------------------------------------
    # Raw line stream
    # ------------------------------------------------------------------

    async def read_lines(self) -> AsyncIterator[str]:
        """Yield decoded lines from the cluster indefinitely.

        Sends a keepalive command after a period of silence to prevent
        the server from dropping the connection.
        """
        assert self._reader is not None, "Not connected – call connect() first"

        while self._connected:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=_LINE_TIMEOUT)
                if not raw:
                    logger.warning("Server closed connection")
                    break
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except asyncio.TimeoutError:
                logger.debug("Keepalive → %s", self.host)
                await self._send(_KEEPALIVE_CMD)
            except (ConnectionError, OSError) as exc:
                logger.error("Connection error: %s", exc)
                break

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        if self._writer:
            self._writer.write((text + "\r\n").encode())
            await self._writer.drain()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "DXClusterClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()
