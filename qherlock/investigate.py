"""Targeted single-bill deep-dive: live getBill (budget-permitting) + Quorum
replica lookup. Behind the investigate_bill tool (Task 12).

Every early exit returns a dict with "error" — never raises.
"""

from qherlock.diff.matchers import (legiscan_number_norm, match_sessions, normalize_bill_number,
                                    quorum_number_norm)
from qherlock.legiscan.cache import LegiScanCache
from qherlock.legiscan.client import LegiScanError
from qherlock.quorum import reader
from qherlock.quorum.reader import BillCounts

TITLE_CAP = 300
ACTION_CAP = 120
RECENT_ACTIONS_LIMIT = 5


def _legiscan_recent_actions(history: list[dict]) -> list[dict]:
    ordered = sorted(history, key=lambda h: h.get("date") or "", reverse=True)
    return [{"date": h.get("date"), "action": (h.get("action") or "")[:ACTION_CAP]}
            for h in ordered[:RECENT_ACTIONS_LIMIT]]


def _legiscan_view(bill_id: int, bill_row: dict, payload: dict) -> dict:
    """Build the "legiscan" contract fields from a payload, falling back to the
    cached row's summary columns when no payload is available at all."""
    if not payload:
        return {
            "bill_id": bill_id, "number": bill_row["number"], "title": "",
            "status": bill_row["status"], "status_date": bill_row["status_date"],
            "last_action_date": bill_row["last_action_date"],
            "n_sponsors": bill_row["n_sponsors"], "n_actions": bill_row["n_actions"],
            "n_texts": bill_row["n_texts"], "n_votes": bill_row["n_votes"],
            "recent_actions": [],
        }
    history = payload.get("history") or []
    dated = [h["date"] for h in history if h.get("date")]
    last_action_date = max(dated) if dated else bill_row["last_action_date"]
    return {
        "bill_id": bill_id,
        "number": payload.get("bill_number") or payload.get("number") or bill_row["number"],
        "title": (payload.get("title") or "")[:TITLE_CAP],
        "status": payload.get("status"),
        "status_date": payload.get("status_date"),
        "last_action_date": last_action_date,
        "n_sponsors": len(payload.get("sponsors") or []),
        "n_actions": len(history),
        "n_texts": len(payload.get("texts") or []),
        "n_votes": len(payload.get("votes") or []),
        "recent_actions": _legiscan_recent_actions(history),
    }


def investigate(state: str, session_id: int, number: str, client, cache: LegiScanCache,
                replica_conn, budget_limit: int = 30000) -> dict:
    # The US prefix map is non-idempotent (HR->HRES), so a caller-supplied
    # `number` may be either an already-normalized LegiScan number (the usual
    # case — callers pass an anomaly's bill_number_norm) or a raw LegiScan
    # number that still needs translation. Accept both, preferring the
    # un-translated interpretation to avoid a confidently wrong match
    # (federal "HR24" must resolve to H.R. 24, not House Resolution 24).
    raw_norm = normalize_bill_number(number)
    translated_norm = legiscan_number_norm(state, number)

    rows_with_norms = [(row, legiscan_number_norm(state, row["number"]))
                       for row in cache.bills_for_session(session_id)]
    match = next(((row, norm) for row, norm in rows_with_norms if norm == raw_norm), None)
    if match is None:
        match = next(((row, norm) for row, norm in rows_with_norms if norm == translated_norm),
                     None)
    if match is None:
        return {"error": f"bill not in cache — run legiscan_sync for {state} first"}
    bill_row, number_norm = match

    bill_id = bill_row["bill_id"]
    notes: list[str] = []
    payload = None
    source = "cache"
    if cache.calls_this_month() < budget_limit:
        try:
            payload = client.get_bill(bill_id)
            cache.upsert_bill(session_id, payload)
            source = "live"
        except LegiScanError as exc:
            notes.append(f"live getBill failed ({exc}) — served from cache")
    else:
        notes.append("quota exhausted — served from cache")

    if payload is None:
        payload = cache.get_bill_payload(bill_id) or {}

    legiscan_out = _legiscan_view(bill_id, bill_row, payload)

    quorum_out = None
    quorum_session_id = None
    matched, session_warnings = match_sessions(cache.get_sessions(state),
                                        reader.get_current_sessions(replica_conn, state))
    notes.extend(session_warnings)
    pair = next((qs for ls, qs in matched if ls["session_id"] == session_id), None)
    if pair is None:
        notes.append(f"no matched Quorum session for LegiScan session {session_id}")
    else:
        quorum_session_id = pair.id
        q_bills = reader.get_bills_for_session(replica_conn, pair.id)
        q_bill = next((b for b in q_bills
                       if quorum_number_norm(b.label, b.number, b.bill_type) == number_norm), None)
        if q_bill is None:
            notes.append(f"bill {number_norm} not found in Quorum replica (session {pair.id})")
        else:
            counts = reader.get_bill_counts_for_session(replica_conn, pair.id).get(
                q_bill.id, BillCounts())
            quorum_out = {
                "bill_id": q_bill.id, "label": q_bill.label,
                "general_status": q_bill.current_general_status,
                "current_status_date": q_bill.current_status_date,
                "most_recent_action_date": q_bill.most_recent_action_date,
                "introduced_date": q_bill.introduced_date,
                "missing_data": q_bill.missing_data,
                "last_quorum_update": q_bill.last_quorum_update,
                "source": q_bill.source,
                "counts": {"actions": counts.actions, "texts": counts.texts,
                           "sponsors": counts.sponsors, "votes": counts.votes},
                "recent_actions": reader.get_recent_actions(replica_conn, q_bill.id),
            }

    return {
        "state": state, "session_id": session_id, "number_norm": number_norm,
        "source": source, "legiscan": legiscan_out, "quorum": quorum_out,
        "quorum_session_id": quorum_session_id, "notes": notes,
        "calls_this_month": cache.calls_this_month(),
    }
