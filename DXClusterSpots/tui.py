"""DXClusterSpots – split-pane terminal UI.

Layout (full-screen):
  ┌─────────────────────────────────────────────────────────────────┐
  │  SPOT OUTPUT PANE  (scrolling, coloured, auto-scroll to bottom) │
  │  [20m] FT8   DX de SP5XYZ      14074.0 kHz  4X4DK   cq cq     │
  │  ...                                                            │
  ├── ▶ streaming │ pi4cc:7300 (ON4KST) │ 42 spots │ band:20m ─────┤
  │ dxcluster> _                                                    │
  └─────────────────────────────────────────────────────────────────┘

Top pane  : spot output – auto-scrolls as spots arrive, keeps last 2 000 lines.
Status bar: live connection state, active filters, spot count.
Input pane: command prompt with tab-completion and persistent history.

Filters, connection settings, and the worked/exclude list all survive
between sessions (stored in the platform config directory).

Requires: prompt_toolkit>=3.0  (pip install prompt_toolkit)
Falls back to interactive.py if prompt_toolkit is unavailable.
"""

import asyncio
import shutil
import sys
from typing import Optional

try:
    from prompt_toolkit import Application
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import TextArea
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter
from dxcluster.config import (
    AppConfig, FilterConfig, load_config, save_config,
    config_path as config_file_path, history_path,
)
from dxcluster.dxcc import describe_entity
from dxcluster.filters import build_filter_from_config

# ── Colour palette ────────────────────────────────────────────────────────────

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

_STYLE = Style.from_dict({
    "status":     "bg:ansiblue ansiwhite bold",
    "separator":  "ansidarkgray",
    "completion-menu.completion":         "bg:ansiblue ansiwhite",
    "completion-menu.completion.current": "bg:ansibrightblue ansiwhite bold",
}) if HAS_PROMPT_TOOLKIT else None

# ── Commands ──────────────────────────────────────────────────────────────────

_ALL_COMMANDS = [
    "help", "connect", "disconnect", "nodes", "bands",
    "filter", "stream", "status", "json", "worked", "w",
    "include", "exclude", "save", "config", "quit", "exit", "q",
]

_HELP: dict[str, str] = {
    "connect": (
        "connect <node|hostname> [callsign] [port]\n"
        "\n"
        "  Connect to a DXCluster node.  Settings are saved automatically.\n"
        "    node      – known node name (see 'nodes')\n"
        "    callsign  – YOUR callsign for login\n"
        "    port      – telnet port (default 7300)\n"
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
        "    filter band   <band...>          20m, 40m, 80m …\n"
        "    filter mode   <mode...>          CW, SSB, FT8, RTTY, DIGI …\n"
        "    filter dx include <prefix...>    show ONLY these DXCC entities\n"
        "    filter dx exclude <prefix...>    hide these DXCC entities\n"
        "    filter show                      display active filters\n"
        "    filter clear                     remove all filters\n"
        "\n"
        "  DXCC entity resolution:\n"
        "    'G'  → England  : G, M, 2E\n"
        "    'ON' → Belgium  : ON, OO, OP, OQ, OR, OS, OT\n"
        "    'DL' → Germany  : DA, DB, DC … DR\n"
        "\n"
        "  Filters are saved and reloaded on next start."
    ),
    "worked": (
        "worked <prefix...>  (alias: w)\n"
        "\n"
        "  Add a DXCC entity to the exclude list.\n"
        "  Resolves full entity prefix set:\n"
        "    worked G    → hides G, M, 2E (England)\n"
        "    worked ON   → hides ON, OO, OP … (Belgium)\n"
        "  Saved automatically, persists between sessions.\n"
        "  Use 'filter dx include <prefix>' to undo."
    ),
    "stream": (
        "stream [start|stop]\n"
        "\n"
        "  Toggle spot streaming (requires active connection)."
    ),
    "status": "status\n\n  Show connection, filter, and session statistics.",
    "json":   "json [on|off]\n\n  Toggle NDJSON output (one JSON object per spot).",
    "nodes":  "nodes\n\n  List all known DXCluster nodes.",
    "bands":  "bands\n\n  Display the band plan with frequency ranges.",
    "save":   "save\n\n  Save current settings to disk immediately.",
    "config": "config\n\n  Show the path of the config file.",
    "quit":   "quit / exit / q\n\n  Stop streaming and exit.",
}

_MAX_OUTPUT_LINES = 2000  # lines kept in memory
_OUTPUT_PANE_OVERHEAD = 3  # status bar + separator + input line


# ── Spot formatter ────────────────────────────────────────────────────────────

def _format_spot_parts(spot) -> list:
    """Return a list of (style, text) tuples for one spot line."""
    band_colour = _BAND_COLOURS.get(spot.band or "", "ansiwhite")
    mode_colour = _MODE_COLOURS.get(spot.mode or "", "ansigray")
    band_tag    = f"[{spot.band}]" if spot.band else "[?]  "
    mode_tag    = f"{spot.mode}" if spot.mode else ""
    return [
        (band_colour,        f"{band_tag:<7}"),
        (mode_colour,        f"{mode_tag:<5} "),
        ("ansiwhite",        f"DX de {spot.spotter:<12} "),
        ("ansibrightyellow", f"{spot.frequency:>9.1f} kHz  "),
        ("ansiwhite",        f"{spot.dx_callsign:<12} "),
        ("ansigray",         f"{spot.comment:<33} "),
        ("ansidarkgray",     spot.time_str),
    ]


# ── Main TUI class ────────────────────────────────────────────────────────────

class DXClusterTUI:
    """Split-pane terminal interface built on prompt_toolkit Application.

    Output pane (top)  – spots stream here, auto-scrolls to latest.
    Status bar         – live connection/filter summary.
    Input pane (bottom)– command line with history and tab-completion.
    """

    def __init__(self) -> None:
        self._cfg: AppConfig = load_config()
        self._feed: Optional[SpotFeed] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._streaming: bool = False
        self._spot_count: int = 0
        self._json_mode: bool = self._cfg.json_mode
        self._app: Optional[Application] = None

        # Each entry: list of (style, text) tuples for one output line
        self._output_lines: list[list] = []

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        if HAS_PROMPT_TOOLKIT:
            await self._run_split_pane()
        else:
            print("prompt_toolkit not installed.  Install with:  pip install prompt_toolkit")
            print("Falling back to plain interactive shell.")
            from interactive import InteractiveShell
            await InteractiveShell().run()

    # ── Split-pane Application ────────────────────────────────────────────────

    async def _run_split_pane(self) -> None:
        try:
            hist = FileHistory(history_path())
        except Exception:
            hist = InMemoryHistory()

        completer = WordCompleter(
            _ALL_COMMANDS + list(KNOWN_CLUSTERS.keys()),
            ignore_case=True,
            sentence=True,
        )

        # ── Output pane (FormattedTextControl) ───────────────────────────────
        def get_output_text():
            # Determine how many lines the output pane can show.
            try:
                rows = shutil.get_terminal_size().lines - _OUTPUT_PANE_OVERHEAD
            except Exception:
                rows = 40
            visible = max(5, rows)
            lines = self._output_lines[-visible:]
            result = []
            for i, parts in enumerate(lines):
                if i > 0:
                    result.append(("", "\n"))
                result.extend(parts)
            return result

        output_window = Window(
            content=FormattedTextControl(get_output_text),
            wrap_lines=False,
            dont_extend_height=False,
        )

        # ── Status bar ────────────────────────────────────────────────────────
        def get_status_text():
            c = self._cfg.connection
            conn = f"{c.host}:{c.port} ({c.callsign})" if c.host else "not connected"
            state = "▶ streaming" if self._streaming else "■ stopped"
            f = self._cfg.filters
            parts = []
            if f.bands:
                parts.append("band:" + ",".join(sorted(f.bands)))
            if f.modes:
                parts.append("mode:" + ",".join(sorted(f.modes)))
            if f.include_prefixes:
                parts.append(f"include({len(f.include_prefixes)})")
            if f.exclude_prefixes:
                parts.append(f"exclude({len(f.exclude_prefixes)})")
            filters_str = "  │  " + "  ".join(parts) if parts else ""
            return [("class:status",
                     f" {state}  │  {conn}  │  {self._spot_count} spots"
                     f"{filters_str}  │  Ctrl-D quit ")]

        status_window = Window(
            content=FormattedTextControl(get_status_text),
            height=1,
            style="class:status",
        )

        separator = Window(height=1, char="─", style="class:separator")

        # ── Input pane (TextArea) ─────────────────────────────────────────────
        input_field = TextArea(
            height=1,
            prompt="dxcluster> ",
            multiline=False,
            history=hist,
            completer=completer,
            accept_handler=self._make_accept_handler(),
        )
        self._input_field = input_field

        # ── Layout ────────────────────────────────────────────────────────────
        layout = Layout(
            HSplit([output_window, status_window, separator, input_field]),
            focused_element=input_field,
        )

        # ── Key bindings ──────────────────────────────────────────────────────
        kb = KeyBindings()

        @kb.add("c-d")
        def _exit(event):
            event.app.exit()

        @kb.add("c-c")
        def _clear(event):
            input_field.buffer.reset()

        # ── Application ───────────────────────────────────────────────────────
        self._app = Application(
            layout=layout,
            key_bindings=kb,
            full_screen=True,
            style=_STYLE,
            mouse_support=False,
        )

        # Show banner and loaded state
        self._print_banner()

        # Auto-resume last session
        if self._cfg.has_connection() and self._cfg.auto_stream:
            c = self._cfg.connection
            self._print(
                f"Resuming last session: {c.host}:{c.port} as {c.callsign}"
            )
            asyncio.ensure_future(self._start_stream())

        await self._app.run_async()

        # Teardown
        await self._stop_stream(silent=True)
        self._cfg.json_mode = self._json_mode
        save_config(self._cfg)
        print("\nGoodbye. 73 de DXClusterSpots")

    def _make_accept_handler(self):
        """Return the accept_handler for the input TextArea."""
        def accept(buf):
            text = buf.text.strip()
            if text:
                self._print(f"dxcluster> {text}")
                asyncio.ensure_future(self._dispatch(text))
            return True  # clear the input buffer
        return accept

    # ── Output helpers ────────────────────────────────────────────────────────

    def _write_line(self, parts: list) -> None:
        """Append a coloured line (list of (style, text) tuples) to the output pane."""
        self._output_lines.append(parts)
        # Keep memory bounded
        if len(self._output_lines) > _MAX_OUTPUT_LINES:
            del self._output_lines[:500]
        if self._app:
            self._app.invalidate()

    def _print(self, msg: str = "") -> None:
        """Append a plain text message to the output pane."""
        self._write_line([("", msg)])

    def _print_banner(self) -> None:
        banner = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║            DXClusterSpots  –  Split-Pane Shell              ║",
            "║  Tab=complete  ↑/↓=history  Ctrl-C=clear  Ctrl-D=quit      ║",
            "╚══════════════════════════════════════════════════════════════╝",
            "Type 'help' for commands,  'nodes' to list cluster servers.",
            "",
        ]
        for line in banner:
            self._print(line)

        if self._cfg.filters.exclude_prefixes:
            n = len(self._cfg.filters.exclude_prefixes)
            self._write_line([("ansiyellow",
                f"Worked/exclude list loaded: {n} ent{'ities' if n != 1 else 'ity'}"
                "  (type 'filter show' to review)"
            )])
        if self._cfg.filters.bands or self._cfg.filters.modes:
            f = self._cfg.filters
            parts = []
            if f.bands:  parts.append("band:" + ",".join(sorted(f.bands)))
            if f.modes:  parts.append("mode:" + ",".join(sorted(f.modes)))
            self._write_line([("ansicyan",
                "Active filters: " + "  ".join(parts)
            )])

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

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str]) -> None:
        if args:
            topic = args[0].lower()
            if topic in _HELP:
                self._print("")
                for line in _HELP[topic].splitlines():
                    self._print(line)
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
        self._print("  worked   <prefix…>  (alias: w)      – add to worked/exclude list")
        self._print("  include  <prefix…>                  – add to include whitelist")
        self._print("  exclude  <prefix…>                  – add to exclude blacklist")
        self._print("  status                              – connection & filter summary")
        self._print("  json     [on|off]                   – toggle JSON output")
        self._print("  save                                – save settings now")
        self._print("  config                              – show config file path")
        self._print("  quit                                – exit")
        self._print("")
        self._print("Type 'help <command>' for details.  Settings auto-save on every change.")
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
            f"Ready: {host}:{port} as {callsign}"
            "  (type 'stream' to start receiving spots)"
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
        self._print("  " + "─" * 88)
        for name, (host, port) in KNOWN_CLUSTERS.items():
            active = "  ← connected" if self._cfg.connection.host == host else ""
            desc   = CLUSTER_DESCRIPTIONS.get(name, "")
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
                "Usage: filter <band|mode|dx|show|clear> [values…]\n"
                "  filter band 20m 40m\n"
                "  filter mode CW FT8 SSB\n"
                "  filter dx include VK ZL\n"
                "  filter dx exclude G ON DL\n"
                "  filter show\n"
                "  filter clear"
            )
            return

        sub    = args[0].lower()
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
            self._print(f"Usage: filter {sub} <value…>")
            return

        f = self._cfg.filters

        if sub == "band":
            invalid = [b for b in values if b.lower() not in BAND_PLAN]
            if invalid:
                self._print(
                    f"Unknown band(s): {', '.join(invalid)}\n"
                    f"Available: {', '.join(BAND_PLAN)}"
                )
                return
            f.bands = sorted({*f.bands, *[v.lower() for v in values]})
            self._print(f"Band filter: {', '.join(f.bands)}")

        elif sub == "mode":
            f.modes = sorted({*f.modes, *[v.upper() for v in values]})
            self._print(f"Mode filter: {', '.join(f.modes)}")

        elif sub == "dx":
            if not values:
                self._print("Usage: filter dx include|exclude <prefix…>")
                return
            direction = values[0].lower()
            prefixes  = values[1:]
            if not prefixes:
                self._print(f"Usage: filter dx {direction} <prefix…>")
                return
            if direction == "include":
                await self._do_include(prefixes)
            elif direction == "exclude":
                await self._do_exclude(prefixes)
            else:
                self._print("Usage: filter dx include|exclude <prefix…>")
            return  # include/exclude already call save and apply

        else:
            self._print(
                f"Unknown filter sub-command '{sub}'.\n"
                "  Use: band, mode, dx include/exclude, show, clear"
            )
            return

        self._apply_filter_to_feed()
        save_config(self._cfg)

    async def _cmd_worked(self, args: list[str]) -> None:
        if not args:
            self._print("Usage: worked <prefix…>  e.g. worked G ON DL")
            return
        await self._do_exclude(args)

    async def _cmd_include(self, args: list[str]) -> None:
        if not args:
            self._print("Usage: include <prefix…>  e.g. include VK ZL")
            return
        await self._do_include(args)

    async def _cmd_exclude(self, args: list[str]) -> None:
        if not args:
            self._print("Usage: exclude <prefix…>  e.g. exclude G ON")
            return
        await self._do_exclude(args)

    async def _cmd_stream(self, args: list[str]) -> None:
        sub = args[0].lower() if args else None
        if sub == "stop" or (sub is None and self._streaming):
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
        c = self._cfg.connection
        self._print("Status:")
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
        if self._app:
            self._app.exit()

    # ── Include / exclude helpers ─────────────────────────────────────────────

    async def _do_include(self, prefixes: list[str]) -> None:
        for pfx in prefixes:
            self._cfg.add_include(pfx.upper())
            self._print(f"Include ✓  {describe_entity(pfx)}")
        self._apply_filter_to_feed()
        save_config(self._cfg)

    async def _do_exclude(self, prefixes: list[str]) -> None:
        for pfx in prefixes:
            self._cfg.add_exclude(pfx.upper())
            self._print(f"Exclude ✓  {describe_entity(pfx)}")
        self._apply_filter_to_feed()
        save_config(self._cfg)

    # ── Filter display ────────────────────────────────────────────────────────

    def _show_filters(self) -> None:
        f = self._cfg.filters
        has_any = any([f.bands, f.modes, f.include_prefixes, f.exclude_prefixes])
        if not has_any:
            self._print("  Filters   : none (all spots shown)")
            return
        self._print("  Filters:")
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
        if self._app:
            self._app.invalidate()
        self._stream_task = asyncio.ensure_future(self._stream_loop())
        self._print(f"Connecting to {c.host}:{c.port}…  (type 'stream stop' to pause)")

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
        if self._app:
            self._app.invalidate()
        if not silent:
            self._print(f"Stream stopped.  ({self._spot_count} spot(s) received.)")

    async def _stream_loop(self) -> None:
        try:
            async for spot in self._feed.spots():
                self._spot_count += 1
                if self._json_mode:
                    self._write_line([("", spot.to_json())])
                else:
                    self._write_line(_format_spot_parts(spot))
        except asyncio.CancelledError:
            pass
        except ConnectionError as exc:
            self._print(f"Connection failed: {exc}")
            self._print("Type 'nodes' to see known nodes, or 'connect' to try another.")
        except OSError as exc:
            self._print(f"Network error: {exc}  – type 'stream' to retry.")
        except Exception as exc:
            self._print(f"Stream error: {type(exc).__name__}: {exc}")
        finally:
            self._streaming = False
            self._stream_task = None
            if self._app:
                self._app.invalidate()
