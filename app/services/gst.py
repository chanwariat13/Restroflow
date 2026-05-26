"""
app/services/gst.py — Indian GST CGST/SGST/IGST split helper for RestroFlow.

A copy of the same logic used by the hotel side: place-of-supply for a
restaurant service (HSN 996331) is the location of the restaurant itself, so
for any in-store dining the supply is INTRA-state and the tax must be split
into CGST + SGST. For takeaway / corporate-billed orders to a customer in a
different state (e.g. an event catered to a B2B client whose GSTIN is in
another state), it becomes inter-state and the single IGST line applies.

We keep this module pure (no DB / no SQLAlchemy import) so it's safe to
import from any layer.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple


# Common HSN/SAC codes used by Indian restaurants.
HSN_RESTAURANT_DINEIN  = "996331"  # restaurant service
HSN_RESTAURANT_TAKEAWAY = "996332" # outdoor catering / takeaway in some states
HSN_BEVERAGES          = "220210"
HSN_PACKAGED_FOOD      = "210690"
HSN_OTHER              = "999799"


def _q(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_inter_state(seller_state_code: Optional[str],
                   place_of_supply_code: Optional[str]) -> bool:
    s = (seller_state_code or "").strip()
    p = (place_of_supply_code or "").strip()
    if not s or not p:
        return False
    return s != p


def compute_split(taxable_amount,
                  rate_percent,
                  seller_state_code: Optional[str] = None,
                  place_of_supply_code: Optional[str] = None,
                  inter_state: Optional[bool] = None) -> dict:
    """
    Compute CGST/SGST or IGST for a single line.

    Returns floats (rounded 2dp) so values round-trip via JSON / Postgres
    NUMERIC(10,2). cgst+sgst always equals total_tax to the paisa.
    """
    amt  = _q(taxable_amount)
    rate = Decimal(str(rate_percent or 0))
    inter = bool(inter_state) if inter_state is not None else is_inter_state(seller_state_code, place_of_supply_code)
    total_tax = _q(amt * rate / Decimal("100"))

    if inter:
        cgst = sgst = Decimal("0.00")
        igst = total_tax
    else:
        half = _q(total_tax / Decimal("2"))
        cgst = half
        sgst = _q(total_tax - half)  # absorbs the rounding remainder
        igst = Decimal("0.00")

    return {
        "rate":           float(rate),
        "is_inter_state": inter,
        "cgst":           float(cgst),
        "sgst":           float(sgst),
        "igst":           float(igst),
        "total_tax":      float(total_tax),
        "taxable":        float(amt),
        "total":          float(_q(amt + total_tax)),
    }


def split_flat_tax(taxable_amount, tax_amount,
                   seller_state_code: Optional[str] = None,
                   place_of_supply_code: Optional[str] = None,
                   inter_state: Optional[bool] = None) -> Tuple[float, float, float]:
    """For legacy rows that store only `tax`, derive (cgst, sgst, igst)."""
    inter = bool(inter_state) if inter_state is not None else is_inter_state(seller_state_code, place_of_supply_code)
    tax = _q(tax_amount)
    if inter:
        return 0.0, 0.0, float(tax)
    half = _q(tax / Decimal("2"))
    return float(half), float(_q(tax - half)), 0.0


def state_code_from_gstin(gstin: str) -> str:
    g = (gstin or "").strip().upper()
    if len(g) >= 2 and g[:2].isdigit():
        return g[:2]
    return ""


# Indian state codes (first two digits of any GSTIN issued there).
INDIAN_STATE_CODES = {
    "01":"Jammu & Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh",
    "05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan","09":"Uttar Pradesh",
    "10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur",
    "15":"Mizoram","16":"Tripura","17":"Meghalaya","18":"Assam","19":"West Bengal",
    "20":"Jharkhand","21":"Odisha","22":"Chhattisgarh","23":"Madhya Pradesh","24":"Gujarat",
    "25":"Daman & Diu","26":"Dadra & Nagar Haveli","27":"Maharashtra","28":"Andhra Pradesh (Old)",
    "29":"Karnataka","30":"Goa","31":"Lakshadweep","32":"Kerala","33":"Tamil Nadu",
    "34":"Puducherry","35":"Andaman & Nicobar","36":"Telangana","37":"Andhra Pradesh",
    "38":"Ladakh","97":"Other Territory","99":"Other Country",
}
