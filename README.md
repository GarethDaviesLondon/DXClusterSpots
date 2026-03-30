# DXClusterSpots

A full-featured DX cluster terminal client for amateur radio operators.
Written and released by **ON8CIT**.

> **This project was created entirely by [Claude Code](https://claude.ai/code),
> Anthropic's AI coding assistant, at the direction of ON8CIT.**

---

## What it does

DXClusterSpots connects to any DX cluster node over Telnet and displays
incoming DX spots in a colour-coded, scrolling split-pane terminal UI.
It runs on Linux, macOS, and Windows — anywhere Python 3.9+ is available.

Key features:

- **Split-pane TUI** — live spot feed in the top pane, command prompt at the
  bottom; the two never overlap regardless of terminal size or resize.
- **Powerful filters** — restrict displayed spots by band, mode, CQ zone,
  DX prefix, or spotter prefix. Filters stack and can be saved between sessions.
- **Full 24-hour rolling log** — *every* spot is written to disk regardless of
  active display filters, so you can search historical spots even after changing
  your filter settings.
- **Search** — query the in-memory log by frequency, callsign, or prefix.
- **DXCC lookup** — resolve any callsign or prefix to its DXCC entity, CQ zone,
  and ITU zone instantly without an internet connection.
- **Callbook lookup** — optional online lookup via HamQTH or QRZ.com.
- **Worked / exclude lists** — hide spots for stations you have already worked
  or callsign prefixes you are not interested in.
- **JSON streaming mode** — pipe spots as NDJSON to scripts, web services, or
  log processors.
- **Tab-completion and command history** — readline-style editing with
  persistent history across sessions.
- **Pre-configured cluster list** — 30+ well-known nodes across Europe, North
  America, Asia, and Oceania; any Telnet cluster can be added manually.

---

## Quick start

### 1. Install dependencies

```bash
pip install prompt_toolkit
```

That is the only required package.  Everything else (DXCC database, band plan,
filters, spot parser) uses the Python standard library.

### 2. Run

```bash
python DXClusterSpots/DXClusterSpots.py
```

This opens the interactive TUI. Type `help` (or `h`) at the prompt for a full
command reference.

**Connect to a cluster:**

```
dxcluster> connect gb7mbc ON8CIT
```

Or pick a node by name from the built-in list:

```
dxcluster> nodes
dxcluster> connect pi4cc ON8CIT
```

### 3. Non-interactive / pipe mode

Stream spots directly to stdout without the TUI — useful for scripting:

```bash
# All spots from GB7MBC
python DXClusterSpots/DXClusterSpots.py --node gb7mbc --callsign ON8CIT

# 20 m and 40 m only, as NDJSON
python DXClusterSpots/DXClusterSpots.py --node gb7mbc --callsign ON8CIT \
    --band 20m 40m --json

# Grab 20 spots then exit
python DXClusterSpots/DXClusterSpots.py --node gb7mbc --callsign ON8CIT \
    --count 20
```

---

## TUI command reference

All commands have a one-letter alias shown in brackets.

| Command | Alias | Description |
|---------|-------|-------------|
| `connect <node> <call>` | `c` | Connect to a cluster node |
| `disconnect` | `d` | Disconnect from the current node |
| `stream` | `s` | Toggle spot streaming on/off |
| `filter band <bands…>` | `f b` | Show only the listed bands (e.g. `20m 40m`) |
| `filter mode <modes…>` | `f m` | Show only the listed modes (e.g. `FT8 SSB`) |
| `filter zone add <zones…>` | `f z a` | Restrict to listed CQ zones |
| `filter dx include <pfx…>` | `f d i` | Show only DX with these prefixes |
| `filter dx exclude <pfx…>` | `f d e` | Hide DX with these prefixes |
| `filter show` | `f s` | Display all active filters |
| `filter clear` | `f c` | Remove all filters |
| `search freq <kHz>` | `/ f` | Search log by frequency |
| `search call <callsign>` | `/ c` | Search log by callsign |
| `search prefix <prefix>` | `/ p` | Search log by DXCC prefix |
| `log` | `l` | Show recent spot log entries |
| `lookup <callsign>` | `u` | DXCC/zone lookup |
| `callbook set hamqth\|qrz` | `k s` | Choose callbook provider |
| `callbook show` | `k sh` | Show current callbook config |
| `nodes` | `n` | List known cluster nodes |
| `bands` | `b` | List the built-in band plan |
| `status` | `t` | Show connection status |
| `worked <call…>` | `w` | Mark callsigns as worked (hide) |
| `include <pfx…>` | `i` | Add prefixes to the include list |
| `exclude <pfx…>` | `e` | Add prefixes to the exclude list |
| `save` | `v` | Save current config to disk |
| `config` | `g` | Show current configuration |
| `json` | `j` | Toggle NDJSON output mode |
| `help [command]` | `h` | Show help |
| `quit` | `q` | Exit |

---

## File locations

| File | Linux | Windows |
|------|-------|---------|
| Config | `~/.config/DXClusterSpots/config.json` | `%APPDATA%\DXClusterSpots\config.json` |
| Spot log | `~/.config/DXClusterSpots/spots.log` | `%APPDATA%\DXClusterSpots\spots.log` |
| History | `~/.config/DXClusterSpots/history.txt` | `%APPDATA%\DXClusterSpots\history.txt` |

The spot log is a rolling NDJSON file — one JSON object per line, trimmed to
the last 24 hours on startup.

---

## Documentation

Two reference documents are included in the repository:

- **DXClusterSpots_User_Manual.docx** — complete operator guide covering all
  commands, filters, callbook setup, and worked-station tracking.
- **DXClusterSpots_Technical_Manual.docx** — developer and sysadmin reference
  covering the codebase architecture, config schema, DXCC database maintenance,
  cluster management, filter engine internals, and how to extend the application.

---

## Using as a Python library

The `dxcluster` package can be imported independently of the TUI:

```python
import asyncio
from dxcluster import SpotFeed, SpotFilter

async def main():
    filt = SpotFilter().band("20m", "40m")
    feed = SpotFeed(host="gb7mbc.gb7mbc.ampr.org", port=7300,
                    callsign="ON8CIT", spot_filter=filt)
    async for spot in feed.spots():
        print(spot.dx, spot.freq, spot.comment)

asyncio.run(main())
```

---

## Requirements

- Python 3.9 or later
- `prompt_toolkit >= 3.0` (TUI only — not needed for library or pipe mode)

Optional:

| Package | Enables |
|---------|---------|
| `fastapi` + `uvicorn` | REST API / web service layer |
| `websockets` | WebSocket push endpoint |
| `redis` | Spot caching and fan-out via Redis pub/sub |

---

## Cluster nodes

The application ships with 30+ pre-configured nodes including GB7MBC, PI4CC,
DX.W9AJ, VE7CC, ON4KST, and many others.  Run `nodes` in the TUI to see the
full list.  Any Telnet-accessible cluster can be added with:

```
dxcluster> connect <hostname> <port> <callsign>
```

---

## Licence

This project is released under the
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)
licence.

You are free to share and adapt the material for any purpose, including
commercially, provided you give appropriate credit to **ON8CIT** and indicate
if changes were made.

---

## Credits

- **ON8CIT** — project concept, direction, and testing
- **[Claude Code](https://claude.ai/code)** — all code, documentation, and
  this README were written entirely by Anthropic's Claude Code AI assistant

73 de ON8CIT
