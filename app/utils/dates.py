"""
app/utils/dates.py

Portable date helpers.

The codebase used to call ``dt.strftime("%-d/%-m/%Y")`` in many places to
build the ``date_only`` column that appears across orders, reports, and
customer pages. The ``%-d`` / ``%-m`` ("no leading zeros") specifiers are a
GNU libc extension — they raise ``ValueError`` on macOS, Alpine Linux, and
Windows. The ``python:3.11-slim`` Docker base image happens to support them,
which is why production hasn't broken, but anyone running tests on macOS or
rebuilding on a different base hits a runtime crash on every order.

Use :func:`fmt_date_short` to produce a stable ``D/M/YYYY`` string that works
on every platform. The output is identical to what ``%-d/%-m/%Y`` would have
produced on glibc, so it's drop-in compatible with values already stored in
the database.
"""
from __future__ import annotations

from datetime import date, datetime


def fmt_date_short(dt: date | datetime) -> str:
    """Return ``D/M/YYYY`` (no leading zeros), portably across platforms.

    Examples:
        >>> fmt_date_short(datetime(2025, 3, 7))
        '7/3/2025'
        >>> fmt_date_short(datetime(2025, 12, 25))
        '25/12/2025'
    """
    return f"{dt.day}/{dt.month}/{dt.year}"
