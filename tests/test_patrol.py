import io
import json

import pytest

from sherlock.agent.patrol import DOCTRINE, build_options, write_transcript_line
from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings


def make_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    return Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")


def test_doctrine_mentions_every_tool_and_forbids_guessing():
    for name in ("legiscan_sync", "diff_state", "list_anomalies", "get_anomaly"):
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
    assert "sherlock" in options.mcp_servers
    assert options.permission_mode == "bypassPermissions"
    assert options.mcp_servers["sherlock"] is server


def test_write_transcript_line_is_jsonl():
    class FakeMsg:
        def __str__(self):
            return "hello"

    fh = io.StringIO()
    write_transcript_line(fh, FakeMsg())
    line = json.loads(fh.getvalue())
    assert line == {"type": "FakeMsg", "repr": "hello"}


class FakeResultMessage:
    def __init__(self, result):
        self.result = result


async def test_run_patrol_happy_path_finishes_and_returns(tmp_path, monkeypatch):
    from sherlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch)

    async def fake_query(prompt, options):
        assert prompt == "Patrol CA."
        yield FakeResultMessage("patrol report")

    monkeypatch.setattr(patrol_mod, "query", fake_query)
    monkeypatch.setattr(patrol_mod, "ResultMessage", FakeResultMessage)
    result = await patrol_mod.run_patrol(settings, "CA")
    assert result == "patrol report"
    transcript = settings.runs_dir / "patrol-1.jsonl"
    assert transcript.exists()
    with CaseFileStore(settings.data_dir / "casefile.db") as store:
        row = store._conn.execute("SELECT finished_at, stats_json FROM patrols WHERE id = 1").fetchone()
        assert row["finished_at"] is not None
        assert '"result_chars": 13' in row["stats_json"]


async def test_run_patrol_finishes_row_on_midstream_error(tmp_path, monkeypatch):
    from sherlock.agent import patrol as patrol_mod

    settings = make_settings(tmp_path, monkeypatch)

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
