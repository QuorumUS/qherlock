import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from qherlock.casefiles.models import Anomaly

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    gap_type TEXT NOT NULL, region TEXT NOT NULL, session_key TEXT NOT NULL,
    bill_number_norm TEXT NOT NULL, field TEXT NOT NULL DEFAULT '',
    legiscan_value TEXT, quorum_value TEXT, evidence_json TEXT,
    severity TEXT, classification TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL, finished_at TEXT,
    scope TEXT NOT NULL, stats_json TEXT, transcript_path TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaseFileStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: tuple = ()):
        """Helper for execute calls in upsert_anomaly, exposed for monkeypatching in tests."""
        return self._conn.execute(sql, params)

    def upsert_anomaly(self, a: Anomaly) -> tuple[str, int]:
        """Returns ("created"|"recurring", id). "created" is the write outcome — distinct from the lifecycle status column, whose initial value stays 'new'."""
        now = _now()
        existing = self._execute(
            "SELECT id FROM anomalies WHERE fingerprint = ?", (a.fingerprint,)
        ).fetchone()
        if existing:
            self._execute(
                """UPDATE anomalies SET last_seen = ?, legiscan_value = ?, quorum_value = ?,
                          evidence_json = ?, severity = ? WHERE id = ?""",
                (now, a.legiscan_value, a.quorum_value, json.dumps(a.evidence), a.severity, existing["id"]),
            )
            self._conn.commit()
            return "recurring", existing["id"]
        try:
            cur = self._execute(
                """INSERT INTO anomalies (fingerprint, gap_type, region, session_key,
                       bill_number_norm, field, legiscan_value, quorum_value, evidence_json,
                       severity, status, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
                (a.fingerprint, a.gap_type, a.region, a.session_key, a.bill_number_norm,
                 a.field, a.legiscan_value, a.quorum_value, json.dumps(a.evidence), a.severity, now, now),
            )
        except sqlite3.IntegrityError:
            # Lost a check-then-insert race with a concurrent writer -> treat as recurring
            row = self._execute(
                "SELECT id FROM anomalies WHERE fingerprint = ?", (a.fingerprint,)
            ).fetchone()
            self._execute(
                """UPDATE anomalies SET last_seen = ?, legiscan_value = ?, quorum_value = ?,
                          evidence_json = ?, severity = ? WHERE id = ?""",
                (now, a.legiscan_value, a.quorum_value, json.dumps(a.evidence), a.severity, row["id"]),
            )
            self._conn.commit()
            return "recurring", row["id"]
        self._conn.commit()
        return "created", cur.lastrowid

    def list_anomalies(self, region: str | None = None, gap_type: str | None = None,
                       status: str | None = None, limit: int = 10) -> list[dict]:
        clauses, params = [], []
        for col, val in (("region", region), ("gap_type", gap_type), ("status", status)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""SELECT id, fingerprint, gap_type, region, session_key, bill_number_norm,
                       field, severity, status, first_seen, last_seen
                FROM anomalies {where} ORDER BY last_seen DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_anomaly(self, anomaly_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM anomalies WHERE id = ?", (anomaly_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        return d

    def start_patrol(self, scope: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO patrols (started_at, scope) VALUES (?, ?)", (_now(), scope)
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_patrol(self, patrol_id: int, stats: dict, transcript_path: str) -> None:
        self._conn.execute(
            "UPDATE patrols SET finished_at = ?, stats_json = ?, transcript_path = ? WHERE id = ?",
            (_now(), json.dumps(stats), transcript_path, patrol_id),
        )
        self._conn.commit()
