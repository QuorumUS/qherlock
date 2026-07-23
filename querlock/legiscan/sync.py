import base64
from datetime import datetime, timedelta, timezone

from querlock.legiscan.cache import LegiScanCache
from querlock.legiscan.client import LegiScanClient, LegiScanError

DEGRADE_THRESHOLD = 0.8
SESSION_LIST_TTL_DAYS = 30   # spec §6: session inventory monthly
DATASET_LIST_TTL_DAYS = 7    # spec §6: dataset hashes weekly
SYNC_ERROR_MSG_CAP = 200


def _fresh(ts: str | None, ttl_days: int, now: datetime) -> bool:
    if not ts:
        return False
    return (now - datetime.fromisoformat(ts)) < timedelta(days=ttl_days)


def sync_state(
    state: str,
    client: LegiScanClient,
    cache: LegiScanCache,
    budget_limit: int = 30000,
    today_year: int | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    today_year = today_year or now.year
    calls_this_month = cache.calls_this_month()
    stats = {"state": state, "sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
             "masterlist_refreshed": 0, "calls_this_month": calls_this_month,
             "degraded": False, "session_list_cached": False,
             "dataset_list_cached": False, "errors": []}

    if calls_this_month >= DEGRADE_THRESHOLD * budget_limit:
        stats["degraded"] = True
        stats["sessions"] = len(cache.get_sessions(state))
        return stats

    meta = cache.get_sync_meta(state) or {}

    if _fresh(meta.get("session_list_fetched_at"), SESSION_LIST_TTL_DAYS, now):
        current = [s for s in cache.get_sessions(state)
                   if (s.get("year_end") or 0) >= today_year]
        stats["session_list_cached"] = True
    else:
        current = [s for s in client.get_session_list(state)
                   if (s.get("year_end") or 0) >= today_year]
        for s in current:
            cache.upsert_session(state, s)
        cache.touch_sync_meta(state, session_list=True)
    stats["sessions"] = len(current)
    current_ids = {s["session_id"] for s in current}

    if _fresh(meta.get("dataset_list_fetched_at"), DATASET_LIST_TTL_DAYS, now):
        stats["dataset_list_cached"] = True
    else:
        for ds in client.get_dataset_list(state):
            try:
                sid = ds["session_id"]
                if sid not in current_ids or cache.dataset_hash(sid) == ds["dataset_hash"]:
                    continue
                dataset = client.get_dataset(sid, ds["access_key"])
                zip_bytes = base64.b64decode(dataset["zip"])
                stats["bills_ingested"] += cache.ingest_dataset_zip(sid, zip_bytes)
                cache.set_dataset_hash(sid, ds["dataset_hash"])
                stats["datasets_ingested"] += 1
            except KeyError:
                continue
        cache.touch_sync_meta(state, dataset_list=True)

    for sid in current_ids:
        try:
            masterlist = client.get_master_list_raw(sid)
        except LegiScanError as exc:
            stats["errors"].append(f"session {sid}: {exc}")
            continue
        for key, entry in masterlist.items():
            if key == "session":
                continue
            try:
                cache.upsert_bill_stub(sid, entry["bill_id"], entry["number"],
                                       entry["change_hash"])
            except KeyError:
                continue
        stats["masterlist_refreshed"] += 1

    stats["calls_this_month"] = cache.calls_this_month()
    return stats


def sync_many(regions, client, cache, budget_limit: int = 30000) -> dict:
    """Deterministic loop over sync_state. Per-region errors are recorded, never
    raised (spec §12). Budget re-checked each region, so exhaustion mid-run
    degrades the tail."""
    totals = {"sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
              "masterlist_refreshed": 0}
    degraded: list[str] = []
    errors: dict[str, str] = {}
    session_fetches = dataset_fetches = synced = 0
    for region in regions:
        try:
            s = sync_state(region, client, cache, budget_limit=budget_limit)
        except LegiScanError as exc:
            errors[region] = str(exc)[:SYNC_ERROR_MSG_CAP]
            continue
        synced += 1
        if s["degraded"]:
            degraded.append(region)
            continue
        for k in totals:
            totals[k] += s[k]
        session_fetches += 0 if s["session_list_cached"] else 1
        dataset_fetches += 0 if s["dataset_list_cached"] else 1
        if s["errors"]:
            errors[region] = "; ".join(s["errors"])[:SYNC_ERROR_MSG_CAP]
    calls = cache.calls_this_month()
    return {"scope_regions": len(list(regions)), "synced": synced,
            "degraded": degraded, "errors": errors, "totals": totals,
            "session_lists_fetched": session_fetches,
            "dataset_lists_fetched": dataset_fetches,
            "calls_this_month": calls, "budget_limit": budget_limit,
            "budget_pct": round(100 * calls / budget_limit, 1) if budget_limit else 0.0}
