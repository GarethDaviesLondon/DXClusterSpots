"""DXClusterSpots – Visual Studio project entry point.

This file is the StartupFile configured in DXClusterSpots.pyproj.

Run from the command line:
    python DXClusterSpots.py --help
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --band 20m 40m
    python DXClusterSpots.py --node gb7mbc --callsign G0ABC --json

Or press F5 in Visual Studio to launch with the arguments configured in
Project > Properties > Debug.
"""

import os
import sys

# Ensure the project directory is on sys.path so that both the `dxcluster`
# package and `cli` module are importable whether launched from VS or the
# command line.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cli import main  # noqa: E402

if __name__ == "__main__":
    main()
