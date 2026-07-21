from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore
from sherlock.diff.matchers import legiscan_number_norm, match_sessions, quorum_number_norm
from sherlock.legiscan.cache import LegiScanCache
from sherlock.quorum import reader

TOP_CASES_LIMIT = 10


def diff_state(state: str, cache: LegiScanCache, casefile: CaseFileStore, replica_conn) -> dict:
    ls_sessions = cache.get_sessions(state)
    q_sessions = reader.get_current_sessions(replica_conn, state)
    matched, warnings = match_sessions(ls_sessions, q_sessions)

    new = recurring = 0
    top_cases: list[dict] = []
    for ls, qs in matched:
        session_key = str(ls["session_id"])
        quorum_numbers = {
            quorum_number_norm(b.label, b.number)
            for b in reader.get_bills_for_session(replica_conn, qs.id)
        }
        for bill in cache.bills_for_session(ls["session_id"]):
            norm = legiscan_number_norm(state, bill["number"])
            if not norm or norm in quorum_numbers:
                continue
            payload = cache.get_bill_payload(bill["bill_id"]) or {}
            anomaly = Anomaly(
                gap_type="missing_bill", region=state, session_key=session_key,
                bill_number_norm=norm, legiscan_value=bill["number"] or "",
                evidence={
                    "legiscan_bill_id": bill["bill_id"],
                    "title": (payload.get("title") or "")[:300],
                    "status": bill["status"], "status_date": bill["status_date"],
                    "last_action_date": bill["last_action_date"],
                    "quorum_session_id": qs.id,
                },
            )
            kind, aid = casefile.upsert_anomaly(anomaly)
            if kind == "created":
                new += 1
            else:
                recurring += 1
            if len(top_cases) < TOP_CASES_LIMIT:
                top_cases.append({"id": aid, "bill_number": norm, "session_key": session_key,
                                  "title": anomaly.evidence["title"]})

    return {"state": state, "sessions_matched": len(matched), "warnings": warnings,
            "anomalies_new": new, "anomalies_recurring": recurring, "top_cases": top_cases}
