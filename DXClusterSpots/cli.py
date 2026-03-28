"""Command-line interface for DXClusterSpots.

Running with no --node / --host argument launches the interactive shell.

Non-interactive (pipe-friendly) usage examples
-----------------------------------------------
# Stream all spots from a known node:
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC

# Stream 20m and 40m spots only:
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --band 20m 40m

# Filter to DX stations with a VK or ZL prefix:
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --dx-prefix VK ZL

# Output as NDJSON (one JSON object per line) – ideal for piping to a web service:
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --json

# Grab 10 spots then exit:
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --count 10

# Launch the interactive shell explicitly:
    python DXClusterSpots.py --interactive

Design notes
------------
Two modes share this entry point:

1.  **Interactive TUI mode** (default when no --node/--host given):
    Launches the split-pane prompt_toolkit terminal UI (tui.py), or falls
    back to the plain readline REPL (interactive.py) if prompt_toolkit is
    not installed.  All user interaction, connection management, and filter
    editing happen inside the TUI.

2.  **Non-interactive streaming mode** (--node / --host given):
    Connects, optionally filters, and streams spots to stdout until the
    user interrupts with Ctrl-C or the --count limit is reached.  Ideal for
    piping to log processors, web services, or other Unix pipelines.
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the argparse parser for the DXCluster CLI.

    Argument groups:
      - (positional / mode)  : --interactive
      - connection            : --node / --host, --port, --callsign, --no-reconnect
      - filters               : --band, --dx-prefix, --spotter-prefix, --comment
      - output                : --count, --json, --verbose

    Returns:
        A configured ArgumentParser ready to call parse_args() on.
    """
    parser = argparse.ArgumentParser(
        prog="dxcluster",
        description=(
            "DXCluster spot client.\n\n"
            "Run with no arguments to launch the interactive shell.\n"
            "Supply --node or --host for a non-interactive streaming session.\n\n"
            f"Known nodes: {', '.join(KNOWN_CLUSTERS.keys())}\n"
            f"Known bands: {', '.join(BAND_PLAN.keys())}"
        ),
        # RawDescriptionHelpFormatter preserves the newlines in the description
        # above so the "Known nodes" list is not collapsed into one long line.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── Mode ──────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Launch the interactive shell (default when no --node/--host given)",
    )

    # ── Connection (both optional – omitting both triggers interactive mode) ──
    conn = parser.add_argument_group("connection (non-interactive mode)")

    # --node and --host are mutually exclusive: you either pick a pre-configured
    # node by name, or you supply an arbitrary hostname.  add_mutually_exclusive_group()
    # makes argparse enforce this constraint automatically and show it clearly in --help.
    node_group = conn.add_mutually_exclusive_group()
    node_group.add_argument(
        "--node", "-n",
        choices=list(KNOWN_CLUSTERS.keys()),
        metavar="NODE",
        help=f"Use a known cluster node ({', '.join(KNOWN_CLUSTERS.keys())})",
    )
    node_group.add_argument(
        "--host", "-H",
        metavar="HOSTNAME",
        help="Custom DXCluster node hostname",
    )
    conn.add_argument(
        "--port", "-p",
        type=int, default=7300,
        metavar="PORT",
        help="Telnet port when using --host (default: 7300)",
    )
    conn.add_argument(
        "--callsign", "-c",
        default="NOCALL",
        metavar="CALL",
        help="Your callsign for cluster login (default: NOCALL)",
    )
    conn.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Exit instead of reconnecting when the connection drops",
    )

    # ── Filtering ─────────────────────────────────────────────────────────────
    filt = parser.add_argument_group("filters (non-interactive mode)")
    filt.add_argument(
        "--band", "-b",
        nargs="+",    # accept one or more band names: --band 20m 40m
        metavar="BAND",
        help=f"Filter by band(s): {', '.join(BAND_PLAN.keys())}",
    )
    filt.add_argument(
        "--dx-prefix",
        nargs="+",
        metavar="PREFIX",
        help="Filter: DX callsign starts with prefix(es), e.g. VK ZL G",
    )
    filt.add_argument(
        "--spotter-prefix",
        nargs="+",
        metavar="PREFIX",
        help="Filter: spotter callsign starts with prefix(es)",
    )
    filt.add_argument(
        "--comment",
        nargs="+",
        metavar="KEYWORD",
        help="Filter: comment contains keyword(s) (case-insensitive)",
    )

    # ── Output ────────────────────────────────────────────────────────────────
    out = parser.add_argument_group("output (non-interactive mode)")
    out.add_argument(
        "--count", "-N",
        type=int, default=0,
        metavar="N",
        help="Stop after N spots (0 = stream indefinitely)",
    )
    out.add_argument(
        "--json",
        action="store_true",
        help="Output one JSON object per spot (NDJSON) instead of formatted text",
    )
    out.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    return parser


async def _stream(args: argparse.Namespace) -> int:
    """Non-interactive streaming mode: connect, filter, and print spots to stdout.

    This coroutine is the heart of the --node / --host mode.  It:
      1. Resolves the target host/port (from --node or --host/--port).
      2. Builds a SpotFilter from CLI arguments if any filters were given.
      3. Starts a SpotFeed (which handles reconnection automatically unless
         --no-reconnect was passed).
      4. Streams spots to stdout, either as formatted text or NDJSON.
      5. Returns 0 on clean exit (KeyboardInterrupt or --count reached).

    Why asyncio.run() wraps this in main()?
      SpotFeed uses asyncio internally (asyncio.open_connection, asyncio.sleep
      for reconnect delay).  Wrapping in asyncio.run() gives us a clean event
      loop without needing to manage it manually.

    Args:
        args: Parsed argparse.Namespace from build_parser().parse_args().

    Returns:
        Exit code integer (0 on success).
    """
    # Resolve connection target: known node takes precedence over --host/--port.
    if args.node:
        host, port = KNOWN_CLUSTERS[args.node]
    else:
        host = args.host
        port = args.port

    # Build the filter only if at least one filter argument was given.
    # An empty SpotFilter (no predicates) passes every spot, but we prefer
    # spot_filter=None because SpotFeed treats None as "no filtering" and
    # skips the call overhead entirely.
    spot_filter: Optional[SpotFilter] = None
    if any([args.band, args.dx_prefix, args.spotter_prefix, args.comment]):
        spot_filter = SpotFilter()
        if args.band:
            spot_filter.band(*args.band)
        if args.dx_prefix:
            # Raw prefix matching (startswith), not DXCC entity expansion.
            # This is appropriate for CLI use where the user knows the exact
            # prefix they want; entity expansion is a TUI convenience.
            spot_filter.dx_callsign_prefix(*args.dx_prefix)
        if args.spotter_prefix:
            spot_filter.spotter_prefix(*args.spotter_prefix)
        if args.comment:
            spot_filter.comment_contains(*args.comment)

    feed = SpotFeed(
        host=host,
        port=port,
        callsign=args.callsign,
        spot_filter=spot_filter,
        reconnect=not args.no_reconnect,   # --no-reconnect → reconnect=False
    )

    # Print a human-readable header unless JSON output was requested.
    # In JSON mode we emit NDJSON only, with no decorative lines or headers,
    # so the output is cleanly parseable by downstream tools (jq, Python, etc.).
    if not args.json:
        parts = []
        if args.band:
            parts.append(f"band={','.join(args.band)}")
        if args.dx_prefix:
            parts.append(f"dx-prefix={','.join(args.dx_prefix)}")
        suffix = f"  [{' | '.join(parts)}]" if parts else ""
        print(f"Connecting to {host}:{port} as {args.callsign}{suffix}")
        print("-" * 80)

    count = 0
    try:
        async for spot in feed.spots():
            # to_json() returns a compact single-line JSON string (NDJSON format).
            # str(spot) returns the fixed-width human-readable display line.
            print(spot.to_json() if args.json else str(spot), flush=True)
            # flush=True ensures each spot is written to stdout immediately
            # rather than being buffered.  This matters when piping to other
            # processes (grep, tee, etc.) that consume input line-by-line.
            count += 1
            if args.count and count >= args.count:
                # --count N: stop cleanly after N spots by calling feed.stop()
                # which sets the internal _running flag to False.  The next
                # spot yielded by the generator will return rather than yield.
                feed.stop()
                break
    except KeyboardInterrupt:
        if not args.json:
            print(f"\nStopped after {count} spot(s).")

    return 0


def main() -> None:
    """Entry point: parse arguments, configure logging, and dispatch to the
    appropriate mode (interactive TUI or non-interactive streaming).

    This function is registered as the console_scripts entry point in
    setup.py / pyproject.toml, so it is called when the user runs `dxcluster`
    from the shell.
    """
    parser = build_parser()
    args = parser.parse_args()

    # Decide which mode to run:
    #   interactive if --interactive is explicitly set, OR if neither --node
    #   nor --host was given (i.e. no connection target specified).
    use_interactive = args.interactive or (not args.node and not args.host)

    # Configure logging:
    #   In interactive mode, suppress library log output so it doesn't bleed
    #   raw text into the TUI panes.  --verbose overrides this and shows DEBUG
    #   logs for troubleshooting connection or parsing issues.
    #   In streaming mode, WARNING level is the default (only important issues
    #   are printed) and --verbose again enables DEBUG.
    if use_interactive:
        log_level = logging.DEBUG if args.verbose else logging.ERROR
    else:
        log_level = logging.DEBUG if args.verbose else logging.WARNING

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if use_interactive:
        # Import inside the branch so the TUI dependencies (prompt_toolkit etc.)
        # are not imported in non-interactive mode, keeping startup fast.
        from tui import DXClusterTUI
        sys.exit(asyncio.run(DXClusterTUI().run()))
    else:
        # asyncio.run() creates a new event loop, runs the coroutine to
        # completion, and tears down the loop cleanly.
        sys.exit(asyncio.run(_stream(args)))


if __name__ == "__main__":
    main()
