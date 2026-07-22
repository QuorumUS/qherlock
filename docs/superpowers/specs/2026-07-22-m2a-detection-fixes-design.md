# M2a — Detection-correctness fixes + eval fixtures

**Date:** 2026-07-22
**Status:** approved (Victor, 2026-07-22; approach "targeted code fixes", no declarative rules layer)
**Parent:** M2 in `2026-07-20-qherlock-design.md` §15, split 2026-07-22 into M2a (this spec) and M2b (doctrine/taxonomy, separate spec later).

## Why

The first full live patrol (2026-07-22) reported 4,292 anomalies. Controller
verification against the replica showed the bulk are detector defects, not
data gaps:

| Family | Size | Verified root cause |
|---|---|---|
| NY "missing" bills | 3,481 | 3,480 exist in Quorum with amendment-suffix labels (`S.115A`); normalizer keeps the suffix, LegiScan reports base numbers. Only `A33878` (status 0, no dates) is genuinely absent. |
| WI + OH status wrong_data | 55 + ~230 | All joint resolutions. `LEGISCAN_MIN_RANK[4] = 6` demands "enacted" for LegiScan "Passed", but resolutions are *adopted*, never enacted — Quorum's rank 4 is correct. |
| MA "missing" Extension Orders | 8 | Procedural committee extension orders; `is_deliberately_unimported` exists but did not suppress them (wiring or prefix-list gap — diagnose first). |
| CA ABX family | 20 missing + 29 wrong_data | LegiScan folds extraordinary-session bills (X-prefix) into the biennium dataset; Quorum keeps a separate special session — 1:1 session matching never finds them. Known since M0. |

Goal: the same patrol reports the genuine residual (~a few hundred, mostly
incomplete_fields lag) and retires the recorded noise. Nei's independent ask
("make it smaller", 2026-07-22) is served by the same work plus §7.

Non-goals (M2b or later): FP taxonomy/classification schema, doctrine
overhaul beyond §7's digest instruction, declarative suppression rules,
Slack surface changes (M2.5), the 45 unmapped 2026 sessions (correctly
warned-and-skipped today; presentation is M2b's call).

## 1. NY amendment-suffix normalization (`qherlock/diff/matchers.py`; collision guard in `qherlock/diff/service.py`)

- New constant `AMENDMENT_SUFFIX_STATES: frozenset[str] = frozenset({"NY"})`.
- `quorum_number_norm(label, number, bill_type=None, state=None)` gains the
  `state` param (every caller has region context). When
  `state in AMENDMENT_SUFFIX_STATES` and the normalized label matches
  `^([A-Z]+)(\d+)([A-Z])$`, drop the trailing letter: `S.115A` → `S115`.
- **Collision guard:** the bill-map builder (service layer) must detect two
  Quorum rows normalizing to the same key in one session. On collision, keep
  the first, count it in the region's `warnings`, and do NOT report either as
  missing. Verified against NY live data (amended bills replace their base
  row) so the guard should never fire — but a silent merge would fabricate
  missing-bill FPs elsewhere, so it must be observable.
- LegiScan side unchanged (`S00115` already normalizes to `S115`).

## 2. Resolution-aware status ranks (`qherlock/diff/detectors.py`)

- New constant `RESOLUTION_PREFIXES: frozenset[str]` =
  `{"HR", "SR", "AR", "JR", "HJR", "SJR", "AJR", "HCR", "SCR", "ACR", "SJRCA", "HJRCA"}`.
- Resolution detection uses the **raw LegiScan number prefix, before
  per-state prefix translation** (after translation US "HR" means a House
  *bill* — using the translated prefix would misclassify all of Congress).
- For resolutions only, `LEGISCAN_MIN_RANK` lookup for LegiScan status 4
  ("Passed") yields 4 (passed_second/adopted) instead of 6 (enacted). All
  other statuses unchanged.

## 3. MA order-rule completion (`qherlock/diff/matchers.py` + detector wiring)

- Step 1 is diagnosis: determine whether the missing_bill detector fails to
  call `is_deliberately_unimported`, or `IGNORED_TITLE_PREFIXES["MA"]` simply
  lacks the extension-order title prefix. The 8 live MA cases (casefile ids
  under region MA, gap_type missing_bill) are the ground truth.
- Fix accordingly and extend the prefix tuple from the 8 real titles.
  Suppressed cases count into the existing `ignored` rollup field.

## 4. CA ABX cross-session matching (`qherlock/diff/matchers.py`)

Riskiest item — if planning reveals it needs structural change to
`match_sessions`, it splits into its own plan rather than blocking 1-3.

- When a LegiScan bill number carries an extraordinary-session marker
  (`ABX…`/`SBX…` pattern: prefix ends in `X` + session ordinal), the bill is
  looked up in a **session group**: the 1:1-matched Quorum session plus
  sibling Quorum sessions of the same region and biennium (same
  `start_year`/overlapping years, `regular_session = false`).
- A bill found in a sibling session is not missing; field comparisons run
  against the row actually found.
- The 29 CA wrong_data cases are expected to be the same confusion (compared
  against the wrong session's row); acceptance is that the whole CA 2172
  family (49 cases + the 1 stale if implicated) disappears without
  suppressing genuine CA gaps in the fixtures.

## 5. Anomaly auto-retirement (`qherlock/diff/service.py` + `qherlock/casefiles/store.py`)

- After diffing a (region, session), compute the set of live fingerprints.
  Recorded anomalies for that exact (region, session_key) whose fingerprint
  is absent from the live set and whose status is `new` flip to
  `status='resolved'` with a `resolved_at` timestamp (new column, additive
  `ALTER TABLE`-safe migration in the store's init).
- Scope safety: only sessions actually diffed in the current run may retire
  their anomalies. A `--scope CA` run must not touch NY rows. A session that
  errors mid-diff retires nothing.
- Reopen: if a resolved fingerprint reappears, the existing upsert marks it
  recurring and status returns to `new` (evidence updated).
- Rollup and digest gain a `resolved` count per run. First post-fix patrol is
  expected to retire ~3,800 rows — visible, deliberate, logged.

## 6. Eval fixtures (`tests/evals/`)

- Committed JSON snapshots, recorded from the live 2026-07-22 data (approved:
  public legislative data, org-internal repo): `ny_amendment_suffix.json`
  (≥50 suffixed pairs + `A33878`), `wi_oh_resolutions.json` (the WI 55 +
  an OH sample), `ma_extension_orders.json` (all 8), `ca_abx.json` (the
  full 2172 family + sibling-session rows).
- Each fixture holds both sides (LegiScan masterlist entries, Quorum rows) in
  the shapes the detectors consume — evals run the **real detector/matcher
  code**, no mocks.
- Assertions, per family: zero anomalies from the FP family, AND the planted
  genuine cases still fire (`A33878` missing_bill; the federal S3051 stale
  case as a synthetic fixture; one synthetic genuine wrong_data resolution
  kept below adopted rank). Over-suppression is a test failure, same as
  under-suppression.

## 7. Digest slimming (doctrine text only, `qherlock/agent/patrol.py`)

- DOCTRINE digest instruction changes: target ≤1,000 characters — one-line
  rollup (totals by gap type: new / recurring / resolved), top 3 case
  families with one-line diagnoses, degraded/errored regions only when
  non-empty, LegiScan budget line. Everything else belongs in the full patrol
  report, not Slack.
- No formatting machinery (threads, Block Kit, mentions): the Slack surface
  is being unified by Nei in actacollecta (M2.5 in the roadmap); Qherlock
  adopts that surface when it lands.

## Acceptance

1. Full suite + new evals green; evals runnable offline (no tunnel, no
   LegiScan key).
2. Live `diff --scope all` (tunnel up): NY, WI, OH-status4, MA, CA-2172
   families produce zero new anomalies; previously recorded family rows are
   `resolved`; residual new+`new`-status anomalies ≈ low hundreds.
3. Live patrol digest ≤1,000 chars and leads with the genuine residual.

## Out of scope

- FP taxonomy schema / `classification` column semantics (M2b).
- The 45 unmapped 2026 sessions (M2b presentation decision).
- Slack identity, threading, Quentin mention (M2.5).
- 429 Retry-After retry and mrkdwn escaping (small backlog; ride along with
  M2b unless trivial to slot into a plan task here).
