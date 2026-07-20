import httpx
import pytest

from sherlock.legiscan.client import BASE_URL, LegiScanClient, LegiScanError


def make_client(handler, on_call=None):
    transport = httpx.MockTransport(handler)
    return LegiScanClient(
        "k", http=httpx.Client(transport=transport, base_url=BASE_URL), on_call=on_call
    )


def test_get_session_list_unwraps_and_counts_calls():
    calls = []

    def handler(request):
        assert request.url.params["key"] == "k"
        assert request.url.params["op"] == "getSessionList"
        assert request.url.params["state"] == "CA"
        return httpx.Response(
            200,
            json={"status": "OK", "sessions": [{"session_id": 2172, "year_start": 2025}]},
        )

    client = make_client(handler, on_call=calls.append)
    sessions = client.get_session_list("CA")
    assert sessions == [{"session_id": 2172, "year_start": 2025}]
    assert calls == ["getSessionList"]


def test_non_ok_status_raises():
    def handler(request):
        return httpx.Response(200, json={"status": "ERROR", "alert": {"message": "bad key"}})

    client = make_client(handler)
    with pytest.raises(LegiScanError):
        client.get_session_list("CA")
