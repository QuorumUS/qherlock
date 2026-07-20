import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    year_start INTEGER, year_end INTEGER, special INTEGER,
    session_name TEXT, dataset_hash TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS bills (
    bill_id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    number TEXT, change_hash TEXT, status INTEGER, status_date TEXT,
    last_action_date TEXT,
    n_sponsors INTEGER, n_actions INTEGER, n_texts INTEGER, n_votes INTEGER,
    payload_json TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bills_session ON bills(session_id);
CREATE TABLE IF NOT EXISTS quota (month TEXT PRIMARY KEY, calls_used INTEGER NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LegiScanCache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- sessions ------------------------------------------------------------
    def upsert_session(self, state: str, s: dict) -> None:
        self._conn.execute(
            """INSERT INTO sessions (session_id, state, year_start, year_end, special,
                                     session_name, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 state=excluded.state, year_start=excluded.year_start,
                 year_end=excluded.year_end, special=excluded.special,
                 session_name=excluded.session_name, fetched_at=excluded.fetched_at""",
            (s["session_id"], state, s.get("year_start"), s.get("year_end"),
             s.get("special", 0), s.get("session_name"), _now()),
        )
        self._conn.commit()

    def get_sessions(self, state: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE state = ? ORDER BY year_start DESC", (state,)
        ).fetchall()
        return [dict(r) for r in rows]

    def dataset_hash(self, session_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT dataset_hash FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["dataset_hash"] if row else None

    def set_dataset_hash(self, session_id: int, h: str) -> None:
        self._conn.execute(
            """INSERT INTO sessions (session_id, state, dataset_hash, fetched_at)
               VALUES (?, '', ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 dataset_hash=excluded.dataset_hash, fetched_at=excluded.fetched_at""",
            (session_id, h, _now()),
        )
        self._conn.commit()

    # -- bills ---------------------------------------------------------------
    def ingest_dataset_zip(self, session_id: int, zip_bytes: bytes) -> int:
        count = 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if "/bill/" not in name or not name.endswith(".json"):
                    continue
                bill = json.loads(zf.read(name))["bill"]
                history = bill.get("history") or []
                last_action = max((h["date"] for h in history), default=None)
                self._conn.execute(
                    """INSERT INTO bills (bill_id, session_id, number, change_hash, status,
                                          status_date, last_action_date, n_sponsors, n_actions,
                                          n_texts, n_votes, payload_json, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bill_id) DO UPDATE SET
                         number=excluded.number, change_hash=excluded.change_hash,
                         status=excluded.status, status_date=excluded.status_date,
                         last_action_date=excluded.last_action_date,
                         n_sponsors=excluded.n_sponsors, n_actions=excluded.n_actions,
                         n_texts=excluded.n_texts, n_votes=excluded.n_votes,
                         payload_json=excluded.payload_json, fetched_at=excluded.fetched_at""",
                    (bill["bill_id"], session_id,
                     bill.get("bill_number") or bill.get("number"),
                     bill.get("change_hash"), bill.get("status"), bill.get("status_date"),
                     last_action, len(bill.get("sponsors") or []), len(history),
                     len(bill.get("texts") or []), len(bill.get("votes") or []),
                     json.dumps(bill), _now()),
                )
                count += 1
        self._conn.commit()
        return count

    def upsert_bill_stub(self, session_id: int, bill_id: int, number: str, change_hash: str) -> None:
        self._conn.execute(
            """INSERT INTO bills (bill_id, session_id, number, change_hash, fetched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(bill_id) DO UPDATE SET
                 number=excluded.number, change_hash=excluded.change_hash,
                 fetched_at=excluded.fetched_at""",
            (bill_id, session_id, number, change_hash, _now()),
        )
        self._conn.commit()

    def bills_for_session(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            """SELECT bill_id, number, change_hash, status, status_date, last_action_date,
                      n_sponsors, n_actions, n_texts, n_votes
               FROM bills WHERE session_id = ?""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_bill_payload(self, bill_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT payload_json FROM bills WHERE bill_id = ?", (bill_id,)
        ).fetchone()
        if row is None or row["payload_json"] is None:
            return None
        return json.loads(row["payload_json"])

    # -- quota ---------------------------------------------------------------
    def add_call(self, op: str) -> None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        self._conn.execute(
            """INSERT INTO quota (month, calls_used) VALUES (?, 1)
               ON CONFLICT(month) DO UPDATE SET calls_used = calls_used + 1""",
            (month,),
        )
        self._conn.commit()

    def calls_this_month(self) -> int:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        row = self._conn.execute(
            "SELECT calls_used FROM quota WHERE month = ?", (month,)
        ).fetchone()
        return row["calls_used"] if row else 0
