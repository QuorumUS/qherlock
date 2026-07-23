# CA Extraordinary-Session Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop CA's 20 extraordinary-session (X1) bills from being reported as false `missing_bill` anomalies by matching them to Quorum's non-current sibling special session.

**Architecture:** LegiScan folds extraordinary-session bills into the *regular* session with fused numbers (`ABX11` = Assembly Bill, session X1, bill 1). Quorum stores them as base labels (`A.B.1`) in a separate special session that is `current=FALSE` and thus invisible to the patrol's `current=TRUE` reader. The fix adds a read-only reader for special sessions, two pure matcher primitives (parse the fused number, select the sibling session by biennium + X-ordinal), and wires them into the diff loop so X-marked LegiScan bills are looked up in the sibling session's bill map. Everything is state-gated to CA.

**Tech Stack:** Python 3.12+, pytest, sqlite (test replicas + LegiScan cache), psycopg (prod replica). No new dependencies.

## Global Constraints

- **All Quorum SQL lives in `querlock/quorum/reader.py`** (spec §7 of the design doc / reader module docstring). No SQL anywhere else.
- **`querlock/diff/detectors.py` and matcher functions stay pure** (no I/O) — golden-testable.
- **The whole mechanism is state-gated** via `EXTRAORDINARY_SESSION_STATES = frozenset({"CA"})`. No other region discovers siblings or parses fused numbers.
- **Anomaly `session_key` stays the LegiScan session id** (`"2172"` for CA). Sibling sessions are consulted only — never added to `processed_sessions`, never independently diffed, never retire their own rows.
- **No schema change / no migration** — retirement of the existing 20 FPs happens through the unchanged `retire_resolved` path.
- **Evals and unit tests must run offline** — no tunnel, no LegiScan key.
- **TDD, DRY, YAGNI, frequent commits.** Run tests with `.venv/bin/python -m pytest`. `uv` is not installed in this environment.

---

### Task 1: `get_special_sessions` reader

**Files:**
- Modify: `querlock/quorum/reader.py` (add SQL constant near `_BILLS_SQL` ~line 21, and a function near `get_current_sessions` ~line 110)
- Test: `tests/test_quorum_reader.py`

**Interfaces:**
- Consumes: existing `SessionRow`, `_execute`.
- Produces: `get_special_sessions(conn, region: str) -> list[SessionRow]` — special (non-regular) sessions for a region, **including `current=FALSE`**.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_quorum_reader.py`:

```python
def test_get_special_sessions_includes_non_current(replica_factory):
    conn = replica_factory()
    conn.executescript(
        """
        INSERT INTO app_legsession VALUES
            (3570, 'ca', '2025-2026', '2025-2026', 2025, TRUE, TRUE),
            (3736, 'ca', '2025 Spec Session 1 - X1', '2025 Spec Session 1 - X1', 2025, FALSE, FALSE),
            (3665, 'ca', '2024 Spec Session 1 - X1', '2024 Spec Session 1 - X1', 2024, FALSE, FALSE);
        """
    )
    specials = reader.get_special_sessions(conn, "CA")
    ids = {s.id for s in specials}
    assert ids == {3736, 3665}                       # both special sessions, regardless of current
    assert all(s.regular_session is False for s in specials)
    assert 3570 not in ids                            # the regular session is excluded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_quorum_reader.py::test_get_special_sessions_includes_non_current -v`
Expected: FAIL with `AttributeError: module 'querlock.quorum.reader' has no attribute 'get_special_sessions'`

- [ ] **Step 3: Write minimal implementation**

In `querlock/quorum/reader.py`, add the SQL constant after `_BILLS_SQL` (around line 28):

```python
_SPECIAL_SESSIONS_SQL = """
SELECT id, region_abbrev, title, session_name, start_year, current, regular_session
FROM app_legsession
WHERE LOWER(region_abbrev) = LOWER({ph}) AND regular_session = FALSE
"""
```

And add the function after `get_current_sessions` (around line 114):

```python
def get_special_sessions(conn, region: str) -> list[SessionRow]:
    """Extraordinary/special sessions for a region, regardless of `current`.
    A concluded special session is current=FALSE, but its bills are still the
    live copy LegiScan diffs against (see the CA X1 family). Read-only."""
    cur = _execute(conn, _SPECIAL_SESSIONS_SQL, (region,))
    return [SessionRow(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]))
            for r in cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_quorum_reader.py::test_get_special_sessions_includes_non_current -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add querlock/quorum/reader.py tests/test_quorum_reader.py
git commit -m "feat: reader.get_special_sessions (non-current special sessions)"
```

---

### Task 2: `parse_extraordinary_number` matcher

**Files:**
- Modify: `querlock/diff/matchers.py` (add near the other constants ~line 34 and a function after `legiscan_number_norm` ~line 59)
- Test: `tests/test_matchers.py`

**Interfaces:**
- Consumes: existing `normalize_bill_number`, `re` (already imported).
- Produces:
  - `EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})` (consumed by Task 4's service gating).
  - `parse_extraordinary_number(raw_number, ordinals) -> list[tuple[int, str]]` — every `(ordinal, base_norm)` the fused number could map to, given the ordinals present among the biennium's sibling sessions. Empty list when the number carries no recognized marker.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_matchers.py` (and add `parse_extraordinary_number` to the top-of-file import from `querlock.diff.matchers`):

```python
def test_parse_extraordinary_number_basic():
    # CA fuses the session marker into the number: ABX11 = Assembly Bill, X1, bill 1.
    assert parse_extraordinary_number("ABX11", {1}) == [(1, "AB1")]
    assert parse_extraordinary_number("ABX110", {1}) == [(1, "AB10")]
    assert parse_extraordinary_number("SBX14", {1}) == [(1, "SB4")]
    assert parse_extraordinary_number("ACAX11", {1}) == [(1, "ACA1")]


def test_parse_extraordinary_number_no_marker_or_no_ordinal():
    assert parse_extraordinary_number("AB1", {1}) == []      # plain number, no marker
    assert parse_extraordinary_number("ABX11", set()) == []  # no known ordinals
    assert parse_extraordinary_number(None, {1}) == []


def test_parse_extraordinary_number_ambiguous_returns_all_candidates():
    # ordinals {1, 11} both parse 'ABX110'; the caller disambiguates by which
    # base actually exists in that ordinal's session (Task 4).
    got = parse_extraordinary_number("ABX110", {1, 11})
    assert (1, "AB10") in got and (11, "AB0") in got
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_matchers.py::test_parse_extraordinary_number_basic -v`
Expected: FAIL with `ImportError: cannot import name 'parse_extraordinary_number'`

- [ ] **Step 3: Write minimal implementation**

In `querlock/diff/matchers.py`, add the constant near `AMENDMENT_SUFFIX_STATES` (~line 34):

```python
# States where LegiScan fuses the extraordinary-session marker into the bill
# number (CA: 'ABX110' = Assembly Bill, extraordinary session X1, bill 10) while
# Quorum keeps the base number in a separate, often non-current, special session.
EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})
```

And add the function after `legiscan_number_norm` (~line 59):

```python
def parse_extraordinary_number(raw_number, ordinals) -> list[tuple[int, str]]:
    """For a LegiScan number that fuses the extraordinary-session marker into the
    number, return every (ordinal, base_norm) candidate for the given ordinals
    (e.g. 'ABX110' with ordinals {1} -> [(1, 'AB10')]). Empty when the number
    carries no recognized marker. Returning all candidates (not one) lets the
    caller disambiguate by which base actually exists in that ordinal's session."""
    norm = normalize_bill_number(raw_number)
    out: list[tuple[int, str]] = []
    for o in ordinals:
        m = re.match(rf"^([A-Z]+)X{o}(\d+)$", norm)
        if m:
            out.append((o, f"{m.group(1)}{int(m.group(2))}"))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matchers.py -k parse_extraordinary -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add querlock/diff/matchers.py tests/test_matchers.py
git commit -m "feat: parse_extraordinary_number + EXTRAORDINARY_SESSION_STATES"
```

---

### Task 3: `extract_session_ordinal` + `select_sibling_special_sessions`

**Files:**
- Modify: `querlock/diff/matchers.py` (add after `parse_extraordinary_number`)
- Test: `tests/test_matchers.py`

**Interfaces:**
- Consumes: `SessionRow` (already imported at `matchers.py:3`), `re`.
- Produces:
  - `extract_session_ordinal(title: str | None) -> int | None` — X-ordinal from a Quorum session title/name.
  - `select_sibling_special_sessions(regular: SessionRow, specials: list[SessionRow]) -> dict[int, SessionRow]` — ordinal → the sibling special session in `regular`'s biennium.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_matchers.py` (add `extract_session_ordinal, select_sibling_special_sessions` to the matchers import):

```python
def test_extract_session_ordinal():
    assert extract_session_ordinal("2025 Spec Session 1 - X1") == 1
    assert extract_session_ordinal("2024 Spec Session 2 - X2") == 2
    assert extract_session_ordinal("California 2025-2026 Regular Session") is None
    assert extract_session_ordinal("2025-2026") is None
    assert extract_session_ordinal(None) is None


def test_select_sibling_special_sessions_keeps_biennium_rejects_stub_and_prior():
    reg = SessionRow(3570, "ca", "2025-2026", "2025-2026", 2025, True, True)
    specials = [
        SessionRow(3736, "ca", "2025 Spec Session 1 - X1", "2025 Spec Session 1 - X1",
                   2025, False, False),
        SessionRow(3809, "ca", "California 2025-2026 Regular Session", None,
                   2025, False, False),   # no X-ordinal -> rejected
        SessionRow(3665, "ca", "2024 Spec Session 1 - X1", "2024 Spec Session 1 - X1",
                   2024, False, False),   # prior biennium -> rejected
    ]
    got = select_sibling_special_sessions(reg, specials)
    assert set(got) == {1}
    assert got[1].id == 3736
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_matchers.py::test_extract_session_ordinal -v`
Expected: FAIL with `ImportError: cannot import name 'extract_session_ordinal'`

- [ ] **Step 3: Write minimal implementation**

In `querlock/diff/matchers.py`, add after `parse_extraordinary_number`:

```python
_ORDINAL_RE = re.compile(r"\bX(\d+)\b")
_SPEC_SESSION_RE = re.compile(r"SPEC(?:IAL)?\s+SESSION\s+(\d+)")


def extract_session_ordinal(title: str | None) -> int | None:
    """Extraordinary-session ordinal from a Quorum session title/name
    ('2025 Spec Session 1 - X1' -> 1). Prefers the 'X<n>' marker; falls back to
    'Spec[ial] Session <n>'. Returns None when neither is present."""
    if not title:
        return None
    up = title.upper()
    m = _ORDINAL_RE.search(up)
    if m:
        return int(m.group(1))
    m = _SPEC_SESSION_RE.search(up)
    return int(m.group(1)) if m else None


def select_sibling_special_sessions(regular: SessionRow,
                                    specials: list[SessionRow]) -> dict[int, SessionRow]:
    """Map ordinal -> the Quorum special session that belongs to `regular`'s
    biennium. A sibling is a special session whose start_year is within the
    biennium window [y, y+1] (y = regular.start_year) AND whose title/name carries
    an X-ordinal. Includes non-current sessions. First session wins per ordinal."""
    if regular.start_year is None:
        return {}
    window = {regular.start_year, regular.start_year + 1}
    out: dict[int, SessionRow] = {}
    for s in specials:
        if s.start_year not in window:
            continue
        ordinal = extract_session_ordinal(s.title)
        if ordinal is None:
            ordinal = extract_session_ordinal(s.session_name)
        if ordinal is None or ordinal in out:
            continue
        out[ordinal] = s
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_matchers.py -k "session_ordinal or sibling_special" -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add querlock/diff/matchers.py tests/test_matchers.py
git commit -m "feat: extract_session_ordinal + select_sibling_special_sessions"
```

---

### Task 4: Wire sibling matching into `diff_region`

**Files:**
- Modify: `querlock/diff/service.py` (imports ~line 6; extract a helper; the `for ls, qs in matched:` loop ~lines 43-99)
- Test: `tests/test_diff_service.py`

**Interfaces:**
- Consumes: `reader.get_special_sessions` (Task 1), `parse_extraordinary_number` + `EXTRAORDINARY_SESSION_STATES` (Task 2), `select_sibling_special_sessions` (Task 3).
- Produces: no new public symbol; `diff_region`'s behavior changes so CA X-marked bills match the sibling session. Adds module-private `_index_quorum_bills(q_bills, region, warnings, warn_label) -> dict[str, BillRow]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_diff_service.py`:

```python
def test_ca_extraordinary_bills_match_sibling_session_and_retire_fp(tmp_path):
    # LegiScan folds X1 bills into the regular session as fused numbers
    # (ABX11 = Assembly Bill, session X1, bill 1). Quorum stores the base label
    # (A.B.1) in a SEPARATE special session that is current=FALSE. The X1 bill must
    # match there (not be reported missing), a prior missing FP for it must retire,
    # and a genuinely-absent X1 bill must still be reported missing.
    from querlock.casefiles.models import Anomaly

    with LegiScanCache(tmp_path / "cache.db") as cache:
        cache.upsert_session("CA", {"session_id": 2172, "year_start": 2025, "year_end": 2026,
                                    "special": 0, "session_name": "2025-2026 Regular Session"})
        x1_bill = dict(BILL, bill_id=811, bill_number="ABX11", status=4,
                       status_date="2026-06-01", history=[{"date": "2026-06-10"}],
                       sponsors=[{"people_id": 1}], texts=[], votes=[])
        gone = dict(BILL, bill_id=812, bill_number="ABX199", title="A missing X1 act",
                    status=1, history=[{"date": "2026-06-10"}], sponsors=[], texts=[], votes=[])
        cache.ingest_dataset_zip(2172, make_dataset_zip([x1_bill, gone]))

        replica = _new_replica()
        replica.executescript(
            """
            INSERT INTO app_legsession VALUES
                (3570, 'ca', '2025-2026', '2025-2026', 2025, TRUE, TRUE),
                (3736, 'ca', '2025 Spec Session 1 - X1', '2025 Spec Session 1 - X1',
                 2025, FALSE, FALSE);
            INSERT INTO bill_bill (id, session_id, label, number, current_general_status,
                most_recent_action_date) VALUES (1, 3736, 'A.B.1', '1', 6, '2026-06-10');
            INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (1, '2026-06-10', 1);
            INSERT INTO bill_sponsor (bill_id, sponsor_type) VALUES (1, 1);
            """
        )
        with CaseFileStore(tmp_path / "casefile.db") as casefile:
            prior_fp = Anomaly(gap_type="missing_bill", region="CA", session_key="2172",
                               bill_number_norm="ABX11")
            casefile.upsert_anomaly(prior_fp)

            summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))

            # ABX11 matched A.B.1 in the sibling session (LS passed=4 vs Quorum
            # enacted rank 6 -> clean); only the genuinely-absent ABX199 is missing.
            assert summary["counts_by_gap_type"]["missing_bill"]["new"] == 1
            assert casefile.list_anomalies(gap_type="missing_bill", status="new")[0][
                "bill_number_norm"] == "ABX199"
            # The prior FP for the now-matched ABX11 auto-retired.
            assert casefile.get_anomaly_by_fingerprint(prior_fp.fingerprint)["status"] == "resolved"
            assert summary["anomalies_resolved"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_diff_service.py::test_ca_extraordinary_bills_match_sibling_session_and_retire_fp -v`
Expected: FAIL — currently `ABX11` is looked up as `ABX11` in the regular session, not found → reported missing, so `missing_bill.new == 2` (and the prior FP stays `new`).

- [ ] **Step 3: Write minimal implementation**

In `querlock/diff/service.py`, extend the matchers import (~line 6):

```python
from querlock.diff.matchers import (EXTRAORDINARY_SESSION_STATES,
                                    is_deliberately_unimported, legiscan_number_norm,
                                    match_sessions, parse_extraordinary_number,
                                    quorum_number_norm, select_sibling_special_sessions)
```

Add this module-level helper (e.g. just above `diff_region`):

```python
def _index_quorum_bills(q_bills, region, warnings, warn_label):
    """Build {normalized_number: BillRow}. On a same-key collision keep the first
    and record an observable warning — a silent merge would fabricate missing-bill
    FPs (spec §1 collision guard)."""
    by_norm = {}
    for b in q_bills:
        norm = quorum_number_norm(b.label, b.number, b.bill_type, state=region)
        if not norm:
            continue
        if norm in by_norm:
            warnings.append(
                f"{warn_label}: bill-number collision on {norm!r} "
                f"(labels {by_norm[norm].label!r} and {b.label!r}) — kept first")
            continue
        by_norm[norm] = b
    return by_norm
```

Inside `diff_region`, replace the inline `q_by_norm` build (current lines ~55-66) with the helper call plus sibling loading:

```python
        q_by_norm = _index_quorum_bills(q_bills, region, warnings,
                                        f"{region} session {session_key}")

        # CA folds extraordinary-session bills into the regular session as fused
        # numbers; their Quorum home is a separate, often non-current, special
        # session. Load those siblings so the fused bills match instead of being
        # reported missing. State-gated; every other region skips this entirely.
        siblings: dict[int, tuple[dict, dict, int]] = {}
        if region.upper() in EXTRAORDINARY_SESSION_STATES:
            specials = reader.get_special_sessions(replica_conn, region)
            for ordinal, sib in select_sibling_special_sessions(qs, specials).items():
                sib_map = _index_quorum_bills(
                    reader.get_bills_for_session(replica_conn, sib.id), region,
                    warnings, f"{region} sibling session {sib.id}")
                sib_counts = reader.get_bill_counts_for_session(replica_conn, sib.id)
                siblings[ordinal] = (sib_map, sib_counts, sib.id)
```

Then replace the body of the `for bill in ls_bills:` loop (current lines ~68-99) with:

```python
        for bill in ls_bills:
            norm = legiscan_number_norm(region, bill["number"])
            if not norm:
                continue

            # Extraordinary-session bills (state-gated) resolve against a sibling
            # session's base number; the first candidate whose base exists wins.
            q_bill = None
            q_bill_counts = q_counts
            match_norm = norm
            sibling_session_id = None
            if siblings:
                for ordinal, base in parse_extraordinary_number(bill["number"], siblings.keys()):
                    sib_map, sib_counts, sib_id = siblings[ordinal]
                    if base in sib_map:
                        q_bill, q_bill_counts, match_norm, sibling_session_id = (
                            sib_map[base], sib_counts, base, sib_id)
                        break

            if q_bill is None:
                q_bill = q_by_norm.get(norm)
                q_bill_counts = q_counts

            if q_bill is None:
                payload = cache.get_bill_payload(bill["bill_id"]) or {}
                title = (payload.get("title") or "").strip()
                # Salvage rule (comparison.py:106): no title (masterlist stub) or a
                # deliberately-unimported type -> ignore, don't flag.
                if not title or is_deliberately_unimported(region, title):
                    ignored += 1
                    continue
                ls_last = _as_date(bill["last_action_date"])
                days_since = (today - ls_last).days if ls_last else None
                record(Anomaly(
                    gap_type="missing_bill", region=region, session_key=session_key,
                    bill_number_norm=norm, legiscan_value=bill["number"] or "",
                    severity=compute_severity("missing_bill", "",
                                              days_since_ls_activity=days_since),
                    evidence={"legiscan_bill_id": bill["bill_id"], "title": title[:300],
                              "status": bill["status"], "status_date": bill["status_date"],
                              "last_action_date": bill["last_action_date"],
                              "quorum_session_id": qs.id},
                ), title)
            else:
                for anomaly in detect_bill_anomalies(
                        region, session_key, match_norm, bill, q_bill,
                        q_bill_counts.get(q_bill.id, reader.BillCounts()),
                        sla_hours=sla_hours, today=today):
                    if sibling_session_id is not None:
                        anomaly.evidence.setdefault("quorum_session_id", sibling_session_id)
                    record(anomaly)
```

- [ ] **Step 4: Run the new test and the full diff-service + matcher suites**

Run: `.venv/bin/python -m pytest tests/test_diff_service.py tests/test_matchers.py tests/test_quorum_reader.py -v`
Expected: all PASS, including the pre-existing `test_diff_finds_missing_bill`, `test_ny_suffix_collision_is_warned_not_silent`, and `test_diff_region_reports_resolved_count` (the `_index_quorum_bills` extraction preserves the exact collision-warning text).

- [ ] **Step 5: Commit**

```bash
git add querlock/diff/service.py tests/test_diff_service.py
git commit -m "feat: match CA extraordinary-session bills to their non-current sibling session"
```

---

### Task 5: CA extraordinary eval fixture + evals

**Files:**
- Create: `tests/evals/fixtures/ca_extraordinary.json`
- Test: `tests/evals/test_fp_families.py`

**Interfaces:**
- Consumes: `parse_extraordinary_number`, `quorum_number_norm`.
- Produces: two offline evals over real 2026-07-23 CA data shapes, run through the real matcher (no mocks) — the family maps cleanly, the planted genuine gap does not.

- [ ] **Step 1: Create the fixture**

Create `tests/evals/fixtures/ca_extraordinary.json` (the real 20 X1 bills from session 2172 mapped to their Quorum 3736 base labels, plus one planted genuine-missing bill):

```json
{
  "state": "CA",
  "session_key": "2172",
  "ordinal": 1,
  "pairs": [
    {"legiscan_number": "ABX11",  "quorum_label": "A.B.1",   "quorum_number": 1,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX12",  "quorum_label": "A.B.2",   "quorum_number": 2,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX13",  "quorum_label": "A.B.3",   "quorum_number": 3,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX14",  "quorum_label": "A.B.4",   "quorum_number": 4,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX15",  "quorum_label": "A.B.5",   "quorum_number": 5,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX16",  "quorum_label": "A.B.6",   "quorum_number": 6,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX17",  "quorum_label": "A.B.7",   "quorum_number": 7,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX18",  "quorum_label": "A.B.8",   "quorum_number": 8,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX19",  "quorum_label": "A.B.9",   "quorum_number": 9,  "quorum_bill_type": 3},
    {"legiscan_number": "ABX110", "quorum_label": "A.B.10",  "quorum_number": 10, "quorum_bill_type": 3},
    {"legiscan_number": "ABX111", "quorum_label": "A.B.11",  "quorum_number": 11, "quorum_bill_type": 3},
    {"legiscan_number": "ABX112", "quorum_label": "A.B.12",  "quorum_number": 12, "quorum_bill_type": 3},
    {"legiscan_number": "ABX113", "quorum_label": "A.B.13",  "quorum_number": 13, "quorum_bill_type": 3},
    {"legiscan_number": "ABX114", "quorum_label": "A.B.14",  "quorum_number": 14, "quorum_bill_type": 3},
    {"legiscan_number": "ABX115", "quorum_label": "A.B.15",  "quorum_number": 15, "quorum_bill_type": 3},
    {"legiscan_number": "SBX11",  "quorum_label": "S.B.1",   "quorum_number": 1,  "quorum_bill_type": 2},
    {"legiscan_number": "SBX12",  "quorum_label": "S.B.2",   "quorum_number": 2,  "quorum_bill_type": 2},
    {"legiscan_number": "SBX13",  "quorum_label": "S.B.3",   "quorum_number": 3,  "quorum_bill_type": 2},
    {"legiscan_number": "SBX14",  "quorum_label": "S.B.4",   "quorum_number": 4,  "quorum_bill_type": 2},
    {"legiscan_number": "ACAX11", "quorum_label": "A.C.A.1", "quorum_number": 1,  "quorum_bill_type": 3}
  ],
  "genuine_missing": {"legiscan_number": "ABX199", "base_expected": "AB99"}
}
```

- [ ] **Step 2: Write the failing evals**

Add to `tests/evals/test_fp_families.py` (extend the top import to include `parse_extraordinary_number`):

```python
def test_ca_extraordinary_family_all_match():
    fx = _load("ca_extraordinary.json")
    for p in fx["pairs"]:
        q_norm = quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                    p["quorum_bill_type"], state=fx["state"])
        cands = parse_extraordinary_number(p["legiscan_number"], {fx["ordinal"]})
        assert any(base == q_norm for _, base in cands), \
            f"{p['legiscan_number']} did not map to {p['quorum_label']}"


def test_ca_extraordinary_genuine_missing_absent():
    fx = _load("ca_extraordinary.json")
    q_bases = {quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                  p["quorum_bill_type"], state=fx["state"])
               for p in fx["pairs"]}
    cands = parse_extraordinary_number(fx["genuine_missing"]["legiscan_number"], {fx["ordinal"]})
    assert cands, "planted bill must still parse as extraordinary"
    assert all(base not in q_bases for _, base in cands), \
        "planted genuine-missing bill must not match any sibling base"
```

- [ ] **Step 3: Run the evals**

Run: `.venv/bin/python -m pytest tests/evals/test_fp_families.py -k ca_extraordinary -v`
Expected: 2 PASSED (real matcher, offline; no tunnel/key).

- [ ] **Step 4: Run the FULL suite offline to confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: entire suite PASSES with no tunnel and no LegiScan key set.

- [ ] **Step 5: Commit**

```bash
git add tests/evals/fixtures/ca_extraordinary.json tests/evals/test_fp_families.py
git commit -m "test: CA extraordinary-session eval fixture (family matches; genuine gap fires)"
```

---

### Task 6: Live acceptance (requires the tsh tunnel)

**Files:** none (verification only). This task mutates `data/casefile.db` (retires the 20 FPs) and needs the `tsh proxy db` tunnel up and `QUORUM_REPLICA_DSN` set.

**Interfaces:** consumes the whole feature end-to-end via the CLI.

- [ ] **Step 1: Record the before-state**

Run:
```bash
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/casefile.db')
for g in ('missing_bill','wrong_data','stale'):
    n=c.execute(\"SELECT COUNT(*) FROM anomalies WHERE region='CA' AND gap_type=? AND status='new'\",(g,)).fetchone()[0]
    print('CA',g,'new=',n)
"
```
Expected (as of 2026-07-23): `missing_bill new= 20`, `wrong_data new= 29`, `stale new= 1`.

- [ ] **Step 2: Run the CA diff against the live replica**

Run: `.venv/bin/querlock diff --scope CA`
Expected: completes without error; the summary shows a non-zero `anomalies_resolved` (~20) and `missing_bill` new count of 0 for the X family.

- [ ] **Step 3: Verify the after-state**

Run:
```bash
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/casefile.db'); c.row_factory=sqlite3.Row
mb=[r['bill_number_norm'] for r in c.execute(\"SELECT bill_number_norm FROM anomalies WHERE region='CA' AND gap_type='missing_bill' AND status='new'\")]
res=[r['bill_number_norm'] for r in c.execute(\"SELECT bill_number_norm FROM anomalies WHERE region='CA' AND gap_type='missing_bill' AND status='resolved'\")]
wd=c.execute(\"SELECT COUNT(*) FROM anomalies WHERE region='CA' AND gap_type='wrong_data' AND status='new'\").fetchone()[0]
print('missing_bill still new:', sorted(mb))
print('missing_bill resolved (ABX*):', sorted(res))
print('wrong_data new (ACR family, must be unchanged):', wd)
"
```
Expected: `missing_bill still new` is empty (no `ABX*`); the 20 `ABX*` numbers appear under `resolved`; `wrong_data new` is still 29 (the ACR family is deliberately untouched — deferred to the doctrine sub-project).

- [ ] **Step 4: No commit** — this task changes only `data/casefile.db`, which is git-ignored runtime state. Report the before/after numbers in the task summary.

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-07-23-m2b-ca-extraordinary-sessions-design.md`):
- §1 sibling-session discovery → Task 1 (`get_special_sessions`) + Task 3 (`select_sibling_special_sessions`, biennium window + ordinal, rejects the 3809 stub / 3665 prior biennium). ✓
- §2 LegiScan extraordinary-number parse → Task 2 (`parse_extraordinary_number`, `EXTRAORDINARY_SESSION_STATES`, candidate list for ambiguity) + Task 3 (`extract_session_ordinal`). ✓
- §3 diff wiring → Task 4 (state-gated sibling load, first-existing-base wins, `session_key` stays LegiScan id, siblings never in `processed_sessions`, sibling `quorum_session_id` recorded via `setdefault`). ✓
- §4 retirement no-change → Task 4's combined test asserts the prior `ABX11` FP flips to `resolved`; Task 6 verifies live. ✓
- §5 testing → Task 5 (offline eval fixture, real matcher, family matches + planted gap fires) + full-suite gate. ✓
- §6 OH (no code) → not a code task; already documented in the spec, no plan action needed. ✓
- §7 deferrals → out of scope by construction (CA ACR/SB574 untouched; Task 6 Step 3 asserts the ACR count is unchanged). ✓
- Acceptance criteria → Task 5 Step 4 (offline suite) + Task 6 (live `diff --scope CA`). ✓

**Placeholder scan:** none — every code/test step contains complete code; commands have expected output.

**Type consistency:** `parse_extraordinary_number(raw_number, ordinals) -> list[tuple[int, str]]` used identically in Tasks 2, 4, 5. `select_sibling_special_sessions(regular, specials) -> dict[int, SessionRow]` and `_index_quorum_bills(q_bills, region, warnings, warn_label) -> dict[str, BillRow]` used consistently in Task 4. `get_special_sessions(conn, region) -> list[SessionRow]` matches its consumer in Task 4.
