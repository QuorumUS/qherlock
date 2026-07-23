"""Pure per-bill detectors (spec §1, §8). No I/O — golden-testable.

Precedence: at most one date anomaly per bill; stale wins over wrong_data.
LegiScan is a recall oracle only — when Quorum is ahead (newer dates, higher
status rank, terminal failed) nothing is flagged. A bill may flip
stale -> wrong_data across patrols; distinct fingerprints, intended.
"""
import re
from datetime import date, datetime, timedelta

from querlock.casefiles.models import Anomaly
from querlock.diff.matchers import normalize_bill_number
from querlock.quorum.reader import BillCounts, BillRow

INCOMPLETE_FIELDS = ("sponsors", "actions", "texts", "votes")

# Quorum GeneralBillStatus id -> ordinal progress rank (quorum-site
# app/bill/status.py:13). Absent = never compare (8 unknown, 10-14
# nominations, 101+ non-US, NULL).
GENERAL_STATUS_RANK: dict[int, int] = {
    1: 1,   # introduced
    2: 2,   # out_of_committee
    3: 3,   # passed_first
    4: 4,   # passed_second
    5: 5,   # to_executive
    6: 6,   # enacted
    7: 6,   # effective — same progress as enacted
    9: 99,  # failed — terminal: counts as ahead of any LegiScan claim
}

# LegiScan bill status code -> MINIMUM expected Quorum rank. wrong_data fires
# only when Quorum's rank is strictly below the minimum (Quorum behind).
# LS 6 (Failed) deliberately unmapped: states mark failure at wildly
# different times (often sine die) — comparing recreates v1's FP noise.
LEGISCAN_MIN_RANK: dict[int, int] = {
    1: 1,  # Introduced
    2: 3,  # Engrossed = passed originating chamber
    3: 4,  # Enrolled = passed both chambers (transmittal timing varies)
    4: 6,  # Passed = law
    5: 5,  # Vetoed -> must at least have reached the executive
}

# Resolution number prefixes (raw LegiScan side, BEFORE per-state translation).
# Resolutions are adopted, never enacted — LegiScan "Passed" (4) means adopted,
# so it requires only adopted rank (4), not enacted rank (6).
RESOLUTION_PREFIXES: frozenset[str] = frozenset({
    "HR", "SR", "AR", "JR", "HJR", "SJR", "AJR", "HCR", "SCR", "ACR", "SJRCA", "HJRCA",
})
_RESOLUTION_MIN_RANK: dict[int, int] = {**LEGISCAN_MIN_RANK, 4: 4}
_PREFIX_RE = re.compile(r"^([A-Z]+)\d+$")


def _as_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def compute_severity(gap_type: str, field: str, *,
                     days_since_ls_activity: int | None,
                     lag_days: int | None = None) -> str:
    recent = days_since_ls_activity is not None and days_since_ls_activity <= 30
    if gap_type == "missing_bill":
        return "P1" if recent else "P2"
    if gap_type == "stale":
        return "P2" if recent or (lag_days or 0) > 30 else "P3"
    if gap_type == "wrong_data":
        return "P2" if recent else "P3"
    if gap_type == "incomplete_fields":
        return "P3" if field in ("sponsors", "actions") else "P4"
    return "P4"


def detect_bill_anomalies(region: str, session_key: str, number_norm: str,
                          ls_bill: dict, q_bill: BillRow, q_counts: BillCounts,
                          *, sla_hours: int, today: date) -> list[Anomaly]:
    out: list[Anomaly] = []
    ls_last = _as_date(ls_bill.get("last_action_date"))
    days_since = (today - ls_last).days if ls_last else None

    for field in INCOMPLETE_FIELDS:
        ls_n = ls_bill.get(f"n_{field}") or 0
        if ls_n >= 1 and getattr(q_counts, field) == 0:
            out.append(Anomaly(
                gap_type="incomplete_fields", region=region, session_key=session_key,
                bill_number_norm=number_norm, field=field,
                legiscan_value=str(ls_n), quorum_value="0",
                severity=compute_severity("incomplete_fields", field,
                                          days_since_ls_activity=days_since),
                evidence={"legiscan_bill_id": ls_bill.get("bill_id"),
                          "quorum_bill_id": q_bill.id,
                          "ls_last_action_date": ls_bill.get("last_action_date")},
            ))

    q_mrad = _as_date(q_bill.most_recent_action_date)
    if ls_last is None or q_mrad is None:
        return out  # no freshness evidence; incomplete_fields covers empty history

    lag = ls_last - q_mrad
    if lag > timedelta(hours=sla_hours):
        out.append(Anomaly(
            gap_type="stale", region=region, session_key=session_key,
            bill_number_norm=number_norm, field="most_recent_action_date",
            legiscan_value=ls_last.isoformat(), quorum_value=q_mrad.isoformat(),
            severity=compute_severity("stale", "most_recent_action_date",
                                      days_since_ls_activity=days_since,
                                      lag_days=lag.days),
            evidence={"lag_days": lag.days,
                      "legiscan_bill_id": ls_bill.get("bill_id"),
                      "quorum_bill_id": q_bill.id,
                      "quorum_general_status": q_bill.current_general_status},
        ))
        return out  # precedence: stale wins over wrong_data

    q_rank = GENERAL_STATUS_RANK.get(q_bill.current_general_status or 0)
    ls_status = ls_bill.get("status") or 0
    raw_norm = normalize_bill_number(ls_bill.get("number"))
    pm = _PREFIX_RE.match(raw_norm)
    is_resolution = bool(pm) and pm.group(1) in RESOLUTION_PREFIXES
    rank_map = _RESOLUTION_MIN_RANK if is_resolution else LEGISCAN_MIN_RANK
    min_rank = rank_map.get(ls_status)
    if q_rank is not None and min_rank is not None and q_rank < min_rank:
        out.append(Anomaly(
            gap_type="wrong_data", region=region, session_key=session_key,
            bill_number_norm=number_norm, field="status",
            legiscan_value=str(ls_bill.get("status")),
            quorum_value=str(q_bill.current_general_status),
            severity=compute_severity("wrong_data", "status",
                                      days_since_ls_activity=days_since),
            evidence={"legiscan_status_code": ls_bill.get("status"),
                      "quorum_general_status": q_bill.current_general_status,
                      "ls_last_action_date": ls_last.isoformat(),
                      "q_most_recent_action_date": q_mrad.isoformat(),
                      "legiscan_bill_id": ls_bill.get("bill_id"),
                      "quorum_bill_id": q_bill.id},
        ))
    return out
