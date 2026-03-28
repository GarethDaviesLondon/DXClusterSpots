"""Interactive REPL shell for DXClusterSpots.

Launched automatically when no --node / --host argument is given, or
explicitly via: python DXClusterSpots.py --interactive

The shell supports:
  • Tab completion of command names
  • Command history (up/down arrows, via readline)
  • Live spot streaming that interleaves with typed commands
  • Composable filters built up incrementally
  • JSON output toggle for piping to downstream services

Architecture note
-----------------
This module is the *fallback* shell used when prompt_toolkit is NOT installed.
If prompt_toolkit is present, tui.py provides a richer split-pane TUI.

The async architecture here is more subtle than it first appears:

1.  The event loop runs a single asyncio task: `run()`.
2.  Inside `run()`, stdin is read inside a *thread-pool executor* via
    `loop.run_in_executor(None, input, _PROMPT)`.  This is necessary because
    the built-in `input()` function blocks the calling thread.  By running it
    in an executor thread we keep the event loop free to process spots from
    the streaming task.
3.  Commands typed by the user are put into an `asyncio.Queue`.
4.  The main loop reads from the queue with a short timeout (0.2 s) so it
    stays responsive to streaming spots without busy-waiting.
5.  The stream task (`_stream_loop`) runs concurrently on the same event loop
    and writes spots to stdout using `sys.stdout.write` with `\r` to clear
    the prompt line before each spot.

This architecture maps cleanly onto web-service scenarios: replace the
stdin executor with a WebSocket handler and the print calls with push
notifications to connected clients.
"""

import asyncio
import sys
from typing import Optional

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter

# ── readline (optional – gracefully absent on Windows without pyreadline) ─────
# readline provides tab-completion and command history (↑↓ navigation).
# On Windows it is NOT available in the standard library; users who want it
# must install `pyreadline3`.  We try to import it and set up completion,
# and fall back to a no-op if it is missing.
try:
    import readline as _readline

    def _setup_readline(commands: list[str]) -> None:
        """Configure tab-completion for the given list of command names."""
        def _complete(text: str, state: int) -> Optional[str]:
            # The completer is called repeatedly with increasing `state` values
            # (0, 1, 2, …) until it returns None.  On each call it returns the
            # next matching completion.  This is the GNU readline protocol.
            options = [c for c in commands if c.startswith(text)]
            return options[state] if state < len(options) else None

        _readline.set_completer(_complete)
        # "tab: complete" binds the Tab key to the completer function.
        _readline.parse_and_bind("tab: complete")
        # Use space and tab as delimiters (not letters/digits) so partial
        # command names are completed as whole words.
        _readline.set_completer_delims(" \t")

except ImportError:
    # readline is not available on this platform.  Define a no-op stub so the
    # rest of the code can call _setup_readline() unconditionally.
    def _setup_readline(_commands: list[str]) -> None:
        pass  # readline not available; tab-completion silently disabled

# ── Command metadata ──────────────────────────────────────────────────────────

# The flat list of top-level command names used for tab-completion.
_COMMANDS = [
    "help", "connect", "disconnect", "nodes", "bands",
    "filter", "stream", "status", "json", "quit", "exit", "q",
]

# Detailed help text for individual commands, accessed via `help <command>`.
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
        "    filter mode   <mode...>   - accept only these modes (CW, SSB, FT8 …)\n"
        "                                 e.g.  filter mode CW FT8\n"
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

_PROMPT = "dxcluster> "  # The prompt shown to the user when waiting for input.

_BANNER = """\
╔══════════════════════════════════════════════════════════╗
║          DXClusterSpots  –  Interactive Shell            ║
║  Type 'help' for commands  |  Tab to complete  |  73!   ║
╚══════════════════════════════════════════════════════════╝
"""


# ── Shell class ───────────────────────────────────────────────────────────────

class InteractiveShell:
    """Async interactive shell for DXClusterSpots.

    State is held as instance variables rather than globals so that multiple
    shell instances could in principle coexist (e.g. in tests), and so the
    class is straightforward to unit-test by calling _dispatch() directly.
    """

    def __init__(self) -> None:
        # ── Connection state ──────────────────────────────────────────────────
        self._host: Optional[str] = None     # resolved hostname, or None
        self._port: int = 7300               # telnet port (default 7300)
        self._callsign: str = "NOCALL"       # callsign used for cluster login
        self._connected: bool = False        # True once 'connect' has been called

        # ── Filter state ──────────────────────────────────────────────────────
        self._filter: Optional[SpotFilter] = None  # active filter, or None
        self._filter_desc: list[str] = []          # human-readable filter descriptions

        # ── Stream state ──────────────────────────────────────────────────────
        self._feed: Optional[SpotFeed] = None            # live SpotFeed, or None
        self._stream_task: Optional[asyncio.Task] = None # streaming coroutine task
        self._streaming: bool = False                    # True while stream is running

        # ── Output state ──────────────────────────────────────────────────────
        self._json_mode: bool = False  # True → print spots as JSON
        self._spot_count: int = 0      # total spots received this session
        self._running: bool = True     # False → exit the main loop

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point: print banner, set up completion, start I/O loop.

        The loop runs until self._running is False (set by 'quit') or until
        KeyboardInterrupt (Ctrl-C).  Cleanup (stop stream, print goodbye) is
        guaranteed by the try/finally block.
        """
        print(_BANNER)
        _setup_readline(_COMMANDS)

        loop = asyncio.get_event_loop()
        # Commands typed by the user travel through this queue from the
        # stdin-reader thread to the async main loop.
        queue: asyncio.Queue[str] = asyncio.Queue()

        async def _read_stdin() -> None:
            """Background coroutine that blocks on stdin in a thread executor.

            input() is a blocking call that cannot be awaited directly, so we
            run it in asyncio's thread pool (run_in_executor with executor=None
            uses the default ThreadPoolExecutor).  The result is placed on the
            queue so the main loop can process it asynchronously without
            interrupting any concurrent spot streaming.

            EOFError occurs when stdin is closed (e.g. Ctrl-D or piped input
            ending).  We treat it as a quit signal.
            """
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
                    # Poll the queue with a short timeout rather than blocking
                    # forever.  The 0.2 s timeout lets the loop stay responsive
                    # to self._running becoming False even between keystrokes.
                    line = await asyncio.wait_for(queue.get(), timeout=0.2)
                    await self._dispatch(line.strip())
                except asyncio.TimeoutError:
                    continue  # no command yet; loop back and poll again
        except KeyboardInterrupt:
            pass  # Ctrl-C exits gracefully
        finally:
            reader.cancel()
            await self._stop_stream(silent=True)
            print("\nGoodbye. 73 de DXClusterSpots")

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def _dispatch(self, line: str) -> None:
        """Parse a command line and call the appropriate handler.

        The dispatch table maps lower-cased command words to handler coroutines.
        Using a dict is idiomatic Python (avoids a long if/elif chain) and
        makes it easy to add or rename commands in one place.

        Args:
            line: A single command string from the user, already stripped.
        """
        if not line:
            return  # empty input – user pressed Enter on a blank line

        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        # Dispatch table: command keyword → handler method.
        # Multiple keywords can point to the same handler (e.g. quit/exit/q).
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
        """Display general help or detailed help for a specific command."""
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

        # No topic given – print the command summary list.
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
        """Set up connection parameters and print a ready message.

        Note that 'connect' does NOT open the TCP connection immediately.
        It saves the host/callsign/port and then the user must type 'stream'
        to actually start receiving spots.  This two-step approach allows the
        user to configure filters before any spots arrive.

        If a stream is already running, it is stopped first so the connection
        parameters can be safely replaced.
        """
        if not args:
            self._print(
                f"Usage: connect <node|hostname> [callsign] [port]\n"
                f"Known nodes: {', '.join(KNOWN_CLUSTERS)}"
            )
            return

        node_or_host = args[0]
        # Upper-case the callsign because cluster software is case-sensitive
        # and amateur callsigns are conventionally written in upper case.
        callsign = args[1].upper() if len(args) > 1 else "NOCALL"
        port_arg = args[2] if len(args) > 2 else None

        if node_or_host in KNOWN_CLUSTERS:
            # Named node: look up host and default port from the table.
            host, port = KNOWN_CLUSTERS[node_or_host]
        else:
            # Custom hostname: use the supplied port or default 7300.
            host = node_or_host
            port = int(port_arg) if port_arg else 7300

        # Allow overriding a named node's default port: 'connect g6nhu G0ABC 7373'
        if port_arg and node_or_host in KNOWN_CLUSTERS:
            port = int(port_arg)

        if self._streaming:
            # Stop any running stream before changing connection parameters.
            # silent=True suppresses the "Stream stopped" message to avoid
            # cluttering the output when reconnecting.
            await self._stop_stream(silent=True)

        self._host = host
        self._port = port
        self._callsign = callsign
        self._connected = True
        self._spot_count = 0  # reset spot counter for the new connection
        self._print(
            f"Ready to connect: {host}:{port} as {callsign}\n"
            f"Type 'stream' to start receiving spots."
        )

    async def _cmd_disconnect(self, args: list[str]) -> None:
        """Stop the stream (if running) and mark as disconnected."""
        if self._streaming:
            await self._stop_stream()
        self._host = None
        self._connected = False
        self._print("Disconnected.")

    async def _cmd_nodes(self, args: list[str]) -> None:
        """Print a formatted table of all known cluster nodes."""
        self._print("")
        self._print("Known DXCluster nodes:")
        self._print(f"  {'Name':<14}  {'Host:Port':<36}  Description")
        self._print("  " + "-" * 90)
        for name, (host, port) in KNOWN_CLUSTERS.items():
            # Mark the currently connected node for easy identification.
            active = "  ← connected" if (self._connected and self._host == host) else ""
            desc = CLUSTER_DESCRIPTIONS.get(name, "")
            self._print(f"  {name:<14}  {host + ':' + str(port):<36}  {desc}{active}")
        self._print("")

    async def _cmd_bands(self, args: list[str]) -> None:
        """Print the ITU Region 1 band plan with frequency ranges."""
        self._print("")
        self._print("Band plan (ITU Region 1):")
        for band, (low, high) in BAND_PLAN.items():
            self._print(f"  {band:<6}  {low:>10.1f} – {high:>10.1f} kHz")
        self._print("")

    async def _cmd_filter(self, args: list[str]) -> None:
        """Build up the active SpotFilter incrementally.

        Filters are cumulative (AND logic): each 'filter band/mode/dx/...'
        call adds a new predicate.  Use 'filter show' to review and
        'filter clear' to reset.

        Changes take effect immediately if a stream is running, because
        the SpotFeed's spot_filter attribute is updated in-place.
        """
        if not args:
            self._print(
                "Usage: filter <band|mode|dx|spotter|comment|show|clear> [values...]"
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
            # Propagate the cleared filter to the running feed immediately.
            if self._feed:
                self._feed.spot_filter = None
            self._print("All filters cleared.")
            return

        if not values:
            self._print(f"Usage: filter {sub} <value...>")
            return

        # Lazily create the SpotFilter the first time a predicate is added.
        if self._filter is None:
            self._filter = SpotFilter()

        if sub == "band":
            self._filter.band(*values)
            self._filter_desc.append(f"band:    {', '.join(v.lower() for v in values)}")
        elif sub == "mode":
            # Normalise to upper-case: "cw" and "ft8" become "CW" and "FT8"
            # to match the canonical mode names used by parse_mode().
            self._filter.mode(*[v.upper() for v in values])
            self._filter_desc.append(f"mode:    {', '.join(v.upper() for v in values)}")
        elif sub == "dx":
            # Simple raw prefix matching (callsign.startswith(prefix)).
            # This is different from the DXCC entity-aware dx_include/dx_exclude
            # in the TUI; the fallback shell uses the simpler variant.
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
                "Use: band, mode, dx, spotter, comment, show, clear"
            )
            return

        # Apply the updated filter to the running feed immediately so the
        # user doesn't need to stop and restart the stream.
        if self._feed:
            self._feed.spot_filter = self._filter

        total = len(self._filter_desc)
        self._print(
            f"Filter added.  {total} active filter(s) – "
            "type 'filter show' to review."
        )

    async def _cmd_stream(self, args: list[str]) -> None:
        """Toggle or explicitly start/stop the live spot stream.

        With no argument, behaves as a toggle: starts if stopped, stops if running.
        With 'start' or 'stop', acts as instructed.
        """
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
        """Print a summary of connection state, filters, and spot count."""
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
        """Toggle or explicitly set JSON output mode.

        With 'on'/'off'/'true'/'false': set explicitly.
        With no argument: toggle the current state.
        """
        if args:
            self._json_mode = args[0].lower() in ("on", "1", "true", "yes")
        else:
            self._json_mode = not self._json_mode
        self._print(f"JSON output: {'on' if self._json_mode else 'off'}")

    async def _cmd_quit(self, args: list[str]) -> None:
        """Signal the main loop to exit after the current iteration."""
        self._running = False

    # ── Stream management ─────────────────────────────────────────────────────

    async def _start_stream(self) -> None:
        """Create a SpotFeed and launch the background streaming task.

        The SpotFeed is created fresh each time streaming starts, so any
        filter changes made since the last stop are picked up automatically.

        We warn about NOCALL because almost all cluster nodes reject it and
        close the connection immediately, causing an infinite reconnect loop
        that is confusing without this warning.
        """
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
            reconnect=True,   # reconnect automatically on connection drops
        )
        self._streaming = True
        # create_task() schedules _stream_loop() as a concurrent asyncio task.
        # It runs alongside the stdin-reader task and the command dispatcher.
        self._stream_task = asyncio.create_task(self._stream_loop())
        self._print(
            f"Connecting to {self._host}:{self._port}…  "
            "Type 'stream stop' to pause."
        )

    async def _stop_stream(self, silent: bool = False) -> None:
        """Stop the streaming task and clean up.

        Args:
            silent: If True, suppress the "Stream stopped" message.  Used
                    internally when stopping before a reconnect or at exit.
        """
        if self._feed:
            self._feed.stop()  # sets SpotFeed._running = False
        if self._stream_task:
            self._stream_task.cancel()   # request cancellation
            try:
                await self._stream_task  # wait for cancellation to complete
            except asyncio.CancelledError:
                pass  # expected: task raised CancelledError as requested
            self._stream_task = None
        self._streaming = False
        self._feed = None
        if not silent:
            self._print(f"Stream stopped.  ({self._spot_count} spot(s) received.)")

    async def _stream_loop(self) -> None:
        """Consume spots from SpotFeed and write them to stdout.

        This coroutine runs as a concurrent task alongside the stdin reader.
        It writes each spot with a leading \r to erase the prompt line before
        the spot, then reprints the prompt after the spot so the user always
        has a clean input line.

        The various except clauses map to different failure scenarios:
          asyncio.CancelledError – normal stop via _stop_stream()
          ConnectionError        – DNS failure or permanent connection error
          OSError                – transient network error
          Exception              – unexpected programming error
        """
        try:
            async for spot in self._feed.spots():
                self._spot_count += 1
                line = spot.to_json() if self._json_mode else str(spot)
                # \r moves the cursor to the beginning of the current line,
                # overwriting the "dxcluster> " prompt.  This prevents spots
                # from appearing on the same line as a partially-typed command.
                # After the spot, the prompt is reprinted so the user can
                # continue typing.
                sys.stdout.write(f"\r{line}\n{_PROMPT}")
                sys.stdout.flush()
        except asyncio.CancelledError:
            # Normal stop – task cancelled by _stop_stream or app shutdown.
            pass
        except ConnectionError as exc:
            # DNS failure or other permanent connection error.
            # SpotFeed raises this when the hostname cannot be resolved
            # (socket.gaierror), which is not worth retrying until the user
            # corrects the hostname.
            self._print(f"")
            self._print(f"  Connection failed: {exc}")
            self._print(f"  Stream stopped.  Type 'nodes' to see known nodes,")
            self._print(f"  or 'connect <hostname> [callsign]' to try another.")
            self._connected = False
        except OSError as exc:
            # Transient network error (connection reset, timeout, etc.)
            # The user can type 'stream' to retry.
            self._print(f"")
            self._print(f"  Network error: {exc}")
            self._print(f"  Stream stopped.  Type 'stream' to retry.")
        except Exception as exc:
            # Unexpected error – print type and message for diagnosis.
            self._print(f"Stream error: {type(exc).__name__}: {exc}")
        finally:
            # Always clean up streaming state, even if an exception occurred.
            self._streaming = False
            self._stream_task = None

    # ── Output helper ──────────────────────────────────────────────────────────

    def _print(self, msg: str = "") -> None:
        """Print a message, clearing any dangling prompt on the current line.

        The \r moves the cursor to the start of the current line before
        writing, which erases the "dxcluster> " prompt that was printed by
        the last input() call.  Without this, messages would appear mid-line
        if the user hasn't pressed Enter yet.
        """
        sys.stdout.write(f"\r{msg}\n")
        sys.stdout.flush()
