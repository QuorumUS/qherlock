import json
import types

import pytest

from qherlock.agent.tools import TOOL_NAMES, build_toolkit
from qherlock.casefiles.models import Anomaly
from qherlock.casefiles.store import CaseFileStore
from qherlock.config import Settings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


@pytest.fixture
def settings_no_dsn(settings):
    return settings


@pytest.fixture
def settings_no_slack(settings):
    return settings


@pytest.fixture
def settings_with_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
                     slack_bot_token="xoxb-test", slack_channel_id="C123")


def test_tool_names_exact_six():
    assert TOOL_NAMES == (
        "mcp__qherlock__legiscan_sync",
        "mcp__qherlock__diff",
        "mcp__qherlock__list_anomalies",
        "mcp__qherlock__get_anomaly",
        "mcp__qherlock__investigate_bill",
        "mcp__qherlock__post_slack",
    )


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


async def test_sync_scope_all_routes_to_sync_many(monkeypatch, settings):
    seen = {}

    def fake_sync_many(regions, *a, **k):
        seen["regions"] = list(regions)
        return {"ok": 1}

    monkeypatch.setattr("qherlock.agent.tools.sync_many", fake_sync_many)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["legiscan_sync"]({"scope": "all"})
    payload = json.loads(result["content"][0]["text"])
    assert len(seen["regions"]) == 51
    assert payload == {"ok": 1}


async def test_sync_single_region_routes_to_sync_state(monkeypatch, settings):
    seen = {}

    def fake_sync_state(state, *a, **k):
        seen["state"] = state
        return {"state": state}

    monkeypatch.setattr("qherlock.agent.tools.sync_state", fake_sync_state)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["legiscan_sync"]({"scope": "ca"})
    payload = json.loads(result["content"][0]["text"])
    assert seen["state"] == "CA"
    assert payload["state"] == "CA"


async def test_sync_invalid_scope_is_error_payload(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["legiscan_sync"]({"scope": "XX"})
    payload = json.loads(result["content"][0]["text"])
    assert "unknown region" in payload["error"]


async def test_legiscan_sync_wires_quota_to_cache(settings, monkeypatch):
    from qherlock.agent import tools as tools_mod
    from qherlock.legiscan.cache import LegiScanCache

    def fake_sync(state, client, cache, **kwargs):
        client._on_call("getSessionList")  # simulate one API call through the hook
        return {"state": state, "degraded": False}

    monkeypatch.setattr(tools_mod, "sync_state", fake_sync)
    _server, handlers = tools_mod.build_toolkit(settings, return_handlers=True)
    result = await handlers["legiscan_sync"]({"scope": "ca"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["state"] == "CA"
    with LegiScanCache(settings.data_dir / "cache.db") as cache:
        assert cache.calls_this_month() == 1


async def test_diff_no_dsn_error_payload(settings_no_dsn):
    _server, handlers = build_toolkit(settings_no_dsn, return_handlers=True)
    result = await handlers["diff"]({"scope": "CA"})
    payload = json.loads(result["content"][0]["text"])
    assert "QUORUM_REPLICA_DSN" in payload["error"]


class _FakeConn:
    def close(self):
        pass


async def test_diff_connection_failure_does_not_leak_dsn(monkeypatch, settings):
    settings.quorum_replica_dsn = "postgresql://user:password=SECRET@host/db"

    def boom(dsn):
        raise RuntimeError(f"connection failed: password=SECRET")

    monkeypatch.setattr("qherlock.agent.tools.reader.connect", boom)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["diff"]({"scope": "CA"})
    payload = json.loads(result["content"][0]["text"])
    assert "password=SECRET" not in json.dumps(payload)
    assert payload["error"] == "replica connection failed: RuntimeError"


async def test_diff_scope_all_routes_to_diff_many(monkeypatch, settings, tmp_path):
    settings.quorum_replica_dsn = "postgresql://fake"
    monkeypatch.setattr("qherlock.agent.tools.reader.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr("qherlock.agent.tools.reader.check_schema", lambda conn: (True, None))
    seen = {}

    def fake_diff_many(regions, *a, **k):
        seen["regions"] = list(regions)
        return {"ok": 1}

    monkeypatch.setattr("qherlock.agent.tools.diff_many", fake_diff_many)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["diff"]({"scope": "all"})
    payload = json.loads(result["content"][0]["text"])
    assert len(seen["regions"]) == 51
    assert payload == {"ok": 1}


async def test_diff_single_region_routes_to_diff_region(monkeypatch, settings):
    settings.quorum_replica_dsn = "postgresql://fake"
    monkeypatch.setattr("qherlock.agent.tools.reader.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr("qherlock.agent.tools.reader.check_schema", lambda conn: (True, None))
    seen = {}

    def fake_diff_region(region, *a, **k):
        seen["region"] = region
        return {"region": region}

    monkeypatch.setattr("qherlock.agent.tools.diff_region", fake_diff_region)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["diff"]({"scope": "ca"})
    payload = json.loads(result["content"][0]["text"])
    assert seen["region"] == "CA"
    assert payload["region"] == "CA"


async def test_investigate_bill_non_integer_session(settings):
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["investigate_bill"]({"state": "CA", "session": "abc", "number": "AB1"})
    payload = json.loads(result["content"][0]["text"])
    assert "session_id" in payload["error"]
    assert "session_key" in payload["error"]


async def test_investigate_bill_no_dsn_error_payload(settings_no_dsn):
    _server, handlers = build_toolkit(settings_no_dsn, return_handlers=True)
    result = await handlers["investigate_bill"]({"state": "CA", "session": "123", "number": "AB1"})
    payload = json.loads(result["content"][0]["text"])
    assert "QUORUM_REPLICA_DSN" in payload["error"]


async def test_investigate_bill_connection_failure_does_not_leak_dsn(monkeypatch, settings):
    settings.quorum_replica_dsn = "postgresql://user:password=SECRET@host/db"

    def boom(dsn):
        raise RuntimeError("connection failed: password=SECRET")

    monkeypatch.setattr("qherlock.agent.tools.reader.connect", boom)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["investigate_bill"]({"state": "CA", "session": "123", "number": "AB1"})
    payload = json.loads(result["content"][0]["text"])
    assert "password=SECRET" not in json.dumps(payload)
    assert payload["error"] == "replica connection failed: RuntimeError"


async def test_investigate_bill_calls_investigate_with_quota_hook(monkeypatch, settings):
    settings.quorum_replica_dsn = "postgresql://fake"
    monkeypatch.setattr("qherlock.agent.tools.reader.connect", lambda dsn: _FakeConn())
    monkeypatch.setattr("qherlock.agent.tools.reader.check_schema", lambda conn: (True, None))
    seen = {}

    def fake_investigate(state, session_id, number, client, cache, replica_conn, **kwargs):
        client._on_call("getBill")
        seen.update(state=state, session_id=session_id, number=number)
        return {"state": state, "session_id": session_id}

    monkeypatch.setattr("qherlock.agent.tools.investigate", fake_investigate)
    _server, handlers = build_toolkit(settings, return_handlers=True)
    result = await handlers["investigate_bill"]({"state": "ca", "session": "123", "number": "AB1"})
    payload = json.loads(result["content"][0]["text"])
    assert seen == {"state": "CA", "session_id": 123, "number": "AB1"}
    assert payload["session_id"] == 123
    from qherlock.legiscan.cache import LegiScanCache
    with LegiScanCache(settings.data_dir / "cache.db") as cache:
        assert cache.calls_this_month() == 1


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
        "qherlock.agent.tools.slack",
        types.SimpleNamespace(post=lambda token, channel, kind, text:
                              seen.update(token=token, channel=channel) or {"ok": True}),
    )
    _server, handlers = build_toolkit(settings_with_slack, return_handlers=True)
    result = await handlers["post_slack"]({"kind": "digest", "text": "hi"})
    payload = json.loads(result["content"][0]["text"])
    assert seen["token"] == "xoxb-test"
    assert seen["channel"] == "C123"
    assert payload == {"ok": True}
