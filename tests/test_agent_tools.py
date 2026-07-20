import json

import pytest

from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


def test_tool_names_are_fully_qualified():
    assert TOOL_NAMES == (
        "mcp__sherlock__legiscan_sync",
        "mcp__sherlock__diff_state",
        "mcp__sherlock__list_anomalies",
        "mcp__sherlock__get_anomaly",
    )


async def test_diff_state_without_dsn_returns_error_payload(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["diff_state"]({"state": "CA"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"].startswith("no QUORUM_REPLICA_DSN")


async def test_list_anomalies_empty_db(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["list_anomalies"]({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["anomalies"] == []


async def test_list_anomalies_filters_state_case_insensitively(settings):
    with CaseFileStore(settings.data_dir / "casefile.db") as casefile:
        casefile.upsert_anomaly(
            Anomaly(gap_type="missing_bill", region="CA", session_key="1", bill_number_norm="AB1")
        )
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["list_anomalies"]({"state": "ca"})
    payload = json.loads(result["content"][0]["text"])
    assert len(payload["anomalies"]) == 1


async def test_get_anomaly_not_found(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["get_anomaly"]({"anomaly_id": 999})
    payload = json.loads(result["content"][0]["text"])
    assert "not found" in payload["error"]


async def test_legiscan_sync_wires_quota_to_cache(settings, monkeypatch):
    from sherlock.agent import tools as tools_mod
    from sherlock.legiscan.cache import LegiScanCache

    def fake_sync(state, client, cache, **kwargs):
        client._on_call("getSessionList")  # simulate one API call through the hook
        return {"state": state, "degraded": False}

    monkeypatch.setattr(tools_mod, "sync_state", fake_sync)
    _server, handlers = tools_mod.build_toolkit(settings, return_handlers=True)
    result = await handlers["legiscan_sync"]({"state": "ca"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "CA"
    with LegiScanCache(settings.data_dir / "cache.db") as cache:
        assert cache.calls_this_month() == 1
