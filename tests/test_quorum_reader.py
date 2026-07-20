import sqlite3

import pytest

from sherlock.quorum import reader


@pytest.fixture
def fake_replica():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE app_legsession (
            id INTEGER PRIMARY KEY, region_abbrev TEXT, title TEXT, session_name TEXT,
            start_year INTEGER, current BOOLEAN, regular_session BOOLEAN
        );
        CREATE TABLE bill_bill (
            id INTEGER PRIMARY KEY, label TEXT, number TEXT, session_id INTEGER
        );
        INSERT INTO app_legsession VALUES
            (10, 'ca', '2025-2026 Regular', '2025-2026 Regular Session', 2025, TRUE, TRUE),
            (11, 'ca', 'Old', 'Old Session', 2021, FALSE, TRUE),
            (12, 'tx', 'TX now', 'TX Session', 2025, TRUE, TRUE);
        INSERT INTO bill_bill VALUES
            (1, 'AB 12', 'AB 12', 10),
            (2, 'SB 1', 'SB 1', 10),
            (3, 'HB 99', 'HB 99', 12);
        """
    )
    return conn


def test_get_current_sessions_filters_by_state_case_insensitive(fake_replica):
    sessions = reader.get_current_sessions(fake_replica, "CA")
    assert [s.id for s in sessions] == [10]
    assert sessions[0].current is True


def test_get_bills_for_session(fake_replica):
    bills = reader.get_bills_for_session(fake_replica, 10)
    assert {b.label for b in bills} == {"AB 12", "SB 1"}


def test_check_schema_ok_and_broken(fake_replica):
    ok, err = reader.check_schema(fake_replica)
    assert ok is True and err == ""
    broken = sqlite3.connect(":memory:")
    ok, err = reader.check_schema(broken)
    assert ok is False and "app_legsession" in err
