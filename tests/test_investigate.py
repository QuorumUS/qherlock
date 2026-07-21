import sqlite3

import pytest

from sherlock.investigate import investigate
from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanError

_REPLICA_SCHEMA = """
CREATE TABLE app_legsession (id INTEGER PRIMARY KEY, region_abbrev TEXT, title TEXT,
    session_name TEXT, start_year INTEGER, current BOOLEAN, regular_session BOOLEAN);
CREATE TABLE bill_bill (id INTEGER PRIMARY KEY, session_id INTEGER, label TEXT,
    number TEXT, bill_type INTEGER, current_general_status INTEGER,
    current_status_date TEXT, most_recent_action_date TEXT, introduced_date TEXT,
    missing_data BOOLEAN DEFAULT 0, last_quorum_update TEXT, source TEXT);
CREATE TABLE bill_billaction (id INTEGER PRIMARY KEY, bill_id INTEGER, date TEXT, action_type INTEGER);
CREATE TABLE bill_billtext (id INTEGER PRIMARY KEY, bill_id INTEGER);
CREATE TABLE bill_sponsor (id INTEGER PRIMARY KEY, bill_id INTEGER, sponsor_type INTEGER);
CREATE TABLE vote_vote (id INTEGER PRIMARY KEY, related_bill_id INTEGER);
"""


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.calls = []
        self._payload = payload
        self._error = error

    def get_bill(self, bill_id):
        self.calls.append("getBill")
        if self._error is not None:
            raise self._error
        return self._payload


def _payload(bill_id=1, title="Test Title", history=None):
    return {
        "bill_id": bill_id, "bill_number": "AB1", "status": 1,
        "status_date": "2026-07-01", "title": title,
        "history": history if history is not None else
        [{"date": "2026-07-01", "action": "Introduced"}],
        "sponsors": [{"people_id": 1}], "texts": [], "votes": [],
    }


@pytest.fixture
def cache(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as c:
        c.upsert_session("CA", {"session_id": 5, "year_start": 2025, "year_end": 2026,
                                "special": 0, "session_name": "2025-2026 Regular Session"})
        yield c


@pytest.fixture
def replica():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_REPLICA_SCHEMA)
    conn.execute(
        "INSERT INTO app_legsession VALUES (50, 'ca', 't', 's', 2025, TRUE, TRUE)"
    )
    conn.execute(
        "INSERT INTO bill_bill (id, session_id, label, number) VALUES (1, 50, 'AB 1', 'AB 1')"
    )
    conn.commit()
    return conn


def test_live_getbill_refreshes_cache(cache, replica):
    cache.upsert_bill_stub(5, 1, "AB1", "hash1")
    fake_client = FakeClient(payload=_payload(title="Test Title"))

    result = investigate("CA", 5, "AB 1", fake_client, cache, replica)

    assert result["source"] == "live"
    assert fake_client.calls == ["getBill"]
    assert cache.get_bill_payload(1)["title"].startswith("T")


def test_quota_exhausted_serves_cache(cache, replica):
    cache.upsert_bill(5, _payload())
    for _ in range(10):
        cache.add_call("x")
    fake_client = FakeClient(payload=_payload(title="Should not be used"))

    result = investigate("CA", 5, "AB1", fake_client, cache, replica, budget_limit=10)

    assert result["source"] == "cache"
    assert fake_client.calls == []
    assert any("quota" in n for n in result["notes"])


def test_bill_not_in_cache_is_error_without_api_call(cache, replica):
    fake_client = FakeClient(payload=_payload())

    result = investigate("CA", 5, "XYZ9", fake_client, cache, replica)

    assert "error" in result
    assert fake_client.calls == []


def test_quorum_missing_bill_confirmation_path(cache):
    cache.upsert_bill(5, _payload())
    # Replica session matches CA session 5 but has no AB1 row.
    conn = sqlite3.connect(":memory:")
    conn.executescript(_REPLICA_SCHEMA)
    conn.execute(
        "INSERT INTO app_legsession VALUES (50, 'ca', 't', 's', 2025, TRUE, TRUE)"
    )
    conn.commit()
    fake_client = FakeClient(payload=_payload())

    result = investigate("CA", 5, "AB1", fake_client, cache, conn)

    assert result["quorum"] is None
    assert result["quorum_session_id"] is not None


def test_output_bounded(cache, replica):
    big_title = "T" * 5000
    history = [{"date": f"2026-01-{d:02d}", "action": "X" * 500} for d in range(1, 13)]
    cache.upsert_bill_stub(5, 1, "AB1", "hash1")
    fake_client = FakeClient(payload=_payload(title=big_title, history=history))

    result = investigate("CA", 5, "AB1", fake_client, cache, replica)

    assert len(result["legiscan"]["title"]) <= 300
    assert len(result["legiscan"]["recent_actions"]) == 5
    assert all(len(a["action"]) <= 120 for a in result["legiscan"]["recent_actions"])


def test_live_error_falls_back_to_cache(cache, replica):
    cache.upsert_bill(5, _payload())
    fake_client = FakeClient(error=LegiScanError("boom"))

    result = investigate("CA", 5, "AB1", fake_client, cache, replica)

    assert result["source"] == "cache"
    assert fake_client.calls == ["getBill"]
    assert result["legiscan"]["title"] == "Test Title"
    assert any("getBill" in n or "boom" in n for n in result["notes"])


def test_stub_only_cache_with_quota_exhausted_never_raises(cache, replica):
    cache.upsert_bill_stub(5, 1, "AB1", "hash1")
    for _ in range(10):
        cache.add_call("x")
    fake_client = FakeClient(payload=_payload(title="Should not be used"))

    result = investigate("CA", 5, "AB1", fake_client, cache, replica, budget_limit=10)

    assert result["source"] == "cache"
    assert fake_client.calls == []
    assert any("quota" in n for n in result["notes"])
    assert result["legiscan"]["bill_id"] == 1
    assert result["legiscan"]["title"] == ""
    assert result["legiscan"]["number"] == "AB1"
    assert result["legiscan"]["status"] is None
    assert result["legiscan"]["recent_actions"] == []
