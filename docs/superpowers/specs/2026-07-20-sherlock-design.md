# Sherlock — Design Spec

**Date:** 2026-07-20 · **Author:** Victor Moraes + Claude · **Status:** Approved (architecture B)

## 1. Mission

Sherlock is a standalone agentic auditor that continuously compares LegiScan against Quorum's
production data for **US federal + all 50 states** (current sessions, including carryover),
detects four gap types, diagnoses each anomaly, and closes the loop — fixing data through
Quorum's own pipelines where possible, alerting humans where not. All output goes to the
`#sherlock-bot` Slack channel until trust is established.

| Gap type | Definition |
|---|---|
| `missing_bill` | LegiScan has a bill absent from Quorum for the matched session |
| `incomplete_fields` | Bill exists in Quorum but has 0 sponsors/actions/texts/votes where LegiScan has ≥1 |
| `stale` | LegiScan's last action is newer than Quorum's `most_recent_action_date` beyond the SLA grace window (default 72h) |
| `wrong_data` | Both sides are equally fresh (last-action dates within the SLA window) yet status disagrees. v1 checks status only — date lag is `stale`, never `wrong_data` |

**Trust model** — same code, one switch: shadow mode (detect + diagnose + report, no writes)
→ auto-fix (behind `SHERLOCK_LIVE=1`, dry-run by default, hard caps, kill switch always armed).

## 2. Context & prior art

quorum-site contains a dead LegiScan checker (`app/management/scraper/legiscan/`): states-only,
existence-only (free-tier `getMasterList`), alert-only, ~99% false positives, correction manual
per its ADR-0001. Sherlock is a standalone restart of that idea. Salvage as reference (read, do
not import):

- `app/management/scraper/legiscan/comparison.py` — bill-number normalization, cross-session
  matching, `LEGISCAN_PREFIX_MAP`, ignored-title-prefix rules.
- `app/management/scraper/STEERING.md` §10 — the false-positive taxonomy (session-selection
  errors dominate). Becomes Sherlock's triage doctrine.
- `docs/superpowers/specs/2026-04-08-legiscan-session-fallback-design.md` (in quorum-site) —
  session fallback / `has_no_bills` lessons.
- `app/bill/models.py` — schema source of truth for the replica reader (`Bill` ~L3577,
  `BillAction`, `BillText`, sponsors M2M, `LegSession` in `app/models.py` ~L17062,
  `Region` enum ~L633, `Bill.missing_data` boolean ~L3948).

## 3. Decision record (2026-07-20)

| Decision | Choice | Notes |
|---|---|---|
| Gap types | All four | |
| Read path | Read-only SQL on prod replica via Teleport | Sherlock never holds DB write credentials |
| Fix path | Fallback chain: re-ingestion → ORM template script → rollback + alert | Every action logged |
| Fix channel | Teleport/SSH exec running `manage.py` on a prod node | |
| Slack | Everything → `#sherlock-bot` (exists) | Escalation to team channels deferred |
| Home | Standalone repo `~/Projects/sherlock` | In-repo module is dead; this is the restart |
| LegiScan tier | Free (30k queries/month) | Bulk `getDataset` strategy mandatory |
| First runtime | cron/launchd on Victor's laptop | Containerize later |
| **Architecture** | **B — full agentic patroller** | Victor chose B over recommended A, accepting run-to-run variance and token cost. Mitigations: deterministic bulk tools, bounded tool outputs, persisted patrol transcripts, tool-level guardrails. |

## 4. Architecture

Sherlock is one Claude Agent SDK loop. **Claude is the control flow**: it decides where to
patrol, what to investigate, what is real, whether to fix or alert, and what to report. The
heavy machinery (bulk sync, mechanical diff, guarded writes) lives inside deterministic,
individually-tested tools — an LLM cannot and should not stream a 150k-bill corpus.

```
              ┌──────────── SHERLOCK (Claude Agent SDK loop) ────────────┐
 cron fires   │  Persona + patrol doctrine + FP taxonomy + safety rules  │
 `sherlock    │  decides: where to patrol → what's anomalous → what's    │
  patrol` ──▶ │  real → fix or alert → what to report                    │
              └──┬───────┬─────────┬──────────┬──────────┬───────────────┘
                 ▼       ▼         ▼          ▼          ▼
            legiscan_  diff_    investigate  trigger_   run_fix_      post_
            sync     state/fed  _bill        rescrape   template      slack
                 │       │         │          └────┬─────┘             │
              LegiScan  SQLite   both-side      Teleport exec       #sherlock-bot
              API+cache diff     deep dive      manage.py           webhook
                          ▼
              CASE FILES (SQLite) ◀── every anomaly, action, snapshot, transcript
```

**Safety invariant:** guardrails are code-level properties of the tools (allowlists, caps,
transactions, dry-run, kill switch). The agent chooses actions; the tools bound them. No
prompt-level rule is load-bearing for safety.

## 5. Tool contracts

All tools return bounded, structured summaries (counts, IDs, top-N digests). Full payloads stay
in the case DB, retrievable by ID. No tool output may exceed ~2k tokens.

| Tool | Purpose | Contract highlights |
|---|---|---|
| `legiscan_sync(scope?)` | Refresh local LegiScan cache | Weekly `getDatasetList`→`getDataset` per changed session; daily `getMasterListRaw` change-hashes; targeted `getBill` only where LegiScan changed AND Quorum looks stale. Enforces API budget internally; returns per-state sync stats + quota used. |
| `diff_state(state)` / `diff_federal()` | Run all four detectors for a region's current sessions | Upserts anomalies (dedup by fingerprint), returns counts by gap type + top cases by severity heuristic. |
| `list_anomalies(filters)` | Query case files | Filters: state, gap_type, status, min_severity, since. Paged. |
| `get_anomaly(id)` | Full evidence for one anomaly | Both-side values, history, prior actions. |
| `investigate_bill(state, session, number)` | Deep-dive one bill | Targeted LegiScan `getBill` + full replica row detail; returns side-by-side evidence pack. |
| `trigger_rescrape(region, session, bill_numbers?)` | Fix leg 1 | Teleport exec of the appropriate scoped `manage.py scrape` invocation (actacollecta replay path for those states). Refuses if kill switch or caps. Returns command, exit code, log tail. |
| `run_fix_template(template_id, params, anomaly_id)` | Fix leg 2 | Allowlisted template IDs only. Renders Jinja→Python, executes via `manage.py shell` (stdin over `tsh ssh`), wrapped in `transaction.atomic()`; snapshots before-values to case DB; verifies in-script; JSON result markers parsed. Honors dry-run (`SHERLOCK_LIVE=0` ⇒ always dry-run), kill switch, per-cycle/per-state caps. |
| `verify_fix(anomaly_id)` | Post-action check | Re-reads replica + LegiScan; sets anomaly `verified`/`regressed`. Failure ⇒ compensating rollback from snapshots + alert. |
| `post_slack(kind, payload)` | Digest or alert to `#sherlock-bot` | Via webhook. Failures are logged, never fatal. Message ≤ ~3500 chars; overflow summarized with case-file pointers. |

## 6. LegiScan collector (inside `legiscan_sync`)

Free-tier discipline (30k/month cap):

- `getSessionList` per state on first run / monthly — session inventory.
- `getDatasetList` weekly (~1 call/state) — `dataset_hash` change detection; `getDataset` only
  for changed sessions. Dataset ZIPs carry **complete per-bill JSON** (sponsors, full action
  history, votes, texts metadata) at one call per session.
- `getMasterListRaw` daily (~51 calls) — per-bill `change_hash` freshness between datasets.
- `getBill` targeted — only for bills in the divergence intersection (LegiScan hash changed AND
  Quorum did not follow within SLA). Expected total: **~3–6k calls/month**. Quota tracked in
  cache DB; at 80% budget the tool degrades to dataset-only mode and reports it.
- 429/quota errors: exponential backoff, skip state, record in patrol digest.

Cache: `cache.db` (SQLite, disposable). Tables: `sessions(session_id, state, year_start,
year_end, dataset_hash, fetched_at)`, `bills(bill_id, session_id, number, number_norm,
change_hash, status, status_date, last_action_date, n_sponsors, n_actions, n_texts, n_votes,
payload_json, fetched_at)`, `quota(month, calls_used)`.

## 7. Quorum replica reader

Plain read-only SQL over a Teleport-tunneled replica connection (`tsh proxy db` → local port,
DSN in `QUORUM_REPLICA_DSN`). No Django/ORM dependency at runtime. All SQL lives in one module
(`sherlock/quorum/reader.py`); exact table names are resolved from `app/bill/models.py` Meta
during M0 and documented inline next to each query. Per-session pulls:

- Bill identity/status: `label`, `number`, `session_id`, `current_status`,
  `current_status_date`, `most_recent_action_date`, `introduced_date`, `missing_data`,
  `last_quorum_update`, `region`, `source`.
- Counts per bill: actions (`BillAction`), texts (`BillText`), sponsors (M2M), votes (`Vote`).
- Session inventory: `LegSession` rows where `current = true` per region, plus `state_info`
  JSON (`has_no_bills` flag).

A startup smoke query validates the schema assumptions; on mismatch the patrol aborts with a
Slack alert (schema drift is an alert, not a crash loop).

## 8. Matching & diff engine (inside `diff_state`/`diff_federal`)

- **Session matching:** LegiScan session ↔ `LegSession` by state + year range (+ special-session
  flag). Both sides' "current" sets are reconciled; mismatches are reported as patrol warnings
  (not anomalies) since session-selection errors were v1's dominant false-positive source.
- **Bill matching:** normalized number — uppercase, strip whitespace/dots/leading zeros
  (salvaged rules), plus an extensible per-state prefix map (seed: CA `AR`→`HR`). Federal seed
  map (LegiScan → Quorum): `HB→HR (H.R.)`, `SB→S`, `HR→HRES`, `SR→SRES`, `HJR→HJRES`,
  `SJR→SJRES`, `HCR→HCONRES`, `SCR→SCONRES`. Cross-session carryover matching per the salvaged
  fallback design.
- **Detectors:** the four gap types per §1. Correctness compares status via an explicit
  LegiScan-progress → Quorum `GeneralBillStatus` mapping table (coarse on purpose — fewer false
  "wrong" flags than mapping onto the ~60 fine-grained `BillStatus` codes).
- **Detector precedence & asymmetry:** at most one date-related anomaly per bill — `stale` wins
  over `wrong_data`, and `wrong_data` fires only when freshness cannot explain the disagreement.
  LegiScan is treated as a *recall* oracle (it can prove Quorum is missing/behind), never a
  *precision* oracle: when Quorum is ahead of LegiScan, nothing is flagged.
- **Anomaly identity:** `fingerprint = sha1(gap_type | region | session_key |
  bill_number_norm | field)` — stable across patrols for dedup and recurrence tracking.
- **Lifecycle:** `new → triaged → fixing → fixed | alerted | suppressed`, post-fix
  `verified | regressed`. Recurrence bumps `last_seen`, preserves history.

## 9. The agent

- **Runtime:** Claude Agent SDK (Python), model `SHERLOCK_MODEL` (default `claude-sonnet-5`),
  `SHERLOCK_MAX_TURNS` per patrol (default 100) as the cost fuse.
- **System prompt (doctrine):** Sherlock persona; patrol strategy (sync → diff → investigate
  top anomalies → decide → report); the FP taxonomy from STEERING §10 as triage rules; severity
  rubric P1–P4 (P1 = missing bill in an active session with recent LegiScan activity; P4 =
  cosmetic); decision rules (fix only when a template's provenance policy allows it and evidence
  is unambiguous; otherwise alert; uncertain ⇒ alert with "needs investigation" tag).
- **Invocation:** `sherlock patrol [--state XX] [--dry-run] [--objective "..."]` — cron fires it
  daily with no objective (default doctrine); ad-hoc runs can focus it.
- **Transcripts:** every patrol's full agent transcript persisted to `runs/<patrol_id>.jsonl` +
  a `patrols` row with stats. This is the audit answer to agentic non-determinism: you can
  always replay *why*.

## 10. Fix executor (inside `trigger_rescrape` / `run_fix_template` / `verify_fix`)

Chain per anomaly: **(1)** scoped re-scrape → wait → `verify_fix`; **(2)** still broken →
fix template via `manage.py shell` in a transaction (snapshot → apply → in-script verify →
commit) → `verify_fix`; **(3)** verification failure → compensating rollback from snapshots +
alert. Caps: `SHERLOCK_MAX_FIXES_PER_CYCLE=25`, `SHERLOCK_MAX_FIXES_PER_STATE=10`.

**Template registry** (`sherlock/templates/registry.py`): each template declares `id`, params
schema, target models, invariants checked post-apply, and **provenance** — `primary_reingest`
(data re-enters via Quorum pipelines), `internal_recompute` (derived fields recomputed from
Quorum's own data), or `legiscan_copy` (values copied from LegiScan). Seed set:

| ID | Action | Provenance | v1 state |
|---|---|---|---|
| `T1_flag_missing_data` | Set `Bill.missing_data=True` on affected bills | internal_recompute | enabled |
| `T2_recompute_action_dates` | Recompute `most_recent_action_date`/`current_status_date` from existing `BillAction` history | internal_recompute | enabled |
| `T3_create_bill_stub` | Create a minimal missing `Bill` from LegiScan values | legiscan_copy | **defined, disabled** pending provenance/licensing call |

`legiscan_copy` templates stay disabled until Victor explicitly enables them (LegiScan data
licensing/provenance is a policy question, not a code question). Missing bills therefore fix
via re-scrape or alert in v1.

## 11. Case files & reporting

`casefile.db` (SQLite WAL, precious): `anomalies` (fingerprint-unique, evidence JSON, severity,
classification, status, first/last_seen), `patrols` (scope, stats, transcript path),
`actions` (anomaly_id, kind, template_id, params, dry_run, result), `snapshots` (action_id,
table, pk, before/after JSON).

Every patrol ends with a digest to `#sherlock-bot`: scope + duration; counts by gap type and
state (new vs recurring); notable cases with Sherlock's narrative diagnosis; actions taken with
before/after; failures/rollbacks; API quota + token spend. The daily digest doubles as the
liveness heartbeat — no digest by the expected hour means the cron is dead.

CLI: `sherlock report [--since ...]` renders history from case files; `sherlock patrol`,
`sherlock sync`, `sherlock diff --state CA` run pipeline pieces directly for debugging.

## 12. Error handling

- Tool exceptions surface to the agent as structured tool errors (bounded); the patrol
  continues with remaining states.
- Patrol-fatal errors (replica unreachable, schema drift, SDK failure) → Slack alert + nonzero
  exit.
- Slack failure: log and continue — reporting must never break the pipeline.
- LegiScan quota/429: backoff, degrade to cached data, note in digest.
- Teleport exec failure: counts as fix-leg failure → next leg of the chain (ultimately alert).
- Kill switch (`SHERLOCK_KILL_SWITCH=1`): write-capable tools refuse before any LLM opinion.

## 13. Runtime & configuration

Python 3.12, `uv`-managed. Deps: `claude-agent-sdk`, `httpx`, `typer`, `pydantic-settings`,
`structlog`, `psycopg`, `jinja2`, `pytest` (dev). Layout:

```
sherlock/
  agent/        # SDK loop, doctrine prompt, patrol entry
  tools/        # tool implementations (one module each)
  legiscan/     # API client + cache
  quorum/       # replica reader (all SQL), teleport exec wrapper
  diff/         # matchers, detectors, fingerprints
  templates/    # fix template registry + Jinja sources
  casefiles/    # SQLite persistence + report rendering
  cli.py
tests/
runs/           # patrol transcripts (gitignored)
docs/superpowers/specs/
```

Env (`.env`): `LEGISCAN_API_KEY` (present), `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`,
`QUORUM_REPLICA_DSN`, `SHERLOCK_MODEL=claude-sonnet-5`, `SHERLOCK_LIVE=0`,
`SHERLOCK_KILL_SWITCH=0`, `SHERLOCK_MAX_FIXES_PER_CYCLE=25`, `SHERLOCK_MAX_FIXES_PER_STATE=10`,
`SHERLOCK_FRESHNESS_SLA_HOURS=72`, `SHERLOCK_MAX_TURNS=100`. Teleport via standard `tsh login`
session. Scheduling: user-level launchd plist (macOS-native cron equivalent) firing
`sherlock patrol` daily at 07:00 local.

## 14. Testing

Pytest, TDD. Recorded fixtures: one real dataset ZIP sample + masterlist JSON (small state);
fake replica = SQLite with the minimal mirrored schema; golden tests for matchers/detectors
(including every salvaged FP family as a must-not-flag case); tool-level unit tests; agent-level
smoke = scripted patrol against fixture-backed fake tools asserting digest content classes;
executor tested through a fake `tsh` shim + dry-run — tests never touch prod. CI later; local
`pytest` gate first.

## 15. Milestones

- **M0 — Skeleton agent:** SDK loop + read-only tools (`legiscan_sync`, `diff_state`,
  `get_anomaly`) for one state (CA), console report. Replica reader schema resolved.
- **M1 — Shadow patrols:** all 50 states + federal, all four detectors, `investigate_bill`,
  `post_slack` → daily digests in `#sherlock-bot` via launchd.
- **M2 — Doctrine:** FP taxonomy + severity rubric tuned against an eval set; transcripts
  persisted; digest quality pass.
- **M3 — Hands, dry-run:** `trigger_rescrape` + `run_fix_template` + `verify_fix` in permanent
  dry-run; Sherlock narrates intended actions.
- **M4 — Hands, live:** `SHERLOCK_LIVE=1` with caps armed; re-ingestion leg first, then T1/T2
  templates + rollback drill.
- **M5 — Pack hunting (optional):** parallel per-state sub-agents; revisit `T3`/provenance;
  consider upstreaming learnings to quorum-site.

Each milestone gets its own implementation plan; M0 is next.

## 16. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Agentic variance (same patrol, different path) | Deterministic tools, bounded outputs, persisted transcripts, doctrine + eval set |
| Token/API cost creep | `SHERLOCK_MAX_TURNS`, bounded tool outputs, digest reports token spend |
| LegiScan free-tier quota | Dataset-first budget, tracked in cache DB, degrade-at-80% rule |
| LegiScan data provenance/licensing for writes | Per-template provenance attribute; `legiscan_copy` disabled by default |
| Replica schema drift | Single SQL module + startup smoke query; drift alerts instead of crashing |
| Prod writes misfire | Dry-run default, caps, kill switch, transactions + snapshots + verify + rollback, `#sherlock-bot` audit trail |
| Laptop runtime reliability | Digest-as-heartbeat; missed digest = dead cron; containerize at M5 if needed |

## 17. Out of scope (v1)

EU and US territories (PR/GU/VI/AS); regulations; amendments/supplements depth; bill-text
content diffing (presence only); historical (non-current) sessions; multi-channel Slack routing;
upstreaming into quorum-site.
