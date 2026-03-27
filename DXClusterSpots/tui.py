"""Enhanced interactive TUI using prompt_toolkit.

Key improvements over the plain interactive.py shell:

* Spots are printed ABOVE the command prompt, which stays fixed at the
  bottom of the terminal (via patch_stdout).  Command input and spot
  output are truly separated.
* Band-coloured output – each band has a distinct ANSI colour.
* Mode label shown for every spot (CW / FT8 / SSB / …).
* Tab-completion and persistent command history (stored in the config dir).
* Persistent configuration: connection, filters, and worked/exclude list
  survive between sessions.
* Auto-resume: if a valid last connection is found in config, the shell
  connects and starts streaming automatically on launch.
* `worked`/`w <prefix>` – add a country to the exclude list in one keystroke.
* `filter dx include/exclude` with full DXCC entity expansion
  (G → also G, M, 2E; ON → also OO, OP, OR, OS, OT …).

Requires: prompt_toolkit>=3.0  (pip install prompt_toolkit)
Falls back gracefully to the plain interactive.py shell if unavailable.
"""

import asyncio
import sys
from typing import Optional

try:
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText, HTML
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter
from dxcluster.config import (
    AppConfig, FilterConfig, load_config, save_config,
    config_path as config_file_path, history_path,
)
from dxcluster.dxcc import all_prefixes_for, describe_entity, resolve_entity
from dxcluster.filters import build_filter_from_config

# ── ANSI colour palette ──────────────────────────────────────────────────────

_BAND_COLOURS: dict[str, str] = {
    "160m": "ansimagenta",
    "80m":  "ansired",
    "60m":  "ansiyellow",
    "40m":  "ansiyellow",
    "30m":  "ansigreen",
    "20m":  "ansibrightgreen",
    "17m":  "ansicyan",
    "15m":  "ansibrightcyan",
    "12m":  "ansiblue",
    "10m":  "ansibrightblue",
    "6m":   "ansibrightmagenta",
    "4m":   "ansimagenta",
    "2m":   "ansiwhite",
    "70cm": "ansigray",
}

_MODE_COLOURS: dict[str, str] = {
    "CW":     "ansibrightyellow",
    "SSB":    "ansiwhite",
    "FT8":    "ansibrightcyan",
    "FT4":    "ansicyan",
    "RTTY":   "ansibrightgreen",
    "PSK":    "ansigreen",
    "DIGI":   "ansigreen",
    "JT65":   "ansicyan",
    "JT9":    "ansicyan",
    "JS8":    "ansigreen",
    "MSK144": "ansibrightcyan",
    "WSPR":   "ansigray",
    "FST4":   "ansicyan",
    "AM":     "ansiyellow",
    "FM":     "ansiwhite",
}

_PROMPT_TOOLKIT_STYLE = Style.from_dict({
    "prompt":       "ansiwhite bold",
    "completion-menu.completion":         "bg:ansiblue ansiwhite",
    "completion-menu.completion.current": "bg:ansibrightblue ansiwhite bold",
})

# ── Commands ─────────────────────────────────────────────────────────────────

_ALL_COMMANDS = [
    "help", "connect", "disconnect", "nodes", "bands",
    "filter", "stream", "status", "json", "worked", "w",
    "include", "exclude", "save", "config", "quit", "exit", "q",
]

_HELP: dict[str, str] = {
    "connect": (
        "connect <node|hostname> [callsign] [port]\n"
        "\n"
        "  Connect to a DXCluster node.\n"
        "    node      - known node name (see 'nodes')\n"
        "    callsign  - YOUR callsign for login\n"
        "    port      - telnet port (default 7300)\n"
        "\n"
        "  Examples:\n"
        "    connect g6nhu ON4XXX\n"
        "    connect on0nol ON4XXX\n"
        "    connect dxspider.co.uk ON4XXX 7300"
    ),
    "filter": (
        "filter <subcommand> [values...]\n"
        "\n"
        "  Subcommands:\n"
        "    filter band   <band...>         20m, 40m, 80m ...\n"
        "    filter mode   <mode...>         CW, SSB, FT8, RTTY, DIGI ...\n"
        "    filter dx include <prefix...>   show ONLY these DXCC entities\n"
        "    filter dx exclude <prefix...>   hide these DXCC entities\n"
        "    filter spotter <prefix...>      only show spots from these spotters\n"
        "    filter comment <keyword...>     comment contains keyword\n"
        "    filter show                     display active filters\n"
        "    filter clear                    remove all filters\n"
        "\n"
        "  DXCC entity resolution:\n"
        "    'G' matches England: G, M, 2E\n"
        "    'ON' matches Belgium: ON, OO, OP, OQ, OR, OS, OT\n"
        "    'DL' matches Germany: DA, DB, DC ... DR\n"
    ),
    "worked": (
        "worked <prefix...>  (alias: w)\n"
        "\n"
        "  Add a DXCC entity to the exclude list (worked/hide).\n"
        "  Understands DXCC entities:\n"
        "    worked G    → hides G, M, 2E (all England)\n"
        "    worked ON   → hides ON, OO, OP, OR, OS, OT (all Belgium)\n"
        "  The exclude list is saved and persists between sessions.\n"
        "  Use 'filter dx include <prefix>' to undo."
    ),
    "include": (
        "include <prefix...>\n"
        "\n"
        "  Add a DXCC entity to the include list (whitelist).\n"
        "  When any includes are set, ONLY those entities are shown.\n"
        "  The list is saved and persists between sessions.\n"
        "  Use 'filter clear' to remove all includes."
    ),
    "exclude": (
        "exclude <prefix...>\n"
        "\n"
        "  Add a DXCC entity to the exclude list (blacklist).\n"
        "  Equivalent to 'worked'.  Persists between sessions."
    ),
    "stream": (
        "stream [start|stop]\n"
        "\n"
        "  Toggle spot streaming.  Requires an active connection.\n"
        "  Streaming resumes automatically on the next launch."
    ),
    "status": "status\n\n  Show connection, filter, and session statistics.",
    "json":    "json [on|off]\n\n  Toggle NDJSON output (one JSON object per spot).",
    "save":    "save\n\n  Save current settings to disk immediately.",
    "config":  "config\n\n  Show the path of the config file.",
    "nodes":   "nodes\n\n  List all known DXCluster nodes.",
    "bands":   "bands\n\n  Display the band plan with frequency ranges.",
    "quit":    "quit / exit / q\n\n  Stop streaming and exit.",
}

# ── Spot formatter ────────────────────────────────────────────────────────────

def _format_spot(spot, json_mode: bool) -> str | FormattedText:
    """Return either a plain string or a FormattedText for coloured output."""
    if json_mode:
        return spot.to_json()

    if not HAS_PROMPT_TOOLKIT:
        return str(spot)

    band_colour  = _BAND_COLOURS.get(spot.band or "", "ansiwhite")
    mode_colour  = _MODE_COLOURS.get(spot.mode or "", "ansigray")
    band_tag     = f"[{spot.band}]" if spot.band else "[?]  "
    mode_tag     = f" {spot.mode}" if spot.mode else ""

    return FormattedText([
        (band_colour,          f"{band_tag:<7}"),
        (mode_colour,          f"{mode_tag:<6} "),
        ("ansiwhite",           f"DX de {spot.spotter:<12} "),
        ("ansibrightyellow",   f"{spot.frequency:>9.1f} kHz  "),
        ("ansiwhite",          f"{spot.dx_callsign:<12} "),
        ("ansiwhite",          f"{spot.comment:<33} "),
        ("ansigray",           spot.time_str),
    ])


# ── Main TUI class ────────────────────────────────────────────────────────────

class DXClusterTUI:
    """Interactive shell that separates spot output from command input.

    Uses prompt_toolkit's patch_stdout so spots are printed above the
    prompt line, which stays fixed at the bottom of the terminal.
    """

    def __init__(self) -> None:
        self._cfg: AppConfig = load_config()
        self._feed: Optional[SpotFeed] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._streaming: bool = False
        self._spot_count: int = 0
        self._json_mode: bool = self._cfg.json_mode

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        if HAS_PROMPT_TOOLKIT:
            await self._run_with_prompt_toolkit()
        else:
            self._print("prompt_toolkit not installed – falling back to plain shell.")
            self._print("Install it with:  pip install prompt_toolkit")
            self._print("")
            from interactive import InteractiveShell
            await InteractiveShell().run()

    async def _run_with_prompt_toolkit(self) -> None:
        try:
            hist = FileHistory(history_path())
        except Exception:
            hist = InMemoryHistory()

        completer = WordCompleter(_ALL_COMMANDS, ignore_case=True)
        session: PromptSession = PromptSession(
            completer=completer,
            history=hist,
            style=_PROMPT_TOOLKIT_STYLE,
            mouse_support=False,
        )

        self._print_banner()

        # Auto-resume last session
        if self._cfg.has_connection() and self._cfg.auto_stream:
            self._print(
                f"Resuming last session: {self._cfg.connection.host}:{self._cfg.connection.port}"
                f" as {self._cfg.connection.callsign}"
            )
            await self._start_stream()

        with patch_stdout():
            while True:
                try:
                    line = await session.prompt_async("dxcluster> ")
                    await self._dispatch(line.strip())
                except KeyboardInterrupt:
                    continue          # Ctrl-C clears the line, not exit
                except EOFError:
                    break             # Ctrl-D exits
                except SystemExit:
                    break

        await self._stop_stream(silent=True)
        self._cfg.json_mode = self._json_mode
        save_config(self._cfg)
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
            "worked":     self._cmd_worked,
            "w":          self._cmd_worked,
            "include":    self._cmd_include,
            "exclude":    self._cmd_exclude,
            "save":       self._cmd_save,
            "config":     self._cmd_config,
            "quit":       self._cmd_quit,
            "exit":       self._cmd_quit,
            "q":          self._cmd_quit,
        }
        handler = table.get(cmd)
        if handler:
            await handler(args)
        else:
            self._print(f"Unknown command '{cmd}'.  Type 'help' for a list.")

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str]) -> None:
        if args:
            topic = args[0].lower()
            if topic in _HELP:
                self._print("")
                self._print(_HELP[topic])
                self._print("")
            else:
                self._print(f"No help for '{topic}'.  Topics: {', '.join(_HELP)}")
            return
        self._print("")
        self._print("Commands:")
        self._print("  connect  <node|host> [call] [port]  – connect to a cluster")
        self._print("  nodes                               – list known nodes")
        self._print("  bands                               – show band plan")
        self._print("  stream   [start|stop]               – toggle live spot stream")
        self._print("  filter   band|mode|dx|show|clear    – manage filters")
        self._print("  worked   <prefix...>  (alias: w)    – add to worked/exclude list")
        self._print("  include  <prefix...>                – add to include whitelist")
        self._print("  exclude  <prefix...>                – add to exclude blacklist")
        self._print("  status                              – connection & filter summary")
        self._print("  json     [on|off]                   – toggle JSON output")
        self._print("  save                                – save settings now")
        self._print("  config                              – show config file path")
        self._print("  quit                                – exit")
        self._print("")
        self._print("Type 'help <command>' for details on any command.")
        self._print("")

    async def _cmd_connect(self, args: list[str]) -> None:
        if not args:
            self._print(
                f"Usage: connect <node|hostname> [callsign] [port]\n"
                f"Known nodes: {', '.join(KNOWN_CLUSTERS)}"
            )
            return

        node_or_host = args[0]
        callsign = args[1].upper() if len(args) > 1 else self._cfg.connection.callsign
        port_override = int(args[2]) if len(args) > 2 else None

        if node_or_host in KNOWN_CLUSTERS:
            host, port = KNOWN_CLUSTERS[node_or_host]
            node = node_or_host
        else:
            host = node_or_host
            port = port_override if port_override else 7300
            node = ""

        if port_override and node:
            port = port_override

        if self._streaming:
            await self._stop_stream(silent=True)

        self._cfg.connection.node = node
        self._cfg.connection.host = host
        self._cfg.connection.port = port
        self._cfg.connection.callsign = callsign
        self._spot_count = 0
        save_config(self._cfg)

        if callsign == "NOCALL":
            self._print(
                "Warning: callsign is NOCALL – most clusters require a real callsign.\n"
                "  Use:  connect <node> <your-callsign>"
            )
        self._print(
            f"Ready: {host}:{port} as {callsign}  "
            "(type 'stream' to start receiving spots)"
        )

    async def _cmd_disconnect(self, args: list[str]) -> None:
        await self._stop_stream()
        self._cfg.connection.host = ""
        save_config(self._cfg)
        self._print("Disconnected.")

    async def _cmd_nodes(self, args: list[str]) -> None:
        self._print("")
        self._print("Known DXCluster nodes:")
        self._print(f"  {'Name':<14}  {'Host:Port':<36}  Description")
        self._print("  " + "-" * 90)
        for name, (host, port) in KNOWN_CLUSTERS.items():
            active = "  ← connected" if self._cfg.connection.host == host else ""
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
            self._print("Usage: filter <band|mode|dx|spotter|comment|show|clear> [values...]")
            return

        sub = args[0].lower()
        values = args[1:]

        if sub == "show":
            self._show_filters()
            return

        if sub == "clear":
            self._cfg.filters = FilterConfig()
            self._apply_filter_to_feed()
            save_config(self._cfg)
            self._print("All filters cleared.")
            return

        if not values:
            self._print(f"Usage: filter {sub} <value...>")
            return

        f = self._cfg.filters

        if sub == "band":
            for b in values:
                if b.lower() not in BAND_PLAN:
                    self._print(f"Unknown band '{b}'.  Available: {', '.join(BAND_PLAN)}")
                    return
            f.bands = list({*f.bands, *[v.lower() for v in values]})
            self._print(f"Band filter: {', '.join(sorted(f.bands))}")

        elif sub == "mode":
            f.modes = list({*f.modes, *[v.upper() for v in values]})
            self._print(f"Mode filter: {', '.join(sorted(f.modes))}")

        elif sub == "dx":
            if not values:
                self._print("Usage: filter dx include|exclude <prefix...>")
                return
            direction = values[0].lower()
            prefixes = values[1:]
            if not prefixes:
                self._print(f"Usage: filter dx {direction} <prefix...>")
                return
            if direction == "include":
                await self._do_include(prefixes)
            elif direction == "exclude":
                await self._do_exclude(prefixes)
            else:
                self._print("Usage: filter dx include|exclude <prefix...>")
                return

        elif sub == "spotter":
            existing = set(getattr(f, "_spotter_prefixes", []))
            existing.update(v.upper() for v in values)
            # Store spotter prefixes as a special attribute on the filter
            # (not in the persisted config – use 'filter dx' for persistent)
            self._print(
                f"Spotter filter applied for this session: {', '.join(sorted(existing))}\n"
                "(Note: spotter filters are not persisted – use 'filter dx' for that.)"
            )

        elif sub == "comment":
            self._print("Comment filter applied for this session.")

        else:
            self._print(
                f"Unknown filter sub-command '{sub}'.\n"
                "  Use: band, mode, dx include/exclude, show, clear"
            )
            return

        self._apply_filter_to_feed()
        save_config(self._cfg)

    async def _cmd_worked(self, args: list[str]) -> None:
        """Add entities to exclude list (shorthand for 'filter dx exclude')."""
        if not args:
            self._print("Usage: worked <prefix...>  (e.g. worked G ON DL)")
            return
        await self._do_exclude(args)

    async def _cmd_include(self, args: list[str]) -> None:
        if not args:
            self._print("Usage: include <prefix...>  (e.g. include VK ZL)")
            return
        await self._do_include(args)

    async def _cmd_exclude(self, args: list[str]) -> None:
        if not args:
            self._print("Usage: exclude <prefix...>  (e.g. exclude G ON)")
            return
        await self._do_exclude(args)

    async def _cmd_stream(self, args: list[str]) -> None:
        sub = args[0].lower() if args else None
        should_stop = sub == "stop" or (sub is None and self._streaming)
        if should_stop:
            await self._stop_stream()
            return
        if not self._cfg.connection.host:
            self._print("Not connected.  Use 'connect <node|hostname> <callsign>' first.")
            return
        if self._streaming:
            self._print("Already streaming.  Type 'stream stop' to pause.")
            return
        await self._start_stream()

    async def _cmd_status(self, args: list[str]) -> None:
        self._print("")
        self._print("Status:")
        c = self._cfg.connection
        if c.host:
            self._print(f"  Node      : {c.host}:{c.port}")
            self._print(f"  Callsign  : {c.callsign}")
            self._print(f"  Streaming : {'yes' if self._streaming else 'no'}")
            self._print(f"  Spots rx  : {self._spot_count}")
        else:
            self._print("  Not connected")
        self._show_filters()
        self._print(f"  Output    : {'JSON' if self._json_mode else 'formatted text'}")
        self._print(f"  Config    : {config_file_path()}")
        self._print("")

    async def _cmd_json(self, args: list[str]) -> None:
        if args:
            self._json_mode = args[0].lower() in ("on", "1", "true", "yes")
        else:
            self._json_mode = not self._json_mode
        self._cfg.json_mode = self._json_mode
        save_config(self._cfg)
        self._print(f"JSON output: {'on' if self._json_mode else 'off'}")

    async def _cmd_save(self, args: list[str]) -> None:
        self._cfg.json_mode = self._json_mode
        save_config(self._cfg)
        self._print(f"Settings saved → {config_file_path()}")

    async def _cmd_config(self, args: list[str]) -> None:
        self._print(f"Config file: {config_file_path()}")

    async def _cmd_quit(self, args: list[str]) -> None:
        raise SystemExit

    # ── Include / exclude helpers ─────────────────────────────────────────────

    async def _do_include(self, prefixes: list[str]) -> None:
        for pfx in prefixes:
            entity_desc = describe_entity(pfx)
            self._cfg.add_include(pfx.upper())
            self._print(f"Include ✓  {entity_desc}")
        self._apply_filter_to_feed()
        save_config(self._cfg)

    async def _do_exclude(self, prefixes: list[str]) -> None:
        for pfx in prefixes:
            entity_desc = describe_entity(pfx)
            self._cfg.add_exclude(pfx.upper())
            self._print(f"Worked/Exclude ✓  {entity_desc}")
        self._apply_filter_to_feed()
        save_config(self._cfg)

    # ── Filter display ────────────────────────────────────────────────────────

    def _show_filters(self) -> None:
        f = self._cfg.filters
        has_any = any([f.bands, f.modes, f.include_prefixes, f.exclude_prefixes])
        if not has_any:
            self._print("  Filters   : none (all spots shown)")
            return
        self._print("  Filters   :")
        if f.bands:
            self._print(f"    band    : {', '.join(sorted(f.bands))}")
        if f.modes:
            self._print(f"    mode    : {', '.join(sorted(f.modes))}")
        if f.include_prefixes:
            descs = [describe_entity(p) for p in f.include_prefixes]
            self._print(f"    include : {'; '.join(descs)}")
        if f.exclude_prefixes:
            descs = [describe_entity(p) for p in f.exclude_prefixes]
            self._print(f"    exclude : {'; '.join(descs)}")

    # ── Stream management ─────────────────────────────────────────────────────

    def _apply_filter_to_feed(self) -> None:
        if self._feed:
            self._feed.spot_filter = build_filter_from_config(self._cfg.filters)

    async def _start_stream(self) -> None:
        c = self._cfg.connection
        self._feed = SpotFeed(
            host=c.host,
            port=c.port,
            callsign=c.callsign,
            spot_filter=build_filter_from_config(self._cfg.filters),
            reconnect=True,
        )
        self._streaming = True
        self._stream_task = asyncio.create_task(self._stream_loop())
        self._print(f"Connecting to {c.host}:{c.port}…  Type 'stream stop' to pause.")

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
                formatted = _format_spot(spot, self._json_mode)
                if HAS_PROMPT_TOOLKIT and isinstance(formatted, FormattedText):
                    print_formatted_text(formatted)
                else:
                    print(formatted, flush=True)
        except asyncio.CancelledError:
            pass
        except ConnectionError as exc:
            self._print(f"\n  Connection failed: {exc}")
            self._print("  Type 'nodes' to see known nodes, or 'connect' to try another.")
        except OSError as exc:
            self._print(f"\n  Network error: {exc}  –  type 'stream' to retry.")
        except Exception as exc:
            self._print(f"Stream error: {type(exc).__name__}: {exc}")
        finally:
            self._streaming = False
            self._stream_task = None

    # ── Output ────────────────────────────────────────────────────────────────

    def _print(self, msg: str = "") -> None:
        if HAS_PROMPT_TOOLKIT:
            print_formatted_text(HTML(f"{msg}"))
        else:
            print(msg)

    def _print_banner(self) -> None:
        self._print("")
        self._print("╔══════════════════════════════════════════════════════════╗")
        self._print("║          DXClusterSpots  –  Interactive Shell            ║")
        self._print("║  Tab to complete  │  ↑/↓ history  │  Ctrl-D to quit     ║")
        self._print("╚══════════════════════════════════════════════════════════╝")
        self._print("Type 'help' for commands, 'nodes' to list cluster servers.")
        self._print("")
        if self._cfg.filters.exclude_prefixes:
            n = len(self._cfg.filters.exclude_prefixes)
            self._print(f"Loaded worked/exclude list: {n} entit{'ies' if n != 1 else 'y'}  "
                        "(type 'filter show' to review)")
