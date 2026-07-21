import sqlite3
from datetime import date

import pytest

from qherlock.casefiles.store import CaseFileStore
from qherlock.diff.service import diff_many, diff_region, diff_state
from qherlock.legiscan.cache import LegiScanCache
from qherlock.quorum import reader
from tests.test_legiscan_cache import BILL, make_dataset_zip

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


def _new_replica() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_REPLICA_SCHEMA)
    return conn


@pytest.fixture
def cache(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as c:
        c.upsert_session("CA", {"session_id": 2172, "year_start": 2025, "year_end": 2026,
                                "special": 0, "session_name": "2025-2026 Regular Session"})
        missing = dict(BILL, bill_id=222, bill_number="AB99", title="Missing act")
        present = dict(BILL, bill_id=111, bill_number="AB12")
        c.ingest_dataset_zip(2172, make_dataset_zip([present, missing]))
        yield c


@pytest.fixture
def replica():
    conn = _new_replica()
    conn.executescript(
        """
        INSERT INTO app_legsession VALUES (10, 'ca', 't', 's', 2025, TRUE, TRUE);
        INSERT INTO bill_bill (id, session_id, label, number, current_general_status,
            most_recent_action_date) VALUES (1, 10, 'AB 12', 'AB 12', 1, '2026-06-10');
        INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (1, '2026-06-10', 1);
        INSERT INTO bill_sponsor (bill_id, sponsor_type) VALUES (1, 1);
        """
    )
    return conn


def test_diff_finds_missing_bill(tmp_path, cache, replica):
    with CaseFileStore(tmp_path / "casefile.db") as casefile:
        summary = diff_state("CA", cache, casefile, replica)
        assert summary["sessions_matched"] == 1
        assert summary["anomalies_new"] == 1          # AB99 missing; AB12 matches "AB 12"
        assert summary["top_cases"][0]["bill_number"] == "AB99"
        assert summary["counts_by_gap_type"]["missing_bill"]["new"] == 1
        # second run: same anomaly is recurring, not new
        summary2 = diff_state("CA", cache, casefile, replica)
        assert summary2["anomalies_new"] == 0
        assert summary2["anomalies_recurring"] == 1


def test_diff_reports_session_warnings(tmp_path, cache):
    empty_replica = sqlite3.connect(":memory:")
    empty_replica.executescript(
        """
        CREATE TABLE app_legsession (id INTEGER PRIMARY KEY, region_abbrev TEXT, title TEXT,
            session_name TEXT, start_year INTEGER, current BOOLEAN, regular_session BOOLEAN);
        CREATE TABLE bill_bill (id INTEGER PRIMARY KEY, session_id INTEGER, label TEXT,
            number TEXT, bill_type INTEGER, current_general_status INTEGER,
            current_status_date TEXT, most_recent_action_date TEXT, introduced_date TEXT,
            missing_data BOOLEAN DEFAULT 0, last_quorum_update TEXT, source TEXT);
        """
    )
    with CaseFileStore(tmp_path / "casefile.db") as casefile:
        summary = diff_state("CA", cache, casefile, empty_replica)
        assert summary["sessions_matched"] == 0
        assert len(summary["warnings"]) == 1
        assert summary["anomalies_new"] == 0


def test_incomplete_fields_end_to_end(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 3001, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        bill = dict(BILL, bill_id=401, bill_number="AB1",
                    status=1, status_date="2026-07-01",
                    history=[{"date": "2026-07-01"}], sponsors=[{"people_id": 1}],
                    texts=[{"doc_id": 1}], votes=[{"vote_id": 1}])
        cache.ingest_dataset_zip(3001, make_dataset_zip([bill]))

        replica = _new_replica()
        replica.executescript(
            """
            INSERT INTO app_legsession VALUES (20, 'ca', 't', 's', 2025, TRUE, TRUE);
            INSERT INTO bill_bill (id, session_id, label, number, current_general_status,
                most_recent_action_date) VALUES (1, 20, 'AB 1', 'AB 1', 1, '2026-07-01');
            INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (1, '2026-07-01', 1);
            INSERT INTO bill_billtext (bill_id) VALUES (1);
            INSERT INTO vote_vote (related_bill_id) VALUES (1);
            """
        )
        # deliberately no bill_sponsor row -> incomplete_fields on "sponsors" only

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
            assert summary["counts_by_gap_type"]["incomplete_fields"]["new"] == 1
            row = casefile.list_anomalies(gap_type="incomplete_fields")[0]
            assert row["field"] == "sponsors"
            full = casefile.get_anomaly(row["id"])
            assert full["severity"] == "P3"


def test_stale_end_to_end_with_injected_today(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 3002, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        bill = dict(BILL, bill_id=402, bill_number="AB2",
                    status=1, status_date="2026-07-01",
                    history=[{"date": "2026-07-20"}], sponsors=[], texts=[], votes=[])
        cache.ingest_dataset_zip(3002, make_dataset_zip([bill]))

        replica = _new_replica()
        replica.executescript(
            """
            INSERT INTO app_legsession VALUES (21, 'ca', 't', 's', 2025, TRUE, TRUE);
            INSERT INTO bill_bill (id, session_id, label, number, current_general_status,
                most_recent_action_date) VALUES (1, 21, 'AB 2', 'AB 2', 1, '2026-07-10');
            INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (1, '2026-07-10', 1);
            """
        )

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
            assert summary["counts_by_gap_type"]["stale"]["new"] == 1


def test_wrong_data_end_to_end(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 3003, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        bill = dict(BILL, bill_id=403, bill_number="AB3",
                    status=2, status_date="2026-07-15",       # Engrossed -> min_rank 3
                    history=[{"date": "2026-07-15"}], sponsors=[], texts=[], votes=[])
        cache.ingest_dataset_zip(3003, make_dataset_zip([bill]))

        replica = _new_replica()
        replica.executescript(
            """
            INSERT INTO app_legsession VALUES (22, 'ca', 't', 's', 2025, TRUE, TRUE);
            INSERT INTO bill_bill (id, session_id, label, number, current_general_status,
                most_recent_action_date) VALUES (1, 22, 'AB 3', 'AB 3', 1, '2026-07-15');
            INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (1, '2026-07-15', 1);
            """
        )
        # Quorum current_general_status=1 (introduced, rank 1) < LS min_rank 3 -> wrong_data

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
            assert summary["counts_by_gap_type"]["wrong_data"]["new"] == 1


def test_ma_orders_and_titleless_stubs_ignored_not_flagged(tmp_path):
    # Part A: MA procedural "Order" title -> deliberately unimported, ignored.
    with LegiScanCache(tmp_path / "ma_cache.db") as ma_cache:
        ma_cache.upsert_session("MA", {"session_id": 4001, "year_start": 2025, "year_end": 2026,
                                       "special": 0, "session_name": "192nd General Court"})
        order_bill = dict(BILL, bill_id=501, bill_number="SO1",
                          title="Order relative to a special commission")
        ma_cache.ingest_dataset_zip(4001, make_dataset_zip([order_bill]))

        ma_replica = _new_replica()
        ma_replica.executescript(
            "INSERT INTO app_legsession VALUES (30, 'ma', 't', 's', 2025, TRUE, TRUE);"
        )

        with CaseFileStore(tmp_path / "ma_casefile.db") as ma_casefile:
            summary = diff_region("MA", ma_cache, ma_casefile, ma_replica,
                                  today=date(2026, 7, 21))
            assert summary["ignored"] == 1
            assert summary["counts_by_gap_type"] == {}

    # Part B: CA masterlist stub (no payload) -> no title evidence, ignored.
    with LegiScanCache(tmp_path / "ca_cache.db") as ca_cache:
        ca_cache.upsert_session("CA", {"session_id": 4002, "year_start": 2025, "year_end": 2026,
                                       "special": 0, "session_name": "2025-2026 Regular Session"})
        ca_cache.upsert_bill_stub(4002, 502, "AB50", "hash1")

        ca_replica = _new_replica()
        ca_replica.executescript(
            "INSERT INTO app_legsession VALUES (31, 'ca', 't', 's', 2025, TRUE, TRUE);"
        )

        with CaseFileStore(tmp_path / "ca_casefile.db") as ca_casefile:
            summary = diff_region("CA", ca_cache, ca_casefile, ca_replica,
                                  today=date(2026, 7, 21))
            assert summary["ignored"] == 1
            assert summary["counts_by_gap_type"] == {}


def test_federal_null_label_matching(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("US", {"session_id": 9001, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "119th Congress"})
        matched_bill = dict(BILL, bill_id=301, bill_number="HB24")
        missing_bill = dict(BILL, bill_id=302, bill_number="HB99",
                            title="A resolution about testing")
        cache.ingest_dataset_zip(9001, make_dataset_zip([matched_bill, missing_bill]))

        replica = _new_replica()
        replica.executescript(
            """
            INSERT INTO app_legsession VALUES (50, 'us', 't', 's', 2025, TRUE, TRUE);
            INSERT INTO bill_bill (id, session_id, label, number, bill_type)
                VALUES (1, 50, NULL, '24', 3);
            """
        )

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("US", cache, casefile, replica, today=date(2026, 7, 21))
            assert summary["counts_by_gap_type"]["missing_bill"]["new"] == 1
            assert casefile.list_anomalies(gap_type="missing_bill")[0]["bill_number_norm"] == "HR99"


def test_diff_many_rollup_and_error_isolation(tmp_path, monkeypatch):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 5001, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        bill = dict(BILL, bill_id=601, bill_number="AB60", title="A bill about testing")
        cache.ingest_dataset_zip(5001, make_dataset_zip([bill]))

        replica = _new_replica()
        replica.executescript(
            "INSERT INTO app_legsession VALUES (60, 'ca', 't', 's', 2025, TRUE, TRUE);"
        )

        original = reader.get_current_sessions

        def fake_get_current_sessions(conn, region):
            if region == "US":
                raise RuntimeError("replica unavailable")
            return original(conn, region)

        monkeypatch.setattr(reader, "get_current_sessions", fake_get_current_sessions)

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            rollup = diff_many(["CA", "US"], cache, casefile, replica, today=date(2026, 7, 21))
            assert rollup["scope_regions"] == 2
            assert "US" in rollup["errors"] and rollup["regions_diffed"] == 1
            assert rollup["anomalies_new"] == 1
            assert rollup["top_cases"]
            assert rollup["top_cases"][0]["severity"] <= rollup["top_cases"][-1]["severity"]


def test_diff_many_error_message_is_truncated(tmp_path, monkeypatch):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 8001, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        replica = _new_replica()

        def fake_get_current_sessions(conn, region):
            raise RuntimeError("X" * 500)

        monkeypatch.setattr(reader, "get_current_sessions", fake_get_current_sessions)

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            rollup = diff_many(["CA"], cache, casefile, replica, today=date(2026, 7, 21))
            assert "CA" in rollup["errors"]
            assert len(rollup["errors"]["CA"]) <= 120


def test_diff_many_errors_capped_at_ten_entries(tmp_path, monkeypatch):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        replica = _new_replica()

        def fake_get_current_sessions(conn, region):
            raise RuntimeError("boom")

        monkeypatch.setattr(reader, "get_current_sessions", fake_get_current_sessions)
        regions = [f"R{i}" for i in range(15)]

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            rollup = diff_many(regions, cache, casefile, replica, today=date(2026, 7, 21))
            assert len([k for k in rollup["errors"] if k != "_more"]) == 10
            assert rollup["errors"]["_more"] == 5


def test_second_run_counts_recurring(tmp_path):
    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 7001, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        bill = dict(BILL, bill_id=701, bill_number="AB70", title="A bill about testing")
        cache.ingest_dataset_zip(7001, make_dataset_zip([bill]))

        replica = _new_replica()
        replica.executescript(
            "INSERT INTO app_legsession VALUES (70, 'ca', 't', 's', 2025, TRUE, TRUE);"
        )

        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
            s2 = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
            assert s2["anomalies_new"] == 0 and s2["anomalies_recurring"] >= 1
