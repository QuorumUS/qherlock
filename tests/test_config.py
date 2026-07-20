from pathlib import Path

from sherlock.config import Settings


def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    assert s.legiscan_api_key == "test-key"
    assert s.sherlock_model == "claude-sonnet-5"
    assert s.sherlock_max_turns == 100
    s.ensure_dirs()
    assert (tmp_path / "data").is_dir() and (tmp_path / "runs").is_dir()
