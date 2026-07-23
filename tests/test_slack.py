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
