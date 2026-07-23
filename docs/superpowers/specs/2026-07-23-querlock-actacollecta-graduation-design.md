# M5 — Querlock graduation into actacollecta as one complementary LegiScan check

**Date:** 2026-07-23
**Status:** DESIGN (brainstormed 2026-07-23, Victor). Deliverable = this spec + migration
map, handed to Nei; **no actacollecta PR opened, no Querlock code refactored** by this
effort. Querlock stays a working standalone auditor on `main` until the port lands.
**Parent:** M5 in `2026-07-20-querlock-design.md` §15 ("Graduation"). This spec supersedes the
naive reading of M5 ("merge Querlock in wholesale") after live inspection of the destination
disproved its premise (see §1).

## Live inspection of the destination (2026-07-23) — corrects the M5 premise

Verified by reading `~/Projects/actacollecta` directly. Two facts reshape graduation:

1. **actacollecta already has a near-twin of Querlock — the `datachecks/` package —
   built without LegiScan.** It is "AI-judged production data checks over the prod
   quorum-site DB, read-only." Its checks map almost one-to-one onto Querlock's detectors:

   | Querlock detector | actacollecta datacheck | Maturity | Detection method |
   |---|---|---|---|
   | `missing_bill` | `bill_gaps` (Missing Bills, AID-247) | **Mature** — 1,684 LOC, 50-state reference dirs, S3 ledger, 50 prod CronJobs | Bill-number **sequence-hole** analysis + `claude -p` opening the **real state source**. No LegiScan. |
   | `wrong_data`/`stale` (status) | `bill_status` (AID-246) | **Mature** — 879 LOC, evals 0 FP/0 FN | Stored status vs the bill's own **action text** (internal consistency) |
   | `incomplete_fields` (texts) | `bill_text` | Partial (schema/select/report; no judge) | Text presence/markup |
   | — | `bill_integrity` (duplicates) | Partial | dup records |

2. **actacollecta deliberately abandoned LegiScan.** `grep` finds **zero** LegiScan usage in
   its live code (`datachecks`, `quentinbot`, `spiders`, `etl`, `backend`, `database`,
   `schemas`); the only mentions are in `STEERING.md` and
   `docs/journal/2026.06.08_datachecks-framework.md`, both describing LegiScan as the **legacy**
   approach the new framework **supersedes** ("`bills/missing-bills` … Supersedes the Legiscan
   cron"). `bill_gaps` verifies against the authoritative state source directly — no paid
   subscription, no third-party aggregator.

**Consequence:** Querlock's central approach (diff against LegiScan) substantially *duplicates*
`bill_gaps`/`bill_status` and partly *contradicts* actacollecta's chosen direction. Porting
Querlock wholesale would re-introduce a dependency the destination deliberately dropped and add
a second, parallel way of doing detection actacollecta already does. Graduation is therefore
**not** a wholesale merge.

## Decisions (2026-07-23, Victor)

1. **Target shape: hybrid — keep the brain, adopt the shells.** Bring only Querlock's
   genuinely additive detection logic; replace its casefile DB / standalone Slack / launchd /
   Agent-SDK loop with actacollecta's proddb + S3 ledger + `quentinbot.threads` handoff + Helm
   scheduling.
2. **Reconciliation: one complementary LegiScan check.** Querlock graduates as a single new
   datachecks check, `legiscan_crosscheck`, using LegiScan as an **independent oracle** for what
   actacollecta's source-direct checks miss. `missing_bill` and the heavy
   number-normalization/session-reconciliation are **dropped** (bill_gaps owns them).
3. **Deliverable now = this spec + migration map for Nei.** Nei co-owns the destination; keep
   the handoff friction-minimal. No actacollecta PR, no Querlock refactor, in this effort.
4. **LegiScan fetch strategy / subscription tier = an open decision for Victor + Nei** (§5),
   written up with a recommendation, not forced.

## Why

M5's pre-merge bar was "arrive in actacollecta already quiet" (M1 breadth + M2 FP suppression).
Querlock meets M1 breadth (50 states + federal) and is largely M2-quiet (−82% at M2a; CA X1
family retired at M2b). But the destination turns out to already cover Querlock's headline
detector better (source-direct, subscription-free). The high-value, honest graduation keeps only
the LegiScan-oracle signal actacollecta lacks and folds it into the existing framework the way a
native check would be written.

## 1. Scope — what graduates, what is dropped

**Graduates (the additive signal):** the pure detector logic in
`querlock/diff/detectors.py`, over the **Quorum ∩ LegiScan intersection** (bills present on both
sides, matched by number within the resolved session):

- **`incomplete_fields`** — LegiScan `n_sponsors/n_actions/n_texts/n_votes ≥ 1` while Quorum's
  count is `0`. Nothing in actacollecta computes this today; it is the strongest unique signal.
- **`stale`** — LegiScan `last_action_date` newer than Quorum `most_recent_action_date` beyond
  the SLA; an *external* freshness oracle (complements `bill_status`'s internal check).
- **`wrong_data`** — Quorum general-status rank below LegiScan's minimum expected rank, with
  Querlock's resolution-aware rank maps and the "stale wins over wrong_data" precedence.

`detectors.py` is already pure and golden-tested, so it ports near-verbatim into a `compare.py`
functional core.

**Dropped (superseded or duplicated by the destination):**

- **`missing_bill`** — `bill_gaps` owns missing-bill detection, source-direct and subscription-
  free. The `legiscan_crosscheck` compares only the intersection; a bill absent from Quorum is
  bill_gaps' job, not ours.
- **Session reconciliation + heavy per-state number normalization** (incl. the CA X1
  extraordinary-session machinery) — `bill_gaps` already handles special sessions, pooled
  sequences, and per-state label normalization across 50 states.
- **The standalone infra** — casefile DB, `querlock/slack.py`, launchd plist, the Claude Agent
  SDK patrol loop. Replaced per §4.

## 2. Home & shape (conforms to the datachecks anatomy)

New package `datachecks/src/datachecks/legiscan_crosscheck/`, following the deterministic-check
template (`bill_text/`, `bill_integrity/`) plus the `bill_gaps` ledger:

| File | Core/Shell | Responsibility |
|---|---|---|
| `__init__.py` | — | package marker |
| `schema.py` | core | pydantic DTOs: `QuorumBill`, `LegiscanBill`, `CrosscheckFinding`, `StateCrosscheckReport` (with `@computed_field` counts + `new_findings`/`known_open`/`resolved`, mirroring `bill_gaps.report.StateGapReport`) |
| `compare.py` | **core** | ported `detectors.py` — pure diff (counts, status rank + precedence, dates). Unit-tested with no I/O. The unique brain. |
| `select.py` | shell (+ pure `rows_to_bills`) | Quorum rows via `proddb.connect()`; per-bill counts (`jsonb_array_length(major_actions)`, `bill_billtext`, sponsor/vote tables per §6); `resolve_session()` (mirror `bill_gaps.select`) |
| `legiscan.py` | shell | `httpx` LegiScan client — ports Querlock's collector (§5) |
| `report.py` | core | `build_crosscheck_report(...) -> StateCrosscheckReport` |
| `ledger.py` | shell | copy `bill_gaps/ledger.py`, re-keyed `(state, session_id, bill_id, field)` |

**Reused from actacollecta, not re-created:** `proddb.connect/normalize_dsn` (read-only,
`QUENTIN_QUORUM_DB_URI`); `bill_status.reference.regions.resolve_region_id` (state → int region);
the `bill_gaps.ledger` S3 machinery (`load_ledger`/`save_ledger`/`prune_resolved`/`open_entries`/
`reconcile`/`select_new_findings`, transparent local-or-`s3://`).

**CLI:** one Typer command in `datachecks/src/datachecks/cli.py` —
`datachecks legiscan-crosscheck <state> [--session ID] [--ledger s3://…] [--ledger-ttl-days N]
[--output PATH] [--report-s3 s3://…]` — body follows the `bill-gaps` ledger order:
`resolve_session → fetch_quorum_bills → fetch_legiscan_bills → load_ledger/prune_resolved/
open_entries → crosscheck_bills → select_new_findings → build_crosscheck_report → reconcile/
save_ledger → output/report_s3`.

**Deliberately NOT created:** `judge.py`, `system-prompt.md`, `user.md`, `evals/`. The check is
**deterministic** (LegiScan returns structured counts/status; the comparison is arithmetic), so
there is no `claude -p` step and no `anthropic` dependency. `pyproject.toml` adds only
`httpx>=0.27`. (An AI reconciliation phase can be added later if fuzzy status adjudication proves
necessary — e.g. the CA ACR Engrossed-vs-committee doctrine case — mirroring `bill_status`.)

## 3. File-by-file migration map (Querlock → destination)

| Querlock module | Destination |
|---|---|
| `querlock/diff/detectors.py` | → `legiscan_crosscheck/compare.py` (near-verbatim; drop `missing_bill`) |
| `querlock/diff/matchers.py` | mostly **dropped**; keep only the minimal intersection-join normalization (bill_gaps owns the rest) |
| `querlock/legiscan/{client,cache,sync}.py` | → `legiscan_crosscheck/legiscan.py` + the §5 fetch layer |
| `querlock/quorum/reader.py` | → `legiscan_crosscheck/select.py` (rewritten onto `proddb` + `major_actions` JSONB; §6) |
| `querlock/casefiles/` (DB, auto-retirement) | **dropped** → `bill_gaps.ledger` (S3, per-state, TTL, self-heal) |
| `querlock/slack.py` | **dropped** → `quentinbot.threads.create_tracked_thread` (§4) |
| `querlock/agent/{patrol,tools}.py` (Agent SDK loop) | **dropped** → the thin `.claude/skills/legiscan-crosscheck-sweep/SKILL.md` shell (§4) |
| `querlock/investigate.py` | **dropped** (single-bill deep-dive; the check reports enough evidence inline) |
| `querlock/regions.py` | **dropped** → `bill_status.reference.regions.resolve_region_id` |
| `deploy/*.plist` (launchd) | **dropped** → Helm CronJob (§4) |
| `tests/`, `tests/evals/` | detector/compare unit tests port to `datachecks/tests/`; the FP-family eval fixtures inform, but the deterministic check needs no LLM eval set |

## 4. Infra seams — adopt actacollecta's

- **State / re-alert suppression:** S3 ledger, one object per state,
  `s3://actacollecta-agent/datachecks/legiscan_crosscheck/ledger/<state>.json`. Self-heals
  (drops entries whose bill now matches), TTL re-verifies (default 30 days), never re-posts a
  known-open finding. Replaces Querlock's casefile DB + auto-retirement exactly.
- **Reporting → Quentin:** post one finding thread per anomaly via
  `quentinbot.threads.create_tracked_thread(load_token, channel=…, bucket=…, kind=…, title=…,
  body=…)` (or `quasar slack post`), following `quentinbot/scripts/publish_metrics_findings.py`
  and `.claude/skills/bill-gaps-sweep/SKILL.md`. Replaces `querlock/slack.py`.
- **Findings → autoheal (the M5 payoff):** a human `@`-mention approval in the thread enqueues
  an `actacollecta_autoheal` SQS message; the agent-worker's fix leg **re-runs the state Scrapy
  spider** (`scrapy crawl <state>_bills -a session_id=… -a bill_numbers=… -a force=true`, or the
  backend SQS re-ingest path). Querlock's old "trigger re-ingestion / ORM fix template"
  *becomes* this re-scrape — precisely why M3/M4 (Teleport-exec fix legs) were deprioritized.
- **Scheduling:** a Helm CronJob per state (mirroring `billGapsSweep` in
  `helm/actacollecta-agent/values.yaml`) enqueues `/legiscan-crosscheck-sweep <state>` via
  `poll-generic` on the shared queue; a thin `.claude/skills/legiscan-crosscheck-sweep/SKILL.md`
  runs the CLI with the state's ledger and posts only NEW findings. Reuses the existing queue +
  worker — no new SQS queue/ScaledJob. Replaces the launchd plist.

## 5. LegiScan data layer — the ported IP + the open decision (Victor + Nei)

This is where Querlock's collector is genuinely additive, and the strategy depends on the
subscription tier actacollecta takes on (M5 says actacollecta "takes ownership of the LegiScan
dependency (proper subscription, no hardcoded-key fallback)"):

- **Option A — free tier (30k queries/mo), dataset-first + S3 cache.** Per-bill `getBill` across
  50 states daily blows the free budget; Querlock already solved this with a dataset-first
  cache + quota discipline. In actacollecta's pod world a local sqlite cache won't survive across
  50 per-state CronJobs, so this becomes a **weekly LegiScan dataset-sync job caching to S3**
  (like the ledger); per-state checks read the cache. Robust; adds one scheduled sync job + an S3
  cache layout.
- **Option B — paid tier, per-state targeted fetch at run time.** `getMasterList` for the
  session + `getBill` for the intersection, in-process per run. Simpler (no shared cache/sync
  job), needs the subscription.

**Recommendation:** hide the source behind the `legiscan.py` interface (`fetch_legiscan_bills(
state, session) -> dict[number_norm, LegiscanBill]`) so both are drop-in; ship Option B if the
subscription lands, else Option A. Secrets: `LEGISCAN_API_KEY` added to `.env.quentin` (local)
and the `data-scraping-secret` AWS Secrets Manager entry (prod), alongside `ZYTE_API_KEY` /
`NY_OPENLEG_API_KEY` / `NEVADA_LIS_API_KEY`. Read in-process by `legiscan.py` (deterministic
check → no judge subprocess → no export-to-child gotcha).

## 6. Two unknowns to resolve during implementation (not blocking this spec)

1. **Quorum sponsor/vote table names.** `datachecks` confirms `bill_billtext` (text count) and
   `major_actions` JSONB (action count) against prod, but references **no** sponsor/vote tables
   (they live in the external quorum-site schema, likely `bill_billsponsor` / `bill_billvote`
   keyed by `bill_id`). These power `incomplete_fields` and must be verified directly against
   prod `quorum_db` before `select.py` is written. If a count is unavailable read-only, that
   field is simply not checked (fail safe, no false positive).
2. **LegiScan session ↔ Quorum `session_id`.** The intersection join needs the LegiScan session
   for a state's current Quorum session; port a slimmed version of Querlock's session matching
   (state + year range + special flag). `bill_gaps.select.resolve_session` already picks the
   Quorum session; the LegiScan side must resolve to the same biennium.

## 7. Readiness note (M5 pre-merge bar)

- **M1 breadth:** met — 50 states + federal, current sessions incl. carryover.
- **M2 quiet:** substantially met — M2a −82% (4,342→~795); CA X1 family retired at M2b; OH HR
  confirmed genuine. Residual FP doctrine (CA ACR Engrossed-vs-committee; general resolution-
  status doctrine) is **narrowed by graduation**: `wrong_data`/`stale` here are a *second
  opinion* feeding a human-approved autoheal, so a borderline flag costs a thread, not a bad
  write. The ACR doctrine can ride as check config later, not a blocker.
- **Superseded on arrival:** `missing_bill` (→ bill_gaps). This is expected and intended, not a
  regression.

## 8. Out of scope (recorded so nothing is lost)

- CA ACR (29 `wrong_data`) resolution doctrine — becomes optional check config / a later AI
  reconciliation phase.
- Querlock's `investigate_bill` deep-dive, `report` CLI, patrol digest formatting — the
  destination's thread/ledger reporting replaces them.
- Renaming the Querlock repo dir / GitHub remote (`QuorumUS/qherlock`) — the code moves *into*
  actacollecta; the standalone repo is archived after the port lands, not renamed.
- Deleting/retiring the standalone Querlock — deferred until the check is live and accepted in
  actacollecta (Querlock keeps running as the interim auditor meanwhile).

## 9. Note for the reader (informational, not introduced here)

actacollecta's existing `claude -p` judges (`bill_status/judge.py`, `bill_gaps/judge.py`) run
with `--permission-mode bypassPermissions` and a scrubbed env (`judge_env` drops
`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`). `legiscan_crosscheck` is deterministic and spawns no
judge, so it does not adopt that posture; noted only so the destination's conventions aren't a
surprise during the port.
