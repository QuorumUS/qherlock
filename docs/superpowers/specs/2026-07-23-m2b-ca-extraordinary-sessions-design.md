# M2b (part 1) — CA extraordinary-session matching + OH resolution conclusion

**Date:** 2026-07-23
**Status:** SHIPPED (2026-07-23, on `main`, commits `88af49a`..`127ca57`). Live-accepted
via `diff --scope CA`: all 20 CA X1 `missing_bill` FPs matched their non-current sibling
session 3736 and auto-retired (0 X-family new); the 29 ACR `wrong_data` and 1 `SB574`
`stale` were left untouched as designed; no `incomplete_fields` FPs. OH: no code (confirmed
genuine scraping gap). 168 tests green offline. A final-review-caught latent fingerprint
collision (extraordinary anomaly keyed on base vs the same-session ordinal bill) was fixed
pre-merge (`127ca57`): extraordinary anomalies are keyed on the fused LegiScan number.
**Parent:** M2b in `2026-07-20-querlock-design.md` §15 and `2026-07-22-m2a-detection-fixes-design.md`
("Live acceptance outcome"). M2b is a basket of independent pieces; this spec is
the first slice — the deferred **detection-correctness** work (CA + OH). Doctrine/
taxonomy, CA ACR, and the M2b backlog are separate specs (see §7).

## Live verification (2026-07-23, tunnel up) — corrects the M2a diagnosis

Verified against the offline LegiScan cache (`data/cache.db`) **and** the Quorum
replica. Both recorded M2a assumptions were wrong; the shapes below are ground truth.

**CA — the two "CA 2172 family" numbers are two *different* phenomena, not one.**

- **20 `missing_bill` = the real X1 family (this spec fixes these).** LegiScan folds
  extraordinary-session bills into the *regular* session (`session_id 2172`,
  "2025-2026 Regular Session", `special=0`) with fused numbers:
  `ABX11` = Assembly Bill, extraordinary session **X1**, bill **1**;
  `ABX110` = X1 bill **10**; `ACAX11` = Assembly Const. Amdt, X1, bill 1.
  The raw LegiScan `bill_number` has **no space** (`ABX110`), so the split is
  lexically ambiguous (`ABX110` could be X1/bill-10 or X11/bill-0) and is resolvable
  only by knowing the biennium's special-session ordinals.
  Quorum stores these as **base** labels (`A.B.1`→norm `AB1`, `S.B.1`→`SB1`,
  `A.C.A.1`→`ACA1`) in a **separate special session — `id 3736`, "2025 Spec Session 1
  - X1", `regular_session=false`, `current=FALSE`.** Because the replica reader
  filters `current = TRUE`, session 3736 is invisible to the patrol, so
  `match_sessions` never sees it and the X1 bills are looked up (as `ABX11`) in the
  current regular session 3570 → not found → `missing_bill`.
  *(M2a's premise "match the X1 bills by number against a sibling session" was
  wrong twice over: LegiScan has no separate special session, and Quorum's sibling
  session is non-current. The real fix is base-number lookup in the non-current
  sibling.)*
  Mapping all 20 into 3736 with the real matcher/detector yields **20 clean matches,
  zero residual** (LegiScan status-6/"failed" bills are unmapped by design; status-4
  bills meet Quorum's enacted rank).

- **29 `wrong_data` = CA `ACR` (concurrent resolutions) in the regular session — a
  separate, mixed phenomenon. DEFERRED (§7).** These fire because LegiScan
  "Engrossed" (status 2, `LEGISCAN_MIN_RANK[2]=3`) outranks Quorum "out_of_committee"
  (rank 2). It is genuinely mixed: `ACR227` was adopted 70-0 and Quorum is behind
  (**genuine**); `ACR2` went to the inactive file and Quorum's lower status is
  arguably *more* correct (LegiScan's Engrossed→rank-3 mapping over-claims). This is
  detector-tuning/doctrine, not a clean deferred fix, and belongs to the doctrine
  sub-project. `SB574` (1 stale) is unrelated and likely genuine — left alone.

**OH — 236 `HR` `wrong_data` = a genuine Quorum scraping gap; the detector is
correct. NO CODE CHANGE (§6).** All 236 are "Honoring/Recognizing…" simple House
resolutions, LegiScan **Adopted** (status 4) via the *"Adopted: Rules and Reference"*
consent calendar; Quorum holds them at **introduced** (status 1) with the same dates.
Tell: the other ~251 OH HR (opening-day organizational, action just "Adopted") **are**
at Quorum adopted/effective (status 7). So Quorum's OH scraper captures plain
"Adopted" but misses the consent-calendar "Adopted: Rules and Reference" action, so
honoring resolutions stay at introduced. That is a real gap for Quentin's scraper to
fix, not a false positive.

Recorded before-state (`data/casefile.db`, all `status='new'`): CA 20 `missing_bill`
+ 29 `wrong_data` + 1 `stale`; OH 236 `wrong_data`.

## Why

M2a shipped the quiet baseline but deferred CA and OH after live acceptance disproved
its assumptions. This spec closes the CA detection defect (the one clean, verified
false-positive family remaining) and records the OH conclusion, so Querlock moves
toward the graduation bar — "arrive in actacollecta already quiet" — on verified
ground rather than on the M2a-era guesses.

## Scope decisions (2026-07-23, Victor)

1. This spec = deferred detection-correctness (CA + OH) only.
2. OH HR: **confirm genuine, no code change**; volume/classification is doctrine's call.
3. CA: **X1 matching only**; the 29 ACR are deferred to the doctrine sub-project.
4. Verification: tunnel was up; both sides verified live and baked in above.

## 1. Sibling-session discovery (`querlock/quorum/reader.py`)

- New read-only `get_special_sessions(conn, region) -> list[SessionRow]`: rows with
  `regular_session = FALSE` for the region **regardless of `current`** (the existing
  `get_current_sessions` filters `current = TRUE` and must stay as-is for the main
  loop). Same column set / `SessionRow` shape.
- `get_bills_for_session` and `get_bill_counts_for_session` already take an explicit
  `session_id` and do not filter on `current`, so they work for 3736 unchanged.
- A pure helper (in `matchers.py`, §2) selects the sibling special sessions for a
  matched regular session: same region, `start_year` within the biennium window
  `[y, y+1]` where `y` is the regular session's `start_year`, **and** the
  `title`/`session_name` matches an X-ordinal (`X(\d+)`, fallback
  `Spec(?:ial)?\s+Session\s+(\d+)`). This rejects the stray `id 3809`
  "California 2025-2026 Regular Session" (`regular_session=false` but no ordinal
  marker) and older-biennium siblings (`3736` for 2025 is kept; `3665` for 2024 is not).

## 2. LegiScan extraordinary-number parse (`querlock/diff/matchers.py`)

- New `EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})`. The whole
  mechanism is state-gated; no other region discovers siblings or attempts X-parsing.
- `extract_session_ordinal(title_or_name) -> int | None` — the `X(\d+)` /
  `Spec Session (\d+)` regex used by both §1's selector and the parse.
- `parse_extraordinary_number(raw_number, ordinals) -> list[tuple[int, str]]`:
  for each ordinal `O` in `ordinals` (the ordinals present among the biennium's
  sibling sessions), try `^([A-Z]+)X{O}(\d+)$` against the normalized number and, on
  match, add `(O, f"{TYPE}{n}")` — the base norm, e.g. `ABX110`→`(1, "AB10")`,
  `ACAX11`→`(1, "ACA1")`. Returns **all** matching candidates (empty list for numbers
  without the marker — plain `AB1` is untouched — and for non-gated states). Returning
  the full candidate list, not a single tuple, is deliberate: when a number is
  ambiguous across ordinals (e.g. ordinals `{1, 11}` both parse `ABX110`), §3 picks the
  candidate whose base actually exists in that ordinal's session rather than committing
  to a guess that could fall through to a false `missing_bill`.

## 3. Diff wiring (`querlock/diff/service.py`)

- For a region in `EXTRAORDINARY_SESSION_STATES`, after the current regular
  `(ls, qs)` pair's `q_by_norm` is built, also build a
  `siblings: dict[int, tuple[q_by_norm, q_counts, quorum_session_id]]` keyed by ordinal
  for the discovered sibling sessions — reusing the existing per-session collision-guard
  logic and `get_bill_counts_for_session`.
- In the bill loop, before the normal lookup: if the region is gated, compute the
  candidates `parse_extraordinary_number(bill["number"], siblings.keys())` and take the
  first `(O, base)` whose `base` is present in `siblings[O]`'s map; run
  `detect_bill_anomalies` against that sibling row (its `quorum_session_id` recorded in
  evidence, `number_norm = base`). If there are candidates but none resolve to a
  sibling row, the bill is correctly reported `missing_bill` (against the LegiScan
  number). If there are no candidates (not X-marked), fall through to the existing
  regular-session path unchanged.
- **`session_key` stays the LegiScan session id (`"2172"`)** for X-marked bills — the
  sibling session is consulted only. Sibling sessions are never added to
  `processed_sessions`, never independently diffed, and never retire their own rows.

## 4. Retirement (no code change — verified compatible)

The 20 recorded `missing_bill` FPs carry `session_key="2172"`, which remains in
`processed_sessions` for the CA run. Once the X1 bills match cleanly their live
fingerprints disappear, so `retire_resolved("CA", processed_sessions, live_fps)` flips
them to `status='resolved'` with `resolved_at`. No schema change, no migration.

## 5. Testing (`tests/`)

- `tests/evals/fixtures/ca_extraordinary.json`: both sides in detector-consumable shape
  — the 20 X1 LegiScan bills (from session 2172) + the base rows from Quorum session
  3736 — plus one **planted genuinely-absent** X1 bill (base not in the sibling). The
  eval runs the **real** matcher/detector (no mocks, offline sqlite), asserting: zero
  `missing_bill` from the 20-bill family, AND the planted bill still fires
  `missing_bill`. Over-suppression fails the test, same as under-suppression.
- Unit tests: `parse_extraordinary_number` (the `ABX110` ambiguity, `ACAX11`
  multi-letter type, multi-ordinal `{1,2}` selection, plain `AB1` → `None`, non-CA →
  `None`); `extract_session_ordinal` + sibling selection (keeps 3736, rejects the 3809
  stub and the 3665 prior-biennium session).
- Full suite + evals green offline (no tunnel, no LegiScan key), per M2a's bar.

## 6. OH resolutions — conclusion only (no code)

Record the verified finding (see "Live verification"): OH's 236 HR honoring-resolutions
are a **genuine Quorum scraping gap** — the detector is behaving correctly, so there is
no change here. Their volume/severity/classification is a doctrine-sub-project decision,
not a detection fix. The finding is also persisted as a project note so it reaches the
actacollecta/Quentin handoff (the scraper should parse "Adopted: Rules and Reference").

## 7. Out of scope (deferred — recorded so nothing is lost)

- **CA ACR (29) `wrong_data`** — the `LEGISCAN_MIN_RANK[2]` (Engrossed) → CA
  concurrent-resolution flow mismatch; mixed genuine/FP, needs per-case judgment with
  the full resolution taxonomy → **doctrine sub-project**.
- **CA `SB574` (1) `stale`** — unrelated regular bill, likely genuine; left alone.
- **FP taxonomy / `classification` column semantics** (the column exists, unused) →
  doctrine sub-project.
- **General resolution-status doctrine** (including OH volume/classification) →
  doctrine sub-project.
- **M2b backlog:** 45 unmapped 2026 sessions presentation; 429 Retry-After on the Slack
  footer; mrkdwn escaping; batched `retire_resolved` UPDATE; migration test; eval-fixture
  breadth backfill.
- **M2.5** Slack surface unification (blocks on Nei's actacollecta work).
