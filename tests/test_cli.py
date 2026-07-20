from typer.testing import CliRunner

from sherlock.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("sync", "diff", "patrol"):
        assert cmd in result.output


def test_sync_command_runs_with_fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    # No network in tests: point sync at a fake client via SHERLOCK_TEST_MODE guard.
    monkeypatch.setenv("SHERLOCK_TEST_MODE", "1")
    result = runner.invoke(app, ["sync", "--state", "CA"])
    assert result.exit_code == 0
    assert '"degraded"' in result.output
