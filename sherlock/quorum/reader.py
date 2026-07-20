"""Read-only SQL against the Quorum replica.

ALL Quorum SQL lives in this module (spec §7). Table names resolved 2026-07-20
from quorum-site INSTALLED_APPS labels ("app", "app.bill") + Django defaults:
  - app_legsession  (app/models.py ~L17062, LegSession)
  - bill_bill       (app/bill/models.py ~L3577, Bill)
Future milestones: bill_billaction, bill_billtext, vote_vote (bill FK column is
related_bill_id — NOT bill_id).
"""

from dataclasses import dataclass

import psycopg

_SESSIONS_SQL = """
SELECT id, region_abbrev, title, session_name, start_year, current, regular_session
FROM app_legsession
WHERE LOWER(region_abbrev) = LOWER({ph}) AND current = TRUE
"""

_BILLS_SQL = """
SELECT id, label, number FROM bill_bill WHERE session_id = {ph}
"""

_SCHEMA_PROBES = (
    ("app_legsession", _SESSIONS_SQL, ("x",)),
    ("bill_bill", _BILLS_SQL, (0,)),
)


@dataclass
class SessionRow:
    id: int
    region_abbrev: str
    title: str | None
    session_name: str | None
    start_year: int | None
    current: bool
    regular_session: bool


@dataclass
class BillRow:
    id: int
    label: str | None
    number: str | None


def connect(dsn: str):
    return psycopg.connect(dsn)


def _execute(conn, sql: str, params: tuple = ()):
    ph = "?" if type(conn).__module__.startswith("sqlite3") else "%s"
    cur = conn.cursor()
    cur.execute(sql.format(ph=ph), params)
    return cur


def get_current_sessions(conn, state: str) -> list[SessionRow]:
    cur = _execute(conn, _SESSIONS_SQL, (state,))
    return [SessionRow(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]))
            for r in cur.fetchall()]


def get_bills_for_session(conn, session_id: int) -> list[BillRow]:
    cur = _execute(conn, _BILLS_SQL, (session_id,))
    return [BillRow(r[0], r[1], r[2]) for r in cur.fetchall()]


def check_schema(conn) -> tuple[bool, str]:
    """Startup smoke test (spec §7): schema drift must alert, not crash-loop."""
    for table, sql, params in _SCHEMA_PROBES:
        try:
            _execute(conn, sql + " LIMIT 1", params)
        except Exception as exc:  # any driver error means drift
            return False, f"schema check failed for {table}: {exc}"
    return True, ""
