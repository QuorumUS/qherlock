import base64
from datetime import datetime, timezone

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient

DEGRADE_THRESHOLD = 0.8


def sync_state(
    state: str,
    client: LegiScanClient,
    cache: LegiScanCache,
    budget_limit: int = 30000,
    today_year: int | None = None,
) -> dict:
    today_year = today_year or datetime.now(timezone.utc).year
    stats = {"state": state, "sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
             "masterlist_refreshed": 0, "calls_this_month": cache.calls_this_month(),
             "degraded": False}

    if cache.calls_this_month() >= DEGRADE_THRESHOLD * budget_limit:
        stats["degraded"] = True
        stats["sessions"] = len(cache.get_sessions(state))
        return stats

    current = [s for s in client.get_session_list(state)
               if (s.get("year_end") or 0) >= today_year]
    for s in current:
        cache.upsert_session(state, s)
    stats["sessions"] = len(current)
    current_ids = {s["session_id"] for s in current}

    for ds in client.get_dataset_list(state):
        sid = ds["session_id"]
        if sid not in current_ids or cache.dataset_hash(sid) == ds["dataset_hash"]:
            continue
        dataset = client.get_dataset(sid, ds["access_key"])
        zip_bytes = base64.b64decode(dataset["zip"])
        stats["bills_ingested"] += cache.ingest_dataset_zip(sid, zip_bytes)
        cache.set_dataset_hash(sid, ds["dataset_hash"])
        stats["datasets_ingested"] += 1

    for sid in current_ids:
        masterlist = client.get_master_list_raw(sid)
        for key, entry in masterlist.items():
            if key == "session":
                continue
            cache.upsert_bill_stub(sid, entry["bill_id"], entry["number"], entry["change_hash"])
        stats["masterlist_refreshed"] += 1

    stats["calls_this_month"] = cache.calls_this_month()
    return stats
