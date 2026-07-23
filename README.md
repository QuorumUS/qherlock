# Querlock 🔍

*An AI agent that audits Quorum's legislative data against LegiScan — and fixes what it finds.*

## Mission

Querlock is a standalone auditor that continuously compares [LegiScan](https://legiscan.com/legiscan) against Quorum's production data for **US federal + all 50 states** (current sessions, including carryover), and detects four kinds of gaps:

| Gap type | Meaning |
|---|---|
| **Missing bills** | LegiScan has a bill that never made it into Quorum |
| **Incomplete fields** | The bill exists but is missing sponsors, actions, texts, or votes |
| **Stale data** | Quorum's status / last action lags behind LegiScan |
| **Wrong data** | The two sources disagree on a field (status, dates) |

Each anomaly is diagnosed with Claude and closed through a guarded fix chain — trigger Quorum's own re-ingestion first, fall back to a pre-approved ORM fix template, roll back and alert if verification fails — with every action logged to the `#quentin-bot` Slack channel.

## Trust model

Querlock earns autonomy in stages — same code, one switch:

1. **Shadow mode** *(start here)* — detect, diagnose, and report to `#quentin-bot`. No writes, ever.
2. **Auto-fix** *(once trusted)* — fixes enabled behind a flag, dry-run by default, hard-capped per cycle, kill switch always armed.

## Daily patrol (launchd)

    mkdir -p ~/Library/Logs/querlock
    cp deploy/us.quorum.querlock.plist ~/Library/LaunchAgents/
    $EDITOR ~/Library/LaunchAgents/us.quorum.querlock.plist   # replace __REPO__ and __HOME__
    plutil -lint ~/Library/LaunchAgents/us.quorum.querlock.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/us.quorum.querlock.plist
    launchctl kickstart -k gui/$(id -u)/us.quorum.querlock    # smoke-run now
    # unload after edits: launchctl bootout gui/$(id -u)/us.quorum.querlock

Prereqs: `.env` must have `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` (the bot
must be `/invite`d into #quentin-bot; `SLACK_APP_TOKEN` is unused) and
`QUORUM_REPLICA_DSN`, and the
`tsh proxy db` tunnel must be up (a dead tunnel produces a Slack alert + exit 2 —
that is the intended failure mode; the daily digest doubles as the liveness
heartbeat). Before the first scheduled run, pre-warm the cache once:

    python3 -m uv run querlock sync --scope all

The first full sync ingests ~85 dataset ZIPs and takes a while; subsequent daily
runs are ~85 cheap masterlist calls.
