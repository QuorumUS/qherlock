import sqlite3

import pytest

from sherlock.casefiles.store import CaseFileStore
from sherlock.diff.service import diff_state
from sherlock.legiscan.cache import LegiScanCache
from tests.test_legiscan_cache import BILL, make_dataset_zip


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
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
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
        INSERT INTO app_legsession VALUES (10, 'ca', 't', 's', 2025, TRUE, TRUE);
        INSERT INTO bill_bill (id, session_id, label, number) VALUES (1, 10, 'AB 12', 'AB 12');
        """
    )
    return conn


def test_diff_finds_missing_bill(tmp_path, cache, replica):
    with CaseFileStore(tmp_path / "casefile.db") as casefile:
        summary = diff_state("CA", cache, casefile, replica)
        assert summary["sessions_matched"] == 1
        assert summary["anomalies_new"] == 1          # AB99 missing; AB12 matches "AB 12"
        assert summary["top_cases"][0]["bill_number"] == "AB99"
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
