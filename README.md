# Sherlock 🔍

*An AI agent that audits Quorum's legislative data against LegiScan — and fixes what it finds.*

## Mission

Sherlock is a standalone auditor that continuously compares [LegiScan](https://legiscan.com/legiscan) against Quorum's production data for **US federal + all 50 states** (current sessions, including carryover), and detects four kinds of gaps:

| Gap type | Meaning |
|---|---|
| **Missing bills** | LegiScan has a bill that never made it into Quorum |
| **Incomplete fields** | The bill exists but is missing sponsors, actions, texts, or votes |
| **Stale data** | Quorum's status / last action lags behind LegiScan |
| **Wrong data** | The two sources disagree on a field (status, dates) |

Each anomaly is diagnosed with Claude and closed through a guarded fix chain — trigger Quorum's own re-ingestion first, fall back to a pre-approved ORM fix template, roll back and alert if verification fails — with every action logged to the `#quentin-bot` Slack channel.

## Trust model

Sherlock earns autonomy in stages — same code, one switch:

1. **Shadow mode** *(start here)* — detect, diagnose, and report to `#quentin-bot`. No writes, ever.
2. **Auto-fix** *(once trusted)* — fixes enabled behind a flag, dry-run by default, hard-capped per cycle, kill switch always armed.
