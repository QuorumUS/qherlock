# Querlock → actacollecta Graduation (`legiscan_crosscheck` check) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new deterministic `datachecks` check, `legiscan_crosscheck`, to the actacollecta monorepo that uses LegiScan as an independent oracle to flag Quorum bills with missing field data (`incomplete_fields`) or a stale/behind status (`stale`/`wrong_data`) — the signal actacollecta's source-direct checks don't compute.

**Architecture:** A new package `datachecks/src/datachecks/legiscan_crosscheck/` following the deterministic-check template (`bill_text/`, `bill_integrity/`) plus the `bill_gaps` S3 ledger. Pure functional core (`schema.py`, `compare.py`, `report.py`) unit-tested with no I/O; imperative shell (`select.py` = Quorum via `proddb`, `legiscan.py` = LegiScan via httpx, `ledger.py` = S3, `cli.py` = orchestration). It compares only the Quorum∩LegiScan intersection (bills present on both sides, matched by normalized number within the resolved session). No `missing_bill` (bill_gaps owns it); no `claude -p` judge (the comparison is arithmetic).

**Tech Stack:** Python ≥3.14, `uv` workspace, `typer`, `pydantic>2`, `psycopg[binary]`, `boto3`, `httpx` (new), `pytest` (+ `pytest-asyncio` if the LegiScan client is async). Reads prod `quorum_db` read-only via `datachecks.proddb`.

**Design source:** `docs/superpowers/specs/2026-07-23-querlock-actacollecta-graduation-design.md` (in the Querlock repo). Detector logic ported from Querlock `querlock/diff/detectors.py`; Quorum count sources from Querlock `querlock/quorum/reader.py` (table names already verified against quorum-site).

## Global Constraints

- **All paths are in `~/Projects/actacollecta`** unless prefixed `qherlock:` (the Querlock source repo, read-only reference).
- **Python ≥3.14**; the check is a subpackage of the existing `datachecks` workspace member — no new workspace member, no Python-floor change.
- **Functional core / imperative shell**: `schema.py`/`compare.py`/`report.py` must have zero I/O and be unit-testable without a DB, network, or S3. All I/O lives in `select.py`/`legiscan.py`/`ledger.py`/`cli.py`.
- **Deterministic check**: NO `judge.py`, NO `system-prompt.md`/`user.md`, NO `evals/`, NO `anthropic` dependency, NO `claude -p`. Add only `httpx>=0.27` to `datachecks/pyproject.toml`.
- **Read-only DB**: every DB read goes through `datachecks.proddb.connect()` (sets `conn.read_only = True`); env var `QUENTIN_QUORUM_DB_URI`.
- **Region resolution**: import `datachecks.bill_status.reference.regions.resolve_region_id` — never duplicate the state→region-int map.
- **Scope**: only `incomplete_fields`, `stale`, `wrong_data`, over the Quorum∩LegiScan intersection. Never emit `missing_bill`.
- **Re-alert suppression**: the S3 per-state ledger, `s3://<bucket>/datachecks/legiscan_crosscheck/ledger/<state>.json`, keyed `(state, session_id, bill_id, field)`.
- **Secrets**: `LEGISCAN_API_KEY` in `.env.quentin` (local) and `data-scraping-secret` (prod), read in-process by `legiscan.py`.
- **Tooling gate**: `ruff` + `pyright (standard)` + `pre-commit` must pass; tests run with `uv run pytest datachecks/tests/legiscan_crosscheck/`.
- **Verified Quorum count sources** (from `qherlock:querlock/quorum/reader.py`, verified against quorum-site models): actions `bill_billaction.bill_id`; texts `bill_billtext.bill_id`; sponsors `bill_sponsor.bill_id` (the `bill_bill_sponsors` M2M is deprecated — do not use it); votes `vote_vote.related_bill_id` (**not** `bill_id`).

---

### Task 1: Confirm prod count sources + record the LegiScan fetch-tier decision

Resolves the spec's two open implementation items before any dependent code. Produces a committed reference note; no application code yet.

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/__init__.py` (empty)
- Create: `datachecks/src/datachecks/legiscan_crosscheck/reference/prod-schema-notes.md`

**Interfaces:**
- Produces: the confirmed table/column names later tasks bind into SQL, and the chosen fetch tier (A dataset-cache / B per-state) that Task 4 implements.

- [ ] **Step 1: Verify the four count sources exist in prod `quorum_db`**

Run (read-only; requires `QUENTIN_QUORUM_DB_URI` for prod, use the `prod-aidata` SSO profile per `datachecks/OPS.md`):

```bash
psql "$QUENTIN_QUORUM_DB_URI" -c "\
SELECT 'actions' src, count(*) FROM bill_billaction LIMIT 1;" \
  -c "SELECT 'texts', count(*) FROM bill_billtext LIMIT 1;" \
  -c "SELECT 'sponsors', count(*) FROM bill_sponsor LIMIT 1;" \
  -c "SELECT 'votes', count(*) FROM vote_vote LIMIT 1;" \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='vote_vote' AND column_name='related_bill_id';"
```

Expected: all four tables resolve; `vote_vote.related_bill_id` column exists. (Querlock verified these on the replica; this confirms prod parity.)

- [ ] **Step 2: Decide whether to use `major_actions` JSONB or `bill_billaction` for the action count**

Run:
```bash
psql "$QUENTIN_QUORUM_DB_URI" -c "\
SELECT jsonb_typeof(major_actions), count(*) FROM bill_bill
WHERE major_actions IS NOT NULL GROUP BY 1 LIMIT 5;"
```
Expected: `array` dominates. **Decision rule:** if `major_actions` is a reliably-populated array (it is, per `bill_status`/`bill_gaps`), use `jsonb_array_length(major_actions)` for the action count (no join, matches datachecks convention); otherwise fall back to `COUNT(*) bill_billaction`. Record the choice in the note.

- [ ] **Step 3: Record the fetch-tier decision (Victor + Nei)**

Write `reference/prod-schema-notes.md` capturing: the four confirmed count sources + the action-count choice from Step 2, and the LegiScan tier — **A** (free 30k/mo → dataset ZIPs cached in S3 via a weekly sync job; needed because per-bill `getBill` across 50 states/day exceeds the free budget) or **B** (paid tier → per-state full-session pull at run time). Note: field COUNTS (`n_sponsors` etc.) require full per-bill data (dataset JSON or `getBill`), not `getMasterList` (which lacks them) — so the tier choice determines Task 4's transport.

- [ ] **Step 4: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/__init__.py \
        datachecks/src/datachecks/legiscan_crosscheck/reference/prod-schema-notes.md
git commit -m "docs(legiscan_crosscheck): confirm prod count sources + fetch-tier decision"
```

---

### Task 2: Package dependency + schema DTOs

**Files:**
- Modify: `datachecks/pyproject.toml` (add `httpx`)
- Create: `datachecks/src/datachecks/legiscan_crosscheck/schema.py`
- Test: `datachecks/tests/legiscan_crosscheck/__init__.py`, `datachecks/tests/legiscan_crosscheck/test_schema.py`

**Interfaces:**
- Produces:
  - `QuorumBill(bill_id:int, number_norm:str, label:str|None, current_general_status:int|None, current_status_date:date|None, most_recent_action_date:date|None, action_count:int, text_count:int, sponsor_count:int, vote_count:int)`
  - `LegiscanBill(bill_id:int, number:str, status:int, last_action_date:date|None, n_sponsors:int, n_actions:int, n_texts:int, n_votes:int)`
  - `CrosscheckFinding(bill_id:int, field:str, gap_type:str, legiscan_value:str, quorum_value:str, severity:str, evidence:dict)`
  - `StateCrosscheckReport(state:str, session_id:int, session_title:str|None, checked_count:int, findings:list[CrosscheckFinding], new_findings:list[CrosscheckFinding], known_open:list[CrosscheckFinding], resolved:list[CrosscheckFinding])` with computed `finding_count`, `new_count`.

- [ ] **Step 1: Add the httpx dependency**

In `datachecks/pyproject.toml`, add `"httpx>=0.27"` to the `dependencies` array (leave the rest unchanged). Then:
```bash
cd ~/Projects/actacollecta && uv sync --package datachecks
```
Expected: resolves, installs httpx.

- [ ] **Step 2: Write the failing schema test**

Create `datachecks/tests/legiscan_crosscheck/__init__.py` (empty) and `test_schema.py`:

```python
from datetime import date
from datachecks.legiscan_crosscheck.schema import (
    QuorumBill, LegiscanBill, CrosscheckFinding, StateCrosscheckReport,
)


def test_dtos_validate_and_counts_compute():
    q = QuorumBill(bill_id=10, number_norm="AB1", label="AB 1",
                   current_general_status=1, current_status_date=date(2026, 7, 1),
                   most_recent_action_date=date(2026, 7, 16),
                   action_count=1, text_count=1, sponsor_count=0, vote_count=0)
    ls = LegiscanBill(bill_id=1, number="AB1", status=2,
                      last_action_date=date(2026, 7, 20),
                      n_sponsors=2, n_actions=3, n_texts=1, n_votes=0)
    f = CrosscheckFinding(bill_id=10, field="sponsors", gap_type="incomplete_fields",
                          legiscan_value="2", quorum_value="0", severity="P3", evidence={})
    rep = StateCrosscheckReport(state="ca", session_id=2172, session_title="2025-2026",
                                checked_count=5, findings=[f], new_findings=[f],
                                known_open=[], resolved=[])
    assert q.number_norm == "AB1" and ls.n_sponsors == 2
    assert rep.finding_count == 1 and rep.new_count == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: datachecks.legiscan_crosscheck.schema`

- [ ] **Step 4: Write `schema.py`**

```python
"""Pydantic DTOs for the LegiScan cross-check (functional core; no I/O)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, computed_field


class QuorumBill(BaseModel):
    bill_id: int
    number_norm: str
    label: str | None = None
    current_general_status: int | None = None
    current_status_date: date | None = None
    most_recent_action_date: date | None = None
    action_count: int = 0
    text_count: int = 0
    sponsor_count: int = 0
    vote_count: int = 0


class LegiscanBill(BaseModel):
    bill_id: int
    number: str
    status: int = 0
    last_action_date: date | None = None
    n_sponsors: int = 0
    n_actions: int = 0
    n_texts: int = 0
    n_votes: int = 0


class CrosscheckFinding(BaseModel):
    bill_id: int
    field: str            # sponsors|actions|texts|votes|most_recent_action_date|status
    gap_type: str         # incomplete_fields|stale|wrong_data
    legiscan_value: str
    quorum_value: str
    severity: str
    evidence: dict = {}


class StateCrosscheckReport(BaseModel):
    state: str
    session_id: int
    session_title: str | None = None
    checked_count: int
    findings: list[CrosscheckFinding] = []
    new_findings: list[CrosscheckFinding] = []
    known_open: list[CrosscheckFinding] = []
    resolved: list[CrosscheckFinding] = []

    @computed_field
    @property
    def finding_count(self) -> int:
        return len(self.findings)

    @computed_field
    @property
    def new_count(self) -> int:
        return len(self.new_findings)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_schema.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add datachecks/pyproject.toml uv.lock \
        datachecks/src/datachecks/legiscan_crosscheck/schema.py \
        datachecks/tests/legiscan_crosscheck/
git commit -m "feat(legiscan_crosscheck): schema DTOs + httpx dep"
```

---

### Task 3: `compare.py` — the ported detectors (pure core)

The highest-value task: ports `qherlock:querlock/diff/detectors.py` verbatim in logic, minus `missing_bill`, emitting `CrosscheckFinding` instead of Querlock's `Anomaly`, and consuming the `QuorumBill`/`LegiscanBill` DTOs.

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/compare.py`
- Test: `datachecks/tests/legiscan_crosscheck/test_compare.py`

**Interfaces:**
- Consumes: `QuorumBill`, `LegiscanBill`, `CrosscheckFinding` (Task 2).
- Produces:
  - `compute_severity(gap_type:str, field:str, *, days_since_ls_activity:int|None, lag_days:int|None=None) -> str`
  - `crosscheck_bill(q:QuorumBill, ls:LegiscanBill, *, sla_hours:int, today:date) -> list[CrosscheckFinding]`
  - `crosscheck_bills(quorum:list[QuorumBill], legiscan:dict[str,LegiscanBill], *, sla_hours:int, today:date) -> list[CrosscheckFinding]` (joins on `number_norm` intersection)
  - module constants `GENERAL_STATUS_RANK`, `LEGISCAN_MIN_RANK`, `RESOLUTION_PREFIXES`.

- [ ] **Step 1: Write the failing compare tests (ported from Querlock, minus missing_bill)**

Create `test_compare.py`:

```python
from datetime import date

import pytest

from datachecks.legiscan_crosscheck.compare import (
    GENERAL_STATUS_RANK, LEGISCAN_MIN_RANK, compute_severity,
    crosscheck_bill, crosscheck_bills,
)
from datachecks.legiscan_crosscheck.schema import LegiscanBill, QuorumBill

TODAY = date(2026, 7, 21)


def _q(status=1, mrad="2026-07-20", **kw):
    base = dict(bill_id=10, number_norm="AB1", label="AB 1",
                current_general_status=status, current_status_date=date(2026, 7, 1),
                most_recent_action_date=date.fromisoformat(mrad) if mrad else None,
                action_count=1, text_count=1, sponsor_count=1, vote_count=0)
    base.update(kw)
    return QuorumBill(**base)


def _ls(status=1, last="2026-07-20", number="AB1", **counts):
    d = dict(bill_id=1, number=number, status=status,
             last_action_date=date.fromisoformat(last) if last else None,
             n_sponsors=1, n_actions=1, n_texts=1, n_votes=0)
    d.update({f"n_{k}": v for k, v in counts.items()})
    return LegiscanBill(**d)


def _run(ls, q, sla=72):
    return crosscheck_bill(q, ls, sla_hours=sla, today=TODAY)


def test_incomplete_fields_one_finding_per_zero_field():
    out = _run(_ls(sponsors=2, votes=1), _q(sponsor_count=0, vote_count=0))
    got = {(f.gap_type, f.field) for f in out}
    assert ("incomplete_fields", "sponsors") in got
    assert ("incomplete_fields", "votes") in got
    assert ("incomplete_fields", "actions") not in got


def test_incomplete_never_fires_when_legiscan_zero_or_quorum_nonzero():
    assert _run(_ls(votes=0), _q()) == []
    assert _run(_ls(votes=2), _q(vote_count=1)) == []


def test_stale_beyond_sla_grace():
    out = _run(_ls(last="2026-07-20"), _q(mrad="2026-07-16"))  # 4-day lag > 72h
    assert [f.gap_type for f in out] == ["stale"]
    assert out[0].field == "most_recent_action_date"
    assert out[0].evidence["lag_days"] == 4


def test_stale_grace_and_quorum_ahead():
    assert _run(_ls(last="2026-07-20"), _q(mrad="2026-07-17")) == []  # exactly 72h grace
    assert _run(_ls(last="2026-07-10"), _q(mrad="2026-07-20")) == []  # Quorum fresher


def test_no_date_detectors_when_quorum_mrad_null():
    out = _run(_ls(last="2026-07-20"), _q(mrad=None, sponsor_count=1, text_count=1, vote_count=0))
    assert {f.gap_type for f in out} == {"incomplete_fields"} or out == []


def test_wrong_data_quorum_rank_below_minimum():
    out = _run(_ls(status=2), _q(status=1))  # Engrossed needs >= passed_first (3)
    assert [f.gap_type for f in out] == ["wrong_data"]
    assert (out[0].legiscan_value, out[0].quorum_value) == ("2", "1")


@pytest.mark.parametrize("ls_status,q_status", [
    (2, 3), (4, 6), (4, 7), (1, 9), (2, 8), (2, None), (6, 1), (2, 12),
])
def test_wrong_data_never_fires(ls_status, q_status):
    assert _run(_ls(status=ls_status), _q(status=q_status)) == []


def test_precedence_stale_wins_over_wrong_data():
    out = _run(_ls(status=4, last="2026-07-20"), _q(status=1, mrad="2026-07-01"))
    assert [f.gap_type for f in out] == ["stale"]


def test_resolution_passed_at_adopted_rank_not_flagged():
    out = _run(_ls(status=4, number="AJR143", last="2025-05-20"), _q(status=4))
    assert [f for f in out if f.gap_type == "wrong_data"] == []


def test_regular_bill_passed_but_introduced_flagged():
    out = _run(_ls(status=4, number="SB10"), _q(status=1))
    assert any(f.gap_type == "wrong_data" for f in out)


def test_crosscheck_bills_joins_on_intersection_only():
    q = [_q(), QuorumBill(bill_id=11, number_norm="AB2", action_count=1, text_count=1,
                          sponsor_count=1, vote_count=1, current_general_status=1,
                          most_recent_action_date=date(2026, 7, 20))]
    ls = {"AB1": _ls(sponsors=2, votes=1), "AB99": _ls()}  # AB99 not in Quorum -> skipped
    out = crosscheck_bills(q, ls, sla_hours=72, today=TODAY)
    assert all(f.bill_id == 10 for f in out)  # only the AB1 match produced findings


def test_status_maps_have_no_non_us_ids():
    assert all(k < 100 for k in GENERAL_STATUS_RANK)
    assert 6 not in LEGISCAN_MIN_RANK  # LS Failed deliberately unmapped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_compare.py -v`
Expected: FAIL — `ModuleNotFoundError: datachecks.legiscan_crosscheck.compare`

- [ ] **Step 3: Write `compare.py`**

Port of `qherlock:querlock/diff/detectors.py`: same rank maps, precedence, and resolution handling; `missing_bill` removed; emits `CrosscheckFinding`; number normalization inlined (drop the `matchers` dependency — the intersection join already normalizes).

```python
"""Pure per-bill LegiScan↔Quorum comparison (functional core; no I/O).

Ported from Querlock querlock/diff/detectors.py, minus missing_bill.
Precedence: at most one date finding per bill; stale wins over wrong_data.
LegiScan is a recall oracle only — when Quorum is ahead nothing is flagged.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .schema import CrosscheckFinding, LegiscanBill, QuorumBill

INCOMPLETE_FIELDS = ("sponsors", "actions", "texts", "votes")

# Quorum GeneralBillStatus id -> ordinal progress rank (quorum-site bill/status.py).
GENERAL_STATUS_RANK: dict[int, int] = {
    1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 6, 9: 99,
}
# LegiScan status code -> MINIMUM expected Quorum rank. 6 (Failed) unmapped by design.
LEGISCAN_MIN_RANK: dict[int, int] = {1: 1, 2: 3, 3: 4, 4: 6, 5: 5}
RESOLUTION_PREFIXES: frozenset[str] = frozenset({
    "HR", "SR", "AR", "JR", "HJR", "SJR", "AJR", "HCR", "SCR", "ACR", "SJRCA", "HJRCA",
})
_RESOLUTION_MIN_RANK: dict[int, int] = {**LEGISCAN_MIN_RANK, 4: 4}
_PREFIX_RE = re.compile(r"^([A-Z]+)\d+$")


def normalize_number(raw: str | None) -> str:
    if not raw:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()
    return re.sub(r"([A-Z]+)0*(\d)", r"\1\2", s)


def compute_severity(gap_type: str, field: str, *,
                     days_since_ls_activity: int | None,
                     lag_days: int | None = None) -> str:
    recent = days_since_ls_activity is not None and days_since_ls_activity <= 30
    if gap_type == "stale":
        return "P2" if recent or (lag_days or 0) > 30 else "P3"
    if gap_type == "wrong_data":
        return "P2" if recent else "P3"
    if gap_type == "incomplete_fields":
        return "P3" if field in ("sponsors", "actions") else "P4"
    return "P4"


def crosscheck_bill(q: QuorumBill, ls: LegiscanBill, *,
                    sla_hours: int, today: date) -> list[CrosscheckFinding]:
    out: list[CrosscheckFinding] = []
    ls_last = ls.last_action_date
    days_since = (today - ls_last).days if ls_last else None
    q_counts = {"sponsors": q.sponsor_count, "actions": q.action_count,
                "texts": q.text_count, "votes": q.vote_count}
    ls_counts = {"sponsors": ls.n_sponsors, "actions": ls.n_actions,
                 "texts": ls.n_texts, "votes": ls.n_votes}

    for field in INCOMPLETE_FIELDS:
        if ls_counts[field] >= 1 and q_counts[field] == 0:
            out.append(CrosscheckFinding(
                bill_id=q.bill_id, field=field, gap_type="incomplete_fields",
                legiscan_value=str(ls_counts[field]), quorum_value="0",
                severity=compute_severity("incomplete_fields", field,
                                          days_since_ls_activity=days_since),
                evidence={"legiscan_bill_id": ls.bill_id,
                          "ls_last_action_date": ls_last.isoformat() if ls_last else None},
            ))

    q_mrad = q.most_recent_action_date
    if ls_last is None or q_mrad is None:
        return out

    lag = ls_last - q_mrad
    if lag > timedelta(hours=sla_hours):
        out.append(CrosscheckFinding(
            bill_id=q.bill_id, field="most_recent_action_date", gap_type="stale",
            legiscan_value=ls_last.isoformat(), quorum_value=q_mrad.isoformat(),
            severity=compute_severity("stale", "most_recent_action_date",
                                      days_since_ls_activity=days_since, lag_days=lag.days),
            evidence={"lag_days": lag.days, "legiscan_bill_id": ls.bill_id,
                      "quorum_general_status": q.current_general_status},
        ))
        return out  # precedence: stale wins over wrong_data

    q_rank = GENERAL_STATUS_RANK.get(q.current_general_status or 0)
    pm = _PREFIX_RE.match(normalize_number(ls.number))
    is_resolution = bool(pm) and pm.group(1) in RESOLUTION_PREFIXES
    rank_map = _RESOLUTION_MIN_RANK if is_resolution else LEGISCAN_MIN_RANK
    min_rank = rank_map.get(ls.status)
    if q_rank is not None and min_rank is not None and q_rank < min_rank:
        out.append(CrosscheckFinding(
            bill_id=q.bill_id, field="status", gap_type="wrong_data",
            legiscan_value=str(ls.status), quorum_value=str(q.current_general_status),
            severity=compute_severity("wrong_data", "status", days_since_ls_activity=days_since),
            evidence={"legiscan_status_code": ls.status,
                      "quorum_general_status": q.current_general_status,
                      "ls_last_action_date": ls_last.isoformat(),
                      "q_most_recent_action_date": q_mrad.isoformat(),
                      "legiscan_bill_id": ls.bill_id},
        ))
    return out


def crosscheck_bills(quorum: list[QuorumBill], legiscan: dict[str, LegiscanBill], *,
                     sla_hours: int, today: date) -> list[CrosscheckFinding]:
    out: list[CrosscheckFinding] = []
    for q in quorum:
        ls = legiscan.get(q.number_norm)
        if ls is not None:  # intersection only; missing-from-Quorum is bill_gaps' job
            out.extend(crosscheck_bill(q, ls, sla_hours=sla_hours, today=today))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_compare.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/compare.py \
        datachecks/tests/legiscan_crosscheck/test_compare.py
git commit -m "feat(legiscan_crosscheck): port pure detectors as compare.py (no missing_bill)"
```

---

### Task 4: `legiscan.py` — LegiScan client (parser pure-tested, transport injectable)

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/legiscan.py`
- Test: `datachecks/tests/legiscan_crosscheck/test_legiscan.py`

**Interfaces:**
- Consumes: `LegiscanBill` (Task 2).
- Produces:
  - `parse_bill(raw: dict) -> LegiscanBill` (pure — folds a LegiScan bill JSON object into the DTO; counts = `len(sponsors)/len(history)/len(texts)/len(votes)`)
  - `require(name: str) -> str` (env accessor; raises on missing)
  - `fetch_legiscan_bills(state: str, session_ref, *, fetch=...) -> dict[str, LegiscanBill]` — keyed by normalized number; `fetch` is an injectable callable returning raw bill dicts (the tier-specific transport from Task 1's decision).

- [ ] **Step 1: Write the failing parser test**

```python
from datetime import date

from datachecks.legiscan_crosscheck.legiscan import parse_bill, fetch_legiscan_bills


RAW = {"bill_id": 55, "number": "AB 1", "status": 2, "last_action_date": "2026-07-20",
       "sponsors": [{"people_id": 1}, {"people_id": 2}],
       "history": [{"date": "2026-07-01"}, {"date": "2026-07-20"}],
       "texts": [{"doc_id": 9}], "votes": []}


def test_parse_bill_folds_counts():
    b = parse_bill(RAW)
    assert b.bill_id == 55 and b.number == "AB 1" and b.status == 2
    assert b.last_action_date == date(2026, 7, 20)
    assert (b.n_sponsors, b.n_actions, b.n_texts, b.n_votes) == (2, 2, 1, 0)


def test_fetch_keys_by_normalized_number():
    bills = fetch_legiscan_bills("ca", object(), fetch=lambda state, ref: [RAW])
    assert "AB1" in bills and bills["AB1"].n_sponsors == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_legiscan.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `legiscan.py`**

```python
"""LegiScan client. parse_bill is pure/tested; the transport is injected so the
free-tier (dataset ZIP cache) vs paid-tier (per-session getBill) choice from
Task 1 lives in one swappable callable. Counts come from full per-bill JSON."""
from __future__ import annotations

import os
from datetime import date
from typing import Callable

import httpx

from .compare import normalize_number
from .schema import LegiscanBill

_BASE = "https://api.legiscan.com/"


def require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} is not set")
    return val


def _as_date(v) -> date | None:
    if not v:
        return None
    return date.fromisoformat(str(v)[:10])


def parse_bill(raw: dict) -> LegiscanBill:
    return LegiscanBill(
        bill_id=raw["bill_id"], number=raw.get("number", ""),
        status=int(raw.get("status") or 0),
        last_action_date=_as_date(raw.get("last_action_date")),
        n_sponsors=len(raw.get("sponsors") or []),
        n_actions=len(raw.get("history") or []),
        n_texts=len(raw.get("texts") or []),
        n_votes=len(raw.get("votes") or []),
    )


def _fetch_paid(state: str, session_ref) -> list[dict]:
    """Option B transport: getMasterList then getBill per bill (needs paid tier)."""
    key = require("LEGISCAN_API_KEY")
    with httpx.Client(base_url=_BASE, timeout=30) as c:
        ml = c.get("", params={"key": key, "op": "getMasterList",
                               "id": session_ref.legiscan_session_id}).json()
        ids = [v["bill_id"] for k, v in ml.get("masterlist", {}).items() if k != "session"]
        out = []
        for bid in ids:
            bill = c.get("", params={"key": key, "op": "getBill", "id": bid}).json()
            out.append(bill["bill"])
        return out


def fetch_legiscan_bills(
    state: str, session_ref, *, fetch: Callable[[str, object], list[dict]] = _fetch_paid,
) -> dict[str, LegiscanBill]:
    bills: dict[str, LegiscanBill] = {}
    for raw in fetch(state, session_ref):
        b = parse_bill(raw)
        bills[normalize_number(b.number)] = b
    return bills
```

> **Tier note (Task 1 decision):** if Option A (free tier) was chosen, add `_fetch_dataset(state, session_ref)` that reads the S3-cached dataset JSON (populated by a separate weekly sync job that ports `qherlock:querlock/legiscan/sync.py`) and pass it as `fetch=`. `parse_bill` is unchanged. The weekly sync job is a follow-on task, tracked in `prod-schema-notes.md`, not built here.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_legiscan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/legiscan.py \
        datachecks/tests/legiscan_crosscheck/test_legiscan.py
git commit -m "feat(legiscan_crosscheck): LegiScan client (pure parser + injectable transport)"
```

---

### Task 5: `select.py` — Quorum fetch via `proddb`

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/select.py`
- Test: `datachecks/tests/legiscan_crosscheck/test_select.py`

**Interfaces:**
- Consumes: `QuorumBill` (Task 2); `datachecks.proddb`; `datachecks.bill_status.reference.regions.resolve_region_id`; `datachecks.bill_gaps.select.resolve_session` (reuse — do not reimplement).
- Produces:
  - `rows_to_bills(bill_rows, count_rows) -> list[QuorumBill]` (pure — folds flat DB rows into DTOs; the unit-tested core)
  - `fetch_quorum_bills(state: str, session_id: int) -> list[QuorumBill]` (shell — opens `proddb.connect()`)

- [ ] **Step 1: Write the failing test for the pure fold**

```python
from datetime import date
from datachecks.legiscan_crosscheck.select import rows_to_bills


def test_rows_to_bills_folds_counts_defaulting_zero():
    bill_rows = [
        {"id": 10, "number": "AB 1", "label": "AB 1", "current_general_status": 1,
         "current_status_date": date(2026, 7, 1), "most_recent_action_date": date(2026, 7, 16)},
        {"id": 11, "number": "AB 2", "label": "AB 2", "current_general_status": 4,
         "current_status_date": date(2026, 7, 2), "most_recent_action_date": None},
    ]
    counts = {10: {"actions": 3, "texts": 1, "sponsors": 2, "votes": 0}}  # 11 absent -> all 0
    bills = {b.bill_id: b for b in rows_to_bills(bill_rows, counts)}
    assert bills[10].number_norm == "AB1" and bills[10].action_count == 3
    assert bills[11].sponsor_count == 0 and bills[11].most_recent_action_date is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_select.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `select.py`**

Uses the count sources confirmed in Task 1. Action count via `jsonb_array_length(major_actions)` if Task 1 chose JSONB, else `bill_billaction`; here the JSONB form is shown (swap to the join if Task 1 decided so).

```python
"""Quorum-side fetch for the LegiScan cross-check (imperative shell + pure fold)."""
from __future__ import annotations

from datachecks import proddb
from datachecks.bill_gaps.select import resolve_session  # reuse session resolution
from datachecks.bill_status.reference.regions import resolve_region_id

from .compare import normalize_number
from .schema import QuorumBill

_BILLS_SQL = """
SELECT b.id, b.number, b.label, b.current_general_status,
       b.current_status_date, b.most_recent_action_date,
       COALESCE(jsonb_array_length(
           CASE WHEN jsonb_typeof(b.major_actions)='array' THEN b.major_actions ELSE '[]'::jsonb END), 0)
         AS action_count
FROM bill_bill b
WHERE b.session_id = %(session_id)s AND b.number IS NOT NULL
"""
# Sponsor/text/vote counts: separate aggregates (verified FK columns, Task 1).
_SPONSORS_SQL = """SELECT s.bill_id, count(*) FROM bill_sponsor s
  JOIN bill_bill b ON b.id = s.bill_id WHERE b.session_id = %(session_id)s GROUP BY s.bill_id"""
_TEXTS_SQL = """SELECT t.bill_id, count(*) FROM bill_billtext t
  JOIN bill_bill b ON b.id = t.bill_id WHERE b.session_id = %(session_id)s GROUP BY t.bill_id"""
_VOTES_SQL = """SELECT v.related_bill_id, count(*) FROM vote_vote v
  JOIN bill_bill b ON b.id = v.related_bill_id WHERE b.session_id = %(session_id)s GROUP BY v.related_bill_id"""


def rows_to_bills(bill_rows, count_rows: dict[int, dict]) -> list[QuorumBill]:
    out = []
    for r in bill_rows:
        c = count_rows.get(r["id"], {})
        out.append(QuorumBill(
            bill_id=r["id"], number_norm=normalize_number(r["number"]), label=r.get("label"),
            current_general_status=r.get("current_general_status"),
            current_status_date=r.get("current_status_date"),
            most_recent_action_date=r.get("most_recent_action_date"),
            action_count=r.get("action_count", 0) or 0,
            text_count=c.get("texts", 0), sponsor_count=c.get("sponsors", 0),
            vote_count=c.get("votes", 0),
        ))
    return out


def fetch_quorum_bills(state: str, session_id: int) -> list[QuorumBill]:
    resolve_region_id(state)  # validate the state code
    params = {"session_id": session_id}
    with proddb.connect() as conn, conn.cursor() as cur:
        from psycopg.rows import dict_row
        cur.row_factory = dict_row
        cur.execute(_BILLS_SQL, params)
        bill_rows = cur.fetchall()
        counts: dict[int, dict] = {}
        for field, sql in (("sponsors", _SPONSORS_SQL), ("texts", _TEXTS_SQL), ("votes", _VOTES_SQL)):
            cur2 = conn.cursor()
            cur2.execute(sql, params)
            for bill_id, n in cur2.fetchall():
                counts.setdefault(bill_id, {})[field] = n
    return rows_to_bills(bill_rows, counts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_select.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/select.py \
        datachecks/tests/legiscan_crosscheck/test_select.py
git commit -m "feat(legiscan_crosscheck): Quorum select via proddb (verified count sources)"
```

---

### Task 6: `report.py` — report builder (pure core)

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/report.py`
- Test: `datachecks/tests/legiscan_crosscheck/test_report.py`

**Interfaces:**
- Consumes: `CrosscheckFinding`, `StateCrosscheckReport` (Task 2).
- Produces: `build_crosscheck_report(state, session_id, session_title, checked_count, findings, *, new_findings=None, known_open=None, resolved=None) -> StateCrosscheckReport`.

- [ ] **Step 1: Write the failing test**

```python
from datachecks.legiscan_crosscheck.report import build_crosscheck_report
from datachecks.legiscan_crosscheck.schema import CrosscheckFinding


def _f(bill_id, field):
    return CrosscheckFinding(bill_id=bill_id, field=field, gap_type="incomplete_fields",
                             legiscan_value="1", quorum_value="0", severity="P3", evidence={})


def test_build_report_rolls_up_counts():
    all_f = [_f(1, "sponsors"), _f(2, "votes")]
    rep = build_crosscheck_report("ca", 2172, "2025-2026", checked_count=10,
                                  findings=all_f, new_findings=[all_f[0]])
    assert rep.finding_count == 2 and rep.new_count == 1
    assert rep.state == "ca" and rep.checked_count == 10
    assert rep.model_dump_json()  # serializes for --output / --report-s3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_report.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `report.py`**

```python
"""Pure aggregation of findings into one per-state report."""
from __future__ import annotations

from .schema import CrosscheckFinding, StateCrosscheckReport


def build_crosscheck_report(
    state: str, session_id: int, session_title: str | None, checked_count: int,
    findings: list[CrosscheckFinding], *,
    new_findings: list[CrosscheckFinding] | None = None,
    known_open: list[CrosscheckFinding] | None = None,
    resolved: list[CrosscheckFinding] | None = None,
) -> StateCrosscheckReport:
    return StateCrosscheckReport(
        state=state, session_id=session_id, session_title=session_title,
        checked_count=checked_count, findings=findings,
        new_findings=new_findings or [], known_open=known_open or [],
        resolved=resolved or [],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/report.py \
        datachecks/tests/legiscan_crosscheck/test_report.py
git commit -m "feat(legiscan_crosscheck): per-state report builder"
```

---

### Task 7: `ledger.py` — S3 open-findings ledger (copy `bill_gaps`, re-key per finding)

**Files:**
- Create: `datachecks/src/datachecks/legiscan_crosscheck/ledger.py` (copy `datachecks/src/datachecks/bill_gaps/ledger.py`, then edit)
- Test: `datachecks/tests/legiscan_crosscheck/test_ledger.py`

**Interfaces:**
- Consumes: `CrosscheckFinding` (Task 2).
- Produces (mirror `bill_gaps.ledger` names so the CLI wiring is copy-paste):
  - `DEFAULT_TTL_DAYS = 30`
  - `class LedgerEntry(BaseModel)` keyed `(state, session_id, bill_id, field)` + `first_seen`, `last_verified`
  - `load_ledger(location) -> list[LedgerEntry]`, `save_ledger(entries, location) -> None`, `write_text(location, text) -> None`
  - `open_entries(entries, state, session_id, today, ttl_days=30) -> list[LedgerEntry]`
  - `prune_resolved(entries, state, session_id, live_keys: set[tuple[int,str]]) -> tuple[list, list]`
  - `reconcile(entries, findings, state, session_id, today) -> list[LedgerEntry]`
  - `select_new_findings(entries, findings, state, session_id) -> list[CrosscheckFinding]`

- [ ] **Step 1: Copy the bill_gaps ledger as the starting point**

```bash
cp datachecks/src/datachecks/bill_gaps/ledger.py \
   datachecks/src/datachecks/legiscan_crosscheck/ledger.py
```

- [ ] **Step 2: Write the failing ledger test (the re-keyed semantics)**

```python
from datetime import date
from datachecks.legiscan_crosscheck.ledger import (
    LedgerEntry, open_entries, prune_resolved, reconcile, select_new_findings,
)
from datachecks.legiscan_crosscheck.schema import CrosscheckFinding

T = date(2026, 7, 21)


def _f(bill_id, field):
    return CrosscheckFinding(bill_id=bill_id, field=field, gap_type="incomplete_fields",
                             legiscan_value="1", quorum_value="0", severity="P3", evidence={})


def test_key_is_bill_id_plus_field():
    e = LedgerEntry(state="ca", session_id=2172, bill_id=10, field="sponsors",
                    first_seen=T, last_verified=T)
    assert e.key() == (10, "sponsors")


def test_select_new_findings_excludes_known():
    prior = [LedgerEntry(state="ca", session_id=2172, bill_id=10, field="sponsors",
                         first_seen=T, last_verified=T)]
    findings = [_f(10, "sponsors"), _f(11, "votes")]
    new = select_new_findings(prior, findings, "ca", 2172)
    assert [(f.bill_id, f.field) for f in new] == [(11, "votes")]


def test_prune_resolved_drops_healed():
    entries = [LedgerEntry(state="ca", session_id=2172, bill_id=10, field="sponsors",
                           first_seen=T, last_verified=T)]
    kept, resolved = prune_resolved(entries, "ca", 2172, live_keys=set())  # nothing still open
    assert kept == [] and len(resolved) == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_ledger.py -v`
Expected: FAIL — `LedgerEntry` still has the bill_gaps `(prefix, number)` shape.

- [ ] **Step 4: Edit the copied `ledger.py`**

Apply these edits to the copied file (keep the S3 `_is_s3_uri`/`_s3_get`/`_s3_put`/`write_text`/`load_ledger`/`save_ledger` helpers unchanged):

1. Replace the `LedgerEntry` fields `prefix: str; number: int` with `bill_id: int; field: str`, and add:
```python
    def key(self) -> tuple[int, str]:
        return (self.bill_id, self.field)
    def belongs_to(self, state: str, session_id: int) -> bool:
        return self.state == state and self.session_id == session_id
    def is_fresh(self, today, ttl_days) -> bool:
        return (today - self.last_verified).days < ttl_days
```
2. Rewrite the finding-facing helpers to key on `CrosscheckFinding.(bill_id, field)`:
```python
def open_entries(entries, state, session_id, today, ttl_days=DEFAULT_TTL_DAYS):
    return [e for e in entries if e.belongs_to(state, session_id) and e.is_fresh(today, ttl_days)]

def prune_resolved(entries, state, session_id, live_keys):
    kept, resolved = [], []
    for e in entries:
        if e.belongs_to(state, session_id) and e.key() not in live_keys:
            resolved.append(e)
        else:
            kept.append(e)
    return kept, resolved

def select_new_findings(entries, findings, state, session_id):
    known = {e.key() for e in entries if e.belongs_to(state, session_id)}
    return [f for f in findings if (f.bill_id, f.field) not in known]

def reconcile(entries, findings, state, session_id, today):
    by_key = {e.key(): e for e in entries if e.belongs_to(state, session_id)}
    others = [e for e in entries if not e.belongs_to(state, session_id)]
    for f in findings:
        k = (f.bill_id, f.field)
        if k in by_key:
            by_key[k].last_verified = today
        else:
            by_key[k] = LedgerEntry(state=state, session_id=session_id, bill_id=f.bill_id,
                                    field=f.field, first_seen=today, last_verified=today)
    return others + list(by_key.values())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_ledger.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add datachecks/src/datachecks/legiscan_crosscheck/ledger.py \
        datachecks/tests/legiscan_crosscheck/test_ledger.py
git commit -m "feat(legiscan_crosscheck): S3 ledger re-keyed to (bill_id, field)"
```

---

### Task 8: CLI subcommand + end-to-end wiring

**Files:**
- Modify: `datachecks/src/datachecks/cli.py` (add `legiscan-crosscheck` command + imports)
- Test: `datachecks/tests/legiscan_crosscheck/test_cli.py`

**Interfaces:**
- Consumes: everything above + `datachecks.bill_gaps.select.resolve_session`.
- Produces: `datachecks legiscan-crosscheck <state> [--session] [--ledger] [--ledger-ttl-days] [--output] [--report-s3]`.

- [ ] **Step 1: Write the failing CLI test (fakes injected via monkeypatch)**

```python
from datetime import date
from typer.testing import CliRunner
from datachecks.cli import app
from datachecks.legiscan_crosscheck.schema import LegiscanBill, QuorumBill

runner = CliRunner()


def test_legiscan_crosscheck_reports_new_finding(monkeypatch, tmp_path):
    import datachecks.cli as cli
    monkeypatch.setattr(cli, "resolve_session", lambda state, session_id=None:
                        type("S", (), {"id": 2172, "title": "2025-2026"})())
    monkeypatch.setattr(cli, "fetch_quorum_bills", lambda state, sid: [
        QuorumBill(bill_id=10, number_norm="AB1", current_general_status=1,
                   most_recent_action_date=date(2026, 7, 20),
                   action_count=1, text_count=1, sponsor_count=0, vote_count=0)])
    monkeypatch.setattr(cli, "fetch_legiscan_bills", lambda state, ref: {
        "AB1": LegiscanBill(bill_id=1, number="AB1", status=1,
                            last_action_date=date(2026, 7, 20),
                            n_sponsors=2, n_actions=1, n_texts=1, n_votes=0)})
    out = tmp_path / "r.json"
    result = runner.invoke(app, ["legiscan-crosscheck", "ca", "--output", str(out)])
    assert result.exit_code == 0
    assert '"field":"sponsors"' in out.read_text().replace(" ", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_cli.py -v`
Expected: FAIL — no `legiscan-crosscheck` command.

- [ ] **Step 3: Add the command to `cli.py`**

Add imports near the other check imports at the top of `datachecks/src/datachecks/cli.py`:

```python
from datetime import date, timedelta
from .bill_gaps.select import resolve_session
from .legiscan_crosscheck.select import fetch_quorum_bills
from .legiscan_crosscheck.legiscan import fetch_legiscan_bills
from .legiscan_crosscheck.compare import crosscheck_bills
from .legiscan_crosscheck.report import build_crosscheck_report
from .legiscan_crosscheck import ledger as lc_ledger
```

Add the command (mirrors the `bill-gaps` ledger order):

```python
@app.command("legiscan-crosscheck")
def legiscan_crosscheck(
    state: str = typer.Argument(..., help="State abbreviation, e.g. 'ca'"),
    session: int | None = typer.Option(None, "--session", help="Quorum LegSession id"),
    sla_hours: int = typer.Option(72, "--sla-hours"),
    ledger: str | None = typer.Option(None, "--ledger", help="Open-findings ledger path or s3://…"),
    ledger_ttl_days: int = typer.Option(lc_ledger.DEFAULT_TTL_DAYS, "--ledger-ttl-days"),
    output: Path | None = typer.Option(None, "--output"),
    report_s3: str | None = typer.Option(None, "--report-s3"),
) -> None:
    """Cross-check Quorum bills against LegiScan for one state's current session."""
    today = date.today()
    sess = resolve_session(state, session)
    quorum = fetch_quorum_bills(state, sess.id)
    legiscan = fetch_legiscan_bills(state, sess)
    findings = crosscheck_bills(quorum, legiscan, sla_hours=sla_hours, today=today)

    entries = lc_ledger.load_ledger(ledger) if ledger else []
    live_keys = {(f.bill_id, f.field) for f in findings}
    entries, resolved = lc_ledger.prune_resolved(entries, state, sess.id, live_keys)
    known = lc_ledger.open_entries(entries, state, sess.id, today, ledger_ttl_days)
    new = lc_ledger.select_new_findings(entries, findings, state, sess.id)
    report = build_crosscheck_report(
        state, sess.id, sess.title, checked_count=len(quorum), findings=findings,
        new_findings=new,
        known_open=[_entry_to_finding(e) for e in known],
        resolved=[_entry_to_finding(e) for e in resolved],
    )
    if ledger:
        lc_ledger.save_ledger(lc_ledger.reconcile(entries, findings, state, sess.id, today), ledger)
    payload = report.model_dump_json(indent=2)
    if output:
        output.write_text(payload)
    if report_s3:
        lc_ledger.write_text(report_s3, payload)
    console.print(payload)
```

Add this module-level helper in `cli.py` (with `from .legiscan_crosscheck.schema import CrosscheckFinding` in the imports):

```python
def _entry_to_finding(e) -> CrosscheckFinding:
    """A ledger entry carries only the (bill_id, field) key — render it as a
    finding stub so the sweep can roll up known-open and resolved counts."""
    return CrosscheckFinding(bill_id=e.bill_id, field=e.field, gap_type="ledger",
                             legiscan_value="", quorum_value="", severity="", evidence={})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest datachecks/tests/legiscan_crosscheck/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Run the full check test suite + linters**

Run:
```bash
uv run pytest datachecks/tests/legiscan_crosscheck/ -v
uv run ruff check datachecks/src/datachecks/legiscan_crosscheck/
uv run pyright datachecks/src/datachecks/legiscan_crosscheck/
```
Expected: all tests PASS; ruff + pyright clean.

- [ ] **Step 6: Commit**

```bash
git add datachecks/src/datachecks/cli.py datachecks/tests/legiscan_crosscheck/test_cli.py
git commit -m "feat(legiscan_crosscheck): CLI subcommand wiring the full pipeline"
```

---

### Task 9: Quentin handoff — skill, Helm schedule, secrets doc

Wires the check into the detect→report→autoheal flow. Config/docs; no unit tests, but each artifact is validated.

**Files:**
- Create: `.claude/skills/legiscan-crosscheck-sweep/SKILL.md`
- Modify: `helm/actacollecta-agent/values.yaml` (add a `legiscanCrosscheck` sweep block mirroring `billGapsSweep`)
- Modify: `.env.quentin.example` (or the documented secrets list) + note the `data-scraping-secret` addition

**Interfaces:**
- Consumes: the `datachecks legiscan-crosscheck` CLI (Task 8); `quentinbot` thread-posting (`quasar slack post` / `quentinbot.threads.create_tracked_thread`).

- [ ] **Step 1: Author the sweep skill (thin imperative shell)**

Create `.claude/skills/legiscan-crosscheck-sweep/SKILL.md` modeled on `.claude/skills/bill-gaps-sweep/SKILL.md`: (1) run `uv run datachecks legiscan-crosscheck <state> --ledger s3://actacollecta-agent/datachecks/legiscan_crosscheck/ledger/<state>.json --report-s3 s3://…/reports/<state>/<run_ts>.json`; (2) post only `new_findings` as tracked threads via `quasar slack post`; (3) reply "resolved" into the original thread for each `resolved` entry.

- [ ] **Step 2: Add the Helm schedule block**

In `helm/actacollecta-agent/values.yaml`, add a `legiscanCrosscheck` block mirroring `billGapsSweep` (per-state CronJobs enqueuing `/legiscan-crosscheck-sweep <state>` via `poll-generic` on the shared queue). Reuse `templates/cronjob-bill-gaps-sweep.yaml` as the template (parameterize the skill name) — do not add a new SQS queue or worker.

- [ ] **Step 3: Document the secret**

Add `LEGISCAN_API_KEY` to `.env.quentin.example` alongside `ZYTE_API_KEY`/`NY_OPENLEG_API_KEY`/`NEVADA_LIS_API_KEY`, and note in `datachecks/OPS.md` that the prod value goes in the `data-scraping-secret` AWS Secrets Manager entry.

- [ ] **Step 4: Validate the config**

Run:
```bash
python -c "import yaml,sys; yaml.safe_load(open('helm/actacollecta-agent/values.yaml'))" && echo "values.yaml OK"
test -f .claude/skills/legiscan-crosscheck-sweep/SKILL.md && echo "skill present"
```
Expected: both print OK.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/legiscan-crosscheck-sweep/SKILL.md \
        helm/actacollecta-agent/values.yaml .env.quentin.example datachecks/OPS.md
git commit -m "feat(legiscan_crosscheck): sweep skill, Helm schedule, secret doc"
```

---

## Self-Review

**Spec coverage** (against `2026-07-23-querlock-actacollecta-graduation-design.md`):
- §1 scope (incomplete_fields/stale/wrong_data, drop missing_bill, intersection-only) → Task 3 (`crosscheck_bills` joins intersection; no missing_bill).
- §2 file shape (schema/compare/select/legiscan/report/ledger + CLI, no judge/evals) → Tasks 2–8.
- §3 migration map (detectors→compare, reader→select, casefiles→ledger, slack→threads, launchd→Helm) → Tasks 3, 5, 7, 9.
- §4 infra seams (ledger, quentinbot.threads, Helm, autoheal re-scrape) → Tasks 7, 9.
- §5 LegiScan fetch tier → Task 1 (decision) + Task 4 (injectable transport, both tiers).
- §6 unknowns (sponsor/vote tables, session mapping) → Task 1 (count sources) + Task 5/Task 8 (`resolve_session` reuse).
- §7 readiness → no code; carried by the spec.

**Placeholder scan:** the only deferred item is the Option-A weekly S3 dataset-sync job, explicitly gated on Task 1's tier decision and marked as a tracked follow-on (not a silent TODO); Task 4 ships a complete Option-B transport so the check runs regardless.

**Type consistency:** `QuorumBill`/`LegiscanBill`/`CrosscheckFinding`/`StateCrosscheckReport` field names are identical across Tasks 2→8; `normalize_number` defined once in `compare.py` and imported by `legiscan.py`/`select.py`; ledger helper names (`open_entries`/`prune_resolved`/`reconcile`/`select_new_findings`/`load_ledger`/`save_ledger`/`write_text`) match between Task 7 and the Task 8 CLI body.

---

## Notes for the executor (Nei)

- This plan lives in the **Querlock** repo as a handoff artifact; execute it **inside `~/Projects/actacollecta`** on a feature branch. Nothing here modifies the standalone Querlock repo.
- Task 1 is a gate: its two decisions (action-count source, LegiScan tier) parameterize Tasks 4–5. Do it first.
- The standalone Querlock keeps running as the interim auditor until this check is live and accepted; retire it only afterward (spec §8).
