import base64

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.sync import sync_state
from tests.test_legiscan_cache import BILL, make_dataset_zip


class FakeClient:
    def __init__(self):
        self.dataset_fetches = 0

    def get_session_list(self, state):
        return [
            {"session_id": 2172, "year_start": 2025, "year_end": 2026, "special": 0,
             "session_name": "2025-2026 Regular Session"},
            {"session_id": 1999, "year_start": 2021, "year_end": 2022, "special": 0,
             "session_name": "old session"},
        ]

    def get_dataset_list(self, state):
        return [{"session_id": 2172, "dataset_hash": "h1", "access_key": "ak"},
                {"session_id": 1999, "dataset_hash": "h0", "access_key": "ak"}]

    def get_dataset(self, session_id, access_key):
        self.dataset_fetches += 1
        return {"session_id": session_id,
                "zip": base64.b64encode(make_dataset_zip([BILL])).decode()}

    def get_master_list_raw(self, session_id):
        return {"session": {"session_id": session_id},
                "0": {"bill_id": 111, "number": "AB12", "change_hash": "hash2"}}


def test_sync_ingests_current_sessions_only(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        stats = sync_state("CA", client, cache, today_year=2026)
        assert stats["sessions"] == 1               # 1999 session filtered out
        assert stats["datasets_ingested"] == 1 and client.dataset_fetches == 1
        assert stats["bills_ingested"] == 1
        assert stats["masterlist_refreshed"] == 1
        assert stats["degraded"] is False
        # change_hash updated from masterlist AFTER dataset ingest
        assert cache.bills_for_session(2172)[0]["change_hash"] == "hash2"


def test_sync_skips_unchanged_dataset(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        sync_state("CA", client, cache, today_year=2026)
        stats = sync_state("CA", client, cache, today_year=2026)
        assert stats["datasets_ingested"] == 0      # hash unchanged
        assert client.dataset_fetches == 1


def test_sync_degrades_at_80_percent_budget(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        for _ in range(8):
            cache.add_call("x")
        stats = sync_state("CA", client, cache, budget_limit=10, today_year=2026)
        assert stats["degraded"] is True
        assert client.dataset_fetches == 0
