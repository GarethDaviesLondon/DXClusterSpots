"""Rolling 24-hour spot log with frequency and callsign search.

Spots are appended to a plain NDJSON file (one JSON object per line).
On startup the file is read and trimmed to the last 24 hours in-place,
so the file never grows beyond one day of traffic.

Search methods return spots ordered chronologically (oldest first).

WHY NDJSON (Newline-Delimited JSON)?
-------------------------------------
NDJSON stores one JSON object per line with no trailing comma and no
surrounding array.  This format has several advantages for a spot log:

1.  Append-only writes: adding a new spot is a single write(line + "\\n")
    with the file opened in "a" mode.  Standard JSON (a top-level array)
    would require parsing and re-serialising the entire file on every append —
    completely impractical at cluster data rates of 10–200 spots per minute.

2.  Stream-parseable at any size: the reader iterates lines one at a time
    with json.loads(), so memory consumption is proportional to the size of a
    single spot record, not the size of the entire file.

3.  Human-readable and grep-friendly: each line is a self-contained JSON
    object.  An operator can run ``grep VK9XY spots.ndjson`` or pipe through
    ``jq`` to extract spots without any custom tooling.

4.  No schema migrations: adding a new field to DXSpot simply means new log
    lines have that field; old lines silently use defaults via from_dict().

5.  No external dependency: SQLite would be faster for large datasets but
    introduces a different API, potential locking issues under multi-process
    access, and binary blobs that cannot be inspected without tooling.

The NDJSON trade-off is that individual files are not valid JSON (a JSON
parser would reject the file as a whole), but every common language has
libraries that handle NDJSON, and the format is trivially hand-readable.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import List

from .models import DXSpot

logger = logging.getLogger(__name__)

# A timedelta constant for the rolling window size.
# Defined at module level so it is constructed once and reused rather than
# being recreated on every call to _cutoff() or search_*.
# Using a named constant (_24H) rather than inlining timedelta(hours=24)
# everywhere makes it immediately clear what the constant means and makes
# future changes (e.g. to a 12-hour window) a one-line edit.
_24H = timedelta(hours=24)


class SpotLog:
    """Append-only rolling log of DX spots covering the last 24 hours.

    The log maintains two parallel representations of the same data:

    In-memory (_spots list)
        The primary store for all search queries.  A plain Python list of
        DXSpot objects, maintained in chronological order (oldest first).
        All searches are O(n) list comprehensions — fast enough for up to
        ~175,000 spots/day (the high end of busy HF cluster traffic) without
        indexing or a database.

    On-disk (NDJSON file)
        The durable store that survives process restarts.  Written
        append-only during normal operation; rewritten in full once at
        startup to trim entries older than 24 hours.

    WHY the duality rather than just one store?
        Memory alone loses all history on restart.  Disk alone requires I/O
        on every search.  Together: disk provides durability; memory provides
        fast, zero-I/O searches.  The two are kept in sync by:
          - append(): adds to _spots first, then writes to disk.
          - _load(): reads from disk at startup and populates _spots.

    Thread safety:
        This class is NOT thread-safe.  All calls must originate from the
        same thread (the asyncio event loop).  Adding a lock would be
        straightforward but is unnecessary for the current single-threaded
        architecture.
    """

    def __init__(self, log_file: str) -> None:
        """Initialise the log, loading any existing data from *log_file*.

        Args:
            log_file: Absolute path to the NDJSON log file.  Parent directories
                      are created automatically by _write_line() and _load()
                      if they do not already exist.
        """
        # Store the path as an instance attribute so all methods share it.
        # An instance attribute (rather than a module global) makes SpotLog
        # easy to test: each test can point at a different temporary file.
        self._path = log_file

        # In-memory mirror of the on-disk file.  Populated synchronously by
        # _load() before the constructor returns, so callers can immediately
        # run searches after constructing a SpotLog.
        self._spots: list[DXSpot] = []

        # Load historical spots from disk.  This is blocking/synchronous
        # because nothing in the application can usefully run until the
        # historical data is available for searches.
        self._load()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, spot: DXSpot) -> None:
        """Record a new spot in memory and append it to the log file.

        This is a "write-through" operation:
          1. The spot is added to the in-memory list immediately so it is
             searchable in the same event-loop turn it arrives.
          2. The spot is written to disk so it survives a process restart.

        File writes are best-effort: if the write fails (disk full, permissions
        changed, NFS stale handle) a warning is logged but the exception is
        NOT re-raised.  The spot remains in _spots and the receiver loop
        continues uninterrupted — losing persistence for one spot is far less
        bad than crashing the entire session.

        Args:
            spot: A DXSpot freshly received and parsed from the telnet stream.
        """
        self._spots.append(spot)
        # to_json() returns a compact single-line JSON string (no indentation).
        # _write_line() appends a newline so the file stays well-formed NDJSON.
        self._write_line(spot.to_json())

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_frequency(
        self,
        freq_khz: float,
        window_khz: float = 5.0,
        hours: float = 24,
    ) -> List[DXSpot]:
        """Return all spots within *window_khz* of *freq_khz* in the last *hours*.

        WHY a window rather than exact-frequency match?
            Spotters often round to the nearest kHz and their VFOs may be
            slightly off-frequency.  A ±5 kHz window (default) catches all
            realistic reports of the same station while being narrow enough
            to exclude spots on adjacent channels.  abs() makes the window
            symmetric: spots above and below freq_khz are treated equally.

        WHY sorted() rather than relying on _spots being in order already?
            _spots is maintained approximately in chronological order (append
            order), but sorted() makes the guarantee explicit.  The sort key
            is received_at (a datetime), which handles midnight boundaries
            correctly.  Because the input is nearly sorted, Python's Timsort
            runs in close to O(n) time — the overhead is negligible.

        WHY return oldest-first rather than newest-first?
            Operators reading a display of spots typically want to see the
            most recent activity at the bottom (scrolling down = forward in
            time), which is the natural order for chronological (oldest-first)
            data in a terminal or web UI.

        Args:
            freq_khz:   Centre frequency in kHz.
            window_khz: Half-width of the search window in kHz (default 5.0).
            hours:      Rolling time window in hours (default 24).

        Returns:
            List of matching DXSpot objects, sorted oldest-first.  Empty list
            if no spots match.
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return sorted(
            [
                s for s in self._spots
                # Time gate: only spots within the requested rolling window.
                if s.received_at >= cutoff
                # Frequency gate: symmetric window around freq_khz.
                and abs(s.frequency - freq_khz) <= window_khz
            ],
            key=lambda s: s.received_at,
        )

    def search_callsign(
        self,
        pattern: str,
        hours: float = 24,
    ) -> List[DXSpot]:
        """Return all spots where the DX callsign or spotter matches *pattern*.

        The match is a case-insensitive substring search: a query for "ON4"
        returns spots with DX callsigns or spotters like "ON4KST", "ON4BHQ", etc.
        Results are ordered chronologically (oldest first).

        WHY substring (``in``) rather than prefix or exact match?
            Users may want to find a partial callsign (e.g. "VK9" to find all
            Cocos-Keeling spots), a full callsign with portable suffix (e.g.
            "G3SXW" to match "EA5/G3SXW" and "G3SXW/P"), or a unique fragment.
            Substring is the most flexible single operation without needing
            regex syntax.

        WHY search both dx_callsign AND spotter?
            An operator may want to find all spots *posted by* a station (to
            see what that station has been hearing, hence propagation data) as
            well as all spots *of* that station.  One call handles both cases.

        WHY upper() the pattern once rather than using case-insensitive search?
            dx_callsign is stored in upper-case (normalised by the parser).
            spotter may have mixed case in edge cases.  Converting pattern to
            upper-case once outside the list comprehension — and calling
            .upper() on spotter inside — is cheaper than re.IGNORECASE or
            repeated pattern.lower() calls inside a tight loop.

        Args:
            pattern: Callsign fragment to search for (case-insensitive).
            hours:   Rolling time window in hours (default 24).

        Returns:
            List of matching DXSpot objects, sorted oldest-first.  Empty list
            if no spots match.
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        # Convert once outside the comprehension — O(1) instead of O(n).
        upper = pattern.upper()
        return sorted(
            [
                s for s in self._spots
                if s.received_at >= cutoff
                and (upper in s.dx_callsign.upper() or upper in s.spotter.upper())
            ],
            key=lambda s: s.received_at,
        )

    def size(self) -> int:
        """Return the number of spots currently held in the in-memory log."""
        return len(self._spots)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cutoff(self) -> datetime:
        """Return the UTC datetime exactly 24 hours ago.

        Extracted into a method so that tests can monkeypatch it to simulate
        time passing without mocking the datetime class itself — a much more
        surgical approach that doesn't affect other code relying on datetime.
        """
        return datetime.utcnow() - _24H

    def _write_line(self, line: str) -> None:
        """Append one NDJSON line (no trailing newline) to the backing file.

        WHY open in "a" (append) mode and re-open on every call?
            Append mode guarantees that each write goes to the end of the
            file even if multiple processes write simultaneously (on Linux,
            single write() calls up to PIPE_BUF bytes are atomic at the OS
            level).  Re-opening the file handle on every call means external
            tools (tail -f, logrotate) always see a consistent file — there
            is no buffered data sitting in a long-lived file handle that has
            not been flushed.

        WHY os.makedirs(exist_ok=True) on every write?
            The directory might not exist on first run, or it might be deleted
            while the process is running (e.g. by an operator clearing log
            directories).  Creating the tree unconditionally is cheaper than
            a separate existence check and eliminates a time-of-check /
            time-of-use race condition.  exist_ok=True makes the call a
            no-op when the directory already exists.

        WHY catch Exception broadly rather than specific I/O errors?
            Disk-full (OSError with errno.ENOSPC), permission denied
            (PermissionError), NFS stale handle (OSError), and write-through
            failures all raise different exception types.  Catching Exception
            broadly ensures that any unexpected I/O problem is logged and
            swallowed rather than crashing the receiver.

        Args:
            line: A compact JSON string (from DXSpot.to_json()).  Must not
                  contain embedded newlines — NDJSON requires one object per
                  physical line.
        """
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")  # NDJSON: one JSON object per line
        except Exception as exc:
            # Log the failure but do not propagate.  The spot is already in
            # the in-memory list; only its disk persistence is lost.
            logger.warning("SpotLog write error: %s", exc)

    def _load(self) -> None:
        """Load the last 24 hours from the log file, then rewrite it trimmed.

        This method runs ONCE at startup.  It performs two sequential phases:

        PHASE 1 — READ AND FILTER
            Read every line from the existing NDJSON file.  Parse each line
            as a DXSpot via json.loads() + DXSpot.from_dict().  Keep only
            spots whose received_at is within the last 24 hours; silently
            discard older ones and any lines that fail to parse.

        PHASE 2 — REWRITE (TRIM)
            Write only the kept spots back to the file in "w" mode, which
            truncates and replaces the previous file contents.

        WHY rewrite the file at startup rather than appending forever?
            Without trimming the file grows without bound.  At 200 spots/min
            that is 288,000 spots/day, or roughly 86 MB/day at ~300 bytes
            per NDJSON line.  After a month the file would be 2.6 GB and
            startup would take seconds.  Trimming at startup keeps the file
            at a stable size: always at most one day of traffic, regardless
            of how long the service has been running.

        WHY at startup specifically rather than a background task?
            - No concurrency: nothing else writes to the file during _load().
            - No timer or thread needed — simpler code.
            - Idempotent: restarting the process multiple times produces the
              same file state, which makes crash recovery straightforward.
            - Correctness: the startup rewrite also repairs partially-written
              lines left by a previous crash mid-write; only successfully
              parsed lines are included in the output.

        Error-tolerant line parsing (inner try/except):
            Each line is parsed inside its own try/except so that a single
            corrupt line does not prevent the rest of the file from loading.
            We do NOT log individual line failures to avoid flooding the log
            when a file has many corrupted lines (e.g. after a crash during
            a high-traffic burst).

        File-level error handling (outer try/except):
            If the file cannot be opened at all (permissions, I/O error) we
            log a warning and return early.  _spots stays empty and the
            session starts with no history — annoying but not fatal.

        Sort before storing:
            Spots in the file are normally in chronological order, but a
            system clock adjustment or crash-and-restart sequence can leave
            slightly out-of-order entries.  Sorting by received_at once at
            load time means _spots is always chronological, which makes the
            sorted() call in search methods nearly free (Timsort is O(n) on
            already-sorted input).
        """
        if not os.path.exists(self._path):
            # First run — the log file does not exist yet.  _spots stays empty
            # and the file will be created by the first call to _write_line().
            return

        cutoff = self._cutoff()
        kept: list[DXSpot] = []

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    # Strip all leading/trailing whitespace, including the
                    # trailing newline added by _write_line().  A line that is
                    # empty after stripping is a blank line or a bare newline —
                    # skip it without attempting to parse.
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)        # parse the NDJSON line
                        spot = DXSpot.from_dict(d) # reconstruct the DXSpot
                        if spot.received_at >= cutoff:
                            # Spot is within the 24-hour window — keep it.
                            kept.append(spot)
                        # Spots older than cutoff are silently dropped here.
                        # They will not appear in the rewritten file, which is
                        # exactly the trimming behaviour we want.
                    except Exception:
                        # Line is malformed JSON, has missing required fields,
                        # has an unparseable timestamp, etc.  Skip silently.
                        # The outer loop continues to the next line.
                        pass
        except Exception as exc:
            # File-level error — log and bail out.  _spots stays empty.
            logger.warning("SpotLog load error: %s", exc)
            return

        # Sort chronologically so _spots is always oldest-first.
        # This also means the rewritten file will be in chronological order,
        # which is a useful invariant for human inspection and for tools
        # that process the file sequentially.
        self._spots = sorted(kept, key=lambda s: s.received_at)

        # PHASE 2: Rewrite the file with only the kept (recent) spots.
        # Open in "w" mode to truncate the old contents before writing the
        # trimmed subset.  This is a separate try/except from the read phase
        # so that a write failure here does not discard _spots — the in-memory
        # data is already correct and the application can serve searches from
        # it even if the on-disk trim fails.
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                # "w" mode truncates (replaces) the file — intentional.
                # We are replacing the entire file content with the trimmed
                # 24-hour subset.
                for spot in self._spots:
                    fh.write(spot.to_json() + "\n")
        except Exception as exc:
            logger.warning("SpotLog trim error: %s", exc)
