import io
import json
import zipfile

from sherlock.legiscan.cache import LegiScanCache


def make_dataset_zip(bills: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for b in bills:
            zf.writestr(
                f"CA/2025-2026_Regular_Session/bill/{b['bill_number']}.json",
                json.dumps({"bill": b}),
            )
    return buf.getvalue()


BILL = {
    "bill_id": 111,
    "session_id": 2172,
    "bill_number": "AB12",
    "status": 1,
    "status_date": "2026-06-01",
    "change_hash": "abc",
    "history": [{"date": "2026-06-10", "action": "Read first time"}],
    "sponsors": [{"people_id": 9}],
    "texts": [],
    "votes": [],
}


def test_ingest_dataset_and_read_back(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 2172, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        n = cache.ingest_dataset_zip(2172, make_dataset_zip([BILL]))
        assert n == 1
        rows = cache.bills_for_session(2172)
        assert rows[0]["number"] == "AB12"
        assert rows[0]["last_action_date"] == "2026-06-10"
        assert rows[0]["n_sponsors"] == 1 and rows[0]["n_texts"] == 0
        assert cache.get_bill_payload(111)["bill_id"] == 111
        assert cache.get_sessions("CA")[0]["session_id"] == 2172


def test_masterlist_stub_upsert_updates_hash_not_payload(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.ingest_dataset_zip(2172, make_dataset_zip([BILL]))
        cache.upsert_bill_stub(2172, 111, "AB12", "newhash")
        row = cache.bills_for_session(2172)[0]
        assert row["change_hash"] == "newhash"
        assert cache.get_bill_payload(111) is not None  # payload survives stub upsert


def test_quota_counting(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        assert cache.calls_this_month() == 0
        cache.add_call("getSessionList")
        cache.add_call("getDataset")
        assert cache.calls_this_month() == 2


def test_dataset_hash_roundtrip(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        assert cache.dataset_hash(2172) is None
        cache.set_dataset_hash(2172, "ffff")
        assert cache.dataset_hash(2172) == "ffff"


def test_ingest_skips_malformed_records(tmp_path):
    good = dict(BILL)
    bad = {"bill_number": "AB13", "session_id": 2172}  # no bill_id
    with LegiScanCache(tmp_path / "cache.db") as cache:
        n = cache.ingest_dataset_zip(2172, make_dataset_zip([good, bad]))
        assert n == 1
        assert [r["number"] for r in cache.bills_for_session(2172)] == ["AB12"]
