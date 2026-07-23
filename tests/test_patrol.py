import io
import json
import types

import pytest

from querlock.agent.patrol import DOCTRINE, PatrolFatalError, build_options, write_transcript_line
from querlock.agent.tools import TOOL_NAMES, build_toolkit
from querlock.casefiles.store import CaseFileStore
from querlock.config import Settings


def make_settings(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs", **kwargs)


def test_doctrine_names_all_six_tools():
    for name in ("legiscan_sync", "diff", "list_anomalies", "get_anomaly",
                 "investigate_bill", "post_slack"):
        assert name in DOCTRINE
    assert "Never invent" in DOCTRINE


def test_build_options_wires_model_tools_and_turns(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)
    assert options.model == "claude-sonnet-5"
    assert options.max_turns == 100
    assert options.allowed_tools == list(TOOL_NAMES)
    assert options.system_prompt == DOCTRINE
    assert "querlock" in options.mcp_servers
    assert options.tools == []
    assert options.permission_mode is None
    assert options.mcp_servers["querlock"] is server


def test_options_allow_exactly_six_tools(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)
    assert len(options.allowed_tools) == 6


def test_build_options_prefers_oauth_token_over_api_key(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    settings.claude_code_oauth_token = "oat-test-token"
    settings.anthropic_api_key = "sk-test-key"
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)
    assert options.env == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-test-token"}


def test_build_options_no_credentials_uses_logged_in_claude(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)
    assert not options.env  # spawned claude CLI falls back to the host's logged-in OAuth


def test_doctrine_digest_targets_small_size():
    from querlock.agent.patrol import DOCTRINE
    assert "1000 character" in DOCTRINE or "1,000 character" in DOCTRINE
    assert "resolved" in DOCTRINE.lower()


def test_write_transcript_line_is_jsonl():
    class FakeMsg:
        def __str__(self):
            return "hello"

    fh = io.StringIO()
    write_transcript_line(fh, FakeMsg())
    line = json.loads(fh.getvalue())
    assert line == {"type": "FakeMsg", "repr": "hello"}


class FakeResultMessage:
    def __init__(self, result, num_turns=1, duration_ms=1000, total_cost_usd=0.01, usage=None):
        self.result = result
        self.num_turns = num_turns
        self.duration_ms = duration_ms
        self.total_cost_usd = total_cost_usd
        self.usage = usage if usage is not None else {"input_tokens": 1}


def _raise_oserror(*a, **k):
    raise OSError("connection refused")


async def test_run_patrol_happy_path_finishes_and_returns(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch)
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        yield FakeResultMessage("patrol report")

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    result = await patrol_mod.run_patrol(settings, "CA")
    assert result == "patrol report"
    assert captured["prompt"] == "Patrol scope: CA."
    transcript = settings.runs_dir / "patrol-1.jsonl"
    assert transcript.exists()
    with CaseFileStore(settings.data_dir / "casefile.db") as store:
        row = store._conn.execute("SELECT finished_at, stats_json FROM patrols WHERE id = 1").fetchone()
        assert row["finished_at"] is not None
        assert '"result_chars": 13' in row["stats_json"]


async def test_no_dsn_skips_preflight_and_runs(tmp_path, monkeypatch):
    """No quorum_replica_dsn configured (the dev flow) — preflight is skipped entirely."""
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch)
    assert not settings.quorum_replica_dsn

    def boom(*a, **k):
        raise AssertionError("reader.connect should not be called when DSN is unset")

    monkeypatch.setattr(patrol_mod, "reader", types.SimpleNamespace(connect=boom))

    async def fake_query(prompt, options):
        yield FakeResultMessage("ok")

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    result = await patrol_mod.run_patrol(settings, "CA")
    assert result == "ok"


async def test_preflight_connect_failure_posts_alert_and_raises(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch, quorum_replica_dsn="postgresql://x",
                              slack_bot_token="xoxb-test", slack_channel_id="C123")
    posts = []
    monkeypatch.setattr(patrol_mod, "slack",
                         types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": True}))
    monkeypatch.setattr(patrol_mod, "reader", types.SimpleNamespace(connect=_raise_oserror))

    with pytest.raises(PatrolFatalError, match="replica unreachable: OSError"):
        await patrol_mod.run_patrol(settings, "all")
    assert posts and posts[0][2] == "alert"


async def test_preflight_schema_drift_posts_alert_and_raises(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch, quorum_replica_dsn="postgresql://x",
                              slack_bot_token="xoxb-test", slack_channel_id="C123")
    posts = []
    monkeypatch.setattr(patrol_mod, "slack",
                         types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": True}))

    closed = {"v": False}

    class FakeConn:
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(
        patrol_mod, "reader",
        types.SimpleNamespace(connect=lambda dsn: FakeConn(),
                               check_schema=lambda conn: (False, "missing table bill_bill")),
    )

    with pytest.raises(PatrolFatalError, match="schema drift"):
        await patrol_mod.run_patrol(settings, "all")
    assert posts and posts[0][2] == "alert"
    assert closed["v"] is True


async def test_run_patrol_stats_include_result_message_fields(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch)

    async def fake_query(prompt, options):
        yield FakeResultMessage("report text", num_turns=7, duration_ms=125000,
                                 total_cost_usd=None, usage={"input_tokens": 10})

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    await patrol_mod.run_patrol(settings, "CA")
    with CaseFileStore(settings.data_dir / "casefile.db") as store:
        row = store._conn.execute("SELECT stats_json FROM patrols WHERE id = 1").fetchone()
        stats = json.loads(row["stats_json"])
    assert stats["num_turns"] == 7
    assert stats["duration_ms"] == 125000
    assert stats["total_cost_usd"] is None
    assert stats["usage"] == {"input_tokens": 10}
    assert stats["legiscan_calls_month"] == 0


async def test_footer_digest_posted_when_slack_configured(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch, slack_bot_token="xoxb-test", slack_channel_id="C123")
    posts = []
    monkeypatch.setattr(patrol_mod, "slack",
                         types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": False}))

    async def fake_query(prompt, options):
        yield FakeResultMessage("report", num_turns=3, duration_ms=61000, total_cost_usd=0.42)

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    result = await patrol_mod.run_patrol(settings, "CA")
    assert result == "report"
    kinds = [p[2] for p in posts]
    assert "digest" in kinds  # deterministic stats footer, posted even though ok=False


async def test_footer_digest_shows_na_when_no_cost(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch, slack_bot_token="xoxb-test", slack_channel_id="C123")
    posts = []
    monkeypatch.setattr(patrol_mod, "slack",
                         types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": True}))

    async def fake_query(prompt, options):
        yield FakeResultMessage("report", num_turns=3, duration_ms=61000, total_cost_usd=None)

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    await patrol_mod.run_patrol(settings, "CA")
    digest_text = next(p[3] for p in posts if p[2] == "digest")
    assert "n/a (subscription)" in digest_text


async def test_run_patrol_finishes_row_on_midstream_error_and_posts_alert(tmp_path, monkeypatch):
    from querlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch, slack_bot_token="xoxb-test", slack_channel_id="C123")
    posts = []
    monkeypatch.setattr(patrol_mod, "slack",
                         types.SimpleNamespace(post=lambda *a, **k: posts.append(a) or {"ok": True}))

    async def exploding_query(prompt, options):
        yield FakeResultMessage("partial")
        raise RuntimeError("transport died")

    monkeypatch.setattr(patrol_mod, "query", exploding_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    with pytest.raises(RuntimeError, match="transport died"):
        await patrol_mod.run_patrol(settings, "CA")
    with CaseFileStore(settings.data_dir / "casefile.db") as store:
        row = store._conn.execute("SELECT finished_at, stats_json FROM patrols WHERE id = 1").fetchone()
        assert row["finished_at"] is not None
        assert "RuntimeError: transport died" in row["stats_json"]
    assert posts and posts[0][2] == "alert"
    assert "Patrol 1 (CA) FAILED" in posts[0][3]
    # no deterministic-stats digest footer on a mid-stream failure (no ResultMessage reached the end)
    assert not any(p[2] == "digest" for p in posts)
