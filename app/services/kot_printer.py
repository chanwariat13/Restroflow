"""
app/services/kot_printer.py — Kitchen Order Ticket (KOT) printer driver.

Indian restaurants overwhelmingly run on cheap thermal printers
(Epson TM-T82, TVS RP-3160, Posiflex Aura, etc.) wired to the LAN. Every
one of them speaks ESC/POS over a raw TCP socket on port 9100 — the same
"raw print" port a Windows queue would dial directly.

We implement just enough ESC/POS to print a clean, kitchen-friendly KOT
with bold headers, large item lines, the table number, the order id,
modifier notes, and a guillotine cut at the end. No dependency on
python-escpos because pulling Pillow + libusb into a small FastAPI image
isn't worth it for a few control bytes.

If the printer is unreachable, the function does NOT raise — it returns a
status dict the caller can show in the dashboard. Kitchens print second
copies by hand all the time; we should never crash an order over a flaky
printer.

Hardware tested layouts:
- 80mm paper, 42 chars per line   (default / `cpl=42`)
- 58mm paper, 32 chars per line   (`cpl=32`)
"""
from __future__ import annotations
import socket
import logging
from datetime import datetime
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ── ESC/POS control bytes ────────────────────────────────────────────────
ESC = b"\x1b"
GS  = b"\x1d"

INIT          = ESC + b"@"            # initialise printer
ALIGN_LEFT    = ESC + b"a\x00"
ALIGN_CENTER  = ESC + b"a\x01"
ALIGN_RIGHT   = ESC + b"a\x02"
BOLD_ON       = ESC + b"E\x01"
BOLD_OFF      = ESC + b"E\x00"
DOUBLE_HEIGHT = GS  + b"!\x01"
DOUBLE_BOTH   = GS  + b"!\x11"
NORMAL_SIZE   = GS  + b"!\x00"
CUT           = GS  + b"V\x42\x00"   # full cut + 0 lines feed
LF            = b"\n"


def _enc(s: str) -> bytes:
    """Encode a string for the printer. CP437 covers ASCII + most ₹ glyphs;
    we substitute when needed."""
    return s.replace("₹", "Rs.").encode("cp437", errors="replace")


def _line(s: str, cpl: int = 42) -> bytes:
    """Wrap a single line of text to the column width."""
    out = b""
    while s:
        out += _enc(s[:cpl]) + LF
        s = s[cpl:]
    return out


def _row(left: str, right: str, cpl: int = 42) -> bytes:
    """Two-column row: left-aligned label, right-aligned value."""
    right = right or ""
    space = cpl - len(left) - len(right)
    if space < 1:
        # value too long, push to next line
        return _line(left, cpl) + _line(right.rjust(cpl), cpl)
    return _enc(left + " " * space + right) + LF


def _hr(cpl: int = 42) -> bytes:
    return _enc("-" * cpl) + LF


def build_kot_payload(
    *,
    restaurant_name: str,
    order_id: str,
    table: str,
    items: Iterable[dict],
    customer_name: str = "",
    notes: str = "",
    is_reprint: bool = False,
    header_text: str = "",
    cpl: int = 42,
) -> bytes:
    """
    Construct the raw bytes to push to the printer.

    `items` is an iterable of dicts: {"name": str, "quantity": int, "modifiers": list[str]?, "notes": str?}
    """
    now = datetime.now().strftime("%d-%b %H:%M")
    buf = bytearray()
    buf += INIT

    # Banner
    buf += ALIGN_CENTER + BOLD_ON + DOUBLE_HEIGHT
    buf += _line(("REPRINT KOT" if is_reprint else "KOT"), cpl)
    buf += NORMAL_SIZE + BOLD_OFF
    buf += _line(restaurant_name[:cpl], cpl)
    if header_text:
        buf += _line(header_text[:cpl], cpl)
    buf += _hr(cpl)

    # Meta
    buf += ALIGN_LEFT
    buf += _row(f"Table: {table}", now, cpl)
    buf += _row(f"Order: {order_id}", customer_name[:20], cpl)
    buf += _hr(cpl)

    # Items — each line large for kitchen visibility
    buf += BOLD_ON
    for it in items:
        qty   = int(it.get("quantity") or it.get("qty") or 1)
        name  = str(it.get("name", "")).strip() or "?"
        # Big item line: "2x Paneer Tikka Masala"
        buf += DOUBLE_HEIGHT
        buf += _line(f"{qty}x {name}"[: cpl], cpl)
        buf += NORMAL_SIZE

        mods = it.get("modifiers") or it.get("variants") or []
        if isinstance(mods, str):
            mods = [m.strip() for m in mods.split(",") if m.strip()]
        for m in mods:
            buf += _line(f"  + {m}"[: cpl], cpl)

        item_notes = (it.get("notes") or "").strip()
        if item_notes:
            buf += _line(f"  >> {item_notes}"[: cpl], cpl)
    buf += BOLD_OFF
    buf += _hr(cpl)

    if notes:
        buf += BOLD_ON
        buf += _line("NOTE:", cpl)
        buf += BOLD_OFF
        for chunk in notes.split("\n"):
            buf += _line(chunk[:cpl], cpl)
        buf += _hr(cpl)

    # Footer + cut
    buf += LF + LF + LF
    buf += CUT
    return bytes(buf)


def send_to_printer(payload: bytes, ip: str, port: int = 9100,
                    timeout: float = 4.0) -> dict:
    """
    Push raw bytes to the printer over TCP. Returns:
        {"ok": True, "bytes_sent": N}            on success
        {"ok": False, "error": "<msg>"}          on any failure

    Never raises — the caller decides what to do with a failed KOT (most
    POS systems just queue them and let staff retry from the order screen).
    """
    if not ip:
        return {"ok": False, "error": "No printer IP configured"}
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(payload)
        return {"ok": True, "bytes_sent": len(payload)}
    except (socket.timeout, OSError) as e:
        logger.warning("KOT printer %s:%s unreachable: %s", ip, port, e)
        return {"ok": False, "error": f"printer unreachable: {e}"}


def print_kot(
    *,
    restaurant_name: str,
    order_id: str,
    table: str,
    items: Iterable[dict],
    printer_ip: str,
    printer_port: int = 9100,
    customer_name: str = "",
    notes: str = "",
    is_reprint: bool = False,
    header_text: str = "",
    cpl: int = 42,
) -> dict:
    """High-level helper: build payload + send. Returns a status dict."""
    if not printer_ip:
        return {"ok": False, "error": "kot_printer_ip is not set for this client"}
    payload = build_kot_payload(
        restaurant_name=restaurant_name,
        order_id=order_id,
        table=table,
        items=list(items),
        customer_name=customer_name,
        notes=notes,
        is_reprint=is_reprint,
        header_text=header_text,
        cpl=cpl,
    )
    return send_to_printer(payload, printer_ip, printer_port)


def parse_items_from_string(items_text: str) -> list:
    """
    Reverse-parse the human-readable `Order.items` text column we already
    store into the structured shape `build_kot_payload` wants. We're loose
    on purpose — the existing column format is "{qty}x {name}, ..." or
    newline-separated.
    """
    items_text = (items_text or "").strip()
    if not items_text:
        return []
    out = []
    for raw in items_text.replace(",", "\n").splitlines():
        s = raw.strip()
        if not s:
            continue
        qty = 1
        name = s
        # try "2x Foo" or "2 x Foo" or "(2) Foo"
        for sep in (" x ", "x ", "X "):
            if sep in s[:6].lower() or sep in s[:6]:
                head, _, tail = s.partition(sep)
                if head.strip().isdigit():
                    qty = int(head.strip())
                    name = tail.strip()
                    break
        out.append({"name": name, "quantity": qty})
    return out
