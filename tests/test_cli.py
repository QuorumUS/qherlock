import pytest
from typer.testing import CliRunner

from qherlock.agent.patrol import PatrolFatalError
from qherlock.cli import app

runner = CliRunner()


@pytest.fixture
def fake_env(tmp_path):
    return {
        "LEGISCAN_API_KEY": "k",
        "DATA_DIR": str(tmp_path / "data"),
        "RUNS_DIR": str(tmp_path / "runs"),
    }


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("sync", "diff", "patrol"):
        assert cmd in result.output


def test_sync_command_runs_with_fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "k")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    # No network in tests: point sync at a fake client via QHERLOCK_TEST_MODE guard.
    monkeypatch.setenv("QHERLOCK_TEST_MODE", "1")
    result = runner.invoke(app, ["sync", "--state", "CA"])
    assert result.exit_code == 0
    assert '"degraded"' in result.output


def test_sync_scope_list_test_mode(fake_env):
    result = runner.invoke(app, ["sync", "--scope", "CA,TX"],
                            env={"QHERLOCK_TEST_MODE": "1", **fake_env})
    assert result.exit_code == 0 and '"synced"' in result.output


def test_sync_state_alias_deprecation(fake_env):
    result = runner.invoke(app, ["sync", "--state", "CA"],
                            env={"QHERLOCK_TEST_MODE": "1", **fake_env})
    assert result.exit_code == 0 and "deprecated" in result.output.lower()


def test_sync_invalid_scope_names_code(fake_env):
    result = runner.invoke(app, ["sync", "--scope", "XX"], env=fake_env)
    assert result.exit_code != 0 and "XX" in result.output


def test_patrol_fatal_exits_2(fake_env, monkeypatch):
    async def boom(*a, **k):
        raise PatrolFatalError("replica unreachable: OSError")
    monkeypatch.setattr("qherlock.cli.run_patrol", boom)
    result = runner.invoke(app, ["patrol", "--scope", "CA"], env=fake_env)
    assert result.exit_code == 2 and "fatal" in result.output
