"""Read-only SQL against the Quorum replica.

ALL Quorum SQL lives in this module (spec §7). Table names resolved 2026-07-20
from quorum-site INSTALLED_APPS labels ("app", "app.bill") + Django defaults:
  - app_legsession  (app/models.py ~L17062, LegSession)
  - bill_bill       (app/bill/models.py ~L3577, Bill)
Related tables: bill_billaction, bill_billtext, bill_sponsor (bill FK column is
bill_id) and vote_vote (bill FK column is related_bill_id — NOT bill_id).
"""

from dataclasses import dataclass

import psycopg

_SESSIONS_SQL = """
SELECT id, region_abbrev, title, session_name, start_year, current, regular_session
FROM app_legsession
WHERE LOWER(region_abbrev) = LOWER({ph}) AND current = TRUE
"""

_BILLS_SQL = """
SELECT id, label, number, bill_type, current_general_status,
       current_status_date, most_recent_action_date, introduced_date,
       missing_data, last_quorum_update, source
FROM bill_bill
WHERE session_id = {ph}
"""

# One aggregate GROUP BY per related table per session — never per-bill.
# FK columns verified against quorum-site: bill_billaction.bill_id
# (app/bill/models.py:3498), bill_billtext.bill_id (:3280),
# bill_sponsor.bill_id (:3094 — the bill_bill_sponsors M2M is deprecated),
# vote_vote.related_bill_id (app/vote/models.py:867).
_COUNTS_SQL = {
    "actions":  """SELECT a.bill_id, COUNT(*) FROM bill_billaction a
                   JOIN bill_bill b ON b.id = a.bill_id
                   WHERE b.session_id = {ph} GROUP BY a.bill_id""",
    "texts":    """SELECT t.bill_id, COUNT(*) FROM bill_billtext t
                   JOIN bill_bill b ON b.id = t.bill_id
                   WHERE b.session_id = {ph} GROUP BY t.bill_id""",
    "sponsors": """SELECT s.bill_id, COUNT(*) FROM bill_sponsor s
                   JOIN bill_bill b ON b.id = s.bill_id
                   WHERE b.session_id = {ph} GROUP BY s.bill_id""",
    "votes":    """SELECT v.related_bill_id, COUNT(*) FROM vote_vote v
                   JOIN bill_bill b ON b.id = v.related_bill_id
                   WHERE b.session_id = {ph} GROUP BY v.related_bill_id""",
}

_ACTIONS_RECENT_SQL = """
SELECT date, action_type FROM bill_billaction
WHERE bill_id = {ph} ORDER BY date DESC LIMIT 5
"""

_SCHEMA_PROBES = (
    ("app_legsession", _SESSIONS_SQL, ("x",)),
    ("bill_bill", _BILLS_SQL, (0,)),
    ("bill_billaction", _COUNTS_SQL["actions"], (0,)),
    ("bill_billtext", _COUNTS_SQL["texts"], (0,)),
    ("bill_sponsor", _COUNTS_SQL["sponsors"], (0,)),
    ("vote_vote", _COUNTS_SQL["votes"], (0,)),
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
    bill_type: int | None
    current_general_status: int | None
    current_status_date: object          # date (psycopg) or ISO str (sqlite fixtures)
    most_recent_action_date: object | None
    introduced_date: object | None
    missing_data: bool
    last_quorum_update: object | None
    source: str | None


@dataclass
class BillCounts:
    actions: int = 0
    texts: int = 0
    sponsors: int = 0
    votes: int = 0


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
    return [BillRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]), r[9], r[10])
            for r in cur.fetchall()]


def get_bill_counts_for_session(conn, session_id: int) -> dict[int, BillCounts]:
    out: dict[int, BillCounts] = {}
    for field, sql in _COUNTS_SQL.items():
        for bill_id, n in _execute(conn, sql, (session_id,)).fetchall():
            setattr(out.setdefault(bill_id, BillCounts()), field, n)
    return out


def get_recent_actions(conn, bill_id: int) -> list[dict]:
    cur = _execute(conn, _ACTIONS_RECENT_SQL, (bill_id,))
    return [{"date": r[0], "action_type": r[1]} for r in cur.fetchall()]


def check_schema(conn) -> tuple[bool, str]:
    """Startup smoke test (spec §7): schema drift must alert, not crash-loop."""
    for table, sql, params in _SCHEMA_PROBES:
        try:
            _execute(conn, sql + " LIMIT 1", params)
        except Exception as exc:  # any driver error means drift
            return False, f"schema check failed for {table}: {exc}"
    return True, ""
