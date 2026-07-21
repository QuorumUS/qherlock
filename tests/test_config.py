from sherlock.config import Settings


def test_settings_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")
    s = Settings(_env_file=None, data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    assert s.legiscan_api_key == "test-key"
    assert s.sherlock_model == "claude-sonnet-5"
    assert s.sherlock_max_turns == 100
    s.ensure_dirs()
    assert (tmp_path / "data").is_dir() and (tmp_path / "runs").is_dir()


def test_m1_defaults(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_webhook_url == ""
    assert s.sherlock_freshness_sla_hours == 72
    assert s.legiscan_monthly_budget == 30000


def test_m1_env_overrides(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    monkeypatch.setenv("SHERLOCK_FRESHNESS_SLA_HOURS", "24")
    s = Settings(legiscan_api_key="k", _env_file=None)
    assert s.slack_webhook_url == "https://hooks.slack.example/x"
    assert s.sherlock_freshness_sla_hours == 24
