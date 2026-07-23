# Slack Bot-Token Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace querlock's webhook Slack posting with bot-token `chat.postMessage` so patrols can post to #quentin-bot with the credentials Nei provided (already in `.env`).

**Architecture:** Clean replacement per `docs/superpowers/specs/2026-07-21-slack-token-design.md` — `slack.post` gains a `(token, channel, kind, text)` signature and POSTs to `https://slack.com/api/chat.postMessage` with a Bearer header; config swaps `slack_webhook_url` for `slack_bot_token` + `slack_channel_id`; the five call sites update mechanically. The webhook path is deleted, not kept.

**Tech Stack:** Python 3.12, httpx (already a dep), pydantic-settings, pytest.

## Global Constraints

- `slack.post` NEVER raises; failures return `{"ok": False, "error": ...}` payloads.
- The token must never appear in logs, error strings, or returned payloads.
- 3500-char cap with the casefile-pointer truncation suffix is unchanged.
- Slack Web API returns HTTP 200 with `{"ok": false, "error": "..."}` — the body's `ok` must be checked, not just the status code.
- Run tests with `.venv/bin/pytest` (the repo .venv shadows module-installed uv; do NOT use bare `pytest` or `uv run pytest`).
- All work on `main` (repo convention so far); one commit per task below.

---

### Task 1: Transport swap (slack.py + config.py + call sites)

One atomic signature change — the suite can only be green with all pieces
moved together, so this is a single task/commit with test-first steps per
file.

**Files:**
- Modify: `querlock/slack.py` (whole file)
- Modify: `querlock/config.py:13`
- Modify: `querlock/agent/tools.py:154-157`
- Modify: `querlock/agent/patrol.py:85,93,117,146` (the 4 `slack.post` call sites)
- Test: `tests/test_slack.py` (whole file), `tests/test_config.py:19,25-28`, `tests/test_agent_tools.py:23-32,238-259`, `tests/test_patrol.py:134,142,149,168,197,216,234,251-252`

**Interfaces:**
- Consumes: existing `Settings` (pydantic-settings, env-mapped by field name), `httpx`, `structlog`.
- Produces: `slack.post(token: str, channel: str, kind: str, text: str, http: httpx.Client | None = None) -> dict` and `Settings.slack_bot_token: str = ""` / `Settings.slack_channel_id: str = ""` (env `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID`). `slack.truncate` unchanged.

- [ ] **Step 1: Rewrite `tests/test_slack.py` for the token transport**

Replace the whole file with:

```python
import httpx

from querlock import slack


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_success_sends_bearer_token_channel_and_kind_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    result = slack.post("xoxb-test-token", "C0QUENTIN", "digest", "hello",
                        http=_client(handler))
    assert result["ok"] is True and result["truncated"] is False
    assert seen["auth"] == "Bearer xoxb-test-token"
    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert "C0QUENTIN" in seen["json"]
    assert "Querlock digest" in seen["json"] and "hello" in seen["json"]


def test_not_configured_when_token_or_channel_missing():
    expected = {"ok": False, "error": "SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not configured"}
    assert slack.post("", "C1", "digest", "x") == expected
    assert slack.post("xoxb-t", "", "digest", "x") == expected


def test_api_ok_false_surfaces_slack_error_without_token():
    def handler(request):
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    result = slack.post("xoxb-SECRET", "C1", "alert", "x", http=_client(handler))
    assert result["ok"] is False
    assert "channel_not_found" in result["error"]
    assert "SECRET" not in result["error"]


def test_http_error_never_raises_and_never_leaks_token():
    def handler(request):
        return httpx.Response(500, text="boom")

    result = slack.post("xoxb-SECRET", "C1", "alert", "x", http=_client(handler))
    assert result["ok"] is False and "SECRET" not in result["error"]


def test_connect_error_never_raises():
    def handler(request):
        raise httpx.ConnectError("nope")

    result = slack.post("xoxb-t", "C1", "digest", "x", http=_client(handler))
    assert result["ok"] is False


def test_non_json_2xx_body_never_raises():
    """A 2xx response with a non-JSON body must yield an error payload, not raise."""
    def handler(request):
        return httpx.Response(200, text="warning: not json")

    result = slack.post("xoxb-t", "C1", "digest", "x", http=_client(handler))
    assert result["ok"] is False


def test_truncation_at_cap_with_pointer():
    body = "y" * 5000
    text, truncated = slack.truncate(body)
    assert truncated is True and len(text) <= slack.MAX_CHARS
    assert "casefile.db" in text


def test_unknown_kind_is_error_payload():
    """Unknown kind returns error payload naming valid kinds; no request attempted."""
    result = slack.post("xoxb-t", "C1", "meme", "x")
    assert result["ok"] is False
    assert "digest" in result["error"] and "alert" in result["error"]


def test_token_never_in_logs_on_failure():
    from structlog.testing import capture_logs

    def handler(request):
        return httpx.Response(500, text="boom")

    with capture_logs() as logs:
        slack.post("xoxb-SECRET", "C1", "alert", "x", http=_client(handler))
    assert "SECRET" not in str(logs)
```

(The old `test_malformed_webhook_url_never_raises` is dropped — the URL is now
a module constant, not caller input.)

- [ ] **Step 2: Run the slack tests to verify they fail**

Run: `.venv/bin/pytest tests/test_slack.py -v`
Expected: FAIL — `TypeError` (post() got unexpected/missing arguments) on every `post` test; truncation test still passes.

- [ ] **Step 3: Update `tests/test_config.py` for the new settings**

At line 19 (inside `test_m1_defaults`), replace
`assert s.slack_webhook_url == ""` with:

```python
    assert s.slack_bot_token == "" and s.slack_channel_id == ""
```

and at the top of `test_m1_defaults`, replace
`monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)` with:

```python
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
```

In `test_m1_env_overrides` (lines 25-28), replace the webhook lines with:

```python
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CENV")
```

and

```python
    assert s.slack_bot_token == "xoxb-env-token"
    assert s.slack_channel_id == "CENV"
```

- [ ] **Step 4: Run config tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'slack_bot_token'`.

- [ ] **Step 5: Implement `querlock/config.py`**

Replace line 13 (`slack_webhook_url: str = ""`) with:

```python
    slack_bot_token: str = ""
    slack_channel_id: str = ""
```

(`SLACK_APP_TOKEN` in `.env` stays unused — `extra="ignore"` tolerates it.)

- [ ] **Step 6: Implement `querlock/slack.py`**

Replace the whole file with:

```python
"""Slack bot-token reporting for #quentin-bot (spec 2026-07-21-slack-token-design).

Posts via chat.postMessage with a Bearer token; the target channel comes from
SLACK_CHANNEL_ID. Failures are logged and returned as payloads — NEVER raised.
Reporting must never break the pipeline. The 3500-char cap is enforced here in
code, not in doctrine.
"""
import httpx
import structlog

MAX_CHARS = 3500
_POINTER = "\n…[truncated — full details in casefile.db / patrol transcript]"
_HEADERS = {"digest": ":mag: *Querlock digest*", "alert": ":rotating_light: *Querlock alert*"}
_API_URL = "https://slack.com/api/chat.postMessage"

log = structlog.get_logger()


def truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[: MAX_CHARS - len(_POINTER)] + _POINTER, True


def post(token: str, channel: str, kind: str, text: str,
         http: httpx.Client | None = None) -> dict:
    if kind not in _HEADERS:
        return {"ok": False, "error": f"unknown kind {kind!r} — use 'digest' or 'alert'"}
    if not token or not channel:
        return {"ok": False, "error": "SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not configured"}
    body, truncated = truncate(f"{_HEADERS[kind]}\n{text}")
    client = http or httpx.Client(timeout=15)
    try:
        resp = client.post(_API_URL, json={"channel": channel, "text": body},
                           headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 300:
            log.warning("slack_post_failed", status=resp.status_code)
            return {"ok": False, "error": f"slack post failed: HTTP {resp.status_code}"}
        payload = resp.json()
        if not payload.get("ok", False):
            # chat.postMessage reports API errors as HTTP 200 + ok=false
            err = str(payload.get("error", "unknown"))
            log.warning("slack_post_failed", slack_error=err)
            return {"ok": False, "error": f"slack api error: {err}"}
        return {"ok": True, "chars": len(body), "truncated": truncated}
    except Exception as exc:
        # never include the token — it is a secret
        log.warning("slack_post_failed", error=type(exc).__name__)
        return {"ok": False, "error": f"slack post failed: {type(exc).__name__}"}
    finally:
        if http is None:
            client.close()
```

(A non-JSON 2xx body makes `resp.json()` raise; the outer `except` turns that
into the standard error payload — that is what the new test asserts.)

- [ ] **Step 7: Run slack + config tests to verify they pass**

Run: `.venv/bin/pytest tests/test_slack.py tests/test_config.py -v`
Expected: PASS (all).

- [ ] **Step 8: Update call site in `querlock/agent/tools.py`**

Lines 154-157 — replace with:

```python
    @tool("post_slack", "Post a digest or alert to the configured Slack channel. "
          "kind: 'digest' or 'alert'.", {"kind": str, "text": str})
    async def post_slack_handler(args: dict) -> dict:
        result = slack.post(settings.slack_bot_token, settings.slack_channel_id,
                            args["kind"], args["text"])
        return _text(result)
```

- [ ] **Step 9: Update the 4 call sites in `querlock/agent/patrol.py`**

Each `slack.post(settings.slack_webhook_url, <kind>, <text>)` becomes
`slack.post(settings.slack_bot_token, settings.slack_channel_id, <kind>, <text>)`.
The four sites (message text unchanged):

```python
            slack.post(settings.slack_bot_token, settings.slack_channel_id,
                       "alert", f"Patrol aborted — {msg}")
```
(twice — replica unreachable at ~line 83, schema drift at ~line 91)

```python
            slack.post(settings.slack_bot_token, settings.slack_channel_id, "alert",
                       f"Patrol {patrol_id} ({scope}) FAILED: {error}")
```

```python
        slack.post(settings.slack_bot_token, settings.slack_channel_id, "digest",
                   f"Patrol {patrol_id} ({scope}) done: {result_msg.num_turns} turns, "
                   f"{(result_msg.duration_ms or 0) // 60000}m, cost {cost}, "
                   f"LegiScan {ls_calls_display}/{settings.legiscan_monthly_budget} this month.")
```

- [ ] **Step 10: Update `tests/test_agent_tools.py`**

Rename the fixtures (lines 23-32) and drop the webhook wording:

```python
@pytest.fixture
def settings_no_slack(settings):
    return settings


@pytest.fixture
def settings_with_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
                     slack_bot_token="xoxb-test", slack_channel_id="C123")
```

Replace the two post_slack tests (lines 238-259) with:

```python
async def test_post_slack_bad_kind_and_missing_config(settings_no_slack):
    _server, handlers = build_toolkit(settings_no_slack, return_handlers=True)
    result = await handlers["post_slack"]({"kind": "meme", "text": "x"})
    payload = json.loads(result["content"][0]["text"])
    assert "digest" in payload["error"]  # error names valid kinds

    result2 = await handlers["post_slack"]({"kind": "digest", "text": "x"})
    payload2 = json.loads(result2["content"][0]["text"])
    assert "not configured" in payload2["error"]  # payload, not exception


async def test_post_slack_passes_token_and_channel_from_settings(monkeypatch, settings_with_slack):
    seen = {}
    monkeypatch.setattr(
        "querlock.agent.tools.slack",
        types.SimpleNamespace(post=lambda token, channel, kind, text:
                              seen.update(token=token, channel=channel) or {"ok": True}),
    )
    _server, handlers = build_toolkit(settings_with_slack, return_handlers=True)
    result = await handlers["post_slack"]({"kind": "digest", "text": "hi"})
    payload = json.loads(result["content"][0]["text"])
    assert seen["token"] == "xoxb-test"
    assert seen["channel"] == "C123"
    assert payload == {"ok": True}
```

- [ ] **Step 11: Update `tests/test_patrol.py`**

Mechanical, five `make_settings` kwargs and three positional asserts:

- Every `slack_webhook_url="https://hooks.example/x"` (lines 134, 149, 197, 216, 234) becomes `slack_bot_token="xoxb-test", slack_channel_id="C123"`.
- The fake-slack `posts` capture keeps `lambda *a, **k: posts.append(a) or {"ok": True}` unchanged, but positional indices shift (new arg order is token, channel, kind, text):
  - lines 142, 168, 251: `posts[0][1] == "alert"` → `posts[0][2] == "alert"`
  - line 252: `"Patrol 1 (CA) FAILED" in posts[0][2]` → `in posts[0][3]`

- [ ] **Step 12: Run the full suite**

Run: `.venv/bin/pytest`
Expected: PASS — 138 tests (136 previous − 1 dropped malformed-URL + 1 new ok-false + 1 new non-JSON + 1 new token-not-in-logs), 0 failures. Also confirm nothing still references the old name: `grep -rn "slack_webhook_url\|SLACK_WEBHOOK_URL" querlock/ tests/` → no matches.

- [ ] **Step 13: Commit**

```bash
git add querlock/slack.py querlock/config.py querlock/agent/tools.py querlock/agent/patrol.py tests/test_slack.py tests/test_config.py tests/test_agent_tools.py tests/test_patrol.py
git commit -m "feat: Slack posting via bot token chat.postMessage (drops webhook)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Documentation updates

**Files:**
- Modify: `README.md:35` (prereqs line)
- Modify: `docs/superpowers/specs/2026-07-20-querlock-design.md:97,251` (supersession notes)

**Interfaces:**
- Consumes: Task 1's env var names `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID`.
- Produces: nothing code-visible.

- [ ] **Step 1: Update README prereqs**

Replace the sentence starting `Prereqs:` (line 35) so the paragraph reads:

```markdown
Prereqs: `.env` must have `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` (the bot
must be `/invite`d into #quentin-bot; `SLACK_APP_TOKEN` is unused) and
`QUORUM_REPLICA_DSN`, and the
`tsh proxy db` tunnel must be up (a dead tunnel produces a Slack alert + exit 2 —
that is the intended failure mode; the daily digest doubles as the liveness
heartbeat). Before the first scheduled run, pre-warm the cache once:
```

- [ ] **Step 2: Annotate the original spec**

In `docs/superpowers/specs/2026-07-20-querlock-design.md`:

Line 97, in the `post_slack` table row, change `Via webhook.` to
`Via bot token (superseded 2026-07-21, see 2026-07-21-slack-token-design.md).`

Line 251, change `SLACK_WEBHOOK_URL` to
`SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` (superseded 2026-07-21),
keeping the rest of the sentence intact. Leave the §73 ASCII-diagram "webhook"
label and all files under `docs/superpowers/plans/` untouched (historical).

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-20-querlock-design.md
git commit -m "docs: env prereqs + spec supersession notes for Slack bot token

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Live smoke — one real message to #quentin-bot (manual gate)

**Files:** none (verification only).

**Interfaces:**
- Consumes: `slack.post` and `Settings` from Task 1; the real `.env` values.

- [ ] **Step 1: Post one real digest through the new transport**

Run (from the repo root, where `.env` lives):

```bash
.venv/bin/python -c "
from querlock.config import Settings
from querlock import slack
s = Settings()
print(slack.post(s.slack_bot_token, s.slack_channel_id, 'digest',
                 'live smoke: querlock bot-token transport'))"
```

Expected: `{'ok': True, 'chars': ..., 'truncated': False}` and the message
visible in #quentin-bot.

Known failure modes (from the spec): `invalid_auth` → token pasted wrong;
`channel_not_found` → `SLACK_CHANNEL_ID` is not the channel's ID (open channel
details in Slack, copy the `C…` ID); `not_in_channel` → `/invite` the bot into
#quentin-bot first.

- [ ] **Step 2: Record the result**

If it fails, fix the `.env`/invite issue and re-run — do not change code for
credential problems. When `ok: True`, this closes the Slack half of the M1
live-smoke prerequisite (the replica half still needs the `tsh proxy db`
tunnel).
