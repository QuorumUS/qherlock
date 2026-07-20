import asyncio
import json
import os

import typer

from sherlock.agent.patrol import run_patrol
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings
from sherlock.diff.service import diff_state as run_diff
from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient
from sherlock.legiscan.sync import sync_state
from sherlock.quorum import reader

app = typer.Typer(help="Sherlock — LegiScan vs Quorum data-integrity patroller")


def _settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


class _NoNetworkClient:
    """SHERLOCK_TEST_MODE stand-in: makes `sync` exercisable without network."""

    def get_session_list(self, state):
        return []

    def get_dataset_list(self, state):
        return []

    def get_master_list_raw(self, session_id):
        return {"session": {}}

    def close(self) -> None:
        pass


@app.command()
def sync(state: str = typer.Option("CA", "--state")) -> None:
    """Refresh the LegiScan cache for STATE."""
    s = _settings()
    with LegiScanCache(s.data_dir / "cache.db") as cache:
        if os.environ.get("SHERLOCK_TEST_MODE") == "1":
            client = _NoNetworkClient()
        else:
            client = LegiScanClient(s.legiscan_api_key, on_call=lambda op: cache.add_call(op))
        try:
            stats = sync_state(state.upper(), client, cache)
        finally:
            client.close()
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def diff(state: str = typer.Option("CA", "--state")) -> None:
    """Diff LegiScan cache vs Quorum replica for STATE."""
    s = _settings()
    if not s.quorum_replica_dsn:
        typer.echo("error: QUORUM_REPLICA_DSN not set — run `tsh proxy db` and set it in .env")
        raise typer.Exit(code=1)
    with LegiScanCache(s.data_dir / "cache.db") as cache, \
         CaseFileStore(s.data_dir / "casefile.db") as casefile:
        try:
            conn = reader.connect(s.quorum_replica_dsn)
        except Exception as exc:
            typer.echo(f"error: replica connection failed: {type(exc).__name__}")
            raise typer.Exit(code=2)
        try:
            ok, err = reader.check_schema(conn)
            if not ok:
                typer.echo(f"error: {err}")
                raise typer.Exit(code=2)
            summary = run_diff(state.upper(), cache, casefile, conn)
        finally:
            conn.close()
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def patrol(state: str = typer.Option("CA", "--state"),
           objective: str = typer.Option("", "--objective")) -> None:
    """Run a full agentic patrol for STATE (calls the Anthropic API)."""
    s = _settings()
    report = asyncio.run(run_patrol(s, state.upper(), objective))
    typer.echo(report)


if __name__ == "__main__":
    app()
