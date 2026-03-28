"""Async telnet client for DXCluster nodes.

Uses asyncio.open_connection (the modern stdlib replacement for the
deprecated telnetlib) so this is web-service friendly and can be used
inside FastAPI/aiohttp without blocking.

Design rationale
----------------
DXCluster nodes speak a simple line-oriented protocol over raw TCP (originally
designed for telnet connections).  The protocol is:
  1.  Client opens a TCP connection to port 7300 (or 23).
  2.  Server sends a login banner and prompts for the user's callsign.
  3.  Client sends its callsign followed by CRLF.
  4.  Server starts sending DX spot lines in real time, interleaved with
      cluster announcements, WWV bulletins, and DX bulletins.

asyncio.open_connection() gives us a (StreamReader, StreamWriter) pair that
works inside an event loop, so this client can coexist with a UI event loop
(prompt_toolkit) or a web framework (FastAPI, aiohttp) without blocking.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# ── Timeout constants ─────────────────────────────────────────────────────────
# These values are chosen to handle the wide range of cluster server behaviours
# seen in practice.  Some servers send a login prompt immediately; others start
# streaming spots with no preamble.  Some drop idle connections after 2 minutes;
# others tolerate indefinite silence.

_LOGIN_KEYWORDS = ("login", "call", "enter your", "please enter")
# These substrings (lower-cased) identify a server login prompt.  Different
# cluster software (DXSpider, CC-Cluster, AR-Cluster) uses different wording,
# so we check for any of these phrases rather than an exact string.

_CONNECT_TIMEOUT = 10.0  # seconds to wait for the TCP connection to be established
# 10 seconds is generous for a local/continental connection.  If the server
# doesn't respond within this window it is likely firewalled or down.

_LOGIN_TIMEOUT = 30.0    # seconds to wait for a login prompt after connecting
# Some servers send a multi-line banner before the prompt, so we need to give
# them time to finish.  30 seconds covers even slow transatlantic links.

_LINE_TIMEOUT = 120.0    # seconds before sending a keepalive
# If the server sends no data for 2 minutes the connection may have been
# silently dropped by a NAT gateway or firewall.  We send a harmless command
# to provoke a response and prove the link is still alive.

_KEEPALIVE_CMD = "sh/dx 1"  # ask for 1 recent spot as a keepalive
# "show/dx 1" is a standard DXSpider / CC-Cluster command that returns the
# most recent spot.  It generates exactly one line of output, which is
# sufficient to reset any server-side idle timer.


class DXClusterClient:
    """Async context-manager client for a single DXCluster node connection.

    Lifecycle::

        async with DXClusterClient("gb7mbc.gb7.me.uk", 7300, "G0ABC") as client:
            async for line in client.read_lines():
                process(line)

    The ``async with`` block handles connect/disconnect automatically.
    ``read_lines()`` is an async generator that yields one decoded string per
    cluster line and sends a keepalive after ``_LINE_TIMEOUT`` seconds of
    server silence.

    Thread safety: this class is *not* thread-safe.  All methods must be
    called from the same event-loop thread.
    """

    def __init__(self, host: str, port: int = 7300, callsign: str = "NOCALL"):
        self.host = host          # e.g. "dxsummit.fi"
        self.port = port          # typically 7300 or 23
        self.callsign = callsign  # YOUR callsign used for cluster login

        # These are set by connect() and cleared by disconnect().
        # Using Optional lets type-checkers catch accidental use before connection.
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False   # True only while the TCP link is up

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the TCP connection and authenticate with our callsign.

        Raises:
            asyncio.TimeoutError: if the server doesn't respond within
                _CONNECT_TIMEOUT seconds.
            socket.gaierror: if the hostname cannot be resolved.
            ConnectionError / OSError: for all other network failures.
        """
        logger.info("Connecting to %s:%d as %s", self.host, self.port, self.callsign)

        # asyncio.wait_for() wraps any coroutine with a timeout.  If the TCP
        # three-way handshake doesn't complete within _CONNECT_TIMEOUT seconds
        # an asyncio.TimeoutError is raised, which the caller (SpotFeed) can
        # catch and treat as a retriable network error.
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=_CONNECT_TIMEOUT,
        )
        self._connected = True
        await self._do_login()
        logger.info("Connected and logged in to %s", self.host)

    async def disconnect(self) -> None:
        """Close the TCP connection gracefully.

        Sets _connected to False *before* closing the socket so that any
        concurrent read_lines() call sees the flag and stops iterating.

        We use a bare ``except Exception`` here because close() and
        wait_closed() can raise a wide variety of OS-specific exceptions
        (BrokenPipeError, ConnectionResetError, etc.) and we don't want a
        teardown path to crash the caller.
        """
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                # wait_closed() waits for the OS to flush the send buffer and
                # close the socket.  Added in Python 3.7; safe to await.
                await self._writer.wait_closed()
            except Exception:
                pass  # ignore errors during teardown
        logger.info("Disconnected from %s", self.host)

    # ── Login ──────────────────────────────────────────────────────────────────

    async def _do_login(self) -> None:
        """Wait for a login prompt then send our callsign.

        Strategy:
          1. Read lines from the server for up to _LOGIN_TIMEOUT seconds.
          2. If any line contains a login-prompt keyword, send the callsign
             and stop.
          3. If a line times out (5 s) with no prompt, assume the server is
             streaming spots immediately (some clusters do this) and send the
             callsign anyway.
          4. If the whole deadline expires without sending, send as a last
             resort so the server knows who we are.

        We deliberately do NOT check the server's response to the callsign.
        A real callsign will be accepted; NOCALL will usually be rejected and
        the server will close the connection – the reconnect logic in SpotFeed
        handles that case.
        """
        deadline = asyncio.get_event_loop().time() + _LOGIN_TIMEOUT
        sent = False  # prevents sending the callsign twice

        while not sent and asyncio.get_event_loop().time() < deadline:
            try:
                # Read one line with a short inner timeout.  A 5-second wait
                # is enough to catch slow banners while not hanging indefinitely
                # if the server skips the prompt entirely.
                line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                text = line.decode("utf-8", errors="replace").lower()
                logger.debug("Server: %s", text.strip())

                if any(kw in text for kw in _LOGIN_KEYWORDS):
                    # Recognised a prompt – send callsign and we're done.
                    await self._send(self.callsign)
                    sent = True

            except asyncio.TimeoutError:
                # 5-second inner timeout expired with no server data.
                # The server probably started streaming spots without prompting
                # (rare but observed in AR-Cluster nodes).  Send callsign now.
                await self._send(self.callsign)
                sent = True

        if not sent:
            # Outer deadline expired without detecting a prompt.  This is a
            # last-resort send; the server may or may not accept it.
            await self._send(self.callsign)

    # ── Raw line stream ────────────────────────────────────────────────────────

    async def read_lines(self) -> AsyncIterator[str]:
        """Yield decoded lines from the cluster indefinitely.

        Each yielded string is one server line with trailing CR/LF stripped.
        The generator exits when:
          - The server closes the connection (readline returns b'').
          - A ConnectionError or OSError is raised.
          - self._connected is set to False (via disconnect()).

        Keepalive mechanism:
        If the server sends no data for _LINE_TIMEOUT seconds, we send
        "sh/dx 1" to reset any server-side idle timer and avoid silent
        disconnection by NAT gateways.  The keepalive response (one spot
        line) flows through the normal yield path.
        """
        assert self._reader is not None, "Not connected – call connect() first"

        while self._connected:
            try:
                # Wait up to _LINE_TIMEOUT seconds for the next server line.
                # readline() returns an empty bytes object when the server
                # closes the connection gracefully (TCP FIN received).
                raw = await asyncio.wait_for(self._reader.readline(), timeout=_LINE_TIMEOUT)
                if not raw:
                    # Empty read = TCP connection closed by the server.
                    # We log it and break cleanly; SpotFeed will reconnect.
                    logger.info("Server closed connection to %s", self.host)
                    break

                # Decode as UTF-8, replacing any unrecognised bytes with '?'
                # (some clusters send ISO-8859-1 characters in callsign comments).
                # rstrip("\r\n") removes the line terminator; some servers send
                # \r\n (Windows-style), others just \n.
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

            except asyncio.TimeoutError:
                # No server data in the last _LINE_TIMEOUT seconds.
                # Send a keepalive command to prove the link is still alive.
                logger.debug("Keepalive → %s", self.host)
                await self._send(_KEEPALIVE_CMD)

            except (ConnectionError, OSError) as exc:
                # Hard network error (e.g. ECONNRESET, ENETUNREACH).
                # Log it and break out; the caller will handle reconnection.
                logger.error("Connection error: %s", exc)
                break

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """Write a line to the server followed by CRLF and flush.

        DXCluster servers use classic telnet line discipline, so CRLF (\r\n)
        is required as the line terminator (plain \n may be silently dropped
        by some older server implementations).

        drain() yields control to the event loop until the OS socket send
        buffer has enough space, preventing us from filling the buffer with
        keepalives faster than the server can consume them.
        """
        if self._writer:
            self._writer.write((text + "\r\n").encode())
            await self._writer.drain()

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "DXClusterClient":
        """Connect on entry to the async with block."""
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        """Disconnect on exit from the async with block (normal or exceptional)."""
        await self.disconnect()
