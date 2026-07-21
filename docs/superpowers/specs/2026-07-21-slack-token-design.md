# Slack posting: webhook → bot token (chat.postMessage)

**Date:** 2026-07-21
**Status:** approved (Victor, 2026-07-21)
**Supersedes:** the webhook transport in `2026-07-20-qherlock-design.md` §5 (`post_slack`) and §12/§13 env references to `SLACK_WEBHOOK_URL`.

## Why

The M1 live smoke was blocked on a `#quentin-bot` webhook that does not exist.
Nei provided the actual credential: a Quentin-specific Slack **bot token**
(plus app token and channel ID), now in the repo `.env`. Quorum posts to Slack
via bearer token + `chat.postMessage` (see `quorum-site/app/slack/client.py`),
not webhooks. Qherlock switches to the same transport. Decision: **clean
replacement** — the webhook path is deleted, not kept as a fallback (it was
never provisioned; dual paths are dead code).

## Config (`qherlock/config.py`)

| Setting | Env var | Notes |
|---|---|---|
| `slack_bot_token: str = ""` | `SLACK_BOT_TOKEN` | xoxb bearer token from Nei |
| `slack_channel_id: str = ""` | `SLACK_CHANNEL_ID` | #quentin-bot channel ID |

`slack_webhook_url` is removed. `SLACK_APP_TOKEN` (xapp, Socket Mode only)
stays in `.env` unused; `extra="ignore"` already tolerates it.

## Transport (`qherlock/slack.py`)

New signature: `post(token: str, channel: str, kind: str, text: str, http=None) -> dict`.

- POST `https://slack.com/api/chat.postMessage` with header
  `Authorization: Bearer <token>` and JSON body `{"channel": channel, "text": body}`.
- **Invariants carried over unchanged:** never raises; digest/alert headers;
  3500-char cap with casefile-pointer truncation; the token is never logged
  or included in any returned payload.
- **New failure mode:** the Slack Web API returns HTTP 200 with
  `{"ok": false, "error": "..."}`. `post` must parse the body and return
  `{"ok": False, "error": "slack api error: <error>"}` for it. Expected error
  strings during bring-up: `invalid_auth`, `channel_not_found`,
  `not_in_channel` (fix: `/invite` the bot into #quentin-bot — it must be a
  member before it can post).
- Non-2xx HTTP and transport exceptions keep the existing
  `{"ok": False, "error": ...}` shape.
- Unconfigured guard: if token **or** channel is empty, return
  `{"ok": False, "error": "SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not configured"}`.

## Call sites

Mechanical update to `slack.post(settings.slack_bot_token,
settings.slack_channel_id, kind, text)`:

- `qherlock/agent/tools.py` — `post_slack` handler (tool description drops
  the word "webhook").
- `qherlock/agent/patrol.py` — 4 call sites (abort alerts ×2, budget alert,
  final digest).

## Tests (`tests/test_slack.py`)

Existing cases adapt to the new signature (truncation, never-raises, unknown
kind, unconfigured). New cases:

1. HTTP 200 + `{"ok": false, "error": "channel_not_found"}` → returned
   payload is `ok: False` and carries the Slack error string.
2. The request carries `Authorization: Bearer <token>` and the channel in
   the JSON body.
3. Token absent from every returned payload and log call on all failure paths.

## Docs

- `2026-07-20-qherlock-design.md` §5/§12/§13: webhook references annotated as
  superseded by this spec (historical text otherwise left intact).
- README / deploy install docs: env prerequisites become `SLACK_BOT_TOKEN`,
  `SLACK_CHANNEL_ID` (and note `SLACK_APP_TOKEN` is unused), plus the
  invite-the-bot-to-#quentin-bot prerequisite. `deploy/us.quorum.qherlock.plist` needs no
  change (it inherits `.env` via WorkingDirectory).

## Out of scope

- Runtime fetch from AWS Secrets Manager (belongs to the M5 actacollecta
  graduation; on the laptop, `.env` survives SSO-session expiry at 07:00).
- Socket Mode / receiving events (`SLACK_APP_TOKEN`).
- Block Kit formatting; messages remain plain `text`.
