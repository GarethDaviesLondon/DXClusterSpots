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

Architecture notes
------------------
The TUI is built on prompt_toolkit's ``Application`` class, which runs an
asyncio event loop internally and redraws the layout on every invalidation.

Output pane (FormattedTextControl)
    Spots are stored in ``self._output_lines`` as lists of (style, text) tuples
    (the prompt_toolkit "formatted text" format).  A ``FormattedTextControl``
    calls ``get_output_text()`` on every redraw and returns the last N visible
    lines based on the current terminal height.  Auto-scrolling is achieved
    by always showing the *tail* of ``self._output_lines`` rather than
    maintaining a scroll offset.

Status bar (FormattedTextControl)
    ``get_status_text()`` is called on every redraw and reads live from
    ``self._cfg`` and ``self._streaming``, so it always reflects the current
    state without any explicit update step.

Input pane (TextArea)
    prompt_toolkit's ``TextArea`` handles readline-style editing, history
    (FileHistory), and tab completion (WordCompleter).  The ``accept_handler``
    is called when the user presses Enter; returning ``False`` from it causes
    prompt_toolkit to reset (clear) the buffer.

Async integration
    ``Application.run_async()`` runs the prompt_toolkit event loop as a
    coroutine.  The spot streaming task (``_stream_loop``) runs concurrently
    on the same event loop via ``asyncio.ensure_future()``.  Each new spot
    calls ``self._app.invalidate()`` to trigger a redraw of the output pane.
    Commands from the input pane are dispatched via
    ``asyncio.ensure_future(self._dispatch(text))`` from the synchronous
    ``accept_handler``.
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

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter, SpotLog
from dxcluster.config import (
    AppConfig, FilterConfig, load_config, save_config,
    config_path as config_file_path, history_path, log_path,
)
from dxcluster.dxcc import describe_entity, entity_name, resolve_entity
from dxcluster.filters import build_filter_from_config

# ── Colour palette ────────────────────────────────────────────────────────────
# prompt_toolkit uses a restricted set of ANSI colour names.  The valid names
# are: ansiblack, ansidarkgray, ansigray, ansiwhite,
#       ansired, ansibrightred, ansiyellow, ansibrightyellow,
#       ansigreen, ansibrightgreen, ansicyan, ansibrightcyan,
#       ansiblue, ansibrightblue, ansimagenta, ansibrightmagenta.
# NOTE: "ansibrightwhite" is NOT valid and raises a ValueError on Windows.
#       Use "ansiwhite" as the bright-white equivalent.

# Band colour coding uses the traditional "rainbow" ordering (LF = warm,
# HF = cool, VHF/UHF = white/gray) to give operators an instant visual cue
# about which part of the spectrum a spot is on.
_BAND_COLOURS: dict[str, str] = {
    "160m": "ansimagenta",      # 1.8 MHz – Top Band (warm/purple for LF)
    "80m":  "ansired",          # 3.5 MHz – warm red for low HF
    "60m":  "ansiyellow",       # 5 MHz – sparse allocation
    "40m":  "ansiyellow",       # 7 MHz – classic DX band (yellow = active)
    "30m":  "ansigreen",        # 10 MHz – WARC band, no contests
    "20m":  "ansibrightgreen",  # 14 MHz – the primary DX band
    "17m":  "ansicyan",         # 18 MHz – WARC band
    "15m":  "ansibrightcyan",   # 21 MHz – excellent when solar conditions allow
    "12m":  "ansiblue",         # 24 MHz – WARC band, good at solar max
    "10m":  "ansibrightblue",   # 28 MHz – spectacular at solar max
    "6m":   "ansibrightmagenta",# 50 MHz – "Magic Band", sporadic-E openings
    "4m":   "ansimagenta",      # 70 MHz – regional allocation
    "2m":   "ansiwhite",        # 144 MHz – primary VHF band
    "70cm": "ansigray",         # 430 MHz – primary UHF band
}

# Mode colours allow operators to instantly distinguish CW/SSB/digital spots.
_MODE_COLOURS: dict[str, str] = {
    "CW":     "ansibrightyellow",  # Morse code – traditional gold/yellow
    "SSB":    "ansiwhite",         # Voice – neutral white
    "FT8":    "ansibrightcyan",    # FT8 – bright cyan for the dominant digital mode
    "FT4":    "ansicyan",          # FT4 – slightly dimmer than FT8
    "RTTY":   "ansibrightgreen",   # Radio teletype – bright green (legacy digital)
    "PSK":    "ansigreen",         # PSK31/63 – green (legacy digital)
    "DIGI":   "ansigreen",         # Generic digital – same as PSK
    "JT65":   "ansicyan",          # JT65 – predecessor to FT8
    "JT9":    "ansicyan",          # JT9 – narrow-band weak signal
    "JS8":    "ansigreen",         # JS8Call – conversational digital
    "MSK144": "ansibrightcyan",    # Meteor scatter
    "WSPR":   "ansigray",          # WSPR beacon – subdued (background traffic)
    "FST4":   "ansicyan",          # FST4 – LF/MF weak signal
    "AM":     "ansiyellow",        # AM – legacy voice
    "FM":     "ansiwhite",         # FM – VHF/UHF voice
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
    "include", "exclude", "search", "log", "lookup", "save", "config", "quit", "exit", "q",
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
        "    filter band          <band...>     20m, 40m, 80m …\n"
        "    filter mode          <mode...>     CW, SSB, FT8, RTTY, DIGI …\n"
        "    filter zone          add <zone...> DX station: add CQ zone(s) to allow list\n"
        "    filter zone          remove <n...> DX station: remove CQ zone(s)\n"
        "    filter zone          open          DX station: accept all zones (default)\n"
        "    filter zone          close         DX station: reject all zones\n"
        "    filter zone          show          DX station: show zone filter\n"
        "    filter spotter zone  add <zone...> Spotter: add CQ zone(s) to allow list\n"
        "    filter spotter zone  remove <n...> Spotter: remove CQ zone(s)\n"
        "    filter spotter zone  open          Spotter: accept all zones (default)\n"
        "    filter spotter zone  close         Spotter: reject all zones\n"
        "    filter spotter zone  show          Spotter: show zone filter\n"
        "    filter dx include <prefix...>      show ONLY DX from these DXCC entities\n"
        "    filter dx exclude <prefix...>      hide DX from these DXCC entities\n"
        "    filter show                      display all active filters\n"
        "    filter clear                     remove all filters\n"
        "\n"
        "  CQ zones: 1=Alaska  3-5=USA  6=Mexico  7-9=Caribbean/SA\n"
        "    14=W.Europe  15=E.Europe  16=Russia/EU  20=Balkans/Turkey\n"
        "    21=Middle East  24=China  25=Japan  29-30=Australia  32=NZ\n"
        "\n"
        "  DXCC entity resolution:\n"
        "    'G'  → England  : G, M, 2E\n"
        "    'ON' → Belgium  : ON, OO, OP, OQ, OR, OS, OT\n"
        "    'DL' → Germany  : DA, DB, DC … DR\n"
        "\n"
        "  Filters are saved and reloaded on next start."
    ),
    "search": (
        "search freq <kHz>           – spots within ±5 kHz in last 24 h\n"
        "search call <pattern>       – spots where DX or spotter matches pattern\n"
        "\n"
        "  Results are ordered chronologically.  All spots from the last 24\n"
        "  hours are logged regardless of active filters, so you can search\n"
        "  for spots you filtered out.\n"
        "\n"
        "  Examples:\n"
        "    search freq 14074\n"
        "    search freq 7040.5\n"
        "    search call G3SXW\n"
        "    search call ON4"
    ),
    "log": "log\n\n  Show spot log statistics (total spots stored, file location).",
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

_MAX_OUTPUT_LINES = 2000  # maximum lines held in _output_lines before trimming
_OUTPUT_PANE_OVERHEAD = 3  # rows consumed by: status bar (1) + separator (1) + input (1)


# ── Spot formatter ────────────────────────────────────────────────────────────

def _format_spot_parts(spot) -> list:
    """Return a list of (style, text) tuples for one spot line in the output pane.

    prompt_toolkit's FormattedTextControl accepts "formatted text" as a list
    of (style_string, text_string) pairs.  Each pair renders the text in the
    given style, allowing per-column colour coding with precise character-level
    control.

    Column order (left to right):
        DX callsign  | country name | CQ zone | frequency | spotter | comment | mode | band | time

    Column widths are chosen so that a typical 130-character wide terminal
    displays all fields without wrapping.  The widths match the field
    significance: callsigns first (most important to the operator), then
    geographic context (country, zone), then operating context (frequency,
    mode, band), then metadata (spotter, comment, time).

    Args:
        spot: A DXSpot instance (fully parsed and enriched).

    Returns:
        A list of (style, text) tuples suitable for FormattedTextControl.
    """
    # Look up colours; fall back to neutral colour if band/mode is unknown.
    band_colour = _BAND_COLOURS.get(spot.band or "", "ansiwhite")
    mode_colour = _MODE_COLOURS.get(spot.mode or "", "ansigray")

    # Format optional tags with fixed width so columns align even when absent.
    # "[?]  " (5 chars) matches the longest band tag "[160m]" (6 chars) + " "
    # — the extra spaces in "[?]  " pad it to 7 chars to match "[160m] ".
    band_tag = f"[{spot.band}]" if spot.band else "[?]  "
    mode_tag = f"{spot.mode}" if spot.mode else ""
    zone_tag = f"Z{spot.zone}" if spot.zone else ""

    # Resolve the DX callsign to a DXCC entity to get the country name.
    # entity_name() returns the canonical display name (e.g. "England",
    # "Germany", "Japan").  Falls back to "" if the prefix is unrecognised,
    # leaving the country column blank rather than showing an error.
    key     = resolve_entity(spot.dx_callsign)
    country = entity_name(key) if key else ""

    return [
        ("ansiwhite",        f"{spot.dx_callsign:<10} "),  # DX callsign, 10 chars
        ("ansicyan",         f"{country:<16} "),           # country name, 16 chars
        ("ansicyan",         f"{zone_tag:<4} "),           # CQ zone e.g. "Z14", 4 chars
        ("ansibrightyellow", f"{spot.frequency:>9.1f}  "), # frequency right-aligned, 1 decimal
        ("ansigray",         f"de {spot.spotter:<12} "),   # spotter with "de " prefix
        ("ansiwhite",        f"{spot.comment:<26} "),      # comment, truncated/padded to 26
        (mode_colour,        f"{mode_tag:<5} "),           # mode e.g. "CW   ", 5 chars
        (band_colour,        f"{band_tag:<7} "),           # band tag e.g. "[20m]  ", 7 chars
        ("ansidarkgray",     spot.time_str),               # time e.g. "1234Z", no padding
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
        self._log: SpotLog = SpotLog(log_path())

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
            """Produce the formatted text for the output pane.

            Called by prompt_toolkit on every redraw.  We return only the
            last N lines (where N = terminal height minus fixed overhead rows)
            so the pane auto-scrolls to the bottom without maintaining an
            explicit scroll offset.

            WHY shutil.get_terminal_size() instead of using the Application's
            own output.get_size()?
              get_terminal_size() is simpler and available synchronously.  The
              Application's output object requires going through the event loop,
              which is overkill for this purpose.  In practice both return the
              same value.

            WHY insert "\n" between lines rather than in each parts list?
              FormattedTextControl concatenates all the (style, text) pairs
              into a single run of styled text.  Newlines must be inserted
              explicitly between lines; they are not added automatically.
              We add the newline separator between entries (not after the last
              one) so there is no trailing blank line at the bottom of the pane.
            """
            # Determine how many lines the output pane can show.
            try:
                rows = shutil.get_terminal_size().lines - _OUTPUT_PANE_OVERHEAD
            except Exception:
                rows = 40  # safe fallback if terminal size cannot be determined
            visible = max(5, rows)   # always show at least 5 lines

            # Slice the tail of _output_lines to auto-scroll to the bottom.
            lines = self._output_lines[-visible:]
            result = []
            for i, parts in enumerate(lines):
                if i > 0:
                    result.append(("", "\n"))  # line separator (not after last)
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
            if f.cq_zones is not None:
                z = "CLOSED" if len(f.cq_zones) == 0 else ",".join(str(z) for z in sorted(f.cq_zones))
                parts.append("dx-z:" + z)
            if f.spotter_cq_zones is not None:
                z = "CLOSED" if len(f.spotter_cq_zones) == 0 else ",".join(str(z) for z in sorted(f.spotter_cq_zones))
                parts.append("sp-z:" + z)
            if f.include_prefixes:
                parts.append(f"incl({len(f.include_prefixes)})")
            if f.exclude_prefixes:
                parts.append(f"excl({len(f.exclude_prefixes)})")
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
        """Return the accept_handler for the input TextArea.

        prompt_toolkit calls the accept_handler when the user presses Enter.
        The handler receives the Buffer object.

        Return value semantics (prompt_toolkit 3.x):
          False / None → buffer.reset() is called automatically, clearing the
                         input field.  This is the correct behaviour for a REPL
                         where each Enter press should produce a clean new line.
          True         → the text is kept as-is (used for multi-line inputs).

        WHY asyncio.ensure_future() rather than await?
          accept_handler is a *synchronous* function (not a coroutine) because
          prompt_toolkit calls it from synchronous internal code.  We cannot
          await a coroutine from a sync function.  ensure_future() schedules
          _dispatch() as a concurrent task on the running event loop, so it
          executes asynchronously after the handler returns.  This is safe
          because the event loop is always running while the Application is
          active.

        WHY echo the command back to the output pane?
          The split-pane layout separates input and output visually.  Without
          echoing, the user would see a response but no record of what command
          produced it.  The echo line acts as a visible command history in the
          output pane.
        """
        def accept(buf):
            text = buf.text.strip()
            if text:
                # Echo the command to the output pane for a visual history trail.
                self._print(f"dxcluster> {text}")
                # Schedule the async command handler without blocking the UI.
                asyncio.ensure_future(self._dispatch(text))
            return False  # False → prompt_toolkit resets (clears) the buffer
        return accept

    # ── Output helpers ────────────────────────────────────────────────────────

    def _write_line(self, parts: list) -> None:
        """Append a coloured line (list of (style, text) tuples) to the output pane.

        This is the single path by which any text reaches the output pane —
        both spot lines (from _stream_loop via _format_spot_parts()) and plain
        text messages (from _print()) go through here.

        Memory management:
          When the list exceeds _MAX_OUTPUT_LINES (2000), we delete the oldest
          500 lines in one slice operation.  Deleting in batches is more
          efficient than popping one line at a time because list.pop(0) is O(n)
          while del list[:500] is O(n) but with a much smaller constant factor
          and only runs every 500 appends.

        args.invalidate():
          Forces prompt_toolkit to redraw the output pane on the next event-
          loop iteration.  Without this call, new lines would only appear when
          the user interacts with the UI (keypress, resize, etc.).

        Args:
            parts: A list of (style_string, text_string) tuples in the
                   prompt_toolkit FormattedText format.
        """
        self._output_lines.append(parts)
        # Trim when we have too many lines to avoid unbounded memory growth.
        if len(self._output_lines) > _MAX_OUTPUT_LINES:
            del self._output_lines[:500]  # remove oldest 500 in one operation
        if self._app:
            self._app.invalidate()  # schedule a screen redraw

    def _print(self, msg: str = "") -> None:
        """Append a plain (unstyled) text message to the output pane.

        A convenience wrapper over _write_line() for cases where coloured
        (styled) output is not needed, such as command responses, help text,
        error messages, and status summaries.

        Args:
            msg: The message to display.  Defaults to "" for a blank line.
        """
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
        f = self._cfg.filters
        active_parts = []
        if f.bands:           active_parts.append("band:" + ",".join(sorted(f.bands)))
        if f.modes:           active_parts.append("mode:" + ",".join(sorted(f.modes)))
        if f.cq_zones is not None:
            z = "CLOSED" if not f.cq_zones else ",".join(str(n) for n in sorted(f.cq_zones))
            active_parts.append("dx-zone:" + z)
        if f.spotter_cq_zones is not None:
            z = "CLOSED" if not f.spotter_cq_zones else ",".join(str(n) for n in sorted(f.spotter_cq_zones))
            active_parts.append("spotter-zone:" + z)
        if active_parts:
            self._write_line([("ansicyan", "Active filters: " + "  ".join(active_parts))])

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
            "search":     self._cmd_search,
            "log":        self._cmd_log,
            "lookup":     self._cmd_lookup,
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
        self._print("  filter   band|mode|zone|spotter zone|dx|show|clear")
        self._print("  worked   <prefix…>  (alias: w)      – add to worked/exclude list")
        self._print("  include  <prefix…>                  – add to include whitelist")
        self._print("  exclude  <prefix…>                  – add to exclude blacklist")
        self._print("  status                              – connection & filter summary")
        self._print("  json     [on|off]                   – toggle JSON output")
        self._print("  lookup   <callsign|prefix>          – show country and CQ zone")
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

        elif sub == "zone":
            await self._cmd_filter_zone(values, is_spotter=False)
            return

        elif sub == "spotter":
            # filter spotter zone add/remove/open/close/show
            if not values or values[0].lower() != "zone":
                self._print(
                    "Usage: filter spotter zone <add|remove|open|close|show> [zones...]\n"
                    "  Filters by the CQ zone of the station that filed the spot."
                )
                return
            await self._cmd_filter_zone(values[1:], is_spotter=True)
            return

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
                "  Use: band, mode, zone, dx include/exclude, show, clear"
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

    async def _cmd_filter_zone(self, args: list[str], is_spotter: bool = False) -> None:
        """Handle zone filter for DX station (is_spotter=False) or spotter (is_spotter=True)."""
        label   = "Spotter zone" if is_spotter else "DX zone"
        cmd_pfx = "filter spotter zone" if is_spotter else "filter zone"

        if not args:
            self._print(
                f"Usage: {cmd_pfx} <add|remove|open|close|show> [zone...]\n"
                f"  {cmd_pfx} open          – accept all zones (default)\n"
                f"  {cmd_pfx} close         – reject all zones\n"
                f"  {cmd_pfx} add 14 15     – add zones to allow list\n"
                f"  {cmd_pfx} remove 14     – remove zone from allow list\n"
                f"  {cmd_pfx} show          – display current zone filter"
            )
            return

        sub = args[0].lower()

        def get_zones():
            return self._cfg.filters.spotter_cq_zones if is_spotter else self._cfg.filters.cq_zones

        def set_zones(val):
            if is_spotter:
                self._cfg.filters.spotter_cq_zones = val
            else:
                self._cfg.filters.cq_zones = val

        if sub == "show":
            z = get_zones()
            if z is None:
                self._print(f"  {label} filter : all zones open (no filtering)")
            elif len(z) == 0:
                self._print(f"  {label} filter : ALL CLOSED")
            else:
                self._print(f"  {label} filter : accepting zones {', '.join(str(n) for n in sorted(z))}")
            return

        if sub == "open":
            set_zones(None)
            self._apply_filter_to_feed()
            save_config(self._cfg)
            self._print(f"{label} filter removed – all zones accepted.")
            return

        if sub == "close":
            set_zones([])
            self._apply_filter_to_feed()
            save_config(self._cfg)
            self._print(f"{label} filter closed.  Use '{cmd_pfx} add <n>' to open specific zones.")
            return

        if sub in ("add", "remove"):
            zone_args = args[1:]
            if not zone_args:
                self._print(f"Usage: {cmd_pfx} {sub} <zone...>  e.g. {cmd_pfx} add 14 15")
                return
            try:
                zones = [int(z) for z in zone_args]
            except ValueError:
                self._print(f"Zone numbers must be integers, e.g. 14  (got: {' '.join(zone_args)})")
                return
            invalid = [z for z in zones if not (1 <= z <= 40)]
            if invalid:
                self._print(f"Invalid CQ zone(s): {invalid}  (valid range: 1–40)")
                return

            current = get_zones()
            if sub == "add":
                if current is None:
                    set_zones(sorted(set(zones)))
                    self._print(
                        f"{label} filter created: {', '.join(str(z) for z in get_zones())}\n"
                        f"  (was all-open – use '{cmd_pfx} open' to revert)"
                    )
                else:
                    set_zones(sorted(set(current) | set(zones)))
                    self._print(f"{label} filter: accepting {', '.join(str(z) for z in get_zones())}")
            else:  # remove
                if current is None:
                    self._print(f"{label} filter is all-open.  Use '{cmd_pfx} close' first, then add the zones you want.")
                    return
                set_zones(sorted(set(current) - set(zones)))
                if get_zones():
                    self._print(f"{label} filter: accepting {', '.join(str(z) for z in get_zones())}")
                else:
                    self._print(f"{label} filter: all zones removed.  Use '{cmd_pfx} open' to accept all.")

            self._apply_filter_to_feed()
            save_config(self._cfg)
            return

        self._print(f"Unknown zone sub-command '{sub}'.  Use: add, remove, open, close, show")

    async def _cmd_search(self, args: list[str]) -> None:
        """search freq <kHz> | search call <pattern>"""
        if len(args) < 2:
            self._print(
                "Usage:\n"
                "  search freq <kHz>       – spots within ±5 kHz in last 24 h\n"
                "  search call <pattern>   – spots matching callsign pattern\n"
                "  search call G3SXW\n"
                "  search freq 14074"
            )
            return

        sub = args[0].lower()

        if sub == "freq":
            try:
                freq = float(args[1])
            except ValueError:
                self._print(f"Expected a frequency in kHz, got '{args[1]}'")
                return
            results = self._log.search_frequency(freq, window_khz=5.0)
            if not results:
                self._print(f"No spots found within ±5 kHz of {freq} kHz in the last 24 hours.")
                return
            self._print(f"Spots within ±5 kHz of {freq:.1f} kHz — last 24 h ({len(results)} found):")
            self._print("─" * 78)
            for s in results:
                ts = s.received_at.strftime("%H:%M")
                zone_tag = f"Z{s.zone}" if s.zone else "   "
                mode_tag = f"{s.mode:<5}" if s.mode else "     "
                self._write_line([
                    ("ansidarkgray",    f" {ts} "),
                    ("ansibrightyellow",f"{s.frequency:>9.1f} kHz  "),
                    ("ansibrightgreen", f"{mode_tag} "),
                    ("ansicyan",        f"{zone_tag} "),
                    ("ansiwhite",       f"DX de {s.spotter:<12} "),
                    ("ansiwhite",       f"{s.dx_callsign:<12} "),
                    ("ansigray",        f"{s.comment:<30} "),
                    ("ansidarkgray",    s.time_str),
                ])
            self._print("─" * 78)

        elif sub == "call":
            pattern = args[1]
            results = self._log.search_callsign(pattern)
            if not results:
                self._print(f"No spots matching '{pattern}' in the last 24 hours.")
                return
            self._print(f"Spots matching '{pattern.upper()}' — last 24 h ({len(results)} found):")
            self._print("─" * 78)
            for s in results:
                ts = s.received_at.strftime("%H:%M")
                band_tag = f"[{s.band}]" if s.band else "[?]  "
                mode_tag = f"{s.mode:<5}" if s.mode else "     "
                zone_tag = f"Z{s.zone}" if s.zone else "   "
                self._write_line([
                    ("ansidarkgray",    f" {ts} "),
                    ("ansibrightgreen", f"{band_tag:<7} {mode_tag} "),
                    ("ansicyan",        f"{zone_tag} "),
                    ("ansibrightyellow",f"{s.frequency:>9.1f} kHz  "),
                    ("ansiwhite",       f"DX de {s.spotter:<12} "),
                    ("ansiwhite",       f"{s.dx_callsign:<12} "),
                    ("ansigray",        f"{s.comment:<30} "),
                    ("ansidarkgray",    s.time_str),
                ])
            self._print("─" * 78)

        else:
            self._print(f"Unknown search type '{sub}'.  Use: freq, call")

    async def _cmd_log(self, args: list[str]) -> None:
        self._print(f"Spot log: {self._log.size()} spots stored (last 24 h)")
        self._print(f"Log file: {log_path()}")

    async def _cmd_lookup(self, args: list[str]) -> None:
        """lookup <callsign|prefix>  – show country name and CQ zone."""
        if not args:
            self._print("Usage: lookup <callsign or prefix>  e.g. lookup G3SXW  or  lookup JA")
            return
        from dxcluster.dxcc import cq_zone_for
        for arg in args:
            key     = resolve_entity(arg)
            country = entity_name(key) if key else "(unknown prefix)"
            zone    = cq_zone_for(arg)
            zone_str = f"CQ Zone {zone}" if zone else "zone unknown"
            prefixes = describe_entity(arg)   # "England (G, M, 2E)"
            self._write_line([
                ("ansiwhite",  f"{arg.upper():<10} "),
                ("ansicyan",         f"{country:<18} "),
                ("ansibrightyellow", f"{zone_str:<14} "),
                ("ansigray",         prefixes),
            ])

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
        has_any = any([f.bands, f.modes, f.include_prefixes, f.exclude_prefixes,
                       f.cq_zones is not None, f.spotter_cq_zones is not None])
        if not has_any:
            self._print("  Filters   : none (all spots shown)")
            return
        self._print("  Filters:")
        if f.bands:
            self._print(f"    band         : {', '.join(sorted(f.bands))}")
        if f.modes:
            self._print(f"    mode         : {', '.join(sorted(f.modes))}")
        if f.cq_zones is not None:
            if len(f.cq_zones) == 0:
                self._print("    dx zone      : ALL CLOSED")
            else:
                self._print(f"    dx zone      : {', '.join(str(z) for z in sorted(f.cq_zones))}")
        if f.spotter_cq_zones is not None:
            if len(f.spotter_cq_zones) == 0:
                self._print("    spotter zone : ALL CLOSED")
            else:
                self._print(f"    spotter zone : {', '.join(str(z) for z in sorted(f.spotter_cq_zones))}")
        if f.include_prefixes:
            descs = [describe_entity(p) for p in f.include_prefixes]
            self._print(f"    dx include   : {'; '.join(descs)}")
        if f.exclude_prefixes:
            descs = [describe_entity(p) for p in f.exclude_prefixes]
            self._print(f"    dx exclude   : {'; '.join(descs)}")

    # ── Stream management ─────────────────────────────────────────────────────

    def _apply_filter_to_feed(self) -> None:
        """Rebuild the SpotFilter from the current config and push it to the feed.

        Called whenever a filter setting changes while a stream is running.
        build_filter_from_config() returns None if no filters are active,
        which SpotFeed interprets as "accept all spots" — slightly more
        efficient than an empty SpotFilter with zero predicates.
        """
        if self._feed:
            self._feed.spot_filter = build_filter_from_config(self._cfg.filters)

    async def _start_stream(self) -> None:
        """Create a fresh SpotFeed and launch the background streaming task.

        A new SpotFeed is created on every start so it picks up the current
        filter configuration.  reconnect=True means SpotFeed will automatically
        reconnect after connection drops (with a 30-second delay), so the
        operator doesn't need to manually restart after a server hiccup.

        ensure_future() vs. create_task():
          Both schedule a coroutine on the event loop.  ensure_future() is
          slightly more general (accepts both coroutines and futures) and is
          used here for consistency with the accept_handler, which also uses
          ensure_future().  In practice they are equivalent in this context.
        """
        c = self._cfg.connection
        self._feed = SpotFeed(
            host=c.host,
            port=c.port,
            callsign=c.callsign,
            spot_filter=build_filter_from_config(self._cfg.filters),
            reconnect=True,  # auto-reconnect on connection loss
        )
        self._streaming = True
        if self._app:
            self._app.invalidate()  # update status bar to show "▶ streaming"
        self._stream_task = asyncio.ensure_future(self._stream_loop())
        self._print(f"Connecting to {c.host}:{c.port}…  (type 'stream stop' to pause)")

    async def _stop_stream(self, silent: bool = False) -> None:
        """Stop the streaming task and reset all streaming state.

        Order of operations matters:
          1. feed.stop() sets SpotFeed._running = False so the generator
             exits cleanly on the next spot or reconnect check.
          2. stream_task.cancel() sends CancelledError into the task.
          3. await stream_task waits for the task to actually finish.
             (Without this await, the task might still be running when the
             caller proceeds, leading to race conditions.)
          4. Reset instance variables.

        Args:
            silent: If True, suppress the "Stream stopped" message.  Used
                    during auto-reconnects and at app shutdown to avoid
                    spurious messages.
        """
        if self._feed:
            self._feed.stop()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task  # wait for clean cancellation
            except asyncio.CancelledError:
                pass  # expected; task raised CancelledError on cancel()
            self._stream_task = None
        self._streaming = False
        self._feed = None
        if self._app:
            self._app.invalidate()  # update status bar to show "■ stopped"
        if not silent:
            self._print(f"Stream stopped.  ({self._spot_count} spot(s) received.)")

    async def _stream_loop(self) -> None:
        """Consume spots from the SpotFeed and render them in the output pane.

        This coroutine runs as a concurrent task alongside the prompt_toolkit
        Application event loop.  For each spot received:
          1. Increment the spot counter (shown in the status bar).
          2. Log the spot to the 24-hour rolling log (BEFORE any display filter
             so that search results include filtered-out spots).
          3. Format and display the spot (or its JSON representation in JSON mode).

        The spot is logged regardless of display filters because the user may
        want to search for a spot they had previously filtered out.  For
        example, if the user filtered to 20m only, a 40m spot would be hidden
        from the live stream but would still appear in 'search freq 7040'.

        Exception handling:
          asyncio.CancelledError – clean stop via _stop_stream() or app exit.
          ConnectionError        – DNS failure; cannot resolve hostname.
          OSError                – network-level error (TCP reset etc.).
          Exception              – unexpected programming error; log for diagnosis.
        """
        try:
            async for spot in self._feed.spots():
                self._spot_count += 1
                # Log ALL spots regardless of active display filters.
                # The SpotFeed applies filters before yielding, so spots that
                # were filtered out by the user never reach this loop.
                # However, we still log everything that passes the filter —
                # the filter is applied by SpotFeed, not here.
                self._log.append(spot)
                if self._json_mode:
                    self._write_line([("", spot.to_json())])
                else:
                    self._write_line(_format_spot_parts(spot))
        except asyncio.CancelledError:
            # Normal cancellation via _stop_stream().  Re-raise is not needed
            # because _stop_stream() awaits this task and handles the exception.
            pass
        except ConnectionError as exc:
            # Raised by SpotFeed when the hostname cannot be resolved.
            # This is a permanent error (nothing will fix itself without user
            # intervention) so we display a helpful message.
            self._print(f"Connection failed: {exc}")
            self._print("Type 'nodes' to see known nodes, or 'connect' to try another.")
        except OSError as exc:
            # Transient network error.  SpotFeed normally handles reconnection
            # internally, but if reconnect=False or the error is unrecoverable,
            # it propagates here.
            self._print(f"Network error: {exc}  – type 'stream' to retry.")
        except Exception as exc:
            # Unexpected error: log type + message for diagnosis.
            self._print(f"Stream error: {type(exc).__name__}: {exc}")
        finally:
            # Always clean up, even if an exception occurred, so the status
            # bar and streaming flag are consistent with the actual state.
            self._streaming = False
            self._stream_task = None
            if self._app:
                self._app.invalidate()
