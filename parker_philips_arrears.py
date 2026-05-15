"""
Parker Philips Arrears Pipeline
================================
Accepts five raw exports (as file paths OR pd.DataFrames) and returns a
fully-typed PipelineResult. No database, no HTTP — pure data logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Dict, List, Optional, Union

import pandas as pd


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CaseRecord:
    reference: str
    client_name: str
    mobile: str
    case_type: str                   # 'IVA' | 'CPI' | 'TD'
    payment_amount: float
    arrears_amount: float
    cycle: str
    cycle_status: str                # 'New' | 'History of arrears' | '' — pipeline sets ''
    months_in_arrears: float
    last_payment_due_date: Optional[date]
    days_since_last_payment_due: Optional[int]
    payment_break: bool
    catchup_agreed: bool
    catchup_amount: Optional[float]
    vulnerable: bool
    case_senior: str
    last_contact_date: Optional[datetime]
    last_contact_notes: str          # truncated to 2000 chars
    case_status: str
    needs_manual_review: bool
    review_reason: str
    sources_present: List[str]
    # per-source breakdown for reconciliation display:
    iva_fees_arrears: Optional[float]
    wf_arrears_amount: Optional[float]
    cases_in_arrears_amount: Optional[float]
    td_arrears_amount: Optional[float]


@dataclass
class PipelineResult:
    snapshot_date: date
    cases: List[CaseRecord]

    # totals
    total_live_iva: int
    total_live_cpi: int
    total_live_td: int
    total_in_arrears: int
    total_arrears_value: float

    # breakdowns
    by_case_type: Dict[str, Dict]
    by_cycle: Dict[str, Dict]
    vulnerable_in_arrears: int
    vulnerable_arrears_value: float
    manual_review_count: int

    warnings: List[str]

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict."""
        def _convert(obj):
            if isinstance(obj, (date, datetime)):
                return obj.isoformat()
            if isinstance(obj, list):
                return [_convert(i) for i in obj]
            if isinstance(obj, dict):
                return {k: _convert(v) for k, v in obj.items()}
            return obj

        d = asdict(self)
        return _convert(d)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CYCLE_ORDER = ["Cycle 1", "Cycle 2", "Cycle 3", "Cycle 4", "Cycle 5+"]


def _load_df(
    source: Union[str, pd.DataFrame],
    header_row: int,
    required_cols: List[str],
    label: str,
) -> pd.DataFrame:
    """Load a DataFrame from a file path or pass through an existing one.
    Normalises column names (strip whitespace). Validates required columns.
    """
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    else:
        path = str(source)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".xls":
            df = pd.read_excel(path, header=header_row, engine="xlrd")
        elif ext in (".xlsx", ".xlsm"):
            df = pd.read_excel(path, header=header_row, engine="openpyxl")
        else:
            # Try openpyxl as a fallback for unknown extensions
            try:
                df = pd.read_excel(path, header=header_row, engine="openpyxl")
            except Exception:
                df = pd.read_excel(path, header=header_row)

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]

    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"{label}: missing required column '{col}'")

    return df


def _safe_str(val) -> str:
    """Return a stripped string, empty string for NaN/None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val).strip()


def _safe_float(val) -> Optional[float]:
    """Return a float or None for NaN/None/empty."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, str):
        val = val.replace(",", "").replace("£", "").strip()
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_date(val) -> Optional[date]:
    """Parse a date value to a Python date, or None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                pass
    return None


def _safe_datetime(val) -> Optional[datetime]:
    """Parse to datetime or None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, pd.Timestamp):
        return val.to_pydatetime()
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        for fmt in (
            "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                pass
    return None


def _compute_cycle(months: float) -> str:
    if months > 0 and months < 2:
        return "Cycle 1"
    if months < 3:
        return "Cycle 2"
    if months < 4:
        return "Cycle 3"
    if months < 5:
        return "Cycle 4"
    return "Cycle 5+"


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------

def run_pipeline(
    iva_fees: Union[str, pd.DataFrame],
    td_fees: Union[str, pd.DataFrame],
    cases_in_arrears: Union[str, pd.DataFrame],
    wf_arrears: Union[str, pd.DataFrame],
    total_live_cases: Union[str, pd.DataFrame],
    snapshot_date: Optional[date] = None,
) -> PipelineResult:
    """
    Parse, merge, reconcile, and return a PipelineResult.

    Parameters
    ----------
    iva_fees           : .xlsx file path or DataFrame  (header row 2, 0-indexed=1)
    td_fees            : .xlsx file path or DataFrame  (header row 2, 0-indexed=1)
    cases_in_arrears   : .xlsx file path or DataFrame  (header row 3, 0-indexed=2)
    wf_arrears         : .xls  file path or DataFrame  (header row 1, 0-indexed=0)
    total_live_cases   : .xls  file path or DataFrame  (header row 1, 0-indexed=0)
    snapshot_date      : date of snapshot; defaults to today
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    warnings: List[str] = []

    # ------------------------------------------------------------------
    # 1. Load all five sources
    # ------------------------------------------------------------------
    df_iva = _load_df(
        iva_fees, header_row=1,
        required_cols=["Reference", "Client Name", "Payment", "Arrears Level"],
        label="iva_fees",
    )
    df_td = _load_df(
        td_fees, header_row=1,
        required_cols=["Reference", "Client Name", "Payment", "Arrears Level"],
        label="td_fees",
    )
    df_cia = _load_df(
        cases_in_arrears, header_row=2,
        required_cols=["Case Ref", "Arrears"],
        label="cases_in_arrears",
    )
    df_wf = _load_df(
        wf_arrears, header_row=0,
        required_cols=["Case Ref.", "Arrears Amt."],
        label="wf_arrears",
    )
    df_live = _load_df(
        total_live_cases, header_row=0,
        required_cols=["Case Ref.", "Type"],
        label="total_live_cases",
    )

    # ------------------------------------------------------------------
    # 2. Normalise reference columns to strings (never cast to int)
    # ------------------------------------------------------------------
    df_iva["Reference"] = df_iva["Reference"].apply(_safe_str)
    df_td["Reference"] = df_td["Reference"].apply(_safe_str)
    df_cia["Case Ref"] = df_cia["Case Ref"].apply(_safe_str)
    df_wf["Case Ref."] = df_wf["Case Ref."].apply(_safe_str)
    df_live["Case Ref."] = df_live["Case Ref."].apply(_safe_str)

    # Drop rows with empty references
    df_iva = df_iva[df_iva["Reference"] != ""]
    df_td  = df_td[df_td["Reference"] != ""]
    df_cia = df_cia[df_cia["Case Ref"] != ""]
    df_wf  = df_wf[df_wf["Case Ref."] != ""]
    df_live = df_live[df_live["Case Ref."] != ""]

    # ------------------------------------------------------------------
    # 3. Case type detection
    #    CPI  → reference starts with "CPI" (from iva_fees export)
    #    TD   → reference in td_fees
    #    IVA  → everything else in iva_fees
    # ------------------------------------------------------------------
    td_refs = set(df_td["Reference"].tolist())

    def _detect_type(ref: str) -> str:
        if ref.upper().startswith("CPI"):
            return "CPI"
        if ref in td_refs:
            return "TD"
        return "IVA"

    # ------------------------------------------------------------------
    # 4. Build lookup maps for the three supplementary sources
    # ------------------------------------------------------------------
    # WF Arrears lookup: ref -> row (first occurrence)
    wf_map: Dict[str, dict] = {}
    for _, row in df_wf.iterrows():
        ref = _safe_str(row["Case Ref."])
        if ref and ref not in wf_map:
            wf_map[ref] = row.to_dict()

    # Cases-in-arrears lookup
    cia_map: Dict[str, dict] = {}
    for _, row in df_cia.iterrows():
        ref = _safe_str(row["Case Ref"])
        if ref and ref not in cia_map:
            cia_map[ref] = row.to_dict()

    # TD Fees lookup
    td_map: Dict[str, dict] = {}
    for _, row in df_td.iterrows():
        ref = _safe_str(row["Reference"])
        if ref and ref not in td_map:
            td_map[ref] = row.to_dict()

    # Live cases lookup: ref -> row
    live_map: Dict[str, dict] = {}
    for _, row in df_live.iterrows():
        ref = _safe_str(row["Case Ref."])
        if ref and ref not in live_map:
            live_map[ref] = row.to_dict()

    # ------------------------------------------------------------------
    # 5. Combine primary sources: iva_fees + td_fees as primary list
    # ------------------------------------------------------------------
    # iva_fees contains both IVA and CPI mixed; td_fees has TD cases.
    # We build a unified list of all cases that appear in either.
    all_primary_rows: List[dict] = []
    seen_refs: set = set()

    for _, row in df_iva.iterrows():
        ref = _safe_str(row["Reference"])
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            all_primary_rows.append({
                "reference": ref,
                "client_name": _safe_str(row.get("Client Name", "")),
                "payment": _safe_float(row.get("Payment")),
                "iva_fees_arrears": _safe_float(row.get("Arrears Level")),
                "source": "iva_fees",
            })

    for _, row in df_td.iterrows():
        ref = _safe_str(row["Reference"])
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            all_primary_rows.append({
                "reference": ref,
                "client_name": _safe_str(row.get("Client Name", "")),
                "payment": _safe_float(row.get("Payment")),
                "iva_fees_arrears": None,   # not from iva_fees
                "source": "td_fees",
            })

    # Also add any cases present in wf_arrears or cia not in primary sources
    for ref, row in wf_map.items():
        if ref not in seen_refs:
            seen_refs.add(ref)
            warnings.append(f"Case {ref} in WF Arrears but not in IVA/TD Fees — included with limited data")
            all_primary_rows.append({
                "reference": ref,
                "client_name": "",
                "payment": None,
                "iva_fees_arrears": None,
                "source": "wf_only",
            })

    # ------------------------------------------------------------------
    # 6. Build CaseRecord for each case
    # ------------------------------------------------------------------
    today = snapshot_date

    # Live case type counts
    total_live_iva = 0
    total_live_cpi = 0
    total_live_td  = 0
    for ref, lrow in live_map.items():
        t = _safe_str(lrow.get("Type", ""))
        ct = _detect_type(ref)
        if ct == "TD":
            total_live_td += 1
        elif ct == "CPI" or t.upper() == "CPI":
            total_live_cpi += 1
        else:
            total_live_iva += 1

    cases: List[CaseRecord] = []

    for prow in all_primary_rows:
        ref = prow["reference"]
        case_type = _detect_type(ref)

        # ---- Arrears amounts from each source ----
        iva_fees_arrears_val: Optional[float] = prow.get("iva_fees_arrears")

        # For TD cases, also check td_map
        td_arr_val: Optional[float] = None
        if case_type == "TD":
            td_row = td_map.get(ref)
            if td_row:
                td_arr_val = _safe_float(td_row.get("Arrears Level"))

        wf_row = wf_map.get(ref)
        wf_arr_val: Optional[float] = None
        wf_mobile = ""
        wf_contact_notes = ""
        wf_contact_date: Optional[datetime] = None
        wf_catchup_agreed = False
        wf_catchup_amount: Optional[float] = None
        wf_payment_break = False

        if wf_row:
            wf_arr_val = _safe_float(wf_row.get("Arrears Amt."))
            wf_mobile = _safe_str(wf_row.get("Contact Number", ""))
            wf_contact_notes = _safe_str(wf_row.get("Last Contact Notes", ""))[:2000]
            # Optional fields that may or may not be present
            if "Last Contact Date" in wf_row:
                wf_contact_date = _safe_datetime(wf_row.get("Last Contact Date"))
            if "catchup_agreed" in wf_row:
                raw_ca = wf_row.get("catchup_agreed")
                if raw_ca is True or _safe_str(raw_ca).lower() in ("true", "yes", "1"):
                    wf_catchup_agreed = True
            if "Catchup Agreed" in wf_row:
                raw_ca = wf_row.get("Catchup Agreed")
                if raw_ca is True or _safe_str(raw_ca).lower() in ("true", "yes", "1"):
                    wf_catchup_agreed = True
            if "catchup_amount" in wf_row:
                wf_catchup_amount = _safe_float(wf_row.get("catchup_amount"))
            if "Catchup Amount" in wf_row:
                wf_catchup_amount = _safe_float(wf_row.get("Catchup Amount"))
            if "payment_break" in wf_row:
                raw_pb = wf_row.get("payment_break")
                if raw_pb is True or _safe_str(raw_pb).lower() in ("true", "yes", "1"):
                    wf_payment_break = True
            if "Payment Break" in wf_row:
                raw_pb = wf_row.get("Payment Break")
                if raw_pb is True or _safe_str(raw_pb).lower() in ("true", "yes", "1"):
                    wf_payment_break = True

        cia_row = cia_map.get(ref)
        cia_arr_val: Optional[float] = None
        cia_last_due: Optional[date] = None

        if cia_row:
            cia_arr_val = _safe_float(cia_row.get("Arrears"))
            if "Last Payment Due Date" in cia_row:
                cia_last_due = _safe_date(cia_row.get("Last Payment Due Date"))

        # ---- Live case data ----
        live_row = live_map.get(ref)
        vulnerable = False
        case_senior = ""
        case_status = ""

        if live_row:
            vuln_raw = _safe_str(live_row.get("Vulnerable", "")).lower()
            vulnerable = vuln_raw in ("yes", "true", "1", "y")
            case_senior = _safe_str(live_row.get("Case Senior", live_row.get("Case Manager", "")))
            case_status = _safe_str(live_row.get("Case Status", ""))

        # ---- Reconciliation logic ----
        sources_present: List[str] = []
        source_vals: List[float] = []

        if iva_fees_arrears_val is not None:
            sources_present.append("iva_fees")
            source_vals.append(iva_fees_arrears_val)
        if wf_arr_val is not None:
            sources_present.append("wf_arrears")
            source_vals.append(wf_arr_val)
        if cia_arr_val is not None:
            sources_present.append("cases_in_arrears")
            source_vals.append(cia_arr_val)
        if td_arr_val is not None:
            sources_present.append("td_fees")
            source_vals.append(td_arr_val)

        needs_review = False
        review_reasons: List[str] = []

        # Check if any two present sources disagree by > £1
        if len(source_vals) >= 2:
            for i in range(len(source_vals)):
                for j in range(i + 1, len(source_vals)):
                    if abs(source_vals[i] - source_vals[j]) > 1.0:
                        needs_review = True
                        s1 = sources_present[i]
                        s2 = sources_present[j]
                        review_reasons.append(
                            f"Arrears mismatch: {s1}={source_vals[i]:.2f} vs {s2}={source_vals[j]:.2f}"
                        )

        # Canonical arrears amount
        if source_vals:
            arrears_amount = sum(source_vals) / len(source_vals)
        else:
            arrears_amount = 0.0

        # ---- 5-day exclusion check ----
        payment_amount = _safe_float(prow.get("payment")) or 0.0
        if cia_last_due is not None and payment_amount > 0:
            days_since_due = (today - cia_last_due).days
            if days_since_due <= 5 and abs(arrears_amount - payment_amount) < 0.01:
                review_reasons.append(
                    "Recent payment: last_payment_due_date within 5 days and arrears == payment amount"
                )
                needs_review = True

        # ---- Cycle calculation ----
        cycle = "Cycle 1"
        months_in_arrears = 0.0

        if payment_amount == 0 or payment_amount is None:
            needs_review = True
            review_reasons.append("Cannot compute cycle (payment is 0 or missing)")
            cycle = "Cycle 1"
        elif arrears_amount > 0:
            months_in_arrears = arrears_amount / payment_amount
            cycle = _compute_cycle(months_in_arrears)

        # ---- Days since last payment due ----
        days_since_due_val: Optional[int] = None
        if cia_last_due is not None:
            days_since_due_val = (today - cia_last_due).days

        review_reason_str = "; ".join(review_reasons) if review_reasons else ""
        if needs_review and not review_reason_str:
            review_reason_str = "Manual review required"

        rec = CaseRecord(
            reference=ref,
            client_name=prow.get("client_name", ""),
            mobile=wf_mobile,
            case_type=case_type,
            payment_amount=payment_amount,
            arrears_amount=round(arrears_amount, 2),
            cycle=cycle,
            cycle_status="",   # set by web layer from DB history
            months_in_arrears=round(months_in_arrears, 4),
            last_payment_due_date=cia_last_due,
            days_since_last_payment_due=days_since_due_val,
            payment_break=wf_payment_break,
            catchup_agreed=wf_catchup_agreed,
            catchup_amount=wf_catchup_amount,
            vulnerable=vulnerable,
            case_senior=case_senior,
            last_contact_date=wf_contact_date,
            last_contact_notes=wf_contact_notes,
            case_status=case_status,
            needs_manual_review=needs_review,
            review_reason=review_reason_str,
            sources_present=sources_present,
            iva_fees_arrears=iva_fees_arrears_val,
            wf_arrears_amount=wf_arr_val,
            cases_in_arrears_amount=cia_arr_val,
            td_arrears_amount=td_arr_val,
        )
        cases.append(rec)

    # ------------------------------------------------------------------
    # 7. Filter to in-arrears cases only (arrears_amount > 0)
    # ------------------------------------------------------------------
    cases = [c for c in cases if c.arrears_amount > 0]

    # ------------------------------------------------------------------
    # 8. Compute totals and breakdowns
    # ------------------------------------------------------------------
    total_in_arrears = len(cases)
    total_arrears_value = sum(c.arrears_amount for c in cases)

    by_case_type: Dict[str, Dict] = {
        "IVA": {"count": 0, "value": 0.0},
        "CPI": {"count": 0, "value": 0.0},
        "TD":  {"count": 0, "value": 0.0},
    }
    by_cycle: Dict[str, Dict] = {c: {"count": 0, "value": 0.0} for c in _CYCLE_ORDER}

    vulnerable_in_arrears = 0
    vulnerable_arrears_value = 0.0
    manual_review_count = 0

    for c in cases:
        ct = c.case_type if c.case_type in by_case_type else "IVA"
        by_case_type[ct]["count"] += 1
        by_case_type[ct]["value"] = round(by_case_type[ct]["value"] + c.arrears_amount, 2)

        cyc = c.cycle if c.cycle in by_cycle else "Cycle 5+"
        by_cycle[cyc]["count"] += 1
        by_cycle[cyc]["value"] = round(by_cycle[cyc]["value"] + c.arrears_amount, 2)

        if c.vulnerable:
            vulnerable_in_arrears += 1
            vulnerable_arrears_value = round(vulnerable_arrears_value + c.arrears_amount, 2)

        if c.needs_manual_review:
            manual_review_count += 1

    return PipelineResult(
        snapshot_date=snapshot_date,
        cases=cases,
        total_live_iva=total_live_iva,
        total_live_cpi=total_live_cpi,
        total_live_td=total_live_td,
        total_in_arrears=total_in_arrears,
        total_arrears_value=round(total_arrears_value, 2),
        by_case_type=by_case_type,
        by_cycle=by_cycle,
        vulnerable_in_arrears=vulnerable_in_arrears,
        vulnerable_arrears_value=vulnerable_arrears_value,
        manual_review_count=manual_review_count,
        warnings=warnings,
    )
