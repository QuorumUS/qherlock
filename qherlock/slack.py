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
_HEADERS = {"digest": ":mag: *Qherlock digest*", "alert": ":rotating_light: *Qherlock alert*"}

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
    except Exception as exc:
        # never include the webhook URL — it is a secret
        log.warning("slack_post_failed", error=type(exc).__name__)
        return {"ok": False, "error": f"slack post failed: {type(exc).__name__}"}
    finally:
        if http is None:
            client.close()
