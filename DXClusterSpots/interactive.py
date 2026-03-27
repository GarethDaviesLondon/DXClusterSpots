"""Interactive REPL shell for DXClusterSpots.

Launched automatically when no --node / --host argument is given, or
explicitly via: python DXClusterSpots.py --interactive

The shell supports:
  • Tab completion of command names
  • Command history (up/down arrows, via readline)
  • Live spot streaming that interleaves with typed commands
  • Composable filters built up incrementally
  • JSON output toggle for piping to downstream services
"""

import asyncio
import sys
from typing import Optional

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter

# ── readline (optional – gracefully absent on Windows without pyreadline) ────
try:
    import readline as _readline

    def _setup_readline(commands: list[str]) -> None:
        def _complete(text: str, state: int) -> Optional[str]:
            options = [c for c in commands if c.startswith(text)]
            return options[state] if state < len(options) else None

        _readline.set_completer(_complete)
        _readline.parse_and_bind("tab: complete")
        _readline.set_completer_delims(" \t")

except ImportError:
    def _setup_readline(_commands: list[str]) -> None:
        pass  # readline not available

# ── Help text ────────────────────────────────────────────────────────────────

_COMMANDS = [
    "help", "connect", "disconnect", "nodes", "bands",
    "filter", "stream", "status", "json", "quit", "exit", "q",
]

_HELP: dict[str, str] = {
    "connect": (
        "connect <node|hostname> [callsign] [port]\n"
        "\n"
        "  Connect to a DXCluster node.\n"
        "    node      - known node name (see 'nodes')\n"
        "    hostname  - any custom hostname\n"
        "    callsign  - your callsign for login  (default: NOCALL)\n"
        "    port      - telnet port              (default: 7300)\n"
        "\n"
        "  Examples:\n"
        "    connect gb7mbc\n"
        "    connect gb7mbc G0ABC\n"
        "    connect dxcluster.example.com G0ABC 7373"
    ),
    "disconnect": (
        "disconnect\n"
        "\n"
        "  Stop the current stream and disconnect from the cluster node."
    ),
    "nodes": (
        "nodes\n"
        "\n"
        "  List all known DXCluster nodes with their host and port."
    ),
    "bands": (
        "bands\n"
        "\n"
        "  Display the full amateur radio band plan (ITU Region 1).\n"
        "  Band names from this list are used with 'filter band'."
    ),
    "filter": (
        "filter <subcommand> [values...]\n"
        "\n"
        "  Manage spot filters.  Filters are additive (AND logic).\n"
        "\n"
        "  Subcommands:\n"
        "    filter band   <band...>    - accept only spots on these band(s)\n"
        "                                 e.g.  filter band 20m 40m\n"
        "    filter dx     <prefix...>  - DX callsign starts with prefix\n"
        "                                 e.g.  filter dx VK ZL\n"
        "    filter spotter <prefix...> - spotter callsign starts with prefix\n"
        "    filter comment <keyword...>- comment contains keyword (case-insensitive)\n"
        "    filter show                - display currently active filters\n"
        "    filter clear               - remove all filters"
    ),
    "stream": (
        "stream [start|stop]\n"
        "\n"
        "  Start or stop the live spot stream.\n"
        "  Calling 'stream' with no argument toggles the current state.\n"
        "  You must 'connect' before starting a stream."
    ),
    "status": (
        "status\n"
        "\n"
        "  Show the current connection, filter, and session statistics."
    ),
    "json": (
        "json [on|off]\n"
        "\n"
        "  Toggle NDJSON output mode.\n"
        "  When on, each spot is printed as a single JSON object per line –\n"
        "  useful for piping to a web service or log processor.\n"
        "  Calling 'json' with no argument toggles the current state."
    ),
    "quit": (
        "quit / exit / q\n"
        "\n"
        "  Stop any active stream and exit the shell."
    ),
}

_PROMPT = "dxcluster> "

_BANNER = """\
╔══════════════════════════════════════════════════════════╗
║          DXClusterSpots  –  Interactive Shell            ║
║  Type 'help' for commands  |  Tab to complete  |  73!   ║
╚══════════════════════════════════════════════════════════╝
"""


# ── Shell class ───────────────────────────────────────────────────────────────

class InteractiveShell:
    """Async interactive shell for DXClusterSpots.

    Architecture note
    -----------------
    Stdin is read in a thread-pool executor so it never blocks the event loop.
    Commands are queued via asyncio.Queue and processed on the main loop, which
    also runs the streaming task concurrently.  This design maps cleanly onto
    a web-service worker: replace the stdin reader with a WebSocket/SSE handler
    and the print calls with push notifications.
    """

    def __init__(self) -> None:
        self._host: Optional[str] = None
        self._port: int = 7300
        self._callsign: str = "NOCALL"
        self._connected: bool = False

        self._filter: Optional[SpotFilter] = None
        self._filter_desc: list[str] = []

        self._feed: Optional[SpotFeed] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._streaming: bool = False

        self._json_mode: bool = False
        self._spot_count: int = 0
        self._running: bool = True

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        print(_BANNER)
        _setup_readline(_COMMANDS)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def _read_stdin() -> None:
            while self._running:
                try:
                    line = await loop.run_in_executor(None, input, _PROMPT)
                    await queue.put(line)
                except EOFError:
                    await queue.put("quit")
                    break

        reader = asyncio.create_task(_read_stdin())

        try:
            while self._running:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=0.2)
                    await self._dispatch(line.strip())
                except asyncio.TimeoutError:
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            reader.cancel()
            await self._stop_stream(silent=True)
            print("\nGoodbye. 73 de DXClusterSpots")

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def _dispatch(self, line: str) -> None:
        if not line:
            return

        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        table = {
            "help":       self._cmd_help,
            "connect":    self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "nodes":      self._cmd_nodes,
            "bands":      self._cmd_bands,
            "filter":     self._cmd_filter,
            "stream":     self._cmd_stream,
            "status":     self._cmd_status,
            "json":       self._cmd_json,
            "quit":       self._cmd_quit,
            "exit":       self._cmd_quit,
            "q":          self._cmd_quit,
        }

        handler = table.get(cmd)
        if handler:
            await handler(args)
        else:
            self._print(
                f"Unknown command '{cmd}'. "
                f"Type 'help' for available commands."
            )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str]) -> None:
        if args:
            topic = args[0].lower()
            if topic in _HELP:
                self._print("")
                self._print(_HELP[topic])
                self._print("")
            else:
                self._print(
                    f"No help for '{topic}'.  "
                    f"Topics: {', '.join(_HELP)}"
                )
            return

        self._print("")
        self._print("Available commands:")
        self._print("  connect     connect to a DXCluster node")
        self._print("  disconnect  stop stream and disconnect")
        self._print("  nodes       list known cluster nodes")
        self._print("  bands       show band plan")
        self._print("  filter      manage spot filters (band, dx, spotter, comment)")
        self._print("  stream      start / stop live spot stream")
        self._print("  status      show connection and filter status")
        self._print("  json        toggle JSON / formatted-text output")
        self._print("  quit        exit the shell")
        self._print("")
        self._print("Type 'help <command>' for detailed help on any command.")
        self._print("")

    async def _cmd_connect(self, args: list[str]) -> None:
        if not args:
            self._print(
                f"Usage: connect <node|hostname> [callsign] [port]\n"
                f"Known nodes: {', '.join(KNOWN_CLUSTERS)}"
            )
            return

        node_or_host = args[0]
        callsign = args[1].upper() if len(args) > 1 else "NOCALL"
        port_arg = args[2] if len(args) > 2 else None

        if node_or_host in KNOWN_CLUSTERS:
            host, port = KNOWN_CLUSTERS[node_or_host]
        else:
            host = node_or_host
            port = int(port_arg) if port_arg else 7300

        if port_arg and node_or_host in KNOWN_CLUSTERS:
            port = int(port_arg)

        if self._streaming:
            await self._stop_stream(silent=True)

        self._host = host
        self._port = port
        self._callsign = callsign
        self._connected = True
        self._spot_count = 0
        self._print(
            f"Ready to connect: {host}:{port} as {callsign}\n"
            f"Type 'stream' to start receiving spots."
        )

    async def _cmd_disconnect(self, args: list[str]) -> None:
        if self._streaming:
            await self._stop_stream()
        self._host = None
        self._connected = False
        self._print("Disconnected.")

    async def _cmd_nodes(self, args: list[str]) -> None:
        self._print("")
        self._print("Known DXCluster nodes:")
        self._print(f"  {'Name':<14}  {'Host:Port':<36}  Description")
        self._print("  " + "-" * 90)
        for name, (host, port) in KNOWN_CLUSTERS.items():
            active = "  ← connected" if (self._connected and self._host == host) else ""
            desc = CLUSTER_DESCRIPTIONS.get(name, "")
            self._print(f"  {name:<14}  {host + ':' + str(port):<36}  {desc}{active}")
        self._print("")

    async def _cmd_bands(self, args: list[str]) -> None:
        self._print("")
        self._print("Band plan (ITU Region 1):")
        for band, (low, high) in BAND_PLAN.items():
            self._print(f"  {band:<6}  {low:>10.1f} – {high:>10.1f} kHz")
        self._print("")

    async def _cmd_filter(self, args: list[str]) -> None:
        if not args:
            self._print(
                "Usage: filter <band|dx|spotter|comment|show|clear> [values...]"
            )
            return

        sub = args[0].lower()
        values = args[1:]

        if sub == "show":
            if self._filter_desc:
                self._print("Active filters:")
                for d in self._filter_desc:
                    self._print(f"  + {d}")
            else:
                self._print("No filters active – all spots will be shown.")
            return

        if sub == "clear":
            self._filter = None
            self._filter_desc = []
            if self._feed:
                self._feed.spot_filter = None
            self._print("All filters cleared.")
            return

        if not values:
            self._print(f"Usage: filter {sub} <value...>")
            return

        if self._filter is None:
            self._filter = SpotFilter()

        if sub == "band":
            self._filter.band(*values)
            self._filter_desc.append(f"band:    {', '.join(v.lower() for v in values)}")
        elif sub == "dx":
            self._filter.dx_callsign_prefix(*values)
            self._filter_desc.append(f"dx-pfx:  {', '.join(v.upper() for v in values)}")
        elif sub == "spotter":
            self._filter.spotter_prefix(*values)
            self._filter_desc.append(f"spotter: {', '.join(v.upper() for v in values)}")
        elif sub == "comment":
            self._filter.comment_contains(*values)
            self._filter_desc.append(f"comment: {', '.join(values)}")
        else:
            self._print(
                f"Unknown filter type '{sub}'.  "
                "Use: band, dx, spotter, comment, show, clear"
            )
            return

        # Apply immediately to a running feed
        if self._feed:
            self._feed.spot_filter = self._filter

        total = len(self._filter_desc)
        self._print(
            f"Filter added.  {total} active filter(s) – "
            "type 'filter show' to review."
        )

    async def _cmd_stream(self, args: list[str]) -> None:
        sub = args[0].lower() if args else None

        should_stop = sub == "stop" or (sub is None and self._streaming)
        if should_stop:
            await self._stop_stream()
            return

        if not self._connected or not self._host:
            self._print(
                "Not connected.  Use 'connect <node|hostname> [callsign]' first."
            )
            return

        if self._streaming:
            self._print("Already streaming.  Type 'stream stop' to pause.")
            return

        await self._start_stream()

    async def _cmd_status(self, args: list[str]) -> None:
        self._print("")
        self._print("Status:")
        if self._connected and self._host:
            self._print(f"  Node      : {self._host}:{self._port}")
            self._print(f"  Callsign  : {self._callsign}")
            self._print(f"  Streaming : {'yes' if self._streaming else 'no'}")
            self._print(f"  Spots rx  : {self._spot_count}")
        else:
            self._print("  Not connected")

        if self._filter_desc:
            self._print("  Filters   :")
            for d in self._filter_desc:
                self._print(f"    + {d}")
        else:
            self._print("  Filters   : none (all spots shown)")

        self._print(f"  Output    : {'JSON' if self._json_mode else 'formatted text'}")
        self._print("")

    async def _cmd_json(self, args: list[str]) -> None:
        if args:
            self._json_mode = args[0].lower() in ("on", "1", "true", "yes")
        else:
            self._json_mode = not self._json_mode
        self._print(f"JSON output: {'on' if self._json_mode else 'off'}")

    async def _cmd_quit(self, args: list[str]) -> None:
        self._running = False

    # ── Stream management ─────────────────────────────────────────────────────

    async def _start_stream(self) -> None:
        if self._callsign == "NOCALL":
            self._print(
                "Warning: your callsign is set to NOCALL.\n"
                "  Most clusters reject NOCALL and will close the connection immediately.\n"
                "  Use:  connect <node> <your-callsign>  to reconnect with a real callsign."
            )
        self._feed = SpotFeed(
            host=self._host,
            port=self._port,
            callsign=self._callsign,
            spot_filter=self._filter,
            reconnect=True,
        )
        self._streaming = True
        self._stream_task = asyncio.create_task(self._stream_loop())
        self._print(
            f"Connecting to {self._host}:{self._port}…  "
            "Type 'stream stop' to pause."
        )

    async def _stop_stream(self, silent: bool = False) -> None:
        if self._feed:
            self._feed.stop()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        self._streaming = False
        self._feed = None
        if not silent:
            self._print(f"Stream stopped.  ({self._spot_count} spot(s) received.)")

    async def _stream_loop(self) -> None:
        try:
            async for spot in self._feed.spots():
                self._spot_count += 1
                line = spot.to_json() if self._json_mode else str(spot)
                # \r clears the prompt that was printed by input()
                sys.stdout.write(f"\r{line}\n{_PROMPT}")
                sys.stdout.flush()
        except asyncio.CancelledError:
            # Normal stop – task cancelled by _stop_stream or app shutdown.
            pass
        except ConnectionError as exc:
            # DNS failure or other permanent connection error.
            self._print(f"")
            self._print(f"  Connection failed: {exc}")
            self._print(f"  Stream stopped.  Type 'nodes' to see known nodes,")
            self._print(f"  or 'connect <hostname> [callsign]' to try another.")
            self._connected = False
        except OSError as exc:
            self._print(f"")
            self._print(f"  Network error: {exc}")
            self._print(f"  Stream stopped.  Type 'stream' to retry.")
        except Exception as exc:
            self._print(f"Stream error: {type(exc).__name__}: {exc}")
        finally:
            self._streaming = False
            self._stream_task = None

    # ── Output helper ─────────────────────────────────────────────────────────

    def _print(self, msg: str = "") -> None:
        """Print a message, clearing any dangling prompt on the current line."""
        sys.stdout.write(f"\r{msg}\n")
        sys.stdout.flush()
