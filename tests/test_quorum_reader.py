import sqlite3

import pytest

from qherlock.quorum import reader


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

def _make_replica(exclude=None):
    conn = sqlite3.connect(":memory:")
    for stmt in _REPLICA_SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        if exclude and f"CREATE TABLE {exclude} " in stmt:
            continue
        conn.execute(stmt)
    return conn


@pytest.fixture
def replica_factory():
    return _make_replica


@pytest.fixture
def fake_replica(replica_factory):
    conn = replica_factory()
    conn.executescript(
        """
        INSERT INTO app_legsession VALUES
            (10, 'ca', '2025-2026 Regular', '2025-2026 Regular Session', 2025, TRUE, TRUE),
            (11, 'ca', 'Old', 'Old Session', 2021, FALSE, TRUE),
            (12, 'tx', 'TX now', 'TX Session', 2025, TRUE, TRUE);
        INSERT INTO bill_bill (id, session_id, label, number) VALUES
            (1, 10, 'AB 12', 'AB 12'),
            (2, 10, 'SB 1', 'SB 1'),
            (3, 12, 'HB 99', 'HB 99');
        """
    )
    return conn


@pytest.fixture
def replica(replica_factory):
    return replica_factory()


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


def test_bill_row_carries_detail_columns(replica):
    replica.execute(
        "INSERT INTO bill_bill VALUES "
        "(10,1,'AB 1','AB1',1,2,'2026-07-01','2026-07-10','2026-01-05',0,'2026-07-11','legiscan')"
    )
    replica.execute(
        "INSERT INTO app_legsession VALUES (1,'CA','t','n',2025,1,1)"
    )
    rows = reader.get_bills_for_session(replica, 1)
    b = rows[0]
    assert b.bill_type == 1 and b.current_general_status == 2
    assert b.most_recent_action_date == "2026-07-10"
    assert b.missing_data is False


def test_counts_aggregate_per_session(replica):
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (20,1,'AB 1',1)")
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (21,1,'AB 2',2)")
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (99,2,'HB 9',9)")
    for _ in range(2):
        replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (20,'2026-01-01',1)")
    replica.execute("INSERT INTO bill_billtext (bill_id) VALUES (20)")
    for _ in range(3):
        replica.execute("INSERT INTO bill_sponsor (bill_id, sponsor_type) VALUES (20,1)")
    replica.execute("INSERT INTO vote_vote (related_bill_id) VALUES (20)")
    replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (99,'2026-01-01',1)")
    counts = reader.get_bill_counts_for_session(replica, 1)
    assert counts[20].actions == 2 and counts[20].texts == 1
    assert counts[20].sponsors == 3 and counts[20].votes == 1
    assert 21 not in counts          # zero related rows -> absent; caller defaults BillCounts()
    assert 99 not in counts          # other session never leaks in


def test_recent_actions_ordered_desc_capped(replica):
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (30,1,'AB 1',1)")
    for d in ("2026-01-01", "2026-03-01", "2026-02-01", "2026-04-01", "2026-05-01", "2026-06-01"):
        replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (30, ?, 1)", (d,))
    acts = reader.get_recent_actions(replica, 30)
    assert len(acts) == 5
    assert acts[0]["date"] == "2026-06-01"


def test_check_schema_flags_each_new_table(replica_factory):
    for missing in ("bill_billaction", "bill_billtext", "bill_sponsor", "vote_vote"):
        conn = replica_factory(exclude=missing)
        ok, err = reader.check_schema(conn)
        assert not ok and missing in err


def test_federal_sessions_match_us(replica_factory):
    conn = replica_factory()
    conn.execute("INSERT INTO app_legsession VALUES (5,'us','119th Congress','119',2025,1,1)")
    assert reader.get_current_sessions(conn, "US")[0].id == 5
