import httpx

from sherlock import slack


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_post_success_includes_kind_header():
    seen = {}

    def handler(request):
        seen["json"] = request.read().decode()
        return httpx.Response(200, text="ok")

    result = slack.post("https://hooks.example/x", "digest", "hello",
                        http=_client(handler))
    assert result["ok"] is True and result["truncated"] is False
    assert "Sherlock digest" in seen["json"] and "hello" in seen["json"]


def test_no_webhook_configured():
    result = slack.post("", "digest", "hello")
    assert result == {"ok": False, "error": "SLACK_WEBHOOK_URL not configured"}


def test_http_error_never_raises_and_never_leaks_url():
    def handler(request):
        return httpx.Response(500, text="boom")

    result = slack.post("https://hooks.example/SECRET", "alert", "x",
                        http=_client(handler))
    assert result["ok"] is False and "SECRET" not in result["error"]


def test_connect_error_never_raises():
    def handler(request):
        raise httpx.ConnectError("nope")

    result = slack.post("https://hooks.example/x", "digest", "x",
                        http=_client(handler))
    assert result["ok"] is False


def test_truncation_at_cap_with_pointer():
    body = "y" * 5000
    text, truncated = slack.truncate(body)
    assert truncated is True and len(text) <= slack.MAX_CHARS
    assert "casefile.db" in text
