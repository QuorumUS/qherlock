# Sherlock M1 — Shadow Patrols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take Sherlock from M0 (one state, missing_bill only, console report) to M1 shadow patrols: all 50 states + federal, all four detectors, `investigate_bill` + `post_slack` tools, daily digests to `#quentin-bot` via launchd.

**Architecture:** Batch-capable deterministic tools (sync/diff accept `scope="all"`), agent spends its ~100-turn budget on triage/investigation/digest. Detection logic is pure functions in a new `diff/detectors.py`; all Quorum SQL stays in `quorum/reader.py`; Slack posting is a standalone `slack.py` module that never raises. Shadow mode: still zero write tools.

**Tech Stack:** Python 3.12+/uv, claude-agent-sdk, typer, httpx, psycopg, pydantic-settings, SQLite (cache + case files), pytest.

## Context

M0 merged at `abd78a1` (44 tests green; smoke: CA 5014 LegiScan vs 4995 Quorum bills, 20 anomalies = the known X1 special-session FP family). Spec §15 defines M1: *"all 50 states + federal, all four detectors, `investigate_bill`, `post_slack` → daily digests in `#sherlock-bot` via launchd."* M1 breadth is also half the pre-merge bar for the decided actacollecta graduation (M5), so this milestone is on the critical path. Victor decided (2026-07-21): **batch-capable tools** (not per-region agent calls, not a deterministic pre-pass), **checked-in launchd plist + docs** (matching his existing `us.quorum.*` LaunchAgents), and — superseding the spec's `#sherlock-bot` — **all Slack output goes to `#quentin-bot`** (the channel is determined by which webhook `SLACK_WEBHOOK_URL` points at; Task 16 must use a `#quentin-bot` webhook, and Task 0 updates the spec's channel references).

Verified facts this plan relies on (from quorum-site checkout, do not re-derive):
- `bill_bill` columns: `label`, `number` (integer), `bill_type`, `current_general_status` (**nullable**, GeneralBillStatus id), `current_status_date`, `most_recent_action_date` (nullable DATE), `introduced_date`, `missing_data`, `last_quorum_update`, `source`, `session_id` (app/bill/models.py:3577+).
- FK columns: `bill_billaction.bill_id` (:3498), `bill_billtext.bill_id` (:3280), `bill_sponsor.bill_id` (:3094 — sponsors live here; the `bill_bill_sponsors` M2M is deprecated, never count it), `vote_vote.related_bill_id` (app/vote/models.py:867). The M0 docstring in `reader.py` saying the FK is `related_bill_id` is only true for votes — fix it.
- Federal: `Region.federal` abbrev is `'us'` (app/models.py:645-650); the existing case-insensitive `LOWER(region_abbrev)` session query matches LegiScan's `"US"` unchanged.
- Federal null-label bills: identity = `bill_type` + `number`; BillType ids 1=hres 2=s 3=hr 4=sres 5=hconres 6=sconres 7=hjres 8=sjres (app/bill/models.py:1923).
- GeneralBillStatus (app/bill/status.py:13): 1 introduced, 2 out_of_committee, 3 passed_first, 4 passed_second, 5 to_executive, 6 enacted, 7 effective, 8 unknown, 9 failed; 10–14 nominations; 101+ non-US.
- Salvage (quorum-site comparison.py): prefix translation is applied to the **LegiScan side only** (:125); MA title-prefix ignore rules (:26); no-title bills skipped, not flagged (:106).
- `LegiScanClient.get_bill(bill_id)` already exists (client.py:53-55); errors raise `LegiScanError` (client.py:8).
- `ResultMessage` exposes `num_turns`, `duration_ms`, `total_cost_usd` (None on OAuth subscription), `usage` (venv claude_agent_sdk/types.py:1201+).
- uv is module-installed on this host: run everything as `python3 -m uv run …`.

## Global Constraints

- Python ≥3.12, `uv`-managed. Test gate: `python3 -m uv run pytest` (all green, 0 warnings) after every task.
- Shadow mode: the agent gets exactly 6 read-only MCP tools via `tools=[]` + `allowed_tools`; no Bash/Write/Edit ever.
- Every tool output bounded ≤~2k tokens; Slack messages ≤3500 chars (enforced in code, not doctrine).
- ALL Quorum SQL lives in `sherlock/quorum/reader.py`, using the `{ph}` placeholder shim (`?` sqlite / `%s` psycopg).
- LegiScan free tier: 30k calls/month, degrade at 80% (`DEGRADE_THRESHOLD = 0.8` in sync.py stays).
- Regions = 50 US states + `"US"` (federal). No territories (spec §17).
- LegiScan is a recall oracle only: Quorum ahead of LegiScan is never an anomaly.
- Detector precedence: at most one date anomaly per bill; `stale` wins over `wrong_data`.
- Env vars via pydantic-settings `.env`: new keys `SLACK_WEBHOOK_URL`, `SHERLOCK_FRESHNESS_SLA_HOURS=72`, `LEGISCAN_MONTHLY_BUDGET=30000`.
- Conventional commits; every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 0: Branch + persist this plan

**Files:**
- Create: `docs/superpowers/plans/2026-07-21-sherlock-m1-shadow-patrols.md`

- [ ] **Step 1: Create the feature branch**

```bash
cd ~/Projects/sherlock && git checkout -b feat/m1-shadow-patrols
```

- [ ] **Step 2: Copy this plan file into the repo** (from `~/.claude/plans/let-s-plan-m1-iridescent-possum.md`) to `docs/superpowers/plans/2026-07-21-sherlock-m1-shadow-patrols.md`.

- [ ] **Step 3: Record the channel decision in the spec** — in `docs/superpowers/specs/2026-07-20-sherlock-design.md`, replace `#sherlock-bot` with `#quentin-bot` (decision 2026-07-21, Victor: Sherlock reports into Quentin's channel per the actacollecta graduation direction) — occurrences in §1, §3, §5 (`post_slack` row), §11, §12 architecture diagram label, and §15. Same replacement in `README.md` if present.

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs: add M1 shadow-patrols implementation plan; Slack channel -> #quentin-bot"
```

---

### Task 1: Regions module

**Files:**
- Create: `sherlock/regions.py`
- Test: `tests/test_regions.py`

**Interfaces:**
- Produces: `ALL_REGIONS: tuple[str, ...]` (51 entries, `"US"` first), `parse_scope(scope: str) -> list[str]` (raises `ValueError` naming every invalid code). Consumed by sync, service, tools, CLI.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_regions.py
import pytest

from sherlock.regions import ALL_REGIONS, parse_scope


def test_all_regions_inventory():
    assert len(ALL_REGIONS) == 51
    assert ALL_REGIONS[0] == "US"
    assert "CA" in ALL_REGIONS
    assert "PR" not in ALL_REGIONS  # territories out of scope (spec §17)
    assert len(set(ALL_REGIONS)) == 51


def test_parse_scope_all_case_insensitive():
    assert parse_scope("all") == list(ALL_REGIONS)
    assert parse_scope("ALL") == list(ALL_REGIONS)


def test_parse_scope_single_list_dedup():
    assert parse_scope("ca") == ["CA"]
    assert parse_scope("ca, tx") == ["CA", "TX"]
    assert parse_scope("CA,ca") == ["CA"]


def test_parse_scope_invalid_names_every_bad_code():
    with pytest.raises(ValueError) as exc:
        parse_scope("CA,XX,YY")
    assert "XX" in str(exc.value) and "YY" in str(exc.value)


def test_parse_scope_empty_raises():
    with pytest.raises(ValueError):
        parse_scope("")
```

- [ ] **Step 2: Run to verify failure** — `python3 -m uv run pytest tests/test_regions.py -v` → FAIL (`ModuleNotFoundError: sherlock.regions`).

- [ ] **Step 3: Implement**

```python
# sherlock/regions.py
"""Region inventory: 50 US states + federal ("US" — LegiScan's code for Congress).

Quorum's federal LegSession rows carry region_abbrev 'us' (quorum-site
app/models.py Region.federal), so "US" flows through the existing
case-insensitive session query unchanged. No territories (spec §17).
"""

ALL_REGIONS: tuple[str, ...] = (
    "US",
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)


def parse_scope(scope: str) -> list[str]:
    """'all' -> every region; 'ca' -> ['CA']; 'ca, tx' -> ['CA', 'TX'].

    Dedups preserving order. Raises ValueError naming every invalid code.
    """
    s = (scope or "").strip()
    if s.lower() == "all":
        return list(ALL_REGIONS)
    out: list[str] = []
    bad: list[str] = []
    for part in s.split(","):
        code = part.strip().upper()
        if not code:
            continue
        if code not in ALL_REGIONS:
            bad.append(code)
        elif code not in out:
            out.append(code)
    if bad:
        raise ValueError(
            f"unknown region code(s): {', '.join(bad)} — use 'all', USPS state codes, or 'US'"
        )
    if not out:
        raise ValueError("empty scope — use 'all', USPS state codes, or 'US'")
    return out
```

- [ ] **Step 4: Run to verify pass** — `python3 -m uv run pytest tests/test_regions.py -v` → PASS.

- [ ] **Step 5: Commit** — `git add sherlock/regions.py tests/test_regions.py && git commit -m "feat: add region inventory + scope parser (50 states + US federal)"`

---

### Task 2: Config additions

**Files:**
- Modify: `sherlock/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.slack_webhook_url: str = ""`, `Settings.sherlock_freshness_sla_hours: int = 72`, `Settings.legiscan_monthly_budget: int = 30000` (env-mapped automatically by pydantic-settings).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`, matching its existing style)

```python
def test_m1_defaults(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_webhook_url == ""
    assert s.sherlock_freshness_sla_hours == 72
    assert s.legiscan_monthly_budget == 30000


def test_m1_env_overrides(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    monkeypatch.setenv("SHERLOCK_FRESHNESS_SLA_HOURS", "24")
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_webhook_url == "https://hooks.slack.example/x"
    assert s.sherlock_freshness_sla_hours == 24
```

- [ ] **Step 2: Run to verify failure** — `python3 -m uv run pytest tests/test_config.py -v` → FAIL (unknown attribute).

- [ ] **Step 3: Implement** — in `sherlock/config.py` after `quorum_replica_dsn`:

```python
    slack_webhook_url: str = ""
    sherlock_freshness_sla_hours: int = 72   # stale-detector SLA grace (spec §1)
    legiscan_monthly_budget: int = 30000     # free-tier cap; sync degrades at 80%
```

- [ ] **Step 4: Run to verify pass**, then run the full suite (`python3 -m uv run pytest`) → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: add Slack webhook, freshness SLA, and LegiScan budget settings"`

---

### Task 3: Case files — severity persistence + created/recurring rename

**Files:**
- Modify: `sherlock/casefiles/models.py`, `sherlock/casefiles/store.py`, `sherlock/diff/service.py:40`
- Test: `tests/test_casefiles.py`

**Interfaces:**
- Produces: `Anomaly.severity: str = ""` (NOT part of fingerprint); `upsert_anomaly` returns `("created"|"recurring", id)` and persists `severity` on insert **and** on recurrence (recency-driven severity changes over time). The anomaly lifecycle `status` column value `'new'` is untouched — this closes the M1 nit from `.superpowers/sdd/progress.md` ("status-new vs recurring naming confusion").

- [ ] **Step 1: Write the failing tests** (append to `tests/test_casefiles.py`; update the two existing assertions that expect `"new"` from `upsert_anomaly` to expect `"created"`)

```python
def test_upsert_returns_created_then_recurring(tmp_path):
    with CaseFileStore(tmp_path / "c.db") as store:
        a = Anomaly(gap_type="missing_bill", region="CA", session_key="1",
                    bill_number_norm="AB1", severity="P2")
        kind, aid = store.upsert_anomaly(a)
        assert kind == "created"
        kind2, aid2 = store.upsert_anomaly(a)
        assert kind2 == "recurring" and aid2 == aid
        assert store.get_anomaly(aid)["status"] == "new"  # lifecycle value untouched


def test_severity_persisted_and_refreshed_on_recurrence(tmp_path):
    import dataclasses
    with CaseFileStore(tmp_path / "c.db") as store:
        a = Anomaly(gap_type="stale", region="CA", session_key="1",
                    bill_number_norm="AB2", field="most_recent_action_date", severity="P3")
        _, aid = store.upsert_anomaly(a)
        assert store.get_anomaly(aid)["severity"] == "P3"
        store.upsert_anomaly(dataclasses.replace(a, severity="P2"))
        assert store.get_anomaly(aid)["severity"] == "P2"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

`sherlock/casefiles/models.py` — add field after `evidence`:

```python
    severity: str = ""  # P1–P4, computed at detection time; NOT part of the fingerprint
```

`sherlock/casefiles/store.py` — `upsert_anomaly`: add `severity = ?` to both UPDATE statements (params gain `a.severity`), add `severity` to the INSERT column list (params gain `a.severity`), and change the two return kinds from `"new"` to `"created"` (the race-fallback path already returns `"recurring"`). Docstring: `"""Returns ("created"|"recurring", id). "created" is the write outcome — distinct from the lifecycle status column, whose initial value stays 'new'."""`

`sherlock/diff/service.py:40` — `if kind == "new":` → `if kind == "created":`.

- [ ] **Step 4: Run full suite** → all green (test_diff_service still passes: summary keys `anomalies_new`/`anomalies_recurring` unchanged).

- [ ] **Step 5: Commit** — `git commit -am "feat: persist anomaly severity; rename upsert outcome to created/recurring (M1 nit)"`

---

### Task 4: Matchers — one-sided prefix translation, federal map, MA ignore rule

**Files:**
- Modify: `sherlock/diff/matchers.py`, `sherlock/diff/service.py:19-24`
- Test: `tests/test_matchers.py`

**Interfaces:**
- Produces: `normalize_bill_number(raw) -> str` (**state param removed** — pure normalization, no translation), `legiscan_number_norm(state, raw) -> str` (normalize + PREFIX_MAP, LegiScan side only), `quorum_number_norm(label, number, bill_type=None) -> str` (label if present, else BillType fallback for federal null labels, else `""`), `is_deliberately_unimported(state, title) -> bool`, dicts `PREFIX_MAP`, `IGNORED_TITLE_PREFIXES`, `BILL_TYPE_PREFIX`. `match_sessions` unchanged.

**Why one-sided:** M0 translated inside `normalize_bill_number`, applied to both sides. Harmless for CA (Quorum never uses "AR") but fatal federally: Quorum's `H.R. 24` → `HR24` → both-sides map would yield `HRES24`, breaking every House bill match. Salvage precedent (comparison.py:125) translates the LegiScan side only.

- [ ] **Step 1: Write the failing tests** (rewrite `tests/test_matchers.py` normalization cases; keep session-match tests as-is)

```python
from sherlock.diff.matchers import (
    is_deliberately_unimported, legiscan_number_norm, normalize_bill_number,
    quorum_number_norm,
)


def test_normalize_pure_rules():
    assert normalize_bill_number(" a b 12 ") == "AB12"
    assert normalize_bill_number("A.B. 0012") == "AB12"
    assert normalize_bill_number(None) == ""
    assert normalize_bill_number(24) == "24"


def test_legiscan_side_federal_prefixes():
    cases = {"HB24": "HR24", "SB5": "S5", "HR7": "HRES7", "SR2": "SRES2",
             "HJR3": "HJRES3", "SJR4": "SJRES4", "HCR10": "HCONRES10",
             "SCR9": "SCONRES9"}
    for raw, want in cases.items():
        assert legiscan_number_norm("US", raw) == want


def test_quorum_side_is_never_translated():
    # THE golden guard: H.R. 24 must stay HR24, not become HRES24.
    assert quorum_number_norm("H.R. 24", 24) == "HR24"
    assert quorum_number_norm("S. 100", 100) == "S100"


def test_quorum_null_label_bill_type_fallback():
    assert quorum_number_norm(None, 24, 3) == "HR24"      # bill_type 3 = hr
    assert quorum_number_norm(None, 3, 8) == "SJRES3"     # bill_type 8 = sjres
    assert quorum_number_norm(None, 3, 999) == ""         # unknown type, no label
    assert quorum_number_norm(None, None, 3) == ""


def test_ca_prefix_still_legiscan_side_only():
    assert legiscan_number_norm("CA", "AR10") == "HR10"
    assert legiscan_number_norm("TX", "AR10") == "AR10"
    assert quorum_number_norm("AR10", 10) == "AR10"


def test_ma_ignored_title_prefixes():
    assert is_deliberately_unimported("MA", "Order relative to procedure") is True
    assert is_deliberately_unimported("MA", "Study Order concerning X") is True
    assert is_deliberately_unimported("MA", "An Act to do things") is False
    assert is_deliberately_unimported("CA", "Order of business") is False
    assert is_deliberately_unimported("MA", None) is False
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — replace `sherlock/diff/matchers.py` head (keep `match_sessions` verbatim):

```python
import re

from sherlock.quorum.reader import SessionRow

# LegiScan prefix -> Quorum prefix, applied to the LEGISCAN side ONLY.
# Quorum's own numbers must never be translated (H.R. 24 -> HR24 must not
# become HRES24). Salvage precedent: quorum-site comparison.py:125.
PREFIX_MAP: dict[str, dict[str, str]] = {
    "CA": {"AR": "HR"},
    "US": {"HB": "HR", "SB": "S", "HR": "HRES", "SR": "SRES",
           "HJR": "HJRES", "SJR": "SJRES", "HCR": "HCONRES", "SCR": "SCONRES"},
}

# Salvaged (comparison.py:26): LegiScan bills whose TITLE marks a type Quorum
# deliberately does not import (MA procedural Orders, AID-226).
IGNORED_TITLE_PREFIXES: dict[str, tuple[str, ...]] = {
    "MA": ("order", "study order"),
}

# Quorum BillType id -> normalized prefix (quorum-site app/bill/models.py:1923),
# for federal bills whose label is NULL (identity = bill_type + number).
BILL_TYPE_PREFIX: dict[int, str] = {
    1: "HRES", 2: "S", 3: "HR", 4: "SRES",
    5: "HCONRES", 6: "SCONRES", 7: "HJRES", 8: "SJRES",
}

_CLEAN_RE = re.compile(r"[\s. ]")
_NUM_RE = re.compile(r"^([A-Z]+)0*(\d+)$")


def normalize_bill_number(raw: str | int | None) -> str:
    """Pure normalization: uppercase, strip spaces/dots, drop leading zeros.
    No prefix translation — see legiscan_number_norm for that."""
    s = _CLEAN_RE.sub("", ("" if raw is None else str(raw)).upper())
    m = _NUM_RE.match(s)
    if not m:
        return s
    return f"{m.group(1)}{m.group(2)}"


def legiscan_number_norm(state: str, raw: str | int | None) -> str:
    """Normalize + per-state prefix translation. LegiScan side only."""
    s = normalize_bill_number(raw)
    m = _NUM_RE.match(s)
    if not m:
        return s
    prefix, num = m.group(1), m.group(2)
    prefix = PREFIX_MAP.get(state.upper(), {}).get(prefix, prefix)
    return f"{prefix}{num}"


def quorum_number_norm(label: str | None, number, bill_type: int | None = None) -> str:
    """Quorum-side identity: normalized label; federal NULL-label fallback via
    bill_type + number; '' when no identity can be derived (caller skips)."""
    if label:
        return normalize_bill_number(label)
    if bill_type in BILL_TYPE_PREFIX and number is not None:
        return f"{BILL_TYPE_PREFIX[bill_type]}{number}"
    return ""


def is_deliberately_unimported(state: str, title: str | None) -> bool:
    """Salvaged MA order rule (comparison.py:31)."""
    prefixes = IGNORED_TITLE_PREFIXES.get(state.upper())
    if not prefixes:
        return False
    return (title or "").strip().lower().startswith(prefixes)
```

Update `sherlock/diff/service.py` call sites (M0 `BillRow` has no `bill_type` yet — pass 2 args):

```python
# line 19-22
        quorum_numbers = {
            quorum_number_norm(b.label, b.number)
            for b in reader.get_bills_for_session(replica_conn, qs.id)
        }
# line 24
            norm = legiscan_number_norm(state, bill["number"])
```

(and update the import line accordingly).

- [ ] **Step 4: Run full suite** → all green. Note `test_quorum_null_label_bill_type_fallback` expects `""` for unknown type — verify `quorum_number_norm(None, 24)` (no bill_type) is `""` too, since M0 fixtures always set labels.

Wait — check `tests/test_diff_service.py` fixtures: if any fixture bill has `label=None, number set`, behavior changes from "match on bare number" to "skip". Inspect and adjust fixtures to use labels (matching real state data, where labels exist — CA smoke matched 4995/5014 via labels).

- [ ] **Step 5: Commit** — `git commit -am "feat: one-sided prefix translation, federal prefix map, MA ignore rule"`

---

### Task 5: Reader — bill detail columns, per-session counts, recent actions

**Files:**
- Modify: `sherlock/quorum/reader.py`
- Test: `tests/test_quorum_reader.py`

**Interfaces:**
- Produces: extended `BillRow(id, label, number, bill_type, current_general_status, current_status_date, most_recent_action_date, introduced_date, missing_data, last_quorum_update, source)`; `BillCounts(actions, texts, sponsors, votes)` dataclass (all default 0); `get_bill_counts_for_session(conn, session_id) -> dict[int, BillCounts]` (4 aggregate queries — never per-bill); `get_recent_actions(conn, bill_id, limit=5) -> list[dict]` (`{"date", "action_type"}`); `_SCHEMA_PROBES` grows to 6 tables. Fix the module docstring (`related_bill_id` is votes-only).

- [ ] **Step 1: Write the failing tests** — extend the fake-replica fixture schema in `tests/test_quorum_reader.py`:

```python
_REPLICA_SCHEMA = """
CREATE TABLE app_legsession (id INTEGER PRIMARY KEY, region_abbrev TEXT, title TEXT,
    session_name TEXT, start_year INTEGER, current BOOLEAN, regular_session BOOLEAN);
CREATE TABLE bill_bill (id INTEGER PRIMARY KEY, session_id INTEGER, label TEXT,
    number INTEGER, bill_type INTEGER, current_general_status INTEGER,
    current_status_date TEXT, most_recent_action_date TEXT, introduced_date TEXT,
    missing_data BOOLEAN DEFAULT 0, last_quorum_update TEXT, source TEXT);
CREATE TABLE bill_billaction (id INTEGER PRIMARY KEY, bill_id INTEGER, date TEXT, action_type INTEGER);
CREATE TABLE bill_billtext (id INTEGER PRIMARY KEY, bill_id INTEGER);
CREATE TABLE bill_sponsor (id INTEGER PRIMARY KEY, bill_id INTEGER, sponsor_type INTEGER);
CREATE TABLE vote_vote (id INTEGER PRIMARY KEY, related_bill_id INTEGER);
"""
```

New tests:

```python
def test_bill_row_carries_detail_columns(replica):
    replica.execute("INSERT INTO app_legsession VALUES (1,'CA','t','n',2025,1,1)")
    replica.execute(
        "INSERT INTO bill_bill VALUES (10,1,'AB 1',1,2,'2026-07-01','2026-07-10','2026-01-05',0,'2026-07-11','legiscan')")
    rows = reader.get_bills_for_session(replica, 1)
    b = rows[0]
    assert b.bill_type == 1 and b.current_general_status == 2
    assert b.most_recent_action_date == "2026-07-10"
    assert b.missing_data is False


def test_counts_aggregate_per_session(replica):
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (10,1,'AB 1',1)")
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (11,1,'AB 2',2)")
    replica.execute("INSERT INTO bill_bill (id, session_id, label, number) VALUES (99,2,'HB 9',9)")
    for _ in range(2):
        replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (10,'2026-01-01',1)")
    replica.execute("INSERT INTO bill_billtext (bill_id) VALUES (10)")
    for _ in range(3):
        replica.execute("INSERT INTO bill_sponsor (bill_id, sponsor_type) VALUES (10,1)")
    replica.execute("INSERT INTO vote_vote (related_bill_id) VALUES (10)")
    replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (99,'2026-01-01',1)")
    counts = reader.get_bill_counts_for_session(replica, 1)
    assert counts[10].actions == 2 and counts[10].texts == 1
    assert counts[10].sponsors == 3 and counts[10].votes == 1
    assert 11 not in counts          # zero related rows -> absent; caller defaults BillCounts()
    assert 99 not in counts          # other session never leaks in


def test_recent_actions_ordered_desc_capped(replica):
    for d in ("2026-01-01", "2026-03-01", "2026-02-01", "2026-04-01", "2026-05-01", "2026-06-01"):
        replica.execute("INSERT INTO bill_billaction (bill_id, date, action_type) VALUES (10, ?, 1)", (d,))
    acts = reader.get_recent_actions(replica, 10)
    assert len(acts) == 5
    assert acts[0]["date"] == "2026-06-01"


def test_check_schema_flags_each_new_table(replica_factory):
    for missing in ("bill_billaction", "bill_billtext", "bill_sponsor", "vote_vote"):
        conn = replica_factory(exclude=missing)
        ok, err = reader.check_schema(conn)
        assert not ok and missing in err


def test_federal_sessions_match_us(replica):
    replica.execute("INSERT INTO app_legsession VALUES (5,'us','119th Congress','119',2025,1,1)")
    assert reader.get_current_sessions(replica, "US")[0].id == 5
```

(Adapt fixture helpers to however the existing file builds its fake replica — extend, don't fork. Existing tests that insert into `bill_bill` with 3 columns must switch to explicit column lists.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — in `sherlock/quorum/reader.py`:

```python
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


_SCHEMA_PROBES = (
    ("app_legsession", _SESSIONS_SQL, ("x",)),
    ("bill_bill", _BILLS_SQL, (0,)),
    ("bill_billaction", _COUNTS_SQL["actions"], (0,)),
    ("bill_billtext", _COUNTS_SQL["texts"], (0,)),
    ("bill_sponsor", _COUNTS_SQL["sponsors"], (0,)),
    ("vote_vote", _COUNTS_SQL["votes"], (0,)),
)
```

Fix module docstring lines 7-8: `bill_billaction`/`bill_billtext`/`bill_sponsor` FK is `bill_id`; only `vote_vote` uses `related_bill_id`. Do NOT probe `_ACTIONS_RECENT_SQL` (it already carries `LIMIT 5`; the probe wrapper appends `LIMIT 1`, which would be invalid SQL).

- [ ] **Step 4: Run full suite** → all green (test_diff_service fixtures need the new `bill_bill` columns; use explicit column lists with defaults).

- [ ] **Step 5: Commit** — `git commit -am "feat: reader pulls bill status/date detail, per-session counts, recent actions"`

---

### Task 6: Detectors — incomplete_fields, stale, wrong_data, severity

**Files:**
- Create: `sherlock/diff/detectors.py`
- Test: `tests/test_detectors.py`

**Interfaces:**
- Consumes: `BillRow`, `BillCounts` from Task 5; `Anomaly` (with `severity`) from Task 3.
- Produces: `detect_bill_anomalies(region, session_key, number_norm, ls_bill: dict, q_bill: BillRow, q_counts: BillCounts, *, sla_hours: int, today: date) -> list[Anomaly]`; `compute_severity(gap_type, field, *, days_since_ls_activity, lag_days=None) -> str`; maps `GENERAL_STATUS_RANK`, `LEGISCAN_MIN_RANK`; `INCOMPLETE_FIELDS`; `_as_date`.

Fingerprint layout (existing `sha1(gap_type|region|session_key|bill_number_norm|field)`): `missing_bill` keeps `field=""` (M0 fingerprints stay stable); `incomplete_fields` uses `field ∈ {sponsors,actions,texts,votes}` (up to 4 anomalies per bill); `stale` uses `field="most_recent_action_date"`; `wrong_data` uses `field="status"`. A bill flipping stale→wrong_data across patrols creates distinct case histories — intended.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_detectors.py
from datetime import date

import pytest

from sherlock.diff.detectors import (
    GENERAL_STATUS_RANK, LEGISCAN_MIN_RANK, compute_severity, detect_bill_anomalies,
)
from sherlock.quorum.reader import BillCounts, BillRow

TODAY = date(2026, 7, 21)


def _q(status=1, mrad="2026-07-20", **kw):
    base = dict(id=10, label="AB 1", number=1, bill_type=None,
                current_general_status=status, current_status_date="2026-07-01",
                most_recent_action_date=mrad, introduced_date="2026-01-05",
                missing_data=False, last_quorum_update=None, source="legiscan")
    base.update(kw)
    return BillRow(**base)


def _ls(status=1, last="2026-07-20", **counts):
    d = {"bill_id": 1, "number": "AB1", "status": status, "status_date": "2026-07-01",
         "last_action_date": last, "n_sponsors": 1, "n_actions": 1, "n_texts": 1, "n_votes": 0}
    d.update({f"n_{k}": v for k, v in counts.items()})
    return d


def _run(ls, q, counts=None, sla=72):
    return detect_bill_anomalies("CA", "2172", "AB1", ls, q, counts or BillCounts(
        actions=1, texts=1, sponsors=1, votes=0), sla_hours=sla, today=TODAY)


def test_incomplete_fields_one_anomaly_per_zero_field():
    out = _run(_ls(sponsors=2, votes=1), _q(), BillCounts(actions=1, texts=1))
    got = {(a.gap_type, a.field) for a in out}
    assert ("incomplete_fields", "sponsors") in got
    assert ("incomplete_fields", "votes") in got
    assert ("incomplete_fields", "actions") not in got
    fps = [a.fingerprint for a in out]
    assert len(fps) == len(set(fps))


def test_incomplete_never_fires_when_legiscan_also_zero_or_quorum_nonzero():
    assert _run(_ls(votes=0), _q(), BillCounts(actions=1, texts=1, sponsors=1)) == []
    assert _run(_ls(votes=2), _q(), BillCounts(actions=1, texts=1, sponsors=1, votes=1)) == []


def test_stale_beyond_sla_grace():
    out = _run(_ls(last="2026-07-20"), _q(mrad="2026-07-16"))  # 4-day lag > 72h
    assert [a.gap_type for a in out] == ["stale"]
    assert out[0].field == "most_recent_action_date"
    assert out[0].evidence["lag_days"] == 4


def test_stale_grace_window_and_quorum_ahead():
    assert _run(_ls(last="2026-07-20"), _q(mrad="2026-07-17")) == []   # exactly 72h: grace
    assert _run(_ls(last="2026-07-10"), _q(mrad="2026-07-20")) == []   # Quorum fresher


def test_no_date_detectors_when_quorum_mrad_null():
    # incomplete_fields:actions already covers empty Quorum history
    out = _run(_ls(last="2026-07-20"), _q(mrad=None), BillCounts(texts=1, sponsors=1))
    assert {a.gap_type for a in out} == {"incomplete_fields"}


def test_wrong_data_quorum_rank_below_minimum():
    out = _run(_ls(status=2), _q(status=1))  # Engrossed needs >= passed_first(3)
    assert [a.gap_type for a in out] == ["wrong_data"]
    assert out[0].field == "status"
    assert (out[0].legiscan_value, out[0].quorum_value) == ("2", "1")


@pytest.mark.parametrize("ls_status,q_status", [
    (2, 3),   # at minimum -> ok
    (4, 6),   # passed vs enacted -> ok
    (4, 7),   # passed vs effective -> ok
    (1, 9),   # failed is terminal = ahead
    (2, 8),   # unknown -> skip
    (2, None),  # NULL -> skip
    (6, 1),   # LS Failed -> never compared
    (2, 12),  # nomination status -> skip
])
def test_wrong_data_never_fires(ls_status, q_status):
    assert _run(_ls(status=ls_status), _q(status=q_status)) == []


def test_precedence_stale_wins_over_wrong_data():
    out = _run(_ls(status=4, last="2026-07-20"), _q(status=1, mrad="2026-07-01"))
    assert [a.gap_type for a in out] == ["stale"]


def test_severity_rubric():
    assert compute_severity("missing_bill", "", days_since_ls_activity=5) == "P1"
    assert compute_severity("missing_bill", "", days_since_ls_activity=90) == "P2"
    assert compute_severity("missing_bill", "", days_since_ls_activity=None) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=10) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=60, lag_days=45) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=60, lag_days=5) == "P3"
    assert compute_severity("wrong_data", "status", days_since_ls_activity=5) == "P2"
    assert compute_severity("incomplete_fields", "sponsors", days_since_ls_activity=None) == "P3"
    assert compute_severity("incomplete_fields", "texts", days_since_ls_activity=None) == "P4"


def test_status_maps_have_no_non_us_ids():
    assert all(k < 100 for k in GENERAL_STATUS_RANK)
    assert 6 not in LEGISCAN_MIN_RANK  # LS Failed deliberately unmapped
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# sherlock/diff/detectors.py
"""Pure per-bill detectors (spec §1, §8). No I/O — golden-testable.

Precedence: at most one date anomaly per bill; stale wins over wrong_data.
LegiScan is a recall oracle only — when Quorum is ahead (newer dates, higher
status rank, terminal failed) nothing is flagged. A bill may flip
stale -> wrong_data across patrols; distinct fingerprints, intended.
"""
from datetime import date, datetime, timedelta

from sherlock.casefiles.models import Anomaly
from sherlock.quorum.reader import BillCounts, BillRow

INCOMPLETE_FIELDS = ("sponsors", "actions", "texts", "votes")

# Quorum GeneralBillStatus id -> ordinal progress rank (quorum-site
# app/bill/status.py:13). Absent = never compare (8 unknown, 10-14
# nominations, 101+ non-US, NULL).
GENERAL_STATUS_RANK: dict[int, int] = {
    1: 1,   # introduced
    2: 2,   # out_of_committee
    3: 3,   # passed_first
    4: 4,   # passed_second
    5: 5,   # to_executive
    6: 6,   # enacted
    7: 6,   # effective — same progress as enacted
    9: 99,  # failed — terminal: counts as ahead of any LegiScan claim
}

# LegiScan bill status code -> MINIMUM expected Quorum rank. wrong_data fires
# only when Quorum's rank is strictly below the minimum (Quorum behind).
# LS 6 (Failed) deliberately unmapped: states mark failure at wildly
# different times (often sine die) — comparing recreates v1's FP noise.
LEGISCAN_MIN_RANK: dict[int, int] = {
    1: 1,  # Introduced
    2: 3,  # Engrossed = passed originating chamber
    3: 4,  # Enrolled = passed both chambers (transmittal timing varies)
    4: 6,  # Passed = law
    5: 5,  # Vetoed -> must at least have reached the executive
}


def _as_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def compute_severity(gap_type: str, field: str, *,
                     days_since_ls_activity: int | None,
                     lag_days: int | None = None) -> str:
    recent = days_since_ls_activity is not None and days_since_ls_activity <= 30
    if gap_type == "missing_bill":
        return "P1" if recent else "P2"
    if gap_type == "stale":
        return "P2" if recent or (lag_days or 0) > 30 else "P3"
    if gap_type == "wrong_data":
        return "P2" if recent else "P3"
    if gap_type == "incomplete_fields":
        return "P3" if field in ("sponsors", "actions") else "P4"
    return "P4"


def detect_bill_anomalies(region: str, session_key: str, number_norm: str,
                          ls_bill: dict, q_bill: BillRow, q_counts: BillCounts,
                          *, sla_hours: int, today: date) -> list[Anomaly]:
    out: list[Anomaly] = []
    ls_last = _as_date(ls_bill.get("last_action_date"))
    days_since = (today - ls_last).days if ls_last else None

    for field in INCOMPLETE_FIELDS:
        ls_n = ls_bill.get(f"n_{field}") or 0
        if ls_n >= 1 and getattr(q_counts, field) == 0:
            out.append(Anomaly(
                gap_type="incomplete_fields", region=region, session_key=session_key,
                bill_number_norm=number_norm, field=field,
                legiscan_value=str(ls_n), quorum_value="0",
                severity=compute_severity("incomplete_fields", field,
                                          days_since_ls_activity=days_since),
                evidence={"legiscan_bill_id": ls_bill.get("bill_id"),
                          "quorum_bill_id": q_bill.id,
                          "ls_last_action_date": ls_bill.get("last_action_date")},
            ))

    q_mrad = _as_date(q_bill.most_recent_action_date)
    if ls_last is None or q_mrad is None:
        return out  # no freshness evidence; incomplete_fields covers empty history

    lag = ls_last - q_mrad
    if lag > timedelta(hours=sla_hours):
        out.append(Anomaly(
            gap_type="stale", region=region, session_key=session_key,
            bill_number_norm=number_norm, field="most_recent_action_date",
            legiscan_value=ls_last.isoformat(), quorum_value=q_mrad.isoformat(),
            severity=compute_severity("stale", "most_recent_action_date",
                                      days_since_ls_activity=days_since,
                                      lag_days=lag.days),
            evidence={"lag_days": lag.days,
                      "legiscan_bill_id": ls_bill.get("bill_id"),
                      "quorum_bill_id": q_bill.id,
                      "quorum_general_status": q_bill.current_general_status},
        ))
        return out  # precedence: stale wins over wrong_data

    q_rank = GENERAL_STATUS_RANK.get(q_bill.current_general_status or 0)
    min_rank = LEGISCAN_MIN_RANK.get(ls_bill.get("status") or 0)
    if q_rank is not None and min_rank is not None and q_rank < min_rank:
        out.append(Anomaly(
            gap_type="wrong_data", region=region, session_key=session_key,
            bill_number_norm=number_norm, field="status",
            legiscan_value=str(ls_bill.get("status")),
            quorum_value=str(q_bill.current_general_status),
            severity=compute_severity("wrong_data", "status",
                                      days_since_ls_activity=days_since),
            evidence={"legiscan_status_code": ls_bill.get("status"),
                      "quorum_general_status": q_bill.current_general_status,
                      "ls_last_action_date": ls_last.isoformat(),
                      "q_most_recent_action_date": q_mrad.isoformat(),
                      "legiscan_bill_id": ls_bill.get("bill_id"),
                      "quorum_bill_id": q_bill.id},
        ))
    return out
```

- [ ] **Step 4: Run to verify pass** — `python3 -m uv run pytest tests/test_detectors.py -v`, then full suite.

- [ ] **Step 5: Commit** — `git commit -am "feat: incomplete_fields/stale/wrong_data detectors with precedence + severity rubric"`

---

### Task 7: Diff service — all four detectors per region + diff_many rollup

**Files:**
- Modify: `sherlock/diff/service.py` (full rewrite)
- Test: `tests/test_diff_service.py`

**Interfaces:**
- Consumes: Tasks 4–6, `ALL_REGIONS` (Task 1).
- Produces: `diff_region(region, cache, casefile, replica_conn, *, sla_hours=72, today=None) -> dict` (per-region summary, keys: `region, sessions_matched, warnings, anomalies_new, anomalies_recurring, counts_by_gap_type, ignored, top_cases`); `diff_state = diff_region` back-compat alias (CLI/tools import it until Tasks 13/15); `diff_many(regions, cache, casefile, replica_conn, *, sla_hours=72, today=None) -> dict` rollup — THE tool contract:

```python
{"scope_regions": 51, "regions_diffed": 49, "errors": {"NV": "OperationalError: …"},
 "counts_by_gap_type": {"missing_bill": {"new": 3, "recurring": 17}, …},
 "anomalies_new": 5, "anomalies_recurring": 20,
 "regions": {"CA": {"missing_bill": 20, "stale": 2}, …},   # non-zero totals only, top 30 rows, then {"_more": N}
 "warnings_count": 9, "warnings_sample": ["CA: LegiScan session 2172 …", …],  # ≤10
 "top_cases": [{"id": 812, "region": "CA", "gap_type": "missing_bill", "severity": "P1",
                "bill_number": "AB123", "session_key": "2172", "kind": "created",
                "title": "…"}, …]}                          # ≤15, sorted (severity, newest)
```

Behavior notes: missing_bill now requires a cached payload with a title — payload-less masterlist stubs and MA-order-style titles are counted in `ignored`, not flagged (salvage precedent comparison.py:106; sacrifices a little recall for FP safety). Payloads are loaded lazily (only for unmatched bills) — never load 5k payload JSONs for matched bills.

- [ ] **Step 1: Write the failing tests** — extend `tests/test_diff_service.py` (fixtures gain the Task 5 replica schema; keep the existing missing-bill and session-warning tests, updated for new columns):

```python
def test_incomplete_fields_end_to_end(env):   # env = existing fixture pattern
    # cache bill AB1 with n_sponsors=1; replica bill 'AB 1' with 0 bill_sponsor rows
    summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
    assert summary["counts_by_gap_type"]["incomplete_fields"]["new"] == 1
    row = casefile.list_anomalies(gap_type="incomplete_fields")[0]
    assert row["field"] == "sponsors" and row["severity"] in ("P3",)


def test_stale_end_to_end_with_injected_today(env):
    # cache bill last_action 2026-07-20; replica mrad 2026-07-10
    summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
    assert summary["counts_by_gap_type"]["stale"]["new"] == 1


def test_wrong_data_end_to_end(env):
    # equal dates; LS status=2 (Engrossed), Quorum current_general_status=1
    summary = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
    assert summary["counts_by_gap_type"]["wrong_data"]["new"] == 1


def test_ma_orders_and_titleless_stubs_ignored_not_flagged(env):
    # MA cache bill titled "Order relative to X" absent from Quorum -> ignored
    # CA masterlist stub (payload_json NULL) absent from Quorum -> ignored
    assert summary["ignored"] == 1 and summary["counts_by_gap_type"] == {}


def test_federal_null_label_matching(env):
    # replica: app_legsession region_abbrev='us'; bill label=NULL, bill_type=3, number=24
    # cache US: bill 'HB24' (matches -> no anomaly), bill 'HB99' w/ title (missing_bill)
    summary = diff_region("US", cache, casefile, replica, today=date(2026, 7, 21))
    assert summary["counts_by_gap_type"]["missing_bill"]["new"] == 1
    assert casefile.list_anomalies(gap_type="missing_bill")[0]["bill_number_norm"] == "HR99"


def test_diff_many_rollup_and_error_isolation(env):
    # two regions in fixtures; monkeypatch reader.get_current_sessions to raise for one
    rollup = diff_many(["CA", "US"], cache, casefile, replica, today=date(2026, 7, 21))
    assert rollup["scope_regions"] == 2
    assert "US" in rollup["errors"] and rollup["regions_diffed"] == 1
    assert rollup["top_cases"][0]["severity"] <= rollup["top_cases"][-1]["severity"]


def test_second_run_counts_recurring(env):
    diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
    s2 = diff_region("CA", cache, casefile, replica, today=date(2026, 7, 21))
    assert s2["anomalies_new"] == 0 and s2["anomalies_recurring"] >= 1
```

(Write these as real tests against the existing fixture helpers — the sketches above fix the assertions; build the fixture data in each test body.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — replace `sherlock/diff/service.py`:

```python
from datetime import datetime, timezone

from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore
from sherlock.diff.detectors import _as_date, compute_severity, detect_bill_anomalies
from sherlock.diff.matchers import (is_deliberately_unimported, legiscan_number_norm,
                                    match_sessions, quorum_number_norm)
from sherlock.legiscan.cache import LegiScanCache
from sherlock.quorum import reader

TOP_CASES_LIMIT = 10
ROLLUP_TOP_LIMIT = 15
ROLLUP_REGION_ROWS = 30
WARNINGS_SAMPLE = 10


def diff_region(region: str, cache: LegiScanCache, casefile: CaseFileStore,
                replica_conn, *, sla_hours: int = 72, today=None) -> dict:
    """All four detectors for one region ('US' = federal)."""
    today = today or datetime.now(timezone.utc).date()
    ls_sessions = cache.get_sessions(region)
    q_sessions = reader.get_current_sessions(replica_conn, region)
    matched, warnings = match_sessions(ls_sessions, q_sessions)

    counts: dict[str, dict[str, int]] = {}
    ignored = 0
    cases: list[dict] = []

    def record(anomaly: Anomaly, title: str = ""):
        kind, aid = casefile.upsert_anomaly(anomaly)
        bucket = counts.setdefault(anomaly.gap_type, {"new": 0, "recurring": 0})
        bucket["new" if kind == "created" else "recurring"] += 1
        cases.append({"id": aid, "gap_type": anomaly.gap_type, "severity": anomaly.severity,
                      "bill_number": anomaly.bill_number_norm,
                      "session_key": anomaly.session_key, "kind": kind,
                      "title": title[:120]})

    for ls, qs in matched:
        session_key = str(ls["session_id"])
        q_bills = reader.get_bills_for_session(replica_conn, qs.id)
        q_counts = reader.get_bill_counts_for_session(replica_conn, qs.id)
        q_by_norm: dict[str, reader.BillRow] = {}
        for b in q_bills:
            norm = quorum_number_norm(b.label, b.number, b.bill_type)
            if norm:
                q_by_norm[norm] = b

        for bill in cache.bills_for_session(ls["session_id"]):
            norm = legiscan_number_norm(region, bill["number"])
            if not norm:
                continue
            q_bill = q_by_norm.get(norm)
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
                        region, session_key, norm, bill, q_bill,
                        q_counts.get(q_bill.id, reader.BillCounts()),
                        sla_hours=sla_hours, today=today):
                    record(anomaly)

    cases.sort(key=lambda c: (c["severity"], -c["id"]))
    new = sum(c["new"] for c in counts.values())
    recurring = sum(c["recurring"] for c in counts.values())
    return {"region": region, "sessions_matched": len(matched), "warnings": warnings,
            "anomalies_new": new, "anomalies_recurring": recurring,
            "counts_by_gap_type": counts, "ignored": ignored,
            "top_cases": cases[:TOP_CASES_LIMIT]}


diff_state = diff_region  # back-compat alias (M0 CLI/tool imports)


def diff_many(regions, cache: LegiScanCache, casefile: CaseFileStore, replica_conn,
              *, sla_hours: int = 72, today=None) -> dict:
    """Bounded rollup across regions. One region's failure never kills the
    patrol (spec §12) — it lands in `errors` and the loop continues."""
    per_gap: dict[str, dict[str, int]] = {}
    region_rows: dict[str, dict[str, int]] = {}
    errors: dict[str, str] = {}
    top: list[dict] = []
    warn_sample: list[str] = []
    total_new = total_rec = warn_count = diffed = 0

    for region in regions:
        try:
            r = diff_region(region, cache, casefile, replica_conn,
                            sla_hours=sla_hours, today=today)
        except Exception as exc:
            errors[region] = f"{type(exc).__name__}: {exc}"
            continue
        diffed += 1
        total_new += r["anomalies_new"]
        total_rec += r["anomalies_recurring"]
        warn_count += len(r["warnings"])
        for w in r["warnings"]:
            if len(warn_sample) < WARNINGS_SAMPLE:
                warn_sample.append(f"{region}: {w}")
        row: dict[str, int] = {}
        for gap, c in r["counts_by_gap_type"].items():
            g = per_gap.setdefault(gap, {"new": 0, "recurring": 0})
            g["new"] += c["new"]
            g["recurring"] += c["recurring"]
            total = c["new"] + c["recurring"]
            if total:
                row[gap] = total
        if row:
            region_rows[region] = row
        for case in r["top_cases"]:
            top.append({**case, "region": region})

    top.sort(key=lambda c: (c["severity"], -c["id"]))
    if len(region_rows) > ROLLUP_REGION_ROWS:
        keep = sorted(region_rows, key=lambda k: -sum(region_rows[k].values()))
        dropped = len(region_rows) - ROLLUP_REGION_ROWS
        region_rows = {k: region_rows[k] for k in keep[:ROLLUP_REGION_ROWS]}
        region_rows["_more"] = dropped
    return {"scope_regions": len(list(regions)), "regions_diffed": diffed,
            "errors": errors, "counts_by_gap_type": per_gap,
            "anomalies_new": total_new, "anomalies_recurring": total_rec,
            "regions": region_rows, "warnings_count": warn_count,
            "warnings_sample": warn_sample, "top_cases": top[:ROLLUP_TOP_LIMIT]}
```

- [ ] **Step 4: Run full suite** → all green. The M0 X1 special-session family still produces missing_bill anomalies (M2 owns suppression — do not "fix" it here).

- [ ] **Step 5: Commit** — `git commit -am "feat: diff_region runs all four detectors; diff_many bounded multi-region rollup"`

---

### Task 8: Cache — sync_meta table + upsert_bill refactor

**Files:**
- Modify: `sherlock/legiscan/cache.py`
- Test: `tests/test_legiscan_cache.py`

**Interfaces:**
- Produces: `get_sync_meta(state) -> dict | None`, `touch_sync_meta(state, *, session_list=False, dataset_list=False)`, `upsert_bill(session_id, bill: dict)` (public, commits — used by `investigate_bill` to refresh one bill after a live getBill). `ingest_dataset_zip` keeps its single-commit performance by calling a private `_upsert_bill_row` (no commit).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_legiscan_cache.py`)

```python
def test_sync_meta_roundtrip(cache):
    assert cache.get_sync_meta("CA") is None
    cache.touch_sync_meta("CA", session_list=True)
    meta = cache.get_sync_meta("CA")
    assert meta["session_list_fetched_at"] and meta["dataset_list_fetched_at"] is None
    cache.touch_sync_meta("CA", dataset_list=True)
    meta2 = cache.get_sync_meta("CA")
    assert meta2["session_list_fetched_at"] == meta["session_list_fetched_at"]
    assert meta2["dataset_list_fetched_at"]


def test_upsert_bill_matches_zip_ingest_shape(cache):
    bill = {"bill_id": 7, "bill_number": "AB7", "change_hash": "h", "status": 1,
            "status_date": "2026-01-01", "title": "T",
            "history": [{"date": "2026-02-01"}, {"date": "2026-01-15"}],
            "sponsors": [{}], "texts": [], "votes": []}
    cache.upsert_bill(3, bill)
    row = cache.bills_for_session(3)[0]
    assert row["last_action_date"] == "2026-02-01" and row["n_sponsors"] == 1
    assert cache.get_bill_payload(7)["title"] == "T"
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — add to `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS sync_meta (
    state TEXT PRIMARY KEY,
    session_list_fetched_at TEXT,
    dataset_list_fetched_at TEXT
);
```

Factor the body of `ingest_dataset_zip`'s per-bill INSERT into `_upsert_bill_row(self, session_id, bill)` (no commit); `ingest_dataset_zip` loops `_upsert_bill_row` + one commit at the end (unchanged perf for 5k-bill ZIPs); add:

```python
    def upsert_bill(self, session_id: int, bill: dict) -> None:
        self._upsert_bill_row(session_id, bill)
        self._conn.commit()

    # -- sync metadata ---------------------------------------------------------
    def get_sync_meta(self, state: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sync_meta WHERE state = ?", (state,)).fetchone()
        return dict(row) if row else None

    def touch_sync_meta(self, state: str, *, session_list: bool = False,
                        dataset_list: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO sync_meta (state) VALUES (?) ON CONFLICT(state) DO NOTHING",
            (state,))
        if session_list:
            self._conn.execute(
                "UPDATE sync_meta SET session_list_fetched_at = ? WHERE state = ?",
                (_now(), state))
        if dataset_list:
            self._conn.execute(
                "UPDATE sync_meta SET dataset_list_fetched_at = ? WHERE state = ?",
                (_now(), state))
        self._conn.commit()
```

- [ ] **Step 4: Run full suite** → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: cache sync_meta timestamps + single-bill upsert"`

---

### Task 9: Sync — TTL caching, per-session error isolation, sync_many

**Files:**
- Modify: `sherlock/legiscan/sync.py`
- Test: `tests/test_legiscan_sync.py`

**Interfaces:**
- Consumes: Task 8 cache methods, `LegiScanError` (client.py:8).
- Produces: `sync_state(state, client, cache, budget_limit=30000, today_year=None, now=None) -> dict` (new keys: `session_list_cached`, `dataset_list_cached`, `errors: list[str]`); `sync_many(regions, client, cache, budget_limit=30000) -> dict` rollup:

```python
{"scope_regions": 51, "synced": 49, "degraded": ["WY"], "errors": {"TX": "getDatasetList failed: HTTP 503"},
 "totals": {"sessions": 84, "datasets_ingested": 3, "bills_ingested": 6210, "masterlist_refreshed": 84},
 "session_lists_fetched": 2, "dataset_lists_fetched": 7,
 "calls_this_month": 3120, "budget_limit": 30000, "budget_pct": 10.4}
```

**Call math (why TTLs):** daily `--scope all`, ~85 current sessions nationally: naive = 51 getSessionList + 51 getDatasetList + ~85 getMasterListRaw ≈ 187 calls/day ≈ 5.6k/mo before any getDataset. With 30-day session-list TTL and 7-day dataset-list TTL: ~85 masterlist + amortized ~10 list calls/day ≈ 2.9k/mo — headroom funds `investigate_bill` getBill calls and ad-hoc reruns. Spec §6 mandates these cadences anyway. Freshness risk is one-sided: a ≤7-day-stale LegiScan view can only *under*-flag (recall oracle), never false-positive; masterlist change-hashes stay daily.

- [ ] **Step 1: Write the failing tests** (extend `tests/test_legiscan_sync.py`, reusing its fake-client pattern; add a `calls: list[str]` recorder to the fake)

```python
def test_session_list_cached_within_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache)                    # first run fetches
    fake_client.calls.clear()
    stats = sync_state("CA", fake_client, cache)
    assert "getSessionList" not in fake_client.calls
    assert stats["session_list_cached"] is True
    assert stats["sessions"] >= 1                           # derived from cache


def test_session_list_refetched_after_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache)
    # age the meta 31 days
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    cache._conn.execute("UPDATE sync_meta SET session_list_fetched_at = ?", (old,))
    fake_client.calls.clear()
    sync_state("CA", fake_client, cache)
    assert "getSessionList" in fake_client.calls


def test_dataset_list_cached_within_ttl(cache, fake_client):
    sync_state("CA", fake_client, cache)
    fake_client.calls.clear()
    stats = sync_state("CA", fake_client, cache)
    assert "getDatasetList" not in fake_client.calls and stats["dataset_list_cached"] is True


def test_masterlist_error_does_not_abort_other_sessions(cache, fake_client_two_sessions):
    # fake raises LegiScanError for session 1's masterlist only
    stats = sync_state("CA", fake_client_two_sessions, cache)
    assert stats["masterlist_refreshed"] == 1 and len(stats["errors"]) == 1


def test_sync_many_aggregates_and_isolates_errors(cache):
    # CA fake works; TX fake raises LegiScanError on get_session_list
    rollup = sync_many(["CA", "TX"], client, cache)
    assert rollup["synced"] == 1 and "TX" in rollup["errors"]
    assert rollup["totals"]["sessions"] >= 1
    assert "budget_pct" in rollup


def test_sync_many_degrades_tail_when_budget_crossed(cache):
    # pre-load quota to 80%: cache.add_call in a loop, budget_limit small
    rollup = sync_many(["CA", "TX"], client, cache, budget_limit=10)
    assert "CA" in rollup["degraded"] and "TX" in rollup["degraded"]


def test_sync_many_rollup_is_bounded(cache):
    rollup = sync_many(["CA", "TX"], client, cache)
    assert "CA" not in rollup  # no per-region stats rows, totals only
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — rewrite `sherlock/legiscan/sync.py`:

```python
import base64
from datetime import datetime, timedelta, timezone

from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient, LegiScanError

DEGRADE_THRESHOLD = 0.8
SESSION_LIST_TTL_DAYS = 30   # spec §6: session inventory monthly
DATASET_LIST_TTL_DAYS = 7    # spec §6: dataset hashes weekly


def _fresh(ts: str | None, ttl_days: int, now: datetime) -> bool:
    if not ts:
        return False
    return (now - datetime.fromisoformat(ts)) < timedelta(days=ttl_days)


def sync_state(state, client, cache, budget_limit: int = 30000,
               today_year: int | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today_year = today_year or now.year
    calls_this_month = cache.calls_this_month()
    stats = {"state": state, "sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
             "masterlist_refreshed": 0, "calls_this_month": calls_this_month,
             "degraded": False, "session_list_cached": False,
             "dataset_list_cached": False, "errors": []}

    if calls_this_month >= DEGRADE_THRESHOLD * budget_limit:
        stats["degraded"] = True
        stats["sessions"] = len(cache.get_sessions(state))
        return stats

    meta = cache.get_sync_meta(state) or {}

    if _fresh(meta.get("session_list_fetched_at"), SESSION_LIST_TTL_DAYS, now):
        current = [s for s in cache.get_sessions(state)
                   if (s.get("year_end") or 0) >= today_year]
        stats["session_list_cached"] = True
    else:
        current = [s for s in client.get_session_list(state)
                   if (s.get("year_end") or 0) >= today_year]
        for s in current:
            cache.upsert_session(state, s)
        cache.touch_sync_meta(state, session_list=True)
    stats["sessions"] = len(current)
    current_ids = {s["session_id"] for s in current}

    if _fresh(meta.get("dataset_list_fetched_at"), DATASET_LIST_TTL_DAYS, now):
        stats["dataset_list_cached"] = True
    else:
        for ds in client.get_dataset_list(state):
            try:
                sid = ds["session_id"]
                if sid not in current_ids or cache.dataset_hash(sid) == ds["dataset_hash"]:
                    continue
                dataset = client.get_dataset(sid, ds["access_key"])
                zip_bytes = base64.b64decode(dataset["zip"])
                stats["bills_ingested"] += cache.ingest_dataset_zip(sid, zip_bytes)
                cache.set_dataset_hash(sid, ds["dataset_hash"])
                stats["datasets_ingested"] += 1
            except KeyError:
                continue
        cache.touch_sync_meta(state, dataset_list=True)

    for sid in current_ids:
        try:
            masterlist = client.get_master_list_raw(sid)
        except LegiScanError as exc:
            stats["errors"].append(f"session {sid}: {exc}")
            continue
        for key, entry in masterlist.items():
            if key == "session":
                continue
            try:
                cache.upsert_bill_stub(sid, entry["bill_id"], entry["number"],
                                       entry["change_hash"])
            except KeyError:
                continue
        stats["masterlist_refreshed"] += 1

    stats["calls_this_month"] = cache.calls_this_month()
    return stats


def sync_many(regions, client, cache, budget_limit: int = 30000) -> dict:
    """Deterministic loop over sync_state. Per-region errors are recorded, never
    raised (spec §12). Budget re-checked each region, so exhaustion mid-run
    degrades the tail."""
    totals = {"sessions": 0, "datasets_ingested": 0, "bills_ingested": 0,
              "masterlist_refreshed": 0}
    degraded: list[str] = []
    errors: dict[str, str] = {}
    session_fetches = dataset_fetches = synced = 0
    for region in regions:
        try:
            s = sync_state(region, client, cache, budget_limit=budget_limit)
        except LegiScanError as exc:
            errors[region] = str(exc)
            continue
        synced += 1
        if s["degraded"]:
            degraded.append(region)
            continue
        for k in totals:
            totals[k] += s[k]
        session_fetches += 0 if s["session_list_cached"] else 1
        dataset_fetches += 0 if s["dataset_list_cached"] else 1
        for e in s["errors"]:
            errors[region] = "; ".join(s["errors"])
    calls = cache.calls_this_month()
    return {"scope_regions": len(list(regions)), "synced": synced,
            "degraded": degraded, "errors": errors, "totals": totals,
            "session_lists_fetched": session_fetches,
            "dataset_lists_fetched": dataset_fetches,
            "calls_this_month": calls, "budget_limit": budget_limit,
            "budget_pct": round(100 * calls / budget_limit, 1)}
```

Note: a degraded region still counts in `synced` (it returned cleanly) but lands in `degraded` — the digest instruction distinguishes them.

- [ ] **Step 4: Run full suite** → all green (existing sync tests: the fake clients need a no-op or the tests need `now` injection; reconcile without weakening the budget-degradation test).

- [ ] **Step 5: Commit** — `git commit -am "feat: TTL-cached session/dataset lists, error isolation, sync_many rollup"`

---

### Task 10: Slack module

**Files:**
- Create: `sherlock/slack.py`
- Test: `tests/test_slack.py`

**Interfaces:**
- Produces: `post(webhook_url, kind, text, http=None) -> dict` — NEVER raises; returns `{"ok": True, "chars": n, "truncated": bool}` or `{"ok": False, "error": str}`; `truncate(text) -> tuple[str, bool]`; `MAX_CHARS = 3500`. Top-level module (not `agent/`): the patrol-fatal alert path fires from `patrol.py` outside the agent loop.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_slack.py
import httpx

from sherlock import slack


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_success_includes_kind_header():
    seen = {}

    def handler(request):
        seen["json"] = request.read().decode()
        return httpx.Response(200, text="ok")

    result = slack.post("https://hooks.example/x", "digest", "hello",
                        http=_client(handler))
    assert result["ok"] is True and result["truncated"] is False
    assert "Sherlock digest" in seen["json"] and "hello" in seen["json"]


def test_no_webhook_configured():
    result = slack.post("", "digest", "hello")
    assert result == {"ok": False, "error": "SLACK_WEBHOOK_URL not configured"}


def test_http_error_never_raises_and_never_leaks_url():
    def handler(request):
        return httpx.Response(500, text="boom")

    result = slack.post("https://hooks.example/SECRET", "alert", "x",
                        http=_client(handler))
    assert result["ok"] is False and "SECRET" not in result["error"]


def test_connect_error_never_raises():
    def handler(request):
        raise httpx.ConnectError("nope")

    result = slack.post("https://hooks.example/x", "digest", "x",
                        http=_client(handler))
    assert result["ok"] is False


def test_truncation_at_cap_with_pointer():
    body = "y" * 5000
    text, truncated = slack.truncate(body)
    assert truncated is True and len(text) <= slack.MAX_CHARS
    assert "casefile.db" in text
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

```python
# sherlock/slack.py
"""Slack webhook reporting for #quentin-bot (spec §5 post_slack, §12; channel decided 2026-07-21).

The channel is whatever SLACK_WEBHOOK_URL points at — webhooks are channel-bound.

Failures are logged and returned as payloads — NEVER raised. Reporting must
never break the pipeline. The 3500-char cap is enforced here in code, not in
doctrine.
"""
import httpx
import structlog

MAX_CHARS = 3500
_POINTER = "\n…[truncated — full details in casefile.db / patrol transcript]"
_HEADERS = {"digest": ":mag: *Sherlock digest*", "alert": ":rotating_light: *Sherlock alert*"}

log = structlog.get_logger()


def truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[: MAX_CHARS - len(_POINTER)] + _POINTER, True


def post(webhook_url: str, kind: str, text: str, http: httpx.Client | None = None) -> dict:
    if kind not in _HEADERS:
        return {"ok": False, "error": f"unknown kind {kind!r} — use 'digest' or 'alert'"}
    if not webhook_url:
        return {"ok": False, "error": "SLACK_WEBHOOK_URL not configured"}
    body, truncated = truncate(f"{_HEADERS[kind]}\n{text}")
    client = http or httpx.Client(timeout=15)
    try:
        resp = client.post(webhook_url, json={"text": body})
        if resp.status_code >= 300:
            log.warning("slack_post_failed", status=resp.status_code)
            return {"ok": False, "error": f"slack post failed: HTTP {resp.status_code}"}
        return {"ok": True, "chars": len(body), "truncated": truncated}
    except httpx.HTTPError as exc:
        # never include the webhook URL — it is a secret
        log.warning("slack_post_failed", error=type(exc).__name__)
        return {"ok": False, "error": f"slack post failed: {type(exc).__name__}"}
    finally:
        if http is None:
            client.close()
```

- [ ] **Step 4: Run to verify pass**, then full suite.

- [ ] **Step 5: Commit** — `git commit -am "feat: slack webhook module — bounded, never-fatal posting"`

---

### Task 11: Investigate module

**Files:**
- Create: `sherlock/investigate.py`
- Test: `tests/test_investigate.py`

**Interfaces:**
- Consumes: `client.get_bill` (client.py:53), `cache.upsert_bill` (Task 8), `reader.get_bills_for_session/get_bill_counts_for_session/get_recent_actions` (Task 5), matchers (Task 4).
- Produces: `investigate(state, session_id: int, number: str, client, cache, replica_conn, budget_limit=30000) -> dict` — the deep-dive evidence pack behind the `investigate_bill` tool:

```python
{"state": "CA", "session_id": 2172, "number_norm": "AB123", "source": "live"|"cache",
 "legiscan": {"bill_id", "number", "title"(≤300), "status", "status_date",
              "last_action_date", "n_sponsors", "n_actions", "n_texts", "n_votes",
              "recent_actions": [≤5 {"date", "action"(≤120)}]},
 "quorum": {"bill_id", "label", "general_status", "current_status_date",
            "most_recent_action_date", "introduced_date", "missing_data",
            "last_quorum_update", "source",
            "counts": {"actions", "texts", "sponsors", "votes"},
            "recent_actions": [≤5 {"date", "action_type"}]} | None,
 "quorum_session_id": int | None, "notes": [str], "calls_this_month": int}
```

Flow: normalize `number` with `legiscan_number_norm(state, …)`; find the bill in `cache.bills_for_session(session_id)` by normalized match (miss → `{"error": "bill not in cache — run legiscan_sync for STATE first"}`, no API call); if `cache.calls_this_month() < budget_limit` → live `client.get_bill(bill_id)` + `cache.upsert_bill(session_id, payload)` (`source="live"`; a `LegiScanError` falls back to cache with a note), else cached payload + note `"quota exhausted — served from cache"`; Quorum side: `match_sessions(cache.get_sessions(state), reader.get_current_sessions(conn, state))`, find the matched pair for `session_id`, look up the bill by `quorum_number_norm` among `get_bills_for_session` (missing pair or missing bill → `quorum: None` + note — that's the missing_bill confirmation path). LegiScan recent actions = last 5 of `payload["history"]` sorted by date desc, each action string cut to 120 chars.

- [ ] **Step 1: Write the failing tests** — key cases (use the sqlite fake-replica pattern from test_quorum_reader and a fake client with a `calls` recorder):

```python
def test_live_getbill_refreshes_cache():
    # cache has AB1 stub in session 5; fake get_bill returns full payload
    result = investigate("CA", 5, "AB 1", fake_client, cache, replica)
    assert result["source"] == "live" and fake_client.calls == ["getBill"]
    assert cache.get_bill_payload(1)["title"].startswith("T")


def test_quota_exhausted_serves_cache():
    for _ in range(10):
        cache.add_call("x")
    result = investigate("CA", 5, "AB1", fake_client, cache, replica, budget_limit=10)
    assert result["source"] == "cache" and fake_client.calls == []
    assert any("quota" in n for n in result["notes"])


def test_bill_not_in_cache_is_error_without_api_call():
    result = investigate("CA", 5, "XYZ9", fake_client, cache, replica)
    assert "error" in result and fake_client.calls == []


def test_quorum_missing_bill_confirmation_path():
    # replica session matches but has no AB1 row
    result = investigate("CA", 5, "AB1", fake_client, cache, replica)
    assert result["quorum"] is None and result["quorum_session_id"] is not None


def test_output_bounded():
    # payload title 5000 chars, 12 history entries with 500-char actions
    result = investigate("CA", 5, "AB1", fake_client, cache, replica)
    assert len(result["legiscan"]["title"]) <= 300
    assert len(result["legiscan"]["recent_actions"]) == 5
    assert all(len(a["action"]) <= 120 for a in result["legiscan"]["recent_actions"])
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** `sherlock/investigate.py` per the flow above (~80 lines). Every early exit returns a dict with `"error"` — never raises. `LegiScanError` on the live call → note + cached payload.

- [ ] **Step 4: Run full suite** → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: investigate — targeted getBill + replica deep-dive evidence pack"`

---

### Task 12: MCP tools — scope routing, diff rename, investigate_bill, post_slack

**Files:**
- Modify: `sherlock/agent/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `parse_scope`/`ALL_REGIONS` (Task 1), `sync_state`/`sync_many` (Task 9), `diff_region`/`diff_many` (Task 7), `investigate` (Task 11), `slack.post` (Task 10), settings (Task 2).
- Produces:

```python
TOOL_NAMES = (
    "mcp__sherlock__legiscan_sync",   # {"scope": str} — "all", "CA", or "CA,TX"
    "mcp__sherlock__diff",            # {"scope": str} — RENAMED from diff_state (it is scope-wide now)
    "mcp__sherlock__list_anomalies",  # unchanged
    "mcp__sherlock__get_anomaly",     # unchanged
    "mcp__sherlock__investigate_bill",# {"state": str, "session": str, "number": str}
    "mcp__sherlock__post_slack",      # {"kind": str, "text": str}
)
```

`return_handlers=True` dict keys: `legiscan_sync, diff, list_anomalies, get_anomaly, investigate_bill, post_slack`.

- [ ] **Step 1: Write the failing tests** (extend `tests/test_agent_tools.py`; update the exact-tuple assertion)

```python
def test_tool_names_exact_six():
    assert TOOL_NAMES == (
        "mcp__sherlock__legiscan_sync", "mcp__sherlock__diff",
        "mcp__sherlock__list_anomalies", "mcp__sherlock__get_anomaly",
        "mcp__sherlock__investigate_bill", "mcp__sherlock__post_slack",
    )


async def test_sync_scope_all_routes_to_sync_many(monkeypatch, settings):
    seen = {}
    monkeypatch.setattr("sherlock.agent.tools.sync_many",
                        lambda regions, *a, **k: seen.setdefault("regions", list(regions)) or {"ok": 1})
    _, handlers = build_toolkit(settings, return_handlers=True)
    await handlers["legiscan_sync"]({"scope": "all"})
    assert len(seen["regions"]) == 51


async def test_sync_single_region_routes_to_sync_state(monkeypatch, settings):
    ...  # monkeypatch sync_state, assert called with "CA" for {"scope": "ca"}


async def test_sync_invalid_scope_is_error_payload(settings):
    _, handlers = build_toolkit(settings, return_handlers=True)
    out = await handlers["legiscan_sync"]({"scope": "XX"})
    assert "unknown region" in out["content"][0]["text"]


async def test_diff_no_dsn_error_payload(settings_no_dsn):
    _, handlers = build_toolkit(settings_no_dsn, return_handlers=True)
    out = await handlers["diff"]({"scope": "CA"})
    assert "QUORUM_REPLICA_DSN" in out["content"][0]["text"]


async def test_investigate_bill_non_integer_session(settings):
    out = await handlers["investigate_bill"]({"state": "CA", "session": "abc", "number": "AB1"})
    assert "error" in out["content"][0]["text"]


async def test_post_slack_bad_kind_and_missing_webhook(settings_no_webhook):
    out = await handlers["post_slack"]({"kind": "meme", "text": "x"})
    assert "digest" in out["content"][0]["text"]          # error names valid kinds
    out2 = await handlers["post_slack"]({"kind": "digest", "text": "x"})
    assert "not configured" in out2["content"][0]["text"]  # payload, not exception


async def test_post_slack_passes_webhook_from_settings(monkeypatch, settings_with_webhook):
    seen = {}
    monkeypatch.setattr("sherlock.agent.tools.slack",
                        types.SimpleNamespace(post=lambda url, kind, text: seen.update(url=url) or {"ok": True}))
    await handlers["post_slack"]({"kind": "digest", "text": "hi"})
    assert seen["url"] == settings_with_webhook.slack_webhook_url
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — in `build_toolkit`:
  - `legiscan_sync`: schema `{"scope": str}`; `parse_scope` → `ValueError` → `_text({"error": str(exc)})`; 1 region → `sync_state(...)`, else `sync_many(...)`; pass `budget_limit=settings.legiscan_monthly_budget`. Tool description: `"Refresh the local LegiScan cache. scope: 'all' (50 states + US federal), one region ('CA'), or a comma list ('CA,TX'). Budget-aware; read-only."`
  - `diff` (renamed): schema `{"scope": str}`; keep the M0 DSN/connect/check_schema guard chain verbatim; 1 region → `diff_region(...)`, else `diff_many(...)`; pass `sla_hours=settings.sherlock_freshness_sla_hours`. Description: `"Run all four detectors (missing_bill, incomplete_fields, stale, wrong_data) for the scope's current sessions vs the Quorum replica. Records anomalies; returns a bounded rollup with counts by gap type and region plus top cases by severity."`
  - `investigate_bill`: schema `{"state": str, "session": str, "number": str}`; `int(args["session"])` → `ValueError` → error payload `"session must be the LegiScan session_id (shown as session_key on anomalies)"`; DSN/connect guard like diff; construct `LegiScanClient` with `on_call=cache.add_call` like the sync handler; call `investigate(...)`; `_text(_bounded(result))`.
  - `post_slack`: schema `{"kind": str, "text": str}`; delegates to `slack.post(settings.slack_webhook_url, kind, text)`; returns the result dict verbatim via `_text` (module already never raises).
  - Update `TOOL_NAMES`, `sdk_tools`, `handlers` dict.

- [ ] **Step 4: Run full suite** → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: scope-aware sync/diff tools + investigate_bill + post_slack"`

---

### Task 13: Patrol — M1 doctrine, preflight, scope, stats, digest footer

**Files:**
- Modify: `sherlock/agent/patrol.py`
- Test: `tests/test_patrol.py`

**Interfaces:**
- Consumes: `slack.post` (Task 10), `reader.connect/check_schema`, TOOL_NAMES (6, Task 12), `parse_scope` (Task 1).
- Produces: `run_patrol(settings, scope: str, objective="") -> str` (scope replaces state; `"CA"` still works so existing callers/tests survive); `PatrolFatalError(RuntimeError)`; patrol stats gain `num_turns, duration_ms, total_cost_usd, usage, legiscan_calls_month`.

- [ ] **Step 1: Write the failing tests** (extend `tests/test_patrol.py`, following its fake-`query` monkeypatch pattern)

```python
def test_doctrine_names_all_six_tools():
    for name in ("legiscan_sync", "diff", "list_anomalies", "get_anomaly",
                 "investigate_bill", "post_slack"):
        assert name in DOCTRINE
    assert "Never invent" in DOCTRINE


def test_options_allow_exactly_six_tools(settings):
    options = build_options(settings, server=object())
    assert len(options.allowed_tools) == 6


async def test_preflight_failure_posts_alert_and_raises(monkeypatch, settings_with_dsn):
    posts = []
    monkeypatch.setattr("sherlock.agent.patrol.slack",
                        types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": True}))
    monkeypatch.setattr("sherlock.agent.patrol.reader",
                        types.SimpleNamespace(connect=_raise_oserror))
    with pytest.raises(PatrolFatalError):
        await run_patrol(settings_with_dsn, "all")
    assert posts and posts[0][1] == "alert"


async def test_no_dsn_skips_preflight_and_runs(monkeypatch, settings):
    # existing happy-path test still passes with scope="CA"; prompt contains scope
    ...
    assert "Patrol scope: CA" in captured_prompt


async def test_result_stats_recorded(monkeypatch, settings, tmp_path):
    # fake ResultMessage with num_turns=7, total_cost_usd=None, usage={...}
    await run_patrol(settings, "CA")
    stats = json.loads(latest_patrol_row["stats_json"])
    assert stats["num_turns"] == 7 and stats["total_cost_usd"] is None


async def test_footer_digest_posted_when_webhook_set(monkeypatch, settings_with_webhook):
    posts = []
    ...
    await run_patrol(settings_with_webhook, "CA")
    kinds = [p[1] for p in posts]
    assert "digest" in kinds        # deterministic stats footer
    # and a footer post failure must not raise (post returns {"ok": False})


async def test_midstream_error_posts_alert_and_finishes_row(monkeypatch, settings_with_webhook):
    # extends the existing mid-stream-error test: alert posted, patrols row finished, re-raised
    ...
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

New `DOCTRINE` (full replacement):

```python
DOCTRINE = """You are Sherlock, a data-integrity detective auditing Quorum's legislative \
database against LegiScan across all 50 US states plus Congress (region code "US").

Patrol procedure (M1 — read-only shadow mode):
1. Sync: call legiscan_sync with the patrol scope (usually {"scope": "all"}). It is \
quota-aware and deterministic; note degraded or errored regions for the digest.
2. Diff: call diff with the same scope. It runs every detector across the scope and \
returns a rollup: counts by gap type and region (new vs recurring), top cases by \
severity, session-match warnings, and per-region errors.
3. Triage: pick the most significant cases (at most ~8) using list_anomalies and \
get_anomaly. Severity guide: P1 = missing bill in an active session with recent \
LegiScan activity; P2 = significant or clustered gaps; P3 = isolated single-bill \
gaps; P4 = cosmetic.
4. Investigate: call investigate_bill(state, session, number) — session is the \
LegiScan session_id shown as session_key on the anomaly — for at most 5 cases where \
the recorded evidence is ambiguous. Each live call spends LegiScan quota; do not \
investigate what the diff evidence already explains.
5. Digest: call post_slack with kind "digest" and one message containing: scope \
covered; anomaly counts by gap type and by region (new vs recurring); up to 5 notable \
cases, each with region, bill number, and your one-line diagnosis; degraded or \
errored regions; LegiScan calls_this_month from the sync output. Keep it under 3500 \
characters — overflow is truncated automatically.
6. Report: finish with a full markdown patrol report: everything in the digest plus \
session-match warnings, cluster diagnoses, and recommended next steps.

Triage rules:
- Session-match warnings usually mean false positives downstream — say so prominently \
and downgrade the affected cases.
- Many anomalies sharing one region and session are usually one root cause (session \
mismatch, prefix quirk, ingestion gap). Diagnose the cluster, not each bill.
- LegiScan is a recall oracle only: Quorum being ahead of LegiScan is never an anomaly.

Rules:
- Never invent data. Every claim in your digest and report must trace to a tool result.
- If a tool returns an error payload, report it and continue with what you have.
- If sync reports degraded regions, work from cached data and say so in the digest.
- If post_slack returns ok=false, note the delivery failure in your report and continue.
- You have no write tools. You observe and report."""
```

`run_patrol` changes:

```python
class PatrolFatalError(RuntimeError):
    """Replica unreachable / schema drift — patrol must not start (spec §12)."""


async def run_patrol(settings: Settings, scope: str, objective: str = "") -> str:
    settings.ensure_dirs()

    if settings.quorum_replica_dsn:
        try:
            conn = reader.connect(settings.quorum_replica_dsn)
        except Exception as exc:
            msg = f"replica unreachable: {type(exc).__name__}"
            slack.post(settings.slack_webhook_url, "alert", f"Patrol aborted — {msg}")
            raise PatrolFatalError(msg) from None
        try:
            ok, err = reader.check_schema(conn)
        finally:
            conn.close()
        if not ok:
            msg = f"replica schema drift: {err}"
            slack.post(settings.slack_webhook_url, "alert", f"Patrol aborted — {msg}")
            raise PatrolFatalError(msg)
    # DSN unset: dev flow — log and continue; the diff tool reports its own error payload.

    server, _ = build_toolkit(settings)
    options = build_options(settings, server)

    with CaseFileStore(settings.data_dir / "casefile.db") as casefile:
        patrol_id = casefile.start_patrol(scope=scope)
        transcript_path = settings.runs_dir / f"patrol-{patrol_id}.jsonl"
        prompt = f"Patrol scope: {scope}." + (f" Objective: {objective}" if objective else "")

        result_text = ""
        result_msg = None
        error: str | None = None
        try:
            with open(transcript_path, "w") as fh:
                async for msg in query(prompt=prompt, options=options):
                    write_transcript_line(fh, msg)
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
                        result_text = msg.result or ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            slack.post(settings.slack_webhook_url, "alert",
                       f"Patrol {patrol_id} ({scope}) FAILED: {error}")
            raise
        finally:
            with LegiScanCache(settings.data_dir / "cache.db") as cache:
                ls_calls = cache.calls_this_month()
            stats: dict = {"result_chars": len(result_text),
                           "legiscan_calls_month": ls_calls}
            if result_msg is not None:
                stats.update(num_turns=result_msg.num_turns,
                             duration_ms=result_msg.duration_ms,
                             total_cost_usd=result_msg.total_cost_usd,
                             usage=result_msg.usage)
            if error is not None:
                stats["error"] = error
            casefile.finish_patrol(patrol_id, stats, str(transcript_path))

    # Deterministic stats footer (spec §11 "quota + token spend"): the agent
    # finishes before ResultMessage exists, so this line comes from code. Also
    # doubles as heartbeat if the agent skipped its digest. Never fatal.
    if result_msg is not None:
        cost = (f"${result_msg.total_cost_usd:.2f}" if result_msg.total_cost_usd
                else "n/a (subscription)")
        slack.post(settings.slack_webhook_url, "digest",
                   f"Patrol {patrol_id} ({scope}) done: {result_msg.num_turns} turns, "
                   f"{(result_msg.duration_ms or 0) // 60000}m, cost {cost}, "
                   f"LegiScan {ls_calls}/{settings.legiscan_monthly_budget} this month.")
    return result_text
```

(imports gain `from sherlock import slack`, `from sherlock.legiscan.cache import LegiScanCache`, `from sherlock.quorum import reader`.)

- [ ] **Step 4: Run full suite** → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: M1 patrol — preflight alerts, scope, doctrine, stats footer"`

---

### Task 14: CLI — --scope everywhere

**Files:**
- Modify: `sherlock/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `sync`/`diff`/`patrol` all take `--scope` (default `"all"`) with `--state` kept as a deprecated alias (prints a note, overrides scope when given — preserves M0 muscle memory and progress-ledger smoke commands). `patrol` exits 2 on `PatrolFatalError`. Invalid scope → `typer.BadParameter` naming the bad codes.

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_scope_list_test_mode(runner, fake_env):
    result = runner.invoke(app, ["sync", "--scope", "CA,TX"],
                           env={"SHERLOCK_TEST_MODE": "1", **fake_env})
    assert result.exit_code == 0 and '"synced"' in result.output


def test_sync_state_alias_deprecation(runner, fake_env):
    result = runner.invoke(app, ["sync", "--state", "CA"],
                           env={"SHERLOCK_TEST_MODE": "1", **fake_env})
    assert result.exit_code == 0 and "deprecated" in result.output.lower()


def test_sync_invalid_scope_names_code(runner, fake_env):
    result = runner.invoke(app, ["sync", "--scope", "XX"], env=fake_env)
    assert result.exit_code != 0 and "XX" in result.output


def test_patrol_fatal_exits_2(runner, fake_env, monkeypatch):
    async def boom(*a, **k):
        raise PatrolFatalError("replica unreachable: OSError")
    monkeypatch.setattr("sherlock.cli.run_patrol", boom)
    result = runner.invoke(app, ["patrol", "--scope", "CA"], env=fake_env)
    assert result.exit_code == 2 and "fatal" in result.output
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — pattern for all three commands:

```python
def _resolve_scope(scope: str, state: str) -> list[str]:
    if state:
        typer.echo("note: --state is deprecated; use --scope")
        scope = state
    try:
        return parse_scope(scope)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def sync(scope: str = typer.Option("all", "--scope", help='"all", "CA", or "CA,TX"'),
         state: str = typer.Option("", "--state", help="deprecated alias for --scope")) -> None:
    """Refresh the LegiScan cache for SCOPE."""
    regions = _resolve_scope(scope, state)
    s = _settings()
    with LegiScanCache(s.data_dir / "cache.db") as cache:
        client = (_NoNetworkClient() if os.environ.get("SHERLOCK_TEST_MODE") == "1"
                  else LegiScanClient(s.legiscan_api_key, on_call=lambda op: cache.add_call(op)))
        try:
            if len(regions) == 1:
                stats = sync_state(regions[0], client, cache,
                                   budget_limit=s.legiscan_monthly_budget)
            else:
                stats = sync_many(regions, client, cache,
                                  budget_limit=s.legiscan_monthly_budget)
        finally:
            client.close()
    typer.echo(json.dumps(stats, indent=2))
```

`diff` mirrors it (same DSN/exit-code guard chain as M0; single → `diff_region`, multi → `diff_many`; pass `sla_hours=s.sherlock_freshness_sla_hours`). `patrol`:

```python
@app.command()
def patrol(scope: str = typer.Option("all", "--scope"),
           state: str = typer.Option("", "--state", help="deprecated alias for --scope"),
           objective: str = typer.Option("", "--objective")) -> None:
    """Run a full agentic patrol over SCOPE (calls the Anthropic API)."""
    _resolve_scope(scope, state)  # early validation
    if state:
        scope = state.upper()
    s = _settings()
    try:
        report = asyncio.run(run_patrol(s, scope, objective))
    except PatrolFatalError as exc:
        typer.echo(f"fatal: {exc}")   # alert already posted inside run_patrol
        raise typer.Exit(code=2)
    typer.echo(report)
```

`_NoNetworkClient` note: `sync_many` loops `sync_state`, which works with it unchanged. Ensure `test_cli.py`'s existing `sync` test (uses `--state CA`) is updated or still passes via the alias.

- [ ] **Step 4: Run full suite** → all green.

- [ ] **Step 5: Commit** — `git commit -am "feat: CLI --scope for sync/diff/patrol with deprecated --state alias"`

---

### Task 15: launchd plist + README

**Files:**
- Create: `deploy/us.quorum.sherlock.plist`
- Modify: `README.md`
- Test: `tests/test_deploy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy.py
import plistlib
from pathlib import Path

PLIST = Path(__file__).parent.parent / "deploy" / "us.quorum.sherlock.plist"


def test_plist_parses_with_expected_schedule():
    with PLIST.open("rb") as fh:
        d = plistlib.load(fh)
    assert d["Label"] == "us.quorum.sherlock"
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert "patrol --scope all" in " ".join(d["ProgramArguments"])
    assert d["RunAtLoad"] is False
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Create the plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>us.quorum.sherlock</string>

    <!-- EDIT: replace __REPO__ with the absolute repo path. WorkingDirectory is
         load-bearing: sherlock reads .env, data/, and runs/ relative to it. -->
    <key>WorkingDirectory</key>
    <string>__REPO__</string>

    <!-- zsh -lc loads the login profile so python3/uv/tsh are on PATH.
         uv is module-installed on this host: python3 -m uv. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>exec python3 -m uv run sherlock patrol --scope all</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <!-- EDIT: replace __HOME__ (launchd does not expand ~).
         mkdir -p ~/Library/Logs/sherlock first. -->
    <key>StandardOutPath</key>
    <string>__HOME__/Library/Logs/sherlock/patrol.log</string>
    <key>StandardErrorPath</key>
    <string>__HOME__/Library/Logs/sherlock/patrol.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Add README section**

```markdown
## Daily patrol (launchd)

    mkdir -p ~/Library/Logs/sherlock
    cp deploy/us.quorum.sherlock.plist ~/Library/LaunchAgents/
    $EDITOR ~/Library/LaunchAgents/us.quorum.sherlock.plist   # replace __REPO__ and __HOME__
    plutil -lint ~/Library/LaunchAgents/us.quorum.sherlock.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/us.quorum.sherlock.plist
    launchctl kickstart -k gui/$(id -u)/us.quorum.sherlock    # smoke-run now
    # unload after edits: launchctl bootout gui/$(id -u)/us.quorum.sherlock

Prereqs: `.env` must have `SLACK_WEBHOOK_URL` and `QUORUM_REPLICA_DSN`, and the
`tsh proxy db` tunnel must be up (a dead tunnel produces a Slack alert + exit 2 —
that is the intended failure mode; the daily digest doubles as the liveness
heartbeat). Before the first scheduled run, pre-warm the cache once:

    python3 -m uv run sherlock sync --scope all

The first full sync ingests ~85 dataset ZIPs and takes a while; subsequent daily
runs are ~85 cheap masterlist calls.
```

- [ ] **Step 5: Run full suite, commit** — `git commit -am "feat: launchd plist + install docs for daily 07:00 patrol"`

---

### Task 16: Full gate, live smoke, ledger

**Files:**
- Modify: `.superpowers/sdd/progress.md`, `.env` (Victor adds secrets — not committed)

- [ ] **Step 1: Full quality gate**

```bash
python3 -m uv run pytest -q      # expect ~110+ tests, 0 failures, 0 warnings
git status                        # clean tree
```

- [ ] **Step 2: Live smoke (needs Victor: `tsh login`, `tsh proxy db`, SLACK_WEBHOOK_URL in .env)**

```bash
# 1. Pre-warm sync — watch quota (expect ~140-190 calls on first full run)
python3 -m uv run sherlock sync --scope all
# 2. Single-state regression vs M0 baseline (CA: expect the known X1 family, corpus otherwise clean)
python3 -m uv run sherlock diff --scope CA
# 3. Full diff
python3 -m uv run sherlock diff --scope all
# 4. Slack round-trip (one-off python: sherlock.slack.post(url, "digest", "M1 smoke")) → message appears in #quentin-bot
#    (SLACK_WEBHOOK_URL must be a webhook bound to #quentin-bot — Victor creates/provides it)
# 5. Full patrol — digest lands in #quentin-bot; footer line appears; transcript in runs/
python3 -m uv run sherlock patrol --scope all
# 6. launchd: install per README, launchctl kickstart, verify log files + digest
```

Expected new-signal review: the three new detectors will fire on real data for the first time — eyeball the top cases with Victor before calling M1 done; wholesale noise from one detector means its threshold/mapping needs a look (capture FP families as M2 eval cases, don't tune here).

- [ ] **Step 3: Update `.superpowers/sdd/progress.md`** with M1 completion status, smoke results, and any FP families captured for M2.

- [ ] **Step 4: Merge decision** — use superpowers:finishing-a-development-branch (merge `feat/m1-shadow-patrols` → `main`, push pending Victor's standing decision).

---

## Verification

1. **Unit gate:** `python3 -m uv run pytest` green with 0 warnings after every task (the suite grows from 44 to ~110+ tests).
2. **Golden guards that must exist and pass:** Quorum-side-never-translated (`H.R. 24` → `HR24`), MA order suppression, LS-Failed-never-compared, stale-beats-wrong_data precedence, Quorum-ahead-never-flagged.
3. **Live end-to-end (Task 16):** full sync within quota; CA diff reproduces the M0 baseline (X1 family only); `#quentin-bot` receives both the agent digest and the deterministic stats footer; launchd kickstart produces a complete patrol with transcript + patrols row.
4. **Budget check after smoke:** `calls_this_month` consistent with the ~3-4.6k/month projection; degrade path untested in prod (covered by unit tests).

## Self-review notes

- Spec coverage: all four detectors (§1) ✓, batch tools within turn budget (§4/§5) ✓, quota discipline + TTLs (§6) ✓, reader columns/counts (§7) ✓, matching incl. federal + carryover-by-session-match (§8) ✓, doctrine (§9) ✓, digest content + heartbeat (§11) ✓, error handling table (§12) ✓, env + launchd (§13) ✓, test style (§14) ✓. Out of M1 scope, deliberately: `sherlock report` CLI (spec §11 mentions it; not in the M1 milestone line — YAGNI until M2's digest quality pass), FP suppression/X1 family (M2), fix tools (deprioritized per M5 decision), cross-session carryover matching beyond current-session reconciliation (flagged as an M2 eval case already).
- Type consistency: `upsert_anomaly` returns `"created"` — consumed in Task 7 (`record()`), asserted in Tasks 3/7 tests. `diff_many` rollup keys consumed verbatim by the `diff` tool (Task 12) and doctrine (Task 13). `investigate(state, session_id: int, number, client, cache, conn, budget_limit)` matches the Task 12 handler's `int(args["session"])` conversion.
- Known accepted risks: first-ever agent-invoked `legiscan_sync {"scope":"all"}` could be slow inside one MCP call — mitigated by the documented CLI pre-warm; if it still bites, set `MCP_TOOL_TIMEOUT` in the SDK env (verify against installed CLI). Blocking httpx inside async tool handlers is a pre-existing M0 pattern, fine single-agent.
