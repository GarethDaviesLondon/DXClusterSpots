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
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from dxcluster import BAND_PLAN, CLUSTER_DESCRIPTIONS, KNOWN_CLUSTERS, SpotFeed, SpotFilter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dxcluster",
        description=(
            "DXCluster spot client.\n\n"
            "Run with no arguments to launch the interactive shell.\n"
            "Supply --node or --host for a non-interactive streaming session.\n\n"
            f"Known nodes: {', '.join(KNOWN_CLUSTERS.keys())}\n"
            f"Known bands: {', '.join(BAND_PLAN.keys())}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Launch the interactive shell (default when no --node/--host given)",
    )

    # Connection (both optional – omitting both triggers interactive mode)
    conn = parser.add_argument_group("connection (non-interactive mode)")
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

    # Filtering
    filt = parser.add_argument_group("filters (non-interactive mode)")
    filt.add_argument(
        "--band", "-b",
        nargs="+",
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

    # Output
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
    """Non-interactive streaming mode."""
    if args.node:
        host, port = KNOWN_CLUSTERS[args.node]
    else:
        host = args.host
        port = args.port

    spot_filter: Optional[SpotFilter] = None
    if any([args.band, args.dx_prefix, args.spotter_prefix, args.comment]):
        spot_filter = SpotFilter()
        if args.band:
            spot_filter.band(*args.band)
        if args.dx_prefix:
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
        reconnect=not args.no_reconnect,
    )

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
            print(spot.to_json() if args.json else str(spot), flush=True)
            count += 1
            if args.count and count >= args.count:
                feed.stop()
                break
    except KeyboardInterrupt:
        if not args.json:
            print(f"\nStopped after {count} spot(s).")

    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Decide mode: interactive shell vs. direct streaming
    use_interactive = args.interactive or (not args.node and not args.host)

    if use_interactive:
        # In interactive mode the shell surfaces errors itself; suppress library
        # log output so it doesn't bleed raw text into the shell UI.
        # --verbose overrides this and shows DEBUG logs for troubleshooting.
        log_level = logging.DEBUG if args.verbose else logging.ERROR
    else:
        log_level = logging.DEBUG if args.verbose else logging.WARNING

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if use_interactive:
        from tui import DXClusterTUI
        sys.exit(asyncio.run(DXClusterTUI().run()))
    else:
        sys.exit(asyncio.run(_stream(args)))


if __name__ == "__main__":
    main()
