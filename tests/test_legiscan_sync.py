import base64
from datetime import datetime, timedelta, timezone

import pytest

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanError
from sherlock.legiscan.sync import sync_many, sync_state
from tests.test_legiscan_cache import BILL, make_dataset_zip


class FakeClient:
    def __init__(self):
        self.dataset_fetches = 0
        self.calls: list[str] = []

    def get_session_list(self, state):
        self.calls.append("getSessionList")
        return [
            {"session_id": 2172, "year_start": 2025, "year_end": 2026, "special": 0,
             "session_name": "2025-2026 Regular Session"},
            {"session_id": 1999, "year_start": 2021, "year_end": 2022, "special": 0,
             "session_name": "old session"},
        ]

    def get_dataset_list(self, state):
        self.calls.append("getDatasetList")
        return [{"session_id": 2172, "dataset_hash": "h1", "access_key": "ak"},
                {"session_id": 1999, "dataset_hash": "h0", "access_key": "ak"}]

    def get_dataset(self, session_id, access_key):
        self.calls.append("getDataset")
        self.dataset_fetches += 1
        return {"session_id": session_id,
                "zip": base64.b64encode(make_dataset_zip([BILL])).decode()}

    def get_master_list_raw(self, session_id):
        self.calls.append("getMasterListRaw")
        return {"session": {"session_id": session_id},
                "0": {"bill_id": 111, "number": "AB12", "change_hash": "hash2"}}


@pytest.fixture
def cache(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as c:
        yield c


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def fake_client_two_sessions():
    class TwoSessionClient:
        def __init__(self):
            self.calls: list[str] = []

        def get_session_list(self, state):
            self.calls.append("getSessionList")
            return [
                {"session_id": 10, "year_start": 2025, "year_end": 2026, "special": 0,
                 "session_name": "s1"},
                {"session_id": 20, "year_start": 2025, "year_end": 2026, "special": 0,
                 "session_name": "s2"},
            ]

        def get_dataset_list(self, state):
            self.calls.append("getDatasetList")
            return []

        def get_dataset(self, session_id, access_key):
            raise AssertionError("get_dataset should not be called: dataset list is empty")

        def get_master_list_raw(self, session_id):
            self.calls.append(f"getMasterListRaw:{session_id}")
            if session_id == 10:
                raise LegiScanError("getMasterListRaw failed: HTTP 500")
            return {"session": {"session_id": session_id},
                    "0": {"bill_id": 201, "number": "AB1", "change_hash": "h1"}}

    return TwoSessionClient()


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


def test_sync_skips_malformed_dataset_and_masterlist_entries(tmp_path):
    class MalformedClient(FakeClient):
        def get_dataset_list(self, state):
            return [{"dataset_hash": "h9"},  # no session_id -> skipped
                    {"session_id": 2172, "dataset_hash": "h1", "access_key": "ak"}]

        def get_master_list_raw(self, session_id):
            return {"session": {"session_id": session_id},
                    "0": {"bill_id": 111},  # missing number/change_hash -> entry skipped
                    "1": {"bill_id": 112, "number": "AB13", "change_hash": "h13"}}

    client = MalformedClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        stats = sync_state("CA", client, cache, today_year=2026)
        assert stats["datasets_ingested"] == 1          # good dataset still ingested
        assert stats["masterlist_refreshed"] == 1       # session still refreshed
        numbers = {b["number"] for b in cache.bills_for_session(2172)}
        assert "AB13" in numbers                        # good entry landed


def test_degraded_mode_makes_zero_client_calls(tmp_path):
    class CountingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.total_calls = 0

        def get_session_list(self, state):
            self.total_calls += 1
            return super().get_session_list(state)

        def get_dataset_list(self, state):
            self.total_calls += 1
            return super().get_dataset_list(state)

        def get_dataset(self, session_id, access_key):
            self.total_calls += 1
            return super().get_dataset(session_id, access_key)

        def get_master_list_raw(self, session_id):
            self.total_calls += 1
            return super().get_master_list_raw(session_id)

    client = CountingClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        for _ in range(8):
            cache.add_call("x")
        stats = sync_state("CA", client, cache, budget_limit=10, today_year=2026)
        assert stats["degraded"] is True
        assert client.total_calls == 0


# -- TTL caching, per-session error isolation, sync_many -----------------------

def test_session_list_cached_within_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache, today_year=2026)    # first run fetches
    fake_client.calls.clear()
    stats = sync_state("CA", fake_client, cache, today_year=2026)
    assert "getSessionList" not in fake_client.calls
    assert stats["session_list_cached"] is True
    assert stats["sessions"] >= 1                           # derived from cache


def test_session_list_refetched_after_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache, today_year=2026)
    # age the meta 31 days
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    cache._conn.execute("UPDATE sync_meta SET session_list_fetched_at = ?", (old,))
    fake_client.calls.clear()
    sync_state("CA", fake_client, cache, today_year=2026)
    assert "getSessionList" in fake_client.calls


def test_dataset_list_cached_within_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache, today_year=2026)
    fake_client.calls.clear()
    stats = sync_state("CA", fake_client, cache, today_year=2026)
    assert "getDatasetList" not in fake_client.calls and stats["dataset_list_cached"] is True


def test_masterlist_error_does_not_abort_other_sessions(cache, fake_client_two_sessions):
    # fake raises LegiScanError for session 10's masterlist only
    stats = sync_state("CA", fake_client_two_sessions, cache)
    assert stats["masterlist_refreshed"] == 1 and len(stats["errors"]) == 1


def test_sync_many_aggregates_and_isolates_errors(cache):
    class RegionClient:
        def __init__(self):
            self.calls: list[str] = []

        def get_session_list(self, state):
            self.calls.append(f"getSessionList:{state}")
            if state == "TX":
                raise LegiScanError("getSessionList failed: HTTP 503")
            return [{"session_id": 1, "year_start": 2025, "year_end": 2026,
                      "special": 0, "session_name": "s"}]

        def get_dataset_list(self, state):
            self.calls.append(f"getDatasetList:{state}")
            return []

        def get_dataset(self, session_id, access_key):
            raise AssertionError("get_dataset should not be called: dataset list is empty")

        def get_master_list_raw(self, session_id):
            self.calls.append(f"getMasterListRaw:{session_id}")
            return {"session": {"session_id": session_id}}

    client = RegionClient()
    rollup = sync_many(["CA", "TX"], client, cache)
    assert rollup["synced"] == 1 and "TX" in rollup["errors"]
    assert rollup["totals"]["sessions"] >= 1
    assert "budget_pct" in rollup


def test_sync_many_degrades_tail_when_budget_crossed(cache, fake_client):
    for _ in range(8):
        cache.add_call("x")
    rollup = sync_many(["CA", "TX"], fake_client, cache, budget_limit=10)
    assert "CA" in rollup["degraded"] and "TX" in rollup["degraded"]


def test_sync_many_rollup_is_bounded(cache, fake_client):
    rollup = sync_many(["CA", "TX"], fake_client, cache)
    assert "CA" not in rollup  # no per-region stats rows, totals only
