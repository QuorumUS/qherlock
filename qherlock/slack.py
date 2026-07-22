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
_HEADERS = {"digest": ":mag: *Qherlock digest*", "alert": ":rotating_light: *Qherlock alert*"}
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
