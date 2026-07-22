from qherlock.config import Settings


def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    assert s.legiscan_api_key == "test-key"
    assert s.qherlock_model == "claude-sonnet-5"
    assert s.qherlock_max_turns == 100
    s.ensure_dirs()
    assert (tmp_path / "data").is_dir() and (tmp_path / "runs").is_dir()


def test_m1_defaults(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("QHERLOCK_FRESHNESS_SLA_HOURS", raising=False)
    monkeypatch.delenv("LEGISCAN_MONTHLY_BUDGET", raising=False)
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_bot_token == "" and s.slack_channel_id == ""
    assert s.qherlock_freshness_sla_hours == 72
    assert s.legiscan_monthly_budget == 30000


def test_m1_env_overrides(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "CENV")
    monkeypatch.setenv("QHERLOCK_FRESHNESS_SLA_HOURS", "24")
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_bot_token == "xoxb-env-token"
    assert s.slack_channel_id == "CENV"
    assert s.qherlock_freshness_sla_hours == 24
