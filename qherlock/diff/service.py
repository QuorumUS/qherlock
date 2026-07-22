from datetime import datetime, timezone

from qherlock.casefiles.models import Anomaly
from qherlock.casefiles.store import CaseFileStore
from qherlock.diff.detectors import _as_date, compute_severity, detect_bill_anomalies
from qherlock.diff.matchers import (is_deliberately_unimported, is_extraordinary_number,
                                    legiscan_number_norm, match_sessions, quorum_number_norm)
from qherlock.legiscan.cache import LegiScanCache
from qherlock.quorum import reader

TOP_CASES_LIMIT = 10
ROLLUP_TOP_LIMIT = 15
ROLLUP_REGION_ROWS = 30
WARNINGS_SAMPLE = 10
ERROR_MSG_CAP = 120
ERRORS_MAX_ENTRIES = 10


def diff_region(region: str, cache: LegiScanCache, casefile: CaseFileStore,
                replica_conn, *, sla_hours: int = 72, today=None) -> dict:
    """All four detectors for one region ('US' = federal)."""
    today = today or datetime.now(timezone.utc).date()
    ls_sessions = cache.get_sessions(region)
    q_sessions = reader.get_current_sessions(replica_conn, region)
    matched, warnings = match_sessions(ls_sessions, q_sessions)

    # Sibling special sessions per biennium start_year, for extraordinary-session
    # bills LegiScan folds into the regular dataset (CA ABX). Only special sessions.
    siblings_by_year: dict[int | None, list] = {}
    for q in q_sessions:
        if not q.regular_session:
            siblings_by_year.setdefault(q.start_year, []).append(q)

    counts: dict[str, dict[str, int]] = {}
    ignored = 0
    cases: list[dict] = []

    # Lazy per-Quorum-session cache: bills + bill counts fetched at most once
    # per session id, whether it's the primary matched session or a sibling
    # special session consulted for CA extraordinary-session (ABX) bills.
    session_data: dict[int, tuple[list, dict]] = {}

    def get_session_data(sid: int):
        cached = session_data.get(sid)
        if cached is None:
            cached = (reader.get_bills_for_session(replica_conn, sid),
                      reader.get_bill_counts_for_session(replica_conn, sid))
            session_data[sid] = cached
        return cached

    def record(anomaly: Anomaly, title: str = ""):
        kind, aid = casefile.upsert_anomaly(anomaly)
        bucket = counts.setdefault(anomaly.gap_type, {"new": 0, "recurring": 0})
        bucket["new" if kind == "created" else "recurring"] += 1
        cases.append({"id": aid, "gap_type": anomaly.gap_type, "severity": anomaly.severity,
                      "bill_number": anomaly.bill_number_norm,
                      "session_key": anomaly.session_key, "kind": kind,
                      "title": title[:120]})

    for ls, qs in matched:
        session_key = str(ls["session_id"])
        q_bills, q_counts = get_session_data(qs.id)
        q_by_norm: dict[str, reader.BillRow] = {}
        for b in q_bills:
            norm = quorum_number_norm(b.label, b.number, b.bill_type, state=region)
            if not norm:
                continue
            if norm in q_by_norm:
                warnings.append(
                    f"{region} session {session_key}: bill-number collision on {norm!r} "
                    f"(labels {q_by_norm[norm].label!r} and {b.label!r}) — kept first"
                )
                continue
            q_by_norm[norm] = b

        sib_by_norm_cache: dict[int, dict[str, reader.BillRow]] = {}

        def get_sibling_norm_index(sid: int) -> dict[str, reader.BillRow]:
            idx = sib_by_norm_cache.get(sid)
            if idx is None:
                sib_bills, _ = get_session_data(sid)
                idx = {}
                for sb in sib_bills:
                    snorm = quorum_number_norm(sb.label, sb.number, sb.bill_type,
                                               state=region)
                    if not snorm:
                        continue
                    if snorm in idx:
                        warnings.append(
                            f"{region} sibling session {sid}: bill-number collision on "
                            f"{snorm!r} (labels {idx[snorm].label!r} and {sb.label!r}) "
                            "— kept first"
                        )
                        continue
                    idx[snorm] = sb
                sib_by_norm_cache[sid] = idx
            return idx

        for bill in cache.bills_for_session(ls["session_id"]):
            norm = legiscan_number_norm(region, bill["number"])
            if not norm:
                continue
            q_bill = q_by_norm.get(norm)
            q_bill_counts = q_counts
            if q_bill is None and is_extraordinary_number(region, bill["number"]):
                for sib in siblings_by_year.get(qs.start_year, []):
                    sb = get_sibling_norm_index(sib.id).get(norm)
                    if sb is not None:
                        q_bill = sb
                        q_bill_counts = get_session_data(sib.id)[1]
                        break
            if q_bill is None:
                payload = cache.get_bill_payload(bill["bill_id"]) or {}
                title = (payload.get("title") or "").strip()
                # Salvage rule (comparison.py:106): no title (masterlist stub) or a
                # deliberately-unimported type -> ignore, don't flag.
                if not title or is_deliberately_unimported(region, title):
                    ignored += 1
                    continue
                ls_last = _as_date(bill["last_action_date"])
                days_since = (today - ls_last).days if ls_last else None
                record(Anomaly(
                    gap_type="missing_bill", region=region, session_key=session_key,
                    bill_number_norm=norm, legiscan_value=bill["number"] or "",
                    severity=compute_severity("missing_bill", "",
                                              days_since_ls_activity=days_since),
                    evidence={"legiscan_bill_id": bill["bill_id"], "title": title[:300],
                              "status": bill["status"], "status_date": bill["status_date"],
                              "last_action_date": bill["last_action_date"],
                              "quorum_session_id": qs.id},
                ), title)
            else:
                for anomaly in detect_bill_anomalies(
                        region, session_key, norm, bill, q_bill,
                        q_bill_counts.get(q_bill.id, reader.BillCounts()),
                        sla_hours=sla_hours, today=today):
                    record(anomaly)

    cases.sort(key=lambda c: (c["severity"], -c["id"]))
    new = sum(c["new"] for c in counts.values())
    recurring = sum(c["recurring"] for c in counts.values())
    return {"region": region, "sessions_matched": len(matched), "warnings": warnings,
            "anomalies_new": new, "anomalies_recurring": recurring,
            "counts_by_gap_type": counts, "ignored": ignored,
            "top_cases": cases[:TOP_CASES_LIMIT]}


diff_state = diff_region  # back-compat alias (M0 CLI/tool imports)


def diff_many(regions, cache: LegiScanCache, casefile: CaseFileStore, replica_conn,
              *, sla_hours: int = 72, today=None) -> dict:
    """Bounded rollup across regions. One region's failure never kills the
    patrol (spec §12) — it lands in `errors` and the loop continues."""
    per_gap: dict[str, dict[str, int]] = {}
    region_rows: dict[str, dict[str, int]] = {}
    errors: dict[str, str] = {}
    top: list[dict] = []
    warn_sample: list[str] = []
    total_new = total_rec = warn_count = diffed = 0

    for region in regions:
        try:
            r = diff_region(region, cache, casefile, replica_conn,
                            sla_hours=sla_hours, today=today)
        except Exception as exc:
            errors[region] = f"{type(exc).__name__}: {exc}"[:ERROR_MSG_CAP]
            continue
        diffed += 1
        total_new += r["anomalies_new"]
        total_rec += r["anomalies_recurring"]
        warn_count += len(r["warnings"])
        for w in r["warnings"]:
            if len(warn_sample) < WARNINGS_SAMPLE:
                warn_sample.append(f"{region}: {w}")
        row: dict[str, int] = {}
        for gap, c in r["counts_by_gap_type"].items():
            g = per_gap.setdefault(gap, {"new": 0, "recurring": 0})
            g["new"] += c["new"]
            g["recurring"] += c["recurring"]
            total = c["new"] + c["recurring"]
            if total:
                row[gap] = total
        if row:
            region_rows[region] = row
        for case in r["top_cases"]:
            top.append({**case, "region": region})

    if len(errors) > ERRORS_MAX_ENTRIES:
        kept_keys = list(errors)[:ERRORS_MAX_ENTRIES]
        more = len(errors) - ERRORS_MAX_ENTRIES
        errors = {k: errors[k] for k in kept_keys}
        errors["_more"] = more

    top.sort(key=lambda c: (c["severity"], -c["id"]))
    if len(region_rows) > ROLLUP_REGION_ROWS:
        keep = sorted(region_rows, key=lambda k: -sum(region_rows[k].values()))
        dropped = len(region_rows) - ROLLUP_REGION_ROWS
        region_rows = {k: region_rows[k] for k in keep[:ROLLUP_REGION_ROWS]}
        region_rows["_more"] = dropped
    return {"scope_regions": len(list(regions)), "regions_diffed": diffed,
            "errors": errors, "counts_by_gap_type": per_gap,
            "anomalies_new": total_new, "anomalies_recurring": total_rec,
            "regions": region_rows, "warnings_count": warn_count,
            "warnings_sample": warn_sample, "top_cases": top[:ROLLUP_TOP_LIMIT]}
