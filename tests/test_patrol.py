import io
import json

from sherlock.agent.patrol import DOCTRINE, build_options, write_transcript_line
from sherlock.agent.tools import TOOL_NAMES, build_toolkit
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


def test_write_transcript_line_is_jsonl():
    class FakeMsg:
        def __str__(self):
            return "hello"

    fh = io.StringIO()
    write_transcript_line(fh, FakeMsg())
    line = json.loads(fh.getvalue())
    assert line == {"type": "FakeMsg", "repr": "hello"}
