# Sherlock M0 — Skeleton Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working Claude-Agent-SDK patroller that syncs LegiScan for California, diffs bill *existence* against Quorum's replica, records anomalies in case files, and prints a patrol report to the console.

**Architecture:** One Python package (`sherlock/`) per spec §13. Deterministic services (LegiScan client+cache, replica reader, matcher, existence detector, case-file store) are plain modules with their own tests; the agent layer wraps four of them as SDK tools and Claude drives the patrol. M0 is strictly read-only: no Slack, no fixes, one state (CA), existence detector only (the other three detectors are M1 per spec §15).

**Tech Stack:** Python 3.12, `uv`, `claude-agent-sdk`, `httpx`, `typer`, `pydantic-settings`, `structlog`, `psycopg[binary]`, SQLite (stdlib `sqlite3`), `pytest` + `pytest-asyncio`.

## Global Constraints

- Python `>=3.12`; all commands run through `uv run …` from the repo root.
- Spec: `docs/superpowers/specs/2026-07-20-sherlock-design.md`. M0 scope = spec §15 M0 only.
- M0 is read-only: the agent gets exactly four tools — `legiscan_sync`, `diff_state`, `list_anomalies`, `get_anomaly`. No write-capable tool exists in M0.
- Every tool returns bounded output: lists truncated to 10 items, evidence strings to 1,500 chars.
- Model default `claude-sonnet-5`; max turns default `100` (env-overridable, spec §13 names: `SHERLOCK_MODEL`, `SHERLOCK_MAX_TURNS`).
- LegiScan budget guard: at ≥80% of 30,000 calls/month, sync degrades to cache-only and says so (spec §6).
- Resolved Quorum schema (this plan's recon; document next to SQL): sessions table `app_legsession` (cols: `id`, `region_abbrev`, `title`, `session_name`, `start_year`, `current`, `regular_session`, `state_info`), bills table `bill_bill` (cols: `id`, `label`, `number`, `session_id`). Future-milestone note: `vote_vote` FK to bill is `related_bill_id`, actions table is `bill_billaction`, texts `bill_billtext`.
- Commit after every task with the message given in the task.

---

### Task 1: Scaffold package + settings

**Files:**
- Create: `pyproject.toml`
- Create: `sherlock/__init__.py`, `sherlock/config.py`
- Create: `tests/__init__.py`, `tests/test_config.py`
- Modify: `.gitignore` (append)

**Interfaces:**
- Produces: `sherlock.config.Settings` — pydantic-settings class; fields
  `legiscan_api_key: str`, `anthropic_api_key: str = ""`, `quorum_replica_dsn: str = ""`,
  `sherlock_model: str = "claude-sonnet-5"`, `sherlock_max_turns: int = 100`,
  `data_dir: Path = Path("data")`, `runs_dir: Path = Path("runs")`;
  method `ensure_dirs() -> None` creates `data_dir`/`runs_dir`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "sherlock"
version = "0.1.0"
description = "Agentic auditor: LegiScan vs Quorum legislative data"
requires-python = ">=3.12"
dependencies = [
    "claude-agent-sdk>=0.1.0",
    "httpx>=0.27",
    "typer>=0.12",
    "pydantic-settings>=2.4",
    "structlog>=24.1",
    "psycopg[binary]>=3.2",
]

[project.scripts]
sherlock = "sherlock.cli:app"

[dependency-groups]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["sherlock"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Append to `.gitignore`**

```gitignore

# Sherlock runtime artifacts
data/
runs/
```

- [ ] **Step 3: Write the failing test** (`tests/test_config.py`)

```python
from pathlib import Path

from sherlock.config import Settings


def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    assert s.legiscan_api_key == "test-key"
    assert s.sherlock_model == "claude-sonnet-5"
    assert s.sherlock_max_turns == 100
    s.ensure_dirs()
    assert (tmp_path / "data").is_dir() and (tmp_path / "runs").is_dir()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'sherlock'` or import error) after uv resolves deps. If `claude-agent-sdk` fails to resolve, run `uv add claude-agent-sdk` and check the exact package name on PyPI before continuing.

- [ ] **Step 5: Implement** — `sherlock/__init__.py` (empty) and `sherlock/config.py`:

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    legiscan_api_key: str
    anthropic_api_key: str = ""
    quorum_replica_dsn: str = ""
    sherlock_model: str = "claude-sonnet-5"
    sherlock_max_turns: int = 100
    data_dir: Path = Path("data")
    runs_dir: Path = Path("runs")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
```

Also create empty `tests/__init__.py`.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (1 passed)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore sherlock/ tests/
git commit -m "feat: scaffold sherlock package and settings"
```

---

### Task 2: LegiScan API client

**Files:**
- Create: `sherlock/legiscan/__init__.py`, `sherlock/legiscan/client.py`
- Test: `tests/test_legiscan_client.py`

**Interfaces:**
- Produces: `sherlock.legiscan.client.LegiScanClient(api_key: str, http: httpx.Client | None = None, on_call: Callable[[str], None] | None = None)` with methods
  `get_session_list(state: str) -> list[dict]`,
  `get_master_list_raw(session_id: int) -> dict`,
  `get_dataset_list(state: str) -> list[dict]`,
  `get_dataset(session_id: int, access_key: str) -> dict`,
  `get_bill(bill_id: int) -> dict`;
  exception `LegiScanError(RuntimeError)`; constant `BASE_URL = "https://api.legiscan.com/"`.
  `on_call(op)` fires before every HTTP request (quota hook for Task 3/4).

- [ ] **Step 1: Write the failing tests** (`tests/test_legiscan_client.py`)

```python
import httpx
import pytest

from sherlock.legiscan.client import BASE_URL, LegiScanClient, LegiScanError


def make_client(handler, on_call=None):
    transport = httpx.MockTransport(handler)
    return LegiScanClient(
        "k", http=httpx.Client(transport=transport, base_url=BASE_URL), on_call=on_call
    )


def test_get_session_list_unwraps_and_counts_calls():
    calls = []

    def handler(request):
        assert request.url.params["key"] == "k"
        assert request.url.params["op"] == "getSessionList"
        assert request.url.params["state"] == "CA"
        return httpx.Response(
            200,
            json={"status": "OK", "sessions": [{"session_id": 2172, "year_start": 2025}]},
        )

    client = make_client(handler, on_call=calls.append)
    sessions = client.get_session_list("CA")
    assert sessions == [{"session_id": 2172, "year_start": 2025}]
    assert calls == ["getSessionList"]


def test_non_ok_status_raises():
    def handler(request):
        return httpx.Response(200, json={"status": "ERROR", "alert": {"message": "bad key"}})

    client = make_client(handler)
    with pytest.raises(LegiScanError):
        client.get_session_list("CA")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_legiscan_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sherlock.legiscan'`

- [ ] **Step 3: Implement** — `sherlock/legiscan/__init__.py` (empty) and `sherlock/legiscan/client.py`:

```python
from collections.abc import Callable

import httpx

BASE_URL = "https://api.legiscan.com/"


class LegiScanError(RuntimeError):
    pass


class LegiScanClient:
    """Thin LegiScan Pull API client. One method per operation; unwraps payloads."""

    def __init__(
        self,
        api_key: str,
        http: httpx.Client | None = None,
        on_call: Callable[[str], None] | None = None,
    ):
        self._key = api_key
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=120)
        self._on_call = on_call or (lambda op: None)

    def _get(self, op: str, **params) -> dict:
        self._on_call(op)
        resp = self._http.get("/", params={"key": self._key, "op": op, **params})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "OK":
            raise LegiScanError(f"{op} failed: {payload.get('alert', payload)}")
        return payload

    def get_session_list(self, state: str) -> list[dict]:
        return self._get("getSessionList", state=state)["sessions"]

    def get_master_list_raw(self, session_id: int) -> dict:
        return self._get("getMasterListRaw", id=session_id)["masterlist"]

    def get_dataset_list(self, state: str) -> list[dict]:
        return self._get("getDatasetList", state=state)["datasetlist"]

    def get_dataset(self, session_id: int, access_key: str) -> dict:
        return self._get("getDataset", id=session_id, access_key=access_key)["dataset"]

    def get_bill(self, bill_id: int) -> dict:
        return self._get("getBill", id=bill_id)["bill"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_legiscan_client.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/legiscan/ tests/test_legiscan_client.py
git commit -m "feat: add LegiScan API client"
```

---

### Task 3: LegiScan cache store

**Files:**
- Create: `sherlock/legiscan/cache.py`
- Test: `tests/test_legiscan_cache.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure SQLite).
- Produces: `sherlock.legiscan.cache.LegiScanCache(db_path: Path)` with methods
  `upsert_session(state: str, s: dict) -> None` (keys used: `session_id`, `year_start`, `year_end`, `special`, `session_name`),
  `get_sessions(state: str) -> list[dict]`,
  `dataset_hash(session_id: int) -> str | None`, `set_dataset_hash(session_id: int, h: str) -> None`,
  `ingest_dataset_zip(session_id: int, zip_bytes: bytes) -> int` (returns bills ingested),
  `upsert_bill_stub(session_id: int, bill_id: int, number: str, change_hash: str) -> None`,
  `bills_for_session(session_id: int) -> list[dict]` (keys: `bill_id`, `number`, `change_hash`, `status`, `status_date`, `last_action_date`, `n_sponsors`, `n_actions`, `n_texts`, `n_votes`),
  `get_bill_payload(bill_id: int) -> dict | None`,
  `add_call(op: str) -> None`, `calls_this_month() -> int`,
  `close() -> None`. Context-manager support (`__enter__`/`__exit__`).

- [ ] **Step 1: Write the failing tests** (`tests/test_legiscan_cache.py`)

```python
import base64
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_legiscan_cache.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.legiscan.cache`)

- [ ] **Step 3: Implement** — `sherlock/legiscan/cache.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_legiscan_cache.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/legiscan/cache.py tests/test_legiscan_cache.py
git commit -m "feat: add LegiScan cache store"
```

---

### Task 4: Sync service (budget-aware)

**Files:**
- Create: `sherlock/legiscan/sync.py`
- Test: `tests/test_legiscan_sync.py`

**Interfaces:**
- Consumes: `LegiScanClient` (Task 2), `LegiScanCache` (Task 3).
- Produces: `sherlock.legiscan.sync.sync_state(state: str, client: LegiScanClient, cache: LegiScanCache, budget_limit: int = 30000, today_year: int | None = None) -> dict` returning
  `{"state", "sessions": int, "datasets_ingested": int, "bills_ingested": int, "masterlist_refreshed": int, "calls_this_month": int, "degraded": bool}`.
  Rule: a LegiScan session is "current" when `year_end >= today_year`. Degraded mode (≥80% budget): no API calls at all, returns cached-session count with `degraded=True`.

- [ ] **Step 1: Write the failing tests** (`tests/test_legiscan_sync.py`)

```python
import base64

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.sync import sync_state
from tests.test_legiscan_cache import BILL, make_dataset_zip


class FakeClient:
    def __init__(self):
        self.dataset_fetches = 0

    def get_session_list(self, state):
        return [
            {"session_id": 2172, "year_start": 2025, "year_end": 2026, "special": 0,
             "session_name": "2025-2026 Regular Session"},
            {"session_id": 1999, "year_start": 2021, "year_end": 2022, "special": 0,
             "session_name": "old session"},
        ]

    def get_dataset_list(self, state):
        return [{"session_id": 2172, "dataset_hash": "h1", "access_key": "ak"},
                {"session_id": 1999, "dataset_hash": "h0", "access_key": "ak"}]

    def get_dataset(self, session_id, access_key):
        self.dataset_fetches += 1
        return {"session_id": session_id,
                "zip": base64.b64encode(make_dataset_zip([BILL])).decode()}

    def get_master_list_raw(self, session_id):
        return {"session": {"session_id": session_id},
                "0": {"bill_id": 111, "number": "AB12", "change_hash": "hash2"}}


def test_sync_ingests_current_sessions_only(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        stats = sync_state("CA", client, cache, today_year=2026)
        assert stats["sessions"] == 1               # 1999 session filtered out
        assert stats["datasets_ingested"] == 1 and client.dataset_fetches == 1
        assert stats["bills_ingested"] == 1
        assert stats["masterlist_refreshed"] == 1
        assert stats["degraded"] is False
        # change_hash updated from masterlist AFTER dataset ingest
        assert cache.bills_for_session(2172)[0]["change_hash"] == "hash2"


def test_sync_skips_unchanged_dataset(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        sync_state("CA", client, cache, today_year=2026)
        stats = sync_state("CA", client, cache, today_year=2026)
        assert stats["datasets_ingested"] == 0      # hash unchanged
        assert client.dataset_fetches == 1


def test_sync_degrades_at_80_percent_budget(tmp_path):
    client = FakeClient()
    with LegiScanCache(tmp_path / "cache.db") as cache:
        for _ in range(8):
            cache.add_call("x")
        stats = sync_state("CA", client, cache, budget_limit=10, today_year=2026)
        assert stats["degraded"] is True
        assert client.dataset_fetches == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_legiscan_sync.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.legiscan.sync`)

- [ ] **Step 3: Implement** — `sherlock/legiscan/sync.py`:

```python
import base64
from datetime import datetime, timezone

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient

DEGRADE_THRESHOLD = 0.8


def sync_state(
    state: str,
    client: LegiScanClient,
    cache: LegiScanCache,
    budget_limit: int = 30000,
    today_year: int | None = None,
) -> dict:
    today_year = today_year or datetime.now(timezone.utc).year
    stats = {"state": state, "sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
             "masterlist_refreshed": 0, "calls_this_month": cache.calls_this_month(),
             "degraded": False}

    if cache.calls_this_month() >= DEGRADE_THRESHOLD * budget_limit:
        stats["degraded"] = True
        stats["sessions"] = len(cache.get_sessions(state))
        return stats

    current = [s for s in client.get_session_list(state)
               if (s.get("year_end") or 0) >= today_year]
    for s in current:
        cache.upsert_session(state, s)
    stats["sessions"] = len(current)
    current_ids = {s["session_id"] for s in current}

    for ds in client.get_dataset_list(state):
        sid = ds["session_id"]
        if sid not in current_ids or cache.dataset_hash(sid) == ds["dataset_hash"]:
            continue
        dataset = client.get_dataset(sid, ds["access_key"])
        zip_bytes = base64.b64decode(dataset["zip"])
        stats["bills_ingested"] += cache.ingest_dataset_zip(sid, zip_bytes)
        cache.set_dataset_hash(sid, ds["dataset_hash"])
        stats["datasets_ingested"] += 1

    for sid in current_ids:
        masterlist = client.get_master_list_raw(sid)
        for key, entry in masterlist.items():
            if key == "session":
                continue
            cache.upsert_bill_stub(sid, entry["bill_id"], entry["number"], entry["change_hash"])
        stats["masterlist_refreshed"] += 1

    stats["calls_this_month"] = cache.calls_this_month()
    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_legiscan_sync.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/legiscan/sync.py tests/test_legiscan_sync.py
git commit -m "feat: add budget-aware LegiScan sync service"
```

---

### Task 5: Quorum replica reader

**Files:**
- Create: `sherlock/quorum/__init__.py`, `sherlock/quorum/reader.py`
- Test: `tests/test_quorum_reader.py`

**Interfaces:**
- Consumes: a DB-API connection (real: `psycopg.connect(dsn)`; tests: `sqlite3`).
- Produces: `sherlock.quorum.reader` with
  `@dataclass SessionRow(id: int, region_abbrev: str, title: str | None, session_name: str | None, start_year: int | None, current: bool, regular_session: bool)`,
  `@dataclass BillRow(id: int, label: str | None, number: str | None)`,
  `get_current_sessions(conn, state: str) -> list[SessionRow]`,
  `get_bills_for_session(conn, session_id: int) -> list[BillRow]`,
  `check_schema(conn) -> tuple[bool, str]`,
  `connect(dsn: str)` (returns a psycopg connection).
  Tables (resolved from quorum-site `INSTALLED_APPS` labels — `"app"` and `"app.bill"`, Django default naming): `app_legsession`, `bill_bill`.

- [ ] **Step 1: Write the failing tests** (`tests/test_quorum_reader.py`)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_quorum_reader.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.quorum`)

- [ ] **Step 3: Implement** — `sherlock/quorum/__init__.py` (empty) and `sherlock/quorum/reader.py`:

```python
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
    for table, sql in (("app_legsession", _SESSIONS_SQL), ("bill_bill", _BILLS_SQL)):
        try:
            _execute(conn, sql + " LIMIT 1", ("x",) if "region_abbrev" in sql else (0,))
        except Exception as exc:  # noqa: BLE001 — any driver error means drift
            return False, f"schema check failed for {table}: {exc}"
    return True, ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_quorum_reader.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/quorum/ tests/test_quorum_reader.py
git commit -m "feat: add read-only Quorum replica reader"
```

---

### Task 6: Matchers (bill-number normalization + session matching)

**Files:**
- Create: `sherlock/diff/__init__.py`, `sherlock/diff/matchers.py`
- Test: `tests/test_matchers.py`

**Interfaces:**
- Consumes: `SessionRow` (Task 5).
- Produces: `sherlock.diff.matchers` with
  `normalize_bill_number(state: str, raw: str | None) -> str` (uppercase; strip spaces/dots/NBSP; strip leading zeros in the numeric part; apply per-state `PREFIX_MAP`, seed `{"CA": {"AR": "HR"}}` — salvaged from quorum-site `app/management/scraper/legiscan/comparison.py`),
  `match_sessions(legiscan_sessions: list[dict], quorum_sessions: list[SessionRow]) -> tuple[list[tuple[dict, SessionRow]], list[str]]` — pairs matched on `regular_session == (special == 0)` and `start_year == year_start`; anything unmatched or ambiguous becomes a warning string (spec §8: session mismatches are warnings, not anomalies).

- [ ] **Step 1: Write the failing tests** (`tests/test_matchers.py`)

```python
from sherlock.diff.matchers import match_sessions, normalize_bill_number
from sherlock.quorum.reader import SessionRow


def test_normalize_strips_and_uppercases():
    assert normalize_bill_number("CA", "ab 0012") == "AB12"
    assert normalize_bill_number("CA", "S.B. 5") == "SB5"
    assert normalize_bill_number("TX", None) == ""


def test_normalize_applies_ca_prefix_map():
    assert normalize_bill_number("CA", "AR 10") == "HR10"
    assert normalize_bill_number("TX", "AR 10") == "AR10"  # map is per-state


def make_qsession(id=10, start_year=2025, regular=True):
    return SessionRow(id=id, region_abbrev="ca", title=None, session_name=None,
                      start_year=start_year, current=True, regular_session=regular)


def test_match_sessions_pairs_regular_by_year():
    ls = [{"session_id": 2172, "year_start": 2025, "year_end": 2026, "special": 0,
           "session_name": "2025-2026 Regular Session"}]
    matched, warnings = match_sessions(ls, [make_qsession()])
    assert len(matched) == 1 and matched[0][1].id == 10
    assert warnings == []


def test_match_sessions_warns_on_no_candidate():
    ls = [{"session_id": 2173, "year_start": 2025, "year_end": 2025, "special": 1,
           "session_name": "First Extraordinary Session"}]
    matched, warnings = match_sessions(ls, [make_qsession()])  # only a regular session
    assert matched == []
    assert len(warnings) == 1 and "2173" in warnings[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_matchers.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.diff`)

- [ ] **Step 3: Implement** — `sherlock/diff/__init__.py` (empty) and `sherlock/diff/matchers.py`:

```python
import re

from sherlock.quorum.reader import SessionRow

# Salvaged seed from quorum-site app/management/scraper/legiscan/comparison.py
PREFIX_MAP: dict[str, dict[str, str]] = {"CA": {"AR": "HR"}}

_CLEAN_RE = re.compile(r"[\s. ]")
_NUM_RE = re.compile(r"^([A-Z]+)0*(\d+)$")


def normalize_bill_number(state: str, raw: str | None) -> str:
    s = _CLEAN_RE.sub("", (raw or "").upper())
    m = _NUM_RE.match(s)
    if not m:
        return s
    prefix, num = m.group(1), m.group(2)
    prefix = PREFIX_MAP.get(state.upper(), {}).get(prefix, prefix)
    return f"{prefix}{num}"


def match_sessions(
    legiscan_sessions: list[dict], quorum_sessions: list[SessionRow]
) -> tuple[list[tuple[dict, SessionRow]], list[str]]:
    matched: list[tuple[dict, SessionRow]] = []
    warnings: list[str] = []
    for ls in legiscan_sessions:
        want_regular = (ls.get("special", 0) == 0)
        candidates = [q for q in quorum_sessions
                      if q.regular_session == want_regular
                      and q.start_year == ls.get("year_start")]
        if len(candidates) == 1:
            matched.append((ls, candidates[0]))
        else:
            warnings.append(
                f"LegiScan session {ls['session_id']} ({ls.get('session_name')!r}, "
                f"years {ls.get('year_start')}-{ls.get('year_end')}, special={ls.get('special', 0)}): "
                f"{len(candidates)} Quorum candidates — skipped"
            )
    return matched, warnings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_matchers.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/diff/ tests/test_matchers.py
git commit -m "feat: add bill-number normalization and session matching"
```

---

### Task 7: Case-file store

**Files:**
- Create: `sherlock/casefiles/__init__.py`, `sherlock/casefiles/models.py`, `sherlock/casefiles/store.py`
- Test: `tests/test_casefiles.py`

**Interfaces:**
- Consumes: nothing (pure SQLite + stdlib).
- Produces:
  - `sherlock.casefiles.models.Anomaly` — dataclass: `gap_type: str`, `region: str`, `session_key: str`, `bill_number_norm: str`, `field: str = ""`, `legiscan_value: str = ""`, `quorum_value: str = ""`, `evidence: dict = field(default_factory=dict)`; property `fingerprint -> str` = `sha1("|".join([gap_type, region, session_key, bill_number_norm, field]))` hexdigest (spec §8).
  - `sherlock.casefiles.store.CaseFileStore(db_path: Path)` with
    `upsert_anomaly(a: Anomaly) -> tuple[str, int]` returning `("new"| "recurring", anomaly_id)`;
    `list_anomalies(region: str | None = None, gap_type: str | None = None, status: str | None = None, limit: int = 10) -> list[dict]`;
    `get_anomaly(anomaly_id: int) -> dict | None` (evidence JSON-decoded);
    `start_patrol(scope: str) -> int`; `finish_patrol(patrol_id: int, stats: dict, transcript_path: str) -> None`;
    `close()`, context-manager support.

- [ ] **Step 1: Write the failing tests** (`tests/test_casefiles.py`)

```python
from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore


def make_anomaly(number="AB12"):
    return Anomaly(gap_type="missing_bill", region="CA", session_key="2172",
                   bill_number_norm=number, legiscan_value="AB12",
                   evidence={"legiscan_bill_id": 111, "title": "An act"})


def test_fingerprint_is_stable_and_field_sensitive():
    a, b = make_anomaly(), make_anomaly()
    assert a.fingerprint == b.fingerprint
    c = make_anomaly()
    c.field = "status"
    assert c.fingerprint != a.fingerprint


def test_upsert_new_then_recurring(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        kind1, aid1 = store.upsert_anomaly(make_anomaly())
        kind2, aid2 = store.upsert_anomaly(make_anomaly())
        assert (kind1, kind2) == ("new", "recurring")
        assert aid1 == aid2
        row = store.get_anomaly(aid1)
        assert row["status"] == "new"
        assert row["evidence"]["legiscan_bill_id"] == 111
        assert row["last_seen"] >= row["first_seen"]


def test_list_anomalies_filters_and_limits(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        for i in range(15):
            store.upsert_anomaly(make_anomaly(number=f"AB{i}"))
        assert len(store.list_anomalies(region="CA")) == 10  # default cap
        assert store.list_anomalies(region="TX") == []
        assert len(store.list_anomalies(gap_type="missing_bill", limit=3)) == 3


def test_patrol_lifecycle(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        pid = store.start_patrol(scope="CA")
        store.finish_patrol(pid, {"anomalies_new": 2}, "runs/1.jsonl")
        assert pid == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_casefiles.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.casefiles`)

- [ ] **Step 3: Implement** — `sherlock/casefiles/__init__.py` (empty), `sherlock/casefiles/models.py`:

```python
import hashlib
from dataclasses import dataclass, field


@dataclass
class Anomaly:
    gap_type: str
    region: str
    session_key: str
    bill_number_norm: str
    field: str = ""
    legiscan_value: str = ""
    quorum_value: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [self.gap_type, self.region, self.session_key, self.bill_number_norm, self.field]
        )
        return hashlib.sha1(raw.encode()).hexdigest()
```

and `sherlock/casefiles/store.py`:

```python
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sherlock.casefiles.models import Anomaly

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
        self._conn.executescript(_SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._conn.close()

    def upsert_anomaly(self, a: Anomaly) -> tuple[str, int]:
        now = _now()
        existing = self._conn.execute(
            "SELECT id FROM anomalies WHERE fingerprint = ?", (a.fingerprint,)
        ).fetchone()
        if existing:
            self._conn.execute(
                """UPDATE anomalies SET last_seen = ?, legiscan_value = ?, quorum_value = ?,
                          evidence_json = ? WHERE id = ?""",
                (now, a.legiscan_value, a.quorum_value, json.dumps(a.evidence), existing["id"]),
            )
            self._conn.commit()
            return "recurring", existing["id"]
        cur = self._conn.execute(
            """INSERT INTO anomalies (fingerprint, gap_type, region, session_key,
                   bill_number_norm, field, legiscan_value, quorum_value, evidence_json,
                   status, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (a.fingerprint, a.gap_type, a.region, a.session_key, a.bill_number_norm,
             a.field, a.legiscan_value, a.quorum_value, json.dumps(a.evidence), now, now),
        )
        self._conn.commit()
        return "new", cur.lastrowid

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
                       field, status, first_seen, last_seen
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_casefiles.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/casefiles/ tests/test_casefiles.py
git commit -m "feat: add case-file store with fingerprint dedup"
```

---

### Task 8: Existence detector + diff service

**Files:**
- Create: `sherlock/diff/service.py`
- Test: `tests/test_diff_service.py`

**Interfaces:**
- Consumes: `LegiScanCache` (Task 3), `reader.get_current_sessions`/`get_bills_for_session` (Task 5), `match_sessions`/`normalize_bill_number` (Task 6), `CaseFileStore`/`Anomaly` (Task 7).
- Produces: `sherlock.diff.service.diff_state(state: str, cache: LegiScanCache, casefile: CaseFileStore, replica_conn) -> dict` returning
  `{"state", "sessions_matched": int, "warnings": list[str], "anomalies_new": int, "anomalies_recurring": int, "top_cases": list[dict]}` where `top_cases` is ≤10 items of `{"id", "bill_number", "session_key", "title"}`. M0 detector: `missing_bill` only (spec §15 M0; other detectors are M1).

- [ ] **Step 1: Write the failing tests** (`tests/test_diff_service.py`)

```python
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
        CREATE TABLE bill_bill (id INTEGER PRIMARY KEY, label TEXT, number TEXT, session_id INTEGER);
        INSERT INTO app_legsession VALUES (10, 'ca', 't', 's', 2025, TRUE, TRUE);
        INSERT INTO bill_bill VALUES (1, 'AB 12', 'AB 12', 10);
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
        CREATE TABLE bill_bill (id INTEGER PRIMARY KEY, label TEXT, number TEXT, session_id INTEGER);
        """
    )
    with CaseFileStore(tmp_path / "casefile.db") as casefile:
        summary = diff_state("CA", cache, casefile, empty_replica)
        assert summary["sessions_matched"] == 0
        assert len(summary["warnings"]) == 1
        assert summary["anomalies_new"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_diff_service.py -v`
Expected: FAIL with `ImportError` (no `sherlock.diff.service`)

- [ ] **Step 3: Implement** — `sherlock/diff/service.py`:

```python
import json

from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore
from sherlock.diff.matchers import match_sessions, normalize_bill_number
from sherlock.legiscan.cache import LegiScanCache
from sherlock.quorum import reader

TOP_CASES_LIMIT = 10


def diff_state(state: str, cache: LegiScanCache, casefile: CaseFileStore, replica_conn) -> dict:
    ls_sessions = cache.get_sessions(state)
    q_sessions = reader.get_current_sessions(replica_conn, state)
    matched, warnings = match_sessions(ls_sessions, q_sessions)

    new = recurring = 0
    top_cases: list[dict] = []
    for ls, qs in matched:
        session_key = str(ls["session_id"])
        quorum_numbers = {
            normalize_bill_number(state, b.label or b.number)
            for b in reader.get_bills_for_session(replica_conn, qs.id)
        }
        for bill in cache.bills_for_session(ls["session_id"]):
            norm = normalize_bill_number(state, bill["number"])
            if not norm or norm in quorum_numbers:
                continue
            payload = cache.get_bill_payload(bill["bill_id"]) or {}
            anomaly = Anomaly(
                gap_type="missing_bill", region=state, session_key=session_key,
                bill_number_norm=norm, legiscan_value=bill["number"] or "",
                evidence={
                    "legiscan_bill_id": bill["bill_id"],
                    "title": (payload.get("title") or "")[:300],
                    "status": bill["status"], "status_date": bill["status_date"],
                    "last_action_date": bill["last_action_date"],
                    "quorum_session_id": qs.id,
                },
            )
            kind, aid = casefile.upsert_anomaly(anomaly)
            if kind == "new":
                new += 1
            else:
                recurring += 1
            if len(top_cases) < TOP_CASES_LIMIT:
                top_cases.append({"id": aid, "bill_number": norm, "session_key": session_key,
                                  "title": anomaly.evidence["title"]})

    return {"state": state, "sessions_matched": len(matched), "warnings": warnings,
            "anomalies_new": new, "anomalies_recurring": recurring, "top_cases": top_cases}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_diff_service.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/diff/service.py tests/test_diff_service.py
git commit -m "feat: add existence detector and diff service"
```

---

### Task 9: Agent tool layer (SDK wiring)

**Files:**
- Create: `sherlock/agent/__init__.py`, `sherlock/agent/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `LegiScanClient` (2), `LegiScanCache` (3), `sync_state` (4), `reader` (5), `diff_state` (8), `CaseFileStore` (7).
- Produces: `sherlock.agent.tools.build_toolkit(settings: Settings) -> tuple[server, list[str]]` — an in-process MCP server named `sherlock` exposing exactly four tools, plus the fully-qualified allowed-tools list `["mcp__sherlock__legiscan_sync", "mcp__sherlock__diff_state", "mcp__sherlock__list_anomalies", "mcp__sherlock__get_anomaly"]` (constant `TOOL_NAMES`). Each handler returns `{"content": [{"type": "text", "text": <json str>}]}` and every text payload is ≤2,000 tokens by construction (top-10 lists, 1,500-char evidence cap — `_bounded()` helper). `diff_state` with no `quorum_replica_dsn` configured returns an explanatory error payload instead of raising.

- [ ] **Step 1: Write the failing tests** (`tests/test_agent_tools.py`)

```python
import json

import pytest

from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


def test_tool_names_are_fully_qualified():
    assert TOOL_NAMES == [
        "mcp__sherlock__legiscan_sync",
        "mcp__sherlock__diff_state",
        "mcp__sherlock__list_anomalies",
        "mcp__sherlock__get_anomaly",
    ]


async def test_diff_state_without_dsn_returns_error_payload(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["diff_state"]({"state": "CA"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"].startswith("no QUORUM_REPLICA_DSN")


async def test_list_anomalies_empty_db(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["list_anomalies"]({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["anomalies"] == []


async def test_get_anomaly_not_found(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["get_anomaly"]({"anomaly_id": 999})
    payload = json.loads(result["content"][0]["text"])
    assert "not found" in payload["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.agent`)

- [ ] **Step 3: Implement** — `sherlock/agent/__init__.py` (empty) and `sherlock/agent/tools.py`:

```python
import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings
from sherlock.diff.service import diff_state as run_diff_state
from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient
from sherlock.legiscan.sync import sync_state
from sherlock.quorum import reader

TOOL_NAMES = [
    "mcp__sherlock__legiscan_sync",
    "mcp__sherlock__diff_state",
    "mcp__sherlock__list_anomalies",
    "mcp__sherlock__get_anomaly",
]

_EVIDENCE_CAP = 1500


def _text(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _bounded(payload: dict) -> dict:
    if "evidence" in payload and payload["evidence"] is not None:
        raw = json.dumps(payload["evidence"], default=str)
        if len(raw) > _EVIDENCE_CAP:
            payload["evidence"] = raw[:_EVIDENCE_CAP] + "…[truncated]"
    return payload


def build_toolkit(settings: Settings, return_handlers: bool = False):
    settings.ensure_dirs()
    cache_path = settings.data_dir / "cache.db"
    casefile_path = settings.data_dir / "casefile.db"

    @tool("legiscan_sync", "Refresh the local LegiScan cache for a state (datasets + "
          "masterlist change-hashes). Budget-aware; read-only.", {"state": str})
    async def legiscan_sync_handler(args: dict) -> dict:
        with LegiScanCache(cache_path) as cache:
            client = LegiScanClient(settings.legiscan_api_key, on_call=lambda op: cache.add_call(op))
            stats = sync_state(args["state"].upper(), client, cache)
        return _text(stats)

    @tool("diff_state", "Diff LegiScan cache vs Quorum replica for a state's current "
          "sessions. Records missing_bill anomalies; returns summary + top cases.",
          {"state": str})
    async def diff_state_handler(args: dict) -> dict:
        if not settings.quorum_replica_dsn:
            return _text({"error": "no QUORUM_REPLICA_DSN configured — start a Teleport "
                                   "tunnel (tsh proxy db) and set it in .env"})
        with LegiScanCache(cache_path) as cache, CaseFileStore(casefile_path) as casefile:
            conn = reader.connect(settings.quorum_replica_dsn)
            try:
                ok, err = reader.check_schema(conn)
                if not ok:
                    return _text({"error": f"replica schema drift: {err}"})
                summary = run_diff_state(args["state"].upper(), cache, casefile, conn)
            finally:
                conn.close()
        return _text(summary)

    @tool("list_anomalies", "List recorded anomalies from case files. Optional filters: "
          "state, gap_type, status. Max 10 rows.",
          {"state": str, "gap_type": str, "status": str})
    async def list_anomalies_handler(args: dict) -> dict:
        with CaseFileStore(casefile_path) as casefile:
            rows = casefile.list_anomalies(
                region=args.get("state"), gap_type=args.get("gap_type"),
                status=args.get("status"), limit=10,
            )
        return _text({"anomalies": rows})

    @tool("get_anomaly", "Fetch one anomaly with full (bounded) evidence by id.",
          {"anomaly_id": int})
    async def get_anomaly_handler(args: dict) -> dict:
        with CaseFileStore(casefile_path) as casefile:
            row = casefile.get_anomaly(int(args["anomaly_id"]))
        if row is None:
            return _text({"error": f"anomaly {args['anomaly_id']} not found"})
        return _text(_bounded(row))

    sdk_tools = [legiscan_sync_handler, diff_state_handler,
                 list_anomalies_handler, get_anomaly_handler]
    server = create_sdk_mcp_server(name="sherlock", version="0.1.0", tools=sdk_tools)
    if return_handlers:
        handlers = {"legiscan_sync": legiscan_sync_handler.handler,
                    "diff_state": diff_state_handler.handler,
                    "list_anomalies": list_anomalies_handler.handler,
                    "get_anomaly": get_anomaly_handler.handler}
        return server, handlers
    return server, TOOL_NAMES
```

Contingency (not a placeholder — a version-drift check): if `from claude_agent_sdk import create_sdk_mcp_server, tool` fails or `@tool`-decorated objects lack a `.handler` attribute, run `uv run python -c "import claude_agent_sdk, inspect; print(claude_agent_sdk.__version__); print([n for n in dir(claude_agent_sdk) if not n.startswith('_')])"` and adapt these two import/attribute names to the installed SDK — everything else in this task is SDK-independent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/agent/ tests/test_agent_tools.py
git commit -m "feat: add SDK tool layer with bounded outputs"
```

---

### Task 10: Patrol runner + M0 doctrine

**Files:**
- Create: `sherlock/agent/patrol.py`
- Test: `tests/test_patrol.py`

**Interfaces:**
- Consumes: `build_toolkit`/`TOOL_NAMES` (Task 9), `CaseFileStore` (7), `Settings` (1).
- Produces: `sherlock.agent.patrol` with
  `DOCTRINE: str` (M0 system prompt),
  `build_options(settings: Settings, server) -> ClaudeAgentOptions`,
  `write_transcript_line(fh, msg) -> None` (JSONL: `{"type": <class name>, "repr": <str(msg)>}`),
  `async run_patrol(settings: Settings, state: str, objective: str = "") -> str` — starts a patrol row, streams `query()` messages to `runs/<patrol_id>.jsonl`, finishes the patrol row with `{"result_chars": len(result)}`, returns the final result text.

- [ ] **Step 1: Write the failing tests** (`tests/test_patrol.py`)

```python
import io
import json

from sherlock.agent.patrol import DOCTRINE, build_options, write_transcript_line
from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.config import Settings


def make_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


def test_doctrine_mentions_every_tool_and_forbids_guessing():
    for name in ("legiscan_sync", "diff_state", "list_anomalies", "get_anomaly"):
        assert name in DOCTRINE
    assert "Never invent" in DOCTRINE


def test_build_options_wires_model_tools_and_turns(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)
    assert options.model == "claude-sonnet-5"
    assert options.max_turns == 100
    assert options.allowed_tools == TOOL_NAMES
    assert options.system_prompt == DOCTRINE
    assert "sherlock" in options.mcp_servers


def test_write_transcript_line_is_jsonl():
    class FakeMsg:
        def __str__(self):
            return "hello"

    fh = io.StringIO()
    write_transcript_line(fh, FakeMsg())
    line = json.loads(fh.getvalue())
    assert line == {"type": "FakeMsg", "repr": "hello"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_patrol.py -v`
Expected: FAIL with `ImportError` (no `sherlock.agent.patrol`)

- [ ] **Step 3: Implement** — `sherlock/agent/patrol.py`:

```python
import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings

DOCTRINE = """You are Sherlock, a data-integrity detective auditing Quorum's legislative \
database against LegiScan.

Patrol procedure (M0 — read-only, one state):
1. Call legiscan_sync for the target state to refresh the local LegiScan cache.
2. Call diff_state to compare LegiScan against Quorum's replica and record anomalies.
3. Inspect the most interesting cases with list_anomalies / get_anomaly.
4. Finish with a patrol report in markdown: session match warnings, anomaly counts \
(new vs recurring), the top cases with bill number, title, and your read on the likely \
cause (ingestion gap? session mismatch? bill-number normalization quirk?), and what you \
would do next.

Rules:
- Never invent data. Every claim in your report must trace to a tool result.
- If a tool returns an error payload, report the error and continue with what you have.
- Session-match warnings usually mean false positives downstream — say so prominently.
- You have no write tools. You observe and report."""


def build_options(settings: Settings, server) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.sherlock_model,
        max_turns=settings.sherlock_max_turns,
        system_prompt=DOCTRINE,
        mcp_servers={"sherlock": server},
        allowed_tools=TOOL_NAMES,
        permission_mode="bypassPermissions",  # only our 4 read-only tools are allowed
    )


def write_transcript_line(fh, msg) -> None:
    fh.write(json.dumps({"type": type(msg).__name__, "repr": str(msg)}) + "\n")


async def run_patrol(settings: Settings, state: str, objective: str = "") -> str:
    settings.ensure_dirs()
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)

    with CaseFileStore(settings.data_dir / "casefile.db") as casefile:
        patrol_id = casefile.start_patrol(scope=state)
        transcript_path = settings.runs_dir / f"patrol-{patrol_id}.jsonl"
        prompt = f"Patrol {state}." + (f" Objective: {objective}" if objective else "")

        result_text = ""
        with open(transcript_path, "w") as fh:
            async for msg in query(prompt=prompt, options=options):
                write_transcript_line(fh, msg)
                if isinstance(msg, ResultMessage):
                    result_text = msg.result or ""

        casefile.finish_patrol(patrol_id, {"result_chars": len(result_text)},
                               str(transcript_path))
    return result_text
```

(Same SDK-version contingency as Task 9 applies to `ClaudeAgentOptions`, `ResultMessage`, `query`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_patrol.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add sherlock/agent/patrol.py tests/test_patrol.py
git commit -m "feat: add patrol runner and M0 doctrine"
```

---

### Task 11: CLI + full-suite gate + manual smoke

**Files:**
- Create: `sherlock/cli.py`
- Test: `tests/test_cli.py`; manual smoke steps below (needs Victor's credentials)

**Interfaces:**
- Consumes: everything above.
- Produces: `sherlock.cli.app` (typer.Typer) with commands
  `sync --state CA`, `diff --state CA`, `patrol --state CA --objective ""` — the `sherlock` console script from Task 1's pyproject.

- [ ] **Step 1: Write the failing test** (`tests/test_cli.py`)

```python
from typer.testing import CliRunner

from sherlock.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("sync", "diff", "patrol"):
        assert cmd in result.output


def test_sync_command_runs_with_fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    # No network in tests: point sync at a fake client via SHERLOCK_TEST_MODE guard.
    monkeypatch.setenv("SHERLOCK_TEST_MODE", "1")
    result = runner.invoke(app, ["sync", "--state", "CA"])
    assert result.exit_code == 0
    assert '"degraded"' in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError` (no `sherlock.cli`)

- [ ] **Step 3: Implement** — `sherlock/cli.py`:

```python
import asyncio
import json
import os

import typer

from sherlock.agent.patrol import run_patrol
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings
from sherlock.diff.service import diff_state as run_diff
from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient
from sherlock.legiscan.sync import sync_state
from sherlock.quorum import reader

app = typer.Typer(help="Sherlock — LegiScan vs Quorum data-integrity patroller")


def _settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


class _NoNetworkClient:
    """SHERLOCK_TEST_MODE stand-in: makes `sync` exercisable without network."""

    def get_session_list(self, state):
        return []

    def get_dataset_list(self, state):
        return []

    def get_master_list_raw(self, session_id):
        return {"session": {}}


@app.command()
def sync(state: str = typer.Option("CA", "--state")) -> None:
    """Refresh the LegiScan cache for STATE."""
    s = _settings()
    with LegiScanCache(s.data_dir / "cache.db") as cache:
        if os.environ.get("SHERLOCK_TEST_MODE") == "1":
            client = _NoNetworkClient()
        else:
            client = LegiScanClient(s.legiscan_api_key, on_call=lambda op: cache.add_call(op))
        stats = sync_state(state.upper(), client, cache)
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def diff(state: str = typer.Option("CA", "--state")) -> None:
    """Diff LegiScan cache vs Quorum replica for STATE."""
    s = _settings()
    if not s.quorum_replica_dsn:
        typer.echo("error: QUORUM_REPLICA_DSN not set — run `tsh proxy db` and set it in .env")
        raise typer.Exit(code=1)
    with LegiScanCache(s.data_dir / "cache.db") as cache, \
         CaseFileStore(s.data_dir / "casefile.db") as casefile:
        conn = reader.connect(s.quorum_replica_dsn)
        try:
            ok, err = reader.check_schema(conn)
            if not ok:
                typer.echo(f"error: {err}")
                raise typer.Exit(code=2)
            summary = run_diff(state.upper(), cache, casefile, conn)
        finally:
            conn.close()
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def patrol(state: str = typer.Option("CA", "--state"),
           objective: str = typer.Option("", "--objective")) -> None:
    """Run a full agentic patrol for STATE (calls the Anthropic API)."""
    s = _settings()
    report = asyncio.run(run_patrol(s, state.upper(), objective))
    typer.echo(report)


if __name__ == "__main__":
    app()
```

Note: `DATA_DIR`/`RUNS_DIR` env vars work because pydantic-settings maps field names to env names case-insensitively.

- [ ] **Step 4: Run test to verify it passes, then the full suite**

Run: `uv run pytest -v`
Expected: PASS — all tests from Tasks 1–11 green (≈25 tests)

- [ ] **Step 5: Commit**

```bash
git add sherlock/cli.py tests/test_cli.py
git commit -m "feat: add sherlock CLI (sync/diff/patrol)"
```

- [ ] **Step 6: Manual smoke (requires Victor's credentials — run together, not in CI)**

1. `.env`: add `ANTHROPIC_API_KEY=...`; keep existing `LEGISCAN_API_KEY`.
2. `uv run sherlock sync --state CA` → expect JSON with `sessions >= 1`, `datasets_ingested >= 1`, `bills_ingested` in the low thousands, `calls_this_month <= ~6`, `degraded: false`. (Real LegiScan calls: sessionlist + datasetlist + dataset(s) + masterlist.)
3. Start Teleport tunnel (`tsh login`, then `tsh proxy db --tunnel <replica> --port 5433`), set `QUORUM_REPLICA_DSN=postgresql://<user>@127.0.0.1:5433/<dbname>` in `.env`.
4. `uv run sherlock diff --state CA` → expect `sessions_matched >= 1` (if 0, read `warnings` — session heuristic needs the real CA session's `start_year`; adjust `match_sessions` only with evidence), plus anomaly counts.
5. `uv run sherlock patrol --state CA` → expect a markdown patrol report citing tool results; verify `runs/patrol-1.jsonl` exists and `sqlite3 data/casefile.db "SELECT id, scope, finished_at FROM patrols"` shows the finished patrol.
6. Commit nothing from this step (runtime artifacts are gitignored).

---

## Plan Self-Review (completed at write time)

- **Spec coverage (M0 slice):** SDK loop ✓ (Task 10), read-only tools ✓ (9), CA existence diff ✓ (6, 8), console report ✓ (10, 11), replica schema resolved ✓ (Global Constraints + Task 5), budget guard ✓ (4), bounded outputs ✓ (9), transcripts + patrol rows ✓ (7, 10). Slack/heartbeat/other detectors/other states — M1 by spec §15, intentionally absent.
- **Placeholder scan:** no TBDs; the two "contingency" notes are version-drift checks with exact commands, not deferred work.
- **Type consistency:** `build_toolkit` returns `(server, TOOL_NAMES)` in production mode and `(server, handlers)` under `return_handlers=True` — both call sites (Tasks 9/10 tests, patrol.py) match; `Anomaly` field names match store columns; `SessionRow`/`BillRow` usage in Tasks 6/8 matches Task 5 definitions.
