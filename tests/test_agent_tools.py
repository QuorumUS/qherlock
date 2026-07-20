import json

import pytest

from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


def test_tool_names_are_fully_qualified():
    assert TOOL_NAMES == [
        "mcp__sherlock__legiscan_sync",
        "mcp__sherlock__diff_state",
        "mcp__sherlock__list_anomalies",
        "mcp__sherlock__get_anomaly",
    ]


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


async def test_get_anomaly_not_found(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["get_anomaly"]({"anomaly_id": 999})
    payload = json.loads(result["content"][0]["text"])
    assert "not found" in payload["error"]
