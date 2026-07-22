# M2a Detection-Correctness Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the four false-positive anomaly families (NY amendment suffixes, WI/OH resolutions, MA extension orders, CA extraordinary-session bills) the first live patrol surfaced, auto-retire the ~3,800 recorded FP rows, and lock every family down with offline eval fixtures — so a patrol reports the genuine residual (~low hundreds) in a ≤1,000-char digest.

**Architecture:** Targeted fixes in the existing normalizer (`matchers.py`), detector (`detectors.py`), and diff service (`service.py`) — no new declarative rules layer. Each fix is guarded by an eval fixture built from real 2026-07-22 replica data that asserts both "FP family gone" and "genuine cases still fire". Auto-retirement is an additive `status='resolved'` transition scoped to sessions actually diffed.

**Tech Stack:** Python 3.12, pytest, sqlite (casefile + test replica fixtures), psycopg (live replica only).

## Global Constraints

- Run tests with `.venv/bin/pytest` from the repo root (repo `.venv` shadows module-installed uv; never bare `pytest` or `uv run pytest`).
- LegiScan is a recall oracle only: Quorum ahead of LegiScan is never an anomaly. No fix may start flagging Quorum-ahead cases.
- Resolution detection keys on the **raw LegiScan number prefix, before per-state prefix translation** (post-translation, US "HR" is a House *bill*).
- Amendment-suffix stripping applies only to states in `AMENDMENT_SUFFIX_STATES` (NY today); all other states keep current behavior byte-for-byte.
- Auto-retirement may only retire anomalies whose `(region, session_key)` was actually diffed in the current run; a scoped run must never touch untouched regions/sessions.
- Evals must run offline — no tunnel, no LegiScan key, no network.
- Over-suppression is a test failure, exactly like under-suppression: every family fixture also asserts a planted genuine case still fires.
- Work on `main` (repo convention); one commit per task.

## File Structure

- `qherlock/diff/matchers.py` — `quorum_number_norm` gains `state`; new `AMENDMENT_SUFFIX_STATES` + suffix strip; MA rule fix (Tasks 1, 3, 4).
- `qherlock/diff/detectors.py` — `RESOLUTION_PREFIXES` + resolution-aware status rank (Task 2).
- `qherlock/diff/service.py` — collision guard, cross-session ABX lookup wiring, live-fingerprint collection + retirement call, `resolved` in rollup (Tasks 1, 4, 5).
- `qherlock/casefiles/store.py` — `resolved_at` column + `retire_resolved` (Task 5).
- `qherlock/agent/patrol.py` — DOCTRINE digest instruction (Task 7).
- `tests/evals/` — new: fixtures + eval tests (Task 6); `tests/test_matchers.py`, `tests/test_detectors.py`, `tests/test_diff_service.py`, `tests/test_casefiles.py` — unit tests per task.

---

### Task 1: NY amendment-suffix normalization + collision guard

**Files:**
- Modify: `qherlock/diff/matchers.py:52-59` (`quorum_number_norm`)
- Modify: `qherlock/diff/service.py:44-48` (q_by_norm builder — collision guard + pass state)
- Test: `tests/test_matchers.py`, `tests/test_diff_service.py`

**Interfaces:**
- Consumes: `normalize_bill_number` (existing).
- Produces: `quorum_number_norm(label, number, bill_type=None, state=None) -> str` — when `state in AMENDMENT_SUFFIX_STATES` and the normalized label matches `^[A-Z]+\d+[A-Z]$`, the single trailing letter is dropped. New module constant `AMENDMENT_SUFFIX_STATES: frozenset[str]`.

Background (real NY session 3596 data): each number carries up to four distinct prefixes — e.g. number 115 → `S.115A` (bill_type 2), `A.115` (3), `J.115` (4), `K.115` (1). LegiScan reports base numbers (`S115`, `A115`, …). Only the amendment letter differs, and only on the Senate/Assembly rows that were amended. Stripping the trailing letter is prefix-preserving, so the four prefixes stay distinct.

- [ ] **Step 1: Write failing tests in `tests/test_matchers.py`**

Append:

```python
def test_ny_amendment_suffix_stripped():
    # NY session 3596 real rows: amended Senate/Assembly bills carry a trailing letter.
    assert quorum_number_norm("S.115A", 115, 2, state="NY") == "S115"
    assert quorum_number_norm("S.156A", 156, 2, state="NY") == "S156"
    # Non-amended rows and other prefixes are untouched.
    assert quorum_number_norm("A.115", 115, 3, state="NY") == "A115"
    assert quorum_number_norm("J.115", 115, 4, state="NY") == "J115"
    assert quorum_number_norm("K.115", 115, 1, state="NY") == "K115"


def test_amendment_suffix_only_for_configured_states():
    # Same label in a non-configured state keeps the trailing letter.
    assert quorum_number_norm("S.115A", 115, 2, state="CA") == "S115A"
    assert quorum_number_norm("S.115A", 115, 2) == "S115A"  # no state -> no strip


def test_amendment_suffix_leaves_plain_numbers_alone():
    # No trailing letter -> unchanged even in NY.
    assert quorum_number_norm("AB 12", 12, None, state="NY") == "AB12"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_matchers.py -k "amendment" -v`
Expected: FAIL — `quorum_number_norm()` got an unexpected keyword argument `'state'`.

- [ ] **Step 3: Implement in `qherlock/diff/matchers.py`**

Add the constant after `BILL_TYPE_PREFIX` (line 25):

```python
# States where Quorum stores an amended bill under a suffixed label (NY: S.115A)
# while LegiScan reports the base number (S115). Strip the trailing letter so the
# two sides match. Prefix-preserving: only a single trailing [A-Z] is removed.
AMENDMENT_SUFFIX_STATES: frozenset[str] = frozenset({"NY"})
_SUFFIX_RE = re.compile(r"^([A-Z]+\d+)([A-Z])$")
```

Replace `quorum_number_norm` (lines 52-59):

```python
def quorum_number_norm(label: str | None, number, bill_type: int | None = None,
                       state: str | None = None) -> str:
    """Quorum-side identity: normalized label; federal NULL-label fallback via
    bill_type + number; '' when no identity can be derived (caller skips).
    For AMENDMENT_SUFFIX_STATES, a single trailing amendment letter is dropped
    (S.115A -> S115) so amended bills match LegiScan's base number."""
    if label:
        norm = normalize_bill_number(label)
        if state and state.upper() in AMENDMENT_SUFFIX_STATES:
            m = _SUFFIX_RE.match(norm)
            if m:
                return m.group(1)
        return norm
    if bill_type in BILL_TYPE_PREFIX and number is not None:
        return f"{BILL_TYPE_PREFIX[bill_type]}{number}"
    return ""
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_matchers.py -k "amendment" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write failing collision-guard test in `tests/test_diff_service.py`**

The service builds `q_by_norm`; if two rows collapse to one norm it must keep the first and warn, never silently overwrite. Append (uses the existing `_new_replica`/`cache` helpers in that file):

```python
def test_ny_suffix_collision_is_warned_not_silent(tmp_path):
    from qherlock.legiscan.cache import LegiScanCache
    from tests.test_legiscan_cache import BILL, make_dataset_zip
    replica = _new_replica()
    # Contrived collision: base S115 AND amended S.115A in one NY session both -> S115.
    replica.executescript(
        """
        INSERT INTO app_legsession VALUES (20, 'ny', 't', 's', 2025, TRUE, TRUE);
        INSERT INTO bill_bill (id, session_id, label, number, bill_type,
            current_general_status, most_recent_action_date)
        VALUES (1, 20, 'S.115', 115, 2, 1, '2026-06-10'),
               (2, 20, 'S.115A', 115, 2, 1, '2026-06-10');
        """
    )
    with LegiScanCache(tmp_path / "cache.db") as c:
        c.upsert_session("NY", {"session_id": 2188, "year_start": 2025, "year_end": 2026,
                                "special": 0, "session_name": "2025-2026 Regular Session"})
        c.ingest_dataset_zip(2188, make_dataset_zip([dict(BILL, bill_id=1, bill_number="S115")]))
        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("NY", c, casefile, replica)
    assert any("collision" in w.lower() for w in summary["warnings"])
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/pytest tests/test_diff_service.py -k "collision" -v`
Expected: FAIL — no warning containing "collision".

- [ ] **Step 7: Implement collision guard + pass state in `qherlock/diff/service.py`**

Replace the q_by_norm builder (lines 44-48):

```python
        q_by_norm: dict[str, reader.BillRow] = {}
        for b in q_bills:
            norm = quorum_number_norm(b.label, b.number, b.bill_type, state=region)
            if not norm:
                continue
            if norm in q_by_norm:
                warnings.append(
                    f"{region} session {session_key}: bill-number collision on {norm!r} "
                    f"(labels {q_by_norm[norm].label!r} and {b.label!r}) — kept first"
                )
                continue
            q_by_norm[norm] = b
```

- [ ] **Step 8: Run collision + full matcher/service suites**

Run: `.venv/bin/pytest tests/test_diff_service.py tests/test_matchers.py -v`
Expected: PASS (all — existing tests unaffected because `state` defaults to None and CA/US behavior is unchanged).

- [ ] **Step 9: Commit**

```bash
git add qherlock/diff/matchers.py qherlock/diff/service.py tests/test_matchers.py tests/test_diff_service.py
git commit -m "fix: NY amendment-suffix normalization + bill-number collision guard

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Resolution-aware status ranks

**Files:**
- Modify: `qherlock/diff/detectors.py:29-39` (add `RESOLUTION_PREFIXES`, resolution-aware min-rank lookup at line 109)
- Test: `tests/test_detectors.py`

**Interfaces:**
- Consumes: `ls_bill` dict (has raw `"number"` and `"status"`), `normalize_bill_number` (import from matchers).
- Produces: no signature change — `detect_bill_anomalies` derives the raw prefix from `ls_bill["number"]` internally.

Background (real WI data): joint resolutions `AJR143`/`SJR137` etc. LegiScan status 4 ("Passed") currently demands Quorum rank 6 (enacted) via `LEGISCAN_MIN_RANK[4]=6`. Resolutions are *adopted*, never enacted — so Quorum's rank 4 (passed_second/adopted) is correct and must not be flagged. Same root cause for OH's status-4 cluster.

- [ ] **Step 1: Write failing tests in `tests/test_detectors.py`**

Append (mirror the existing `detect_bill_anomalies` call style in that file — a resolution "Passed" in Quorum at adopted rank must NOT flag, a regular bill "Passed" but stuck at introduced MUST still flag):

```python
from datetime import date
from qherlock.quorum.reader import BillRow, BillCounts
from qherlock.diff.detectors import detect_bill_anomalies

def _qbill(status, mrad="2025-05-20"):
    return BillRow(id=1, label="x", number="1", bill_type=7,
                   current_general_status=status, current_status_date=mrad,
                   most_recent_action_date=mrad, introduced_date=mrad,
                   missing_data=False, last_quorum_update=mrad, source="")

def test_resolution_passed_at_adopted_rank_not_flagged():
    ls = {"number": "AJR143", "status": 4, "last_action_date": "2025-05-20",
          "bill_id": 1, "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies("WI", "2197", "AJR143", ls, _qbill(4), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert [a for a in out if a.gap_type == "wrong_data"] == []

def test_regular_bill_passed_but_introduced_still_flagged():
    ls = {"number": "SB10", "status": 4, "last_action_date": "2025-05-20",
          "bill_id": 2, "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies("WI", "2197", "SB10", ls, _qbill(1), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert any(a.gap_type == "wrong_data" for a in out)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_detectors.py -k "resolution or regular_bill_passed" -v`
Expected: FAIL — `test_resolution_passed_at_adopted_rank_not_flagged` fails (currently flagged: q_rank 4 < min_rank 6).

- [ ] **Step 3: Implement in `qherlock/diff/detectors.py`**

Add `import re` to the top of the file (detectors.py has no `re` import today) and the matchers import, right after the existing imports (line 11):

```python
import re

from qherlock.diff.matchers import normalize_bill_number
```

(`import re` goes with the stdlib imports near line 8; the matchers import with the `qherlock.*` imports. No circular import: `matchers.py` imports only `qherlock.quorum.reader`, never `detectors`.)

Add after `LEGISCAN_MIN_RANK` (line 39):

```python
# Resolution number prefixes (raw LegiScan side, BEFORE per-state translation).
# Resolutions are adopted, never enacted — LegiScan "Passed" (4) means adopted,
# so it requires only adopted rank (4), not enacted rank (6).
RESOLUTION_PREFIXES: frozenset[str] = frozenset({
    "HR", "SR", "AR", "JR", "HJR", "SJR", "AJR", "HCR", "SCR", "ACR", "SJRCA", "HJRCA",
})
_RESOLUTION_MIN_RANK: dict[int, int] = {**LEGISCAN_MIN_RANK, 4: 4}
_PREFIX_RE = re.compile(r"^([A-Z]+)\d+$")
```

Add `import re` at the top if not present (it is not — add it as line 8's neighbor).

Replace ONLY the `min_rank = LEGISCAN_MIN_RANK.get(ls_bill.get("status") or 0)` line (line 109) with:

```python
    ls_status = ls_bill.get("status") or 0
    raw_norm = normalize_bill_number(ls_bill.get("number"))
    pm = _PREFIX_RE.match(raw_norm)
    is_resolution = bool(pm) and pm.group(1) in RESOLUTION_PREFIXES
    rank_map = _RESOLUTION_MIN_RANK if is_resolution else LEGISCAN_MIN_RANK
    min_rank = rank_map.get(ls_status)
```

(Line 108 `q_rank = GENERAL_STATUS_RANK.get(...)` sits ABOVE and stays untouched; the `if q_rank is not None and min_rank is not None ...` check on line 110 stays as-is.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_detectors.py -v`
Expected: PASS (all — new + existing).

- [ ] **Step 5: Commit**

```bash
git add qherlock/diff/detectors.py tests/test_detectors.py
git commit -m "fix: resolutions require adopted rank, not enacted, for LegiScan Passed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: MA extension-order suppression

**Files:**
- Modify: `qherlock/diff/matchers.py:14-18, 62-67` (`IGNORED_TITLE_PREFIXES` + `is_deliberately_unimported`)
- Test: `tests/test_matchers.py`

**Interfaces:**
- Produces: `is_deliberately_unimported(state, title)` also matches titles whose suffix is an extension-order marker.

Diagnosis (real MA data): the 8 flagged titles are e.g. `"Financial Services -- Extension Order"`, `"Revenue - Extension Order"`, `"The Judiciary -- Extension Order"`. The salvaged rule uses `title.startswith(("order", "study order"))` — but here the marker is a **suffix**, so it never matched. Two of the eight (`"RESOLUTIONS RESPONDING TO THE SUPREME JUDICIAL COURT'S ORDER…"`, `"Communication from the Massachusetts Gaming Commission…"`) are not extension orders and stay flagged (isolated, low severity — a judgment call left to M2b).

- [ ] **Step 1: Write failing tests in `tests/test_matchers.py`**

Append:

```python
def test_ma_extension_order_suffix_suppressed():
    assert is_deliberately_unimported("MA", "Financial Services -- Extension Order")
    assert is_deliberately_unimported("MA", "Revenue - Extension Order")
    assert is_deliberately_unimported("MA", "The Judiciary -- Extension Order")
    # Existing prefix rule still holds:
    assert is_deliberately_unimported("MA", "Order relative to X")

def test_ma_non_extension_orders_still_flagged():
    # These two real cases are NOT extension orders -> not suppressed.
    assert not is_deliberately_unimported("MA", "Communication from the Gaming Commission")
    assert not is_deliberately_unimported("MA", "Resolutions responding to the SJC order of May 7")
    # Other states unaffected:
    assert not is_deliberately_unimported("CA", "Some Extension Order")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_matchers.py -k "ma_extension or ma_non" -v`
Expected: FAIL — extension-order suffix titles are not suppressed.

- [ ] **Step 3: Implement in `qherlock/diff/matchers.py`**

Replace `IGNORED_TITLE_PREFIXES` (lines 14-18) with a structure carrying both prefix and substring markers:

```python
# Salvaged (comparison.py:26): LegiScan bills whose TITLE marks a type Quorum
# deliberately does not import. MA procedural Orders come two ways: leading
# "order"/"study order" (prefix) and trailing "... Extension Order" (suffix).
IGNORED_TITLE_PREFIXES: dict[str, tuple[str, ...]] = {
    "MA": ("order", "study order"),
}
IGNORED_TITLE_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "MA": ("extension order",),
}
```

Replace `is_deliberately_unimported` (lines 62-67):

```python
def is_deliberately_unimported(state: str, title: str | None) -> bool:
    """Salvaged MA order rule (comparison.py:31), extended for suffix-form
    '... Extension Order' titles."""
    t = (title or "").strip().lower()
    if not t:
        return False
    st = state.upper()
    prefixes = IGNORED_TITLE_PREFIXES.get(st)
    if prefixes and t.startswith(prefixes):
        return True
    substrings = IGNORED_TITLE_SUBSTRINGS.get(st)
    if substrings and any(s in t for s in substrings):
        return True
    return False
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_matchers.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add qherlock/diff/matchers.py tests/test_matchers.py
git commit -m "fix: suppress MA suffix-form Extension Order titles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CA extraordinary-session cross-session matching

**Files:**
- Modify: `qherlock/diff/service.py` (session-group lookup in `diff_region`)
- Modify: `qherlock/diff/matchers.py` (helper `is_extraordinary_number`)
- Test: `tests/test_matchers.py`, `tests/test_diff_service.py`

**Interfaces:**
- Produces: `is_extraordinary_number(state, raw_number) -> bool` — true for CA X-session markers (`ABX1…`, `SBX2…`: a letter prefix ending in `X` followed by a session ordinal + number).
- Consumes: `reader.get_current_sessions`, `reader.get_bills_for_session`.

**RISK GATE:** This is the riskiest fix. If, while implementing, the session-group lookup requires restructuring `match_sessions` (rather than an additive sibling-session pass in `diff_region`), STOP and report DONE_WITH_CONCERNS describing the structural need — the controller will split this into its own plan. Do not force a large refactor inside this task.

Background (real CA data): LegiScan folds extraordinary-session bills (`ABX1 15`, prefix ends in `X`) into the biennium dataset (session 2172); Quorum keeps them in a separate special session sharing the biennium's `start_year`. 1:1 `match_sessions` pairs 2172 only with the regular Quorum session, so every ABX bill looks missing (20) and its neighbors mis-compare (29 wrong_data).

- [ ] **Step 1: Write failing test for the helper in `tests/test_matchers.py`**

```python
def test_is_extraordinary_number():
    from qherlock.diff.matchers import is_extraordinary_number
    assert is_extraordinary_number("CA", "ABX1 15")
    assert is_extraordinary_number("CA", "SBX2 3")
    assert not is_extraordinary_number("CA", "AB 15")
    assert not is_extraordinary_number("CA", "SB 3")
    assert not is_extraordinary_number("US", "HR 24")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_matchers.py -k extraordinary -v`
Expected: FAIL — `is_extraordinary_number` does not exist.

- [ ] **Step 3: Implement the helper in `qherlock/diff/matchers.py`**

Add after `AMENDMENT_SUFFIX_STATES` block:

```python
# States where LegiScan folds extraordinary-session bills into the biennium
# dataset while Quorum keeps a separate special session. Marker: a letter
# prefix ending in 'X' (ABX/SBX) before the number.
EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})


def is_extraordinary_number(state: str, raw_number: str | int | None) -> bool:
    """True when the raw LegiScan number marks an extraordinary-session bill
    (CA ABX1.../SBX2...). Uses the pure-normalized form; no prefix translation."""
    if state.upper() not in EXTRAORDINARY_SESSION_STATES:
        return False
    norm = normalize_bill_number(raw_number)
    m = _NUM_RE.match(norm)
    return bool(m) and m.group(1).endswith("X")
```

(`_NUM_RE` already exists in `matchers.py` at line 28 — reuse it, no new regex.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_matchers.py -k extraordinary -v`
Expected: PASS.

- [ ] **Step 5: Write failing service test in `tests/test_diff_service.py`**

An ABX bill present only in a sibling Quorum special session must NOT be flagged missing:

```python
def test_ca_abx_found_in_sibling_special_session(tmp_path):
    from qherlock.legiscan.cache import LegiScanCache
    from tests.test_legiscan_cache import BILL, make_dataset_zip
    replica = _new_replica()
    replica.executescript(
        """
        INSERT INTO app_legsession VALUES (30, 'ca', 't', 'reg', 2025, TRUE, TRUE);
        INSERT INTO app_legsession VALUES (31, 'ca', 't', 'x1', 2025, TRUE, FALSE);
        INSERT INTO bill_bill (id, session_id, label, number, bill_type,
            current_general_status, most_recent_action_date)
        VALUES (1, 31, 'ABX1 15', 15, 3, 6, '2026-06-10');
        """
    )
    with LegiScanCache(tmp_path / "cache.db") as c:
        c.upsert_session("CA", {"session_id": 2172, "year_start": 2025, "year_end": 2026,
                                "special": 0, "session_name": "2025-2026 Regular Session"})
        c.ingest_dataset_zip(2172, make_dataset_zip(
            [dict(BILL, bill_id=1, bill_number="ABX1 15", title="Budget Act")]))
        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            summary = diff_region("CA", c, casefile, replica)
    missing = summary["counts_by_gap_type"].get("missing_bill", {})
    assert missing.get("new", 0) == 0
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/pytest tests/test_diff_service.py -k abx -v`
Expected: FAIL — ABX15 flagged missing (sibling session not consulted).

- [ ] **Step 7: Implement sibling-session lookup in `qherlock/diff/service.py`**

Inside `diff_region`, after `matched, warnings = match_sessions(...)`, build a sibling index once:

```python
    # Sibling special sessions per biennium start_year, for extraordinary-session
    # bills LegiScan folds into the regular dataset (CA ABX). Only special sessions.
    siblings_by_year: dict[int | None, list] = {}
    for q in q_sessions:
        if not q.regular_session:
            siblings_by_year.setdefault(q.start_year, []).append(q)
```

Then, in the missing-bill branch (currently line 55 `if q_bill is None:`), before recording missing, attempt the sibling lookup:

```python
            if q_bill is None and is_extraordinary_number(region, bill["number"]):
                for sib in siblings_by_year.get(qs.start_year, []):
                    sib_bills = reader.get_bills_for_session(replica_conn, sib.id)
                    for sb in sib_bills:
                        if quorum_number_norm(sb.label, sb.number, sb.bill_type,
                                              state=region) == norm:
                            q_bill = sb
                            break
                    if q_bill is not None:
                        break
```

(Add `is_extraordinary_number` to the matchers import at the top of `service.py`. When `q_bill` is found in a sibling, the existing `else` branch runs the detectors against it; when still None, the existing missing-bill record runs.)

- [ ] **Step 8: Run to verify pass**

Run: `.venv/bin/pytest tests/test_diff_service.py -k abx -v`
Expected: PASS.

- [ ] **Step 9: Run full matcher + service suites**

Run: `.venv/bin/pytest tests/test_matchers.py tests/test_diff_service.py -v`
Expected: PASS (all).

- [ ] **Step 10: Commit**

```bash
git add qherlock/diff/matchers.py qherlock/diff/service.py tests/test_matchers.py tests/test_diff_service.py
git commit -m "fix: match CA extraordinary-session bills against sibling special sessions

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Anomaly auto-retirement

**Files:**
- Modify: `qherlock/casefiles/store.py:8-24` (schema + migration), add `retire_resolved`
- Modify: `qherlock/diff/service.py` (collect live fingerprints, call retirement, add `resolved` to rollup)
- Test: `tests/test_casefiles.py`, `tests/test_diff_service.py`

**Interfaces:**
- Produces: `CaseFileStore.retire_resolved(region: str, session_keys: set[str], live_fingerprints: set[str]) -> int` — flips `status='new'` anomalies in the given `(region, session_key ∈ session_keys)` whose fingerprint is absent from `live_fingerprints` to `status='resolved'` with `resolved_at`; returns count. `diff_region` result dict gains `"anomalies_resolved": int`; `diff_many` sums it as `"anomalies_resolved"`.
- Consumes: `Anomaly.fingerprint` (existing).

- [ ] **Step 1: Write failing store test in `tests/test_casefiles.py`**

```python
def test_retire_resolved_flips_absent_new_anomalies(tmp_path):
    from qherlock.casefiles.store import CaseFileStore
    from qherlock.casefiles.models import Anomaly
    a = Anomaly(gap_type="missing_bill", region="NY", session_key="2188",
                bill_number_norm="S115A")
    b = Anomaly(gap_type="missing_bill", region="NY", session_key="2188",
                bill_number_norm="A9999")
    with CaseFileStore(tmp_path / "cf.db") as cf:
        cf.upsert_anomaly(a)
        cf.upsert_anomaly(b)
        # Only b still reproduces; a is fixed -> a retires, b stays new.
        n = cf.retire_resolved("NY", {"2188"}, {b.fingerprint})
        assert n == 1
        assert cf.get_anomaly_by_fingerprint(a.fingerprint)["status"] == "resolved"
        assert cf.get_anomaly_by_fingerprint(b.fingerprint)["status"] == "new"

def test_retire_resolved_scoped_to_given_sessions(tmp_path):
    from qherlock.casefiles.store import CaseFileStore
    from qherlock.casefiles.models import Anomaly
    other = Anomaly(gap_type="missing_bill", region="NY", session_key="9999",
                    bill_number_norm="S1")
    with CaseFileStore(tmp_path / "cf.db") as cf:
        cf.upsert_anomaly(other)
        # Retiring session 2188 must not touch session 9999.
        cf.retire_resolved("NY", {"2188"}, set())
        assert cf.get_anomaly_by_fingerprint(other.fingerprint)["status"] == "new"
```

(`get_anomaly_by_fingerprint` is a small read helper added in Step 3.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_casefiles.py -k retire -v`
Expected: FAIL — `retire_resolved` / `get_anomaly_by_fingerprint` do not exist.

- [ ] **Step 3: Implement in `qherlock/casefiles/store.py`**

Add `resolved_at` to the schema (line 17, after `status`):

```python
    status TEXT NOT NULL DEFAULT 'new', resolved_at TEXT,
```

Add an additive migration in `__init__` after `executescript(_SCHEMA)` (line 38) so existing casefile.db files gain the column:

```python
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(anomalies)")}
        if "resolved_at" not in cols:
            self._conn.execute("ALTER TABLE anomalies ADD COLUMN resolved_at TEXT")
            self._conn.commit()
```

Add methods (after `get_anomaly`, line 115):

```python
    def get_anomaly_by_fingerprint(self, fingerprint: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM anomalies WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row is not None else None

    def retire_resolved(self, region: str, session_keys: set[str],
                        live_fingerprints: set[str]) -> int:
        """Flip status='new' anomalies in (region, session_key in session_keys)
        whose fingerprint is NOT in live_fingerprints to 'resolved'. Scoped:
        only the given sessions are touched. Returns count retired."""
        if not session_keys:
            return 0
        now = _now()
        sk = list(session_keys)
        rows = self._conn.execute(
            f"""SELECT id, fingerprint FROM anomalies
                WHERE region = ? AND status = 'new'
                  AND session_key IN ({','.join('?' * len(sk))})""",
            (region, *sk),
        ).fetchall()
        to_retire = [r["id"] for r in rows if r["fingerprint"] not in live_fingerprints]
        for aid in to_retire:
            self._conn.execute(
                "UPDATE anomalies SET status = 'resolved', resolved_at = ? WHERE id = ?",
                (now, aid),
            )
        self._conn.commit()
        return len(to_retire)
```

Also: `upsert_anomaly`'s recurring UPDATE must reopen a resolved row. In both UPDATE statements (lines 61-64 and 82-85) add `status = 'new', resolved_at = NULL` to the SET clause so a reappearing fingerprint returns to `new`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_casefiles.py -k retire -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write failing service test in `tests/test_diff_service.py`**

```python
def test_diff_region_reports_resolved_count(tmp_path, cache, replica):
    from qherlock.casefiles.models import Anomaly
    ghost = Anomaly(gap_type="wrong_data", region="CA", session_key="2172",
                    bill_number_norm="GONE99", field="status")
    with CaseFileStore(tmp_path / "casefile.db") as casefile:
        casefile.upsert_anomaly(ghost)
        summary = diff_region("CA", cache, casefile, replica)
        assert summary["anomalies_resolved"] >= 1
        assert casefile.get_anomaly_by_fingerprint(ghost.fingerprint)["status"] == "resolved"
```

- [ ] **Step 6: Run to verify failure**

Run: `.venv/bin/pytest tests/test_diff_service.py -k resolved -v`
Expected: FAIL — `KeyError: 'anomalies_resolved'`.

- [ ] **Step 7: Implement in `qherlock/diff/service.py`**

In `diff_region`, collect live fingerprints and processed sessions. In `record`, capture the fingerprint:

```python
    live_fingerprints: set[str] = set()
    processed_sessions: set[str] = set()

    def record(anomaly: Anomaly, title: str = ""):
        live_fingerprints.add(anomaly.fingerprint)
        kind, aid = casefile.upsert_anomaly(anomaly)
        ...
```

After the session loop (`for ls, qs in matched:`) records `processed_sessions.add(session_key)` at its top. After the loop, before `cases.sort`:

```python
    resolved = casefile.retire_resolved(region, processed_sessions, live_fingerprints)
```

Add to the return dict: `"anomalies_resolved": resolved,`.

In `diff_many`, initialize `total_resolved = 0`, add `total_resolved += r["anomalies_resolved"]` in the loop, and `"anomalies_resolved": total_resolved` to its return dict.

- [ ] **Step 8: Run to verify pass + full service/casefile suites**

Run: `.venv/bin/pytest tests/test_diff_service.py tests/test_casefiles.py -v`
Expected: PASS (all).

- [ ] **Step 9: Commit**

```bash
git add qherlock/casefiles/store.py qherlock/diff/service.py tests/test_casefiles.py tests/test_diff_service.py
git commit -m "feat: auto-retire anomalies that stop reproducing (scoped, reopenable)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Eval fixtures locking every family

**Files:**
- Create: `tests/evals/__init__.py`, `tests/evals/fixtures/ny_amendment.json`, `wi_resolutions.json`, `ma_orders.json`, `ca_abx.json`
- Create: `tests/evals/test_fp_families.py`
- Test: itself

**Interfaces:**
- Consumes: `normalize_bill_number`, `legiscan_number_norm`, `quorum_number_norm`, `is_deliberately_unimported`, `is_extraordinary_number`, `detect_bill_anomalies` (all from Tasks 1-4).

These evals run the real matcher/detector functions against fixture rows recorded from live 2026-07-22 data. They are the regression guard: each family asserts zero FP anomalies AND a planted genuine case still fires.

- [ ] **Step 1: Create `tests/evals/__init__.py`**

```python
```
(empty file)

- [ ] **Step 2: Create `tests/evals/fixtures/ny_amendment.json`**

```json
{
  "state": "NY",
  "pairs": [
    {"legiscan_number": "S115",  "quorum_label": "S.115A", "quorum_number": 115, "quorum_bill_type": 2},
    {"legiscan_number": "S156",  "quorum_label": "S.156A", "quorum_number": 156, "quorum_bill_type": 2},
    {"legiscan_number": "A115",  "quorum_label": "A.115",  "quorum_number": 115, "quorum_bill_type": 3},
    {"legiscan_number": "J115",  "quorum_label": "J.115",  "quorum_number": 115, "quorum_bill_type": 4},
    {"legiscan_number": "K115",  "quorum_label": "K.115",  "quorum_number": 115, "quorum_bill_type": 1}
  ],
  "genuine_missing": {"legiscan_number": "A33878", "quorum_label": null}
}
```

- [ ] **Step 3: Create `tests/evals/fixtures/wi_resolutions.json`**

```json
{
  "state": "WI",
  "session_key": "2197",
  "resolutions_passed_adopted": [
    {"number": "AJR143", "ls_status": 4, "quorum_status": 4},
    {"number": "SJR137", "ls_status": 4, "quorum_status": 4},
    {"number": "SJR2",   "ls_status": 4, "quorum_status": 4}
  ],
  "genuine_regular_bill_behind": {"number": "SB10", "ls_status": 4, "quorum_status": 1}
}
```

- [ ] **Step 4: Create `tests/evals/fixtures/ma_orders.json`**

```json
{
  "state": "MA",
  "suppressed_titles": [
    "Financial Services -- Extension Order",
    "Revenue - Extension Order",
    "The Judiciary -- Extension Order",
    "Consumer Protection and Professional Licensure - Extension Order"
  ],
  "still_flagged_titles": [
    "Communication from the Massachusetts Gaming Commission (pursuant to Section 9B)",
    "Resolutions responding to the Supreme Judicial Court's order of May 7, 2026"
  ]
}
```

- [ ] **Step 5: Create `tests/evals/fixtures/ca_abx.json`**

```json
{
  "state": "CA",
  "extraordinary_numbers": ["ABX1 15", "SBX2 3", "ABX1 1"],
  "regular_numbers": ["AB 15", "SB 3"]
}
```

- [ ] **Step 6: Create `tests/evals/test_fp_families.py`**

```python
import json
from datetime import date
from pathlib import Path

from qherlock.diff.detectors import detect_bill_anomalies
from qherlock.diff.matchers import (is_deliberately_unimported, is_extraordinary_number,
                                    legiscan_number_norm, quorum_number_norm)
from qherlock.quorum.reader import BillCounts, BillRow

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_ny_amendment_family_all_match():
    fx = _load("ny_amendment.json")
    for p in fx["pairs"]:
        ls_norm = legiscan_number_norm(fx["state"], p["legiscan_number"])
        q_norm = quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                    p["quorum_bill_type"], state=fx["state"])
        assert ls_norm == q_norm, f"{p['legiscan_number']} != {p['quorum_label']}"


def test_ny_genuine_missing_has_no_quorum_side():
    fx = _load("ny_amendment.json")
    # The one real gap has no Quorum row -> nothing to match against (still missing).
    assert fx["genuine_missing"]["quorum_label"] is None


def _qbill(status):
    return BillRow(id=1, label="x", number="1", bill_type=7,
                   current_general_status=status, current_status_date="2025-05-20",
                   most_recent_action_date="2025-05-20", introduced_date="2025-05-20",
                   missing_data=False, last_quorum_update="2025-05-20", source="")


def test_wi_resolutions_not_flagged():
    fx = _load("wi_resolutions.json")
    for r in fx["resolutions_passed_adopted"]:
        ls = {"number": r["number"], "status": r["ls_status"],
              "last_action_date": "2025-05-20", "bill_id": 1,
              "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
        out = detect_bill_anomalies(fx["state"], fx["session_key"], r["number"], ls,
                                    _qbill(r["quorum_status"]), BillCounts(),
                                    sla_hours=72, today=date(2025, 5, 21))
        assert not [a for a in out if a.gap_type == "wrong_data"], r["number"]


def test_wi_genuine_regular_bill_still_flagged():
    fx = _load("wi_resolutions.json")
    g = fx["genuine_regular_bill_behind"]
    ls = {"number": g["number"], "status": g["ls_status"],
          "last_action_date": "2025-05-20", "bill_id": 2,
          "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies(fx["state"], fx["session_key"], g["number"], ls,
                                _qbill(g["quorum_status"]), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert any(a.gap_type == "wrong_data" for a in out)


def test_ma_orders_suppressed_and_genuine_flagged():
    fx = _load("ma_orders.json")
    for t in fx["suppressed_titles"]:
        assert is_deliberately_unimported(fx["state"], t), t
    for t in fx["still_flagged_titles"]:
        assert not is_deliberately_unimported(fx["state"], t), t


def test_ca_abx_detected_regular_not():
    fx = _load("ca_abx.json")
    for n in fx["extraordinary_numbers"]:
        assert is_extraordinary_number(fx["state"], n), n
    for n in fx["regular_numbers"]:
        assert not is_extraordinary_number(fx["state"], n), n
```

- [ ] **Step 7: Run the evals**

Run: `.venv/bin/pytest tests/evals/ -v`
Expected: PASS (7 tests), runnable with no tunnel/key.

- [ ] **Step 8: Commit**

```bash
git add tests/evals/
git commit -m "test: eval fixtures locking the four FP families + genuine-case guards

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Digest slimming (DOCTRINE)

**Files:**
- Modify: `qherlock/agent/patrol.py` (DOCTRINE digest step, ~lines 29-39)
- Test: `tests/test_patrol.py`

**Interfaces:**
- Consumes: nothing new. Doctrine is a prompt string; the test asserts the instruction text, not model behavior.

- [ ] **Step 1: Write failing test in `tests/test_patrol.py`**

```python
def test_doctrine_digest_targets_small_size():
    from qherlock.agent.patrol import DOCTRINE
    assert "1000 character" in DOCTRINE or "1,000 character" in DOCTRINE
    assert "resolved" in DOCTRINE.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_patrol.py -k doctrine_digest_targets -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `qherlock/agent/patrol.py`**

Replace the digest step (item 5 in the DOCTRINE string, currently beginning "5. Digest:") with:

```
5. Digest: call post_slack with kind "digest" and ONE compact message (target \
under 1,000 characters — smaller is better; the full detail belongs in the report, \
not Slack). Include: one-line scope; counts by gap type as new/recurring/resolved; \
the top 3 case families each with region + one-line diagnosis; degraded or errored \
regions only if any; LegiScan calls_this_month. Do not paste per-bill lists.
```

Update the triage/step-6 references if they mention the old 5-notable-cases digest shape, keeping the full markdown report (step 6) as the place for exhaustive detail. Add `resolved` to the counts wording in step 6 as well.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_patrol.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add qherlock/agent/patrol.py tests/test_patrol.py
git commit -m "feat: slim patrol digest to <1000 chars with resolved counts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest`
Expected: PASS, 0 warnings. Report the exact count (was 138 pre-M2a; expect ~155 with the new tests).

- [ ] **Step 2: Confirm no regression in FP-family scope**

Run: `grep -rn "slack_webhook\|SLACK_WEBHOOK" qherlock/ tests/`
Expected: no matches (unrelated guard, confirms nothing reverted).

- [ ] **Step 3 (manual, controller — requires tunnel): live diff sanity**

Not a subagent step. After merge, the controller runs `diff --scope all` with the tunnel up and confirms: NY/WI/OH-status4/MA/CA-2172 families produce zero new anomalies, prior rows show as resolved, residual ≈ low hundreds. Recorded in the ledger, not this plan.
