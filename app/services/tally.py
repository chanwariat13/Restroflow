"""
app/services/tally.py — Tally export.

Indian restaurant owners overwhelmingly use Tally Prime / Tally ERP 9 for
their books. Asking them to manually re-key every day's restaurant sales
is the #1 reason "we'll think about it" turns into a no.

This module produces two artefacts the owner can hand to their CA on the
1st of every month with one click:

1. A CSV with one row per Sales voucher (date, voucher_no, party,
   item summary, taxable amount, CGST, SGST, IGST, total, payment).
   Drop straight into Excel.

2. A Tally XML that imports natively via Gateway → Import → Vouchers.
   Each Order becomes a Sales Voucher with the right ledger postings:
     - "Sales — Restaurant"     (taxable value, sales account)
     - "CGST Output @ rate"     (intra-state)
     - "SGST Output @ rate"     (intra-state)
     - "IGST Output @ rate"     (inter-state)
     - "Cash" / "Bank — UPI" / "Bank — Razorpay" (payment ledger)
   We use the SAC code already on the order (default 996331,
   "Hotel/restaurant services").

We never call Tally directly; Tally has no live API. The user uploads
the XML in Tally's import wizard. That's the standard ICAI workflow.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Iterable, List, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xescape

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────
def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def _date_for_tally(s: str) -> str:
    """Tally wants YYYYMMDD. Order.date_only is 'd/m/YYYY' (no leading zeros).

    The `%-d/%-m/%Y` format literal is a GNU-libc strftime *output* extension
    and is NOT understood by `strptime` on any platform — it always raises
    `ValueError`. The plain `%d/%m/%Y` parser handles both '7/3/2025' and
    '07/03/2025' on every platform, so the GNU-only entry was just a dead
    branch and has been dropped.
    """
    if not s:
        return datetime.now().strftime("%Y%m%d")
    s = str(s).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    # last resort
    try:
        return datetime.fromisoformat(s.split(" ")[0]).strftime("%Y%m%d")
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def _payment_ledger(payment_method: str) -> str:
    pm = (payment_method or "").strip().lower()
    if pm == "cash":
        return "Cash"
    if pm in ("upi", "gpay", "phonepe", "paytm"):
        return "Bank — UPI"
    if pm == "razorpay":
        return "Bank — Razorpay"
    if pm == "card":
        return "Bank — Card"
    return "Cash"


def _items_to_descr(raw_items: str) -> str:
    """
    Order.items in Restroflow is a human-readable comma/newline-separated
    string. For the CSV we collapse it to a single short line, for the
    Tally narration we keep it as-is but bounded.
    """
    txt = (raw_items or "").strip().replace("\n", ", ")
    return (txt[:300] + "…") if len(txt) > 300 else txt


# ── Public surface ─────────────────────────────────────────────────
def build_orders_csv(orders: Iterable[dict], cfg) -> bytes:
    """
    Flat CSV; one row per paid order. UTF-8 with BOM so Excel opens
    Indian-language item names cleanly.
    """
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM for Excel
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow([
        "Voucher Date", "Voucher No", "Customer", "Phone", "Table",
        "GSTIN", "Place of Supply", "HSN/SAC",
        "Items", "Taxable Amount", "CGST", "SGST", "IGST",
        "Total Tax", "Total", "Payment", "Status",
    ])

    for o in orders:
        date = (o.get("date_only") or o.get("date") or "")
        voucher = o.get("order_id") or f"#{o.get('id', '')}"
        items = _items_to_descr(o.get("items"))
        subtotal = _money(o.get("subtotal"))
        cgst = _money(o.get("cgst_amount"))
        sgst = _money(o.get("sgst_amount"))
        igst = _money(o.get("igst_amount"))
        tax = _money(o.get("tax")) or round(cgst + sgst + igst, 2)
        total = _money(o.get("total"))
        w.writerow([
            date, voucher,
            o.get("customer_name") or "",
            o.get("phone") or "",
            o.get("table_name") or "",
            o.get("customer_gstin") or "",
            o.get("place_of_supply") or "",
            o.get("hsn_code") or "996331",
            items,
            f"{subtotal:.2f}", f"{cgst:.2f}", f"{sgst:.2f}", f"{igst:.2f}",
            f"{tax:.2f}", f"{total:.2f}",
            o.get("payment_method") or "",
            o.get("status") or "",
        ])
    return buf.getvalue().encode("utf-8")


def build_tally_xml(orders: Iterable[dict], cfg) -> bytes:
    """
    Tally Day Book voucher import XML. Each Order is a Sales Voucher.

    We avoid building the XML through ElementTree's tree (Tally's
    import is whitespace-fussy) and emit the canonical envelope used by
    Tally Prime's voucher import directly.
    """
    company_name = (
        getattr(cfg, "legal_name", "") or
        getattr(cfg, "restaurant_name", "") or
        "Default Company"
    )
    sales_ledger = "Sales — Restaurant"
    cgst_ledger_tpl = "CGST Output @ {rate:g}%"
    sgst_ledger_tpl = "SGST Output @ {rate:g}%"
    igst_ledger_tpl = "IGST Output @ {rate:g}%"

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    parts.append("<ENVELOPE>")
    parts.append("<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>")
    parts.append("<BODY><IMPORTDATA>")
    parts.append("<REQUESTDESC>")
    parts.append("<REPORTNAME>Vouchers</REPORTNAME>")
    parts.append(
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{_xescape(company_name)}"
        f"</SVCURRENTCOMPANY></STATICVARIABLES>"
    )
    parts.append("</REQUESTDESC>")
    parts.append("<REQUESTDATA>")

    count = 0
    for o in orders:
        count += 1
        voucher_no = o.get("order_id") or f"R{o.get('id', count)}"
        guid = f"restroflow-{voucher_no}-{count}"
        date_yyyymmdd = _date_for_tally(o.get("date_only") or o.get("date"))
        narration = (
            f"Restaurant sales — Table {o.get('table_name','')} — "
            f"Customer {o.get('customer_name','')}. "
            f"Items: {_items_to_descr(o.get('items'))}"
        )
        party = o.get("customer_name") or "Walk-in Customer"
        gstin = (o.get("customer_gstin") or "").strip()

        subtotal = _money(o.get("subtotal"))
        cgst = _money(o.get("cgst_amount"))
        sgst = _money(o.get("sgst_amount"))
        igst = _money(o.get("igst_amount"))
        total = _money(o.get("total")) or round(subtotal + cgst + sgst + igst, 2)
        is_inter = bool(o.get("is_inter_state")) or igst > 0

        # Effective tax rate (used in ledger names so Tally aggregates
        # correctly per slab — 5% / 12% / 18% etc.)
        if is_inter and subtotal:
            rate = round((igst / subtotal) * 100, 2) if subtotal else 0
        elif subtotal:
            rate = round(((cgst + sgst) / subtotal) * 100, 2)
        else:
            rate = 0

        payment_ledger = _payment_ledger(o.get("payment_method"))

        parts.append(
            f'<TALLYMESSAGE xmlns:UDF="TallyUDF">'
            f'<VOUCHER REMOTEID="{_xescape(guid)}" '
            f'VCHTYPE="Sales" ACTION="Create">'
            f'<DATE>{date_yyyymmdd}</DATE>'
            f'<NARRATION>{_xescape(narration)}</NARRATION>'
            f'<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>'
            f'<VOUCHERNUMBER>{_xescape(voucher_no)}</VOUCHERNUMBER>'
            f'<PARTYLEDGERNAME>{_xescape(party)}</PARTYLEDGERNAME>'
            f'<EFFECTIVEDATE>{date_yyyymmdd}</EFFECTIVEDATE>'
            f'<ISINVOICE>Yes</ISINVOICE>'
            f'<GSTREGISTRATIONTYPE>'
            f'{"Regular" if gstin else "Consumer"}'
            f'</GSTREGISTRATIONTYPE>'
            f'<PLACEOFSUPPLY>{_xescape(o.get("place_of_supply") or "")}'
            f'</PLACEOFSUPPLY>'
        )
        if gstin:
            parts.append(f'<PARTYGSTIN>{_xescape(gstin)}</PARTYGSTIN>')

        # Party ledger entry: total receivable / collected (Dr).
        parts.append(
            f'<ALLLEDGERENTRIES.LIST>'
            f'<LEDGERNAME>{_xescape(payment_ledger)}</LEDGERNAME>'
            f'<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>'
            f'<AMOUNT>-{total:.2f}</AMOUNT>'
            f'</ALLLEDGERENTRIES.LIST>'
        )
        # Sales ledger (Cr) — taxable value.
        parts.append(
            f'<ALLLEDGERENTRIES.LIST>'
            f'<LEDGERNAME>{_xescape(sales_ledger)}</LEDGERNAME>'
            f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>'
            f'<AMOUNT>{subtotal:.2f}</AMOUNT>'
            f'</ALLLEDGERENTRIES.LIST>'
        )
        # GST ledgers (Cr).
        if is_inter and igst > 0:
            parts.append(
                f'<ALLLEDGERENTRIES.LIST>'
                f'<LEDGERNAME>'
                f'{_xescape(igst_ledger_tpl.format(rate=rate))}'
                f'</LEDGERNAME>'
                f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>'
                f'<AMOUNT>{igst:.2f}</AMOUNT>'
                f'</ALLLEDGERENTRIES.LIST>'
            )
        else:
            half = round(rate / 2, 2)
            if cgst > 0:
                parts.append(
                    f'<ALLLEDGERENTRIES.LIST>'
                    f'<LEDGERNAME>'
                    f'{_xescape(cgst_ledger_tpl.format(rate=half))}'
                    f'</LEDGERNAME>'
                    f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>'
                    f'<AMOUNT>{cgst:.2f}</AMOUNT>'
                    f'</ALLLEDGERENTRIES.LIST>'
                )
            if sgst > 0:
                parts.append(
                    f'<ALLLEDGERENTRIES.LIST>'
                    f'<LEDGERNAME>'
                    f'{_xescape(sgst_ledger_tpl.format(rate=half))}'
                    f'</LEDGERNAME>'
                    f'<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>'
                    f'<AMOUNT>{sgst:.2f}</AMOUNT>'
                    f'</ALLLEDGERENTRIES.LIST>'
                )
        parts.append("</VOUCHER></TALLYMESSAGE>")

    parts.append("</REQUESTDATA>")
    parts.append("</IMPORTDATA></BODY>")
    parts.append("</ENVELOPE>")
    return "".join(parts).encode("utf-8")


def fetch_orders_for_export(cfg, *, from_date: str, to_date: str,
                            include_unpaid: bool = False) -> List[dict]:
    """
    Pull paid orders from the tenant DB in the given date range. Date
    columns in Restroflow are stored as `d/m/YYYY` strings (no leading
    zeros). To stay robust against that format we convert through
    `to_date()` on read.
    """
    from sqlalchemy import text
    where = []
    params = {"from": from_date, "to": to_date}
    where.append("(date_only IS NOT NULL AND date_only <> '')")
    where.append(
        "to_date(date_only, 'FMDD/FMMM/YYYY') BETWEEN :from::date AND :to::date"
    )
    if not include_unpaid:
        where.append("status = 'Paid'")
    sql = (
        "SELECT id, order_id, date, date_only, customer_name, phone, "
        "table_name, items, subtotal, tax, total, payment_method, status, "
        "billed, customer_gstin, cgst_amount, sgst_amount, igst_amount, "
        "place_of_supply, is_inter_state, hsn_code "
        "FROM orders WHERE " + " AND ".join(where) +
        " ORDER BY to_date(date_only, 'FMDD/FMMM/YYYY'), id"
    )
    with cfg.db_session() as db:
        rows = db.execute(text(sql), params).fetchall()
    return [dict(r._mapping) for r in rows]
