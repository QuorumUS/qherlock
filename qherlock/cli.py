import asyncio
import json
import os

import typer

from qherlock.agent.patrol import PatrolFatalError, run_patrol
from qherlock.casefiles.store import CaseFileStore
from qherlock.config import Settings
from qherlock.diff.service import diff_many, diff_region
from qherlock.legiscan.cache import LegiScanCache
from qherlock.legiscan.client import LegiScanClient
from qherlock.legiscan.sync import sync_many, sync_state
from qherlock.quorum import reader
from qherlock.regions import parse_scope

app = typer.Typer(help="Qherlock — LegiScan vs Quorum data-integrity patroller")


def _settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


class _NoNetworkClient:
    """QHERLOCK_TEST_MODE stand-in: makes `sync` exercisable without network."""

    def get_session_list(self, state):
        return []

    def get_dataset_list(self, state):
        return []

    def get_master_list_raw(self, session_id):
        return {"session": {}}

    def close(self) -> None:
        pass


def _resolve_scope(scope: str, state: str) -> list[str]:
    if state:
        typer.echo("note: --state is deprecated; use --scope")
        scope = state
    try:
        return parse_scope(scope)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def sync(scope: str = typer.Option("all", "--scope", help='"all", "CA", or "CA,TX"'),
          state: str = typer.Option("", "--state", help="deprecated alias for --scope")) -> None:
    """Refresh the LegiScan cache for SCOPE."""
    regions = _resolve_scope(scope, state)
    s = _settings()
    with LegiScanCache(s.data_dir / "cache.db") as cache:
        client = (_NoNetworkClient() if os.environ.get("QHERLOCK_TEST_MODE") == "1"
                  else LegiScanClient(s.legiscan_api_key, on_call=lambda op: cache.add_call(op)))
        try:
            if len(regions) == 1:
                stats = sync_state(regions[0], client, cache,
                                    budget_limit=s.legiscan_monthly_budget)
            else:
                stats = sync_many(regions, client, cache,
                                   budget_limit=s.legiscan_monthly_budget)
        finally:
            client.close()
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def diff(scope: str = typer.Option("all", "--scope", help='"all", "CA", or "CA,TX"'),
         state: str = typer.Option("", "--state", help="deprecated alias for --scope")) -> None:
    """Diff LegiScan cache vs Quorum replica for SCOPE."""
    regions = _resolve_scope(scope, state)
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
            if len(regions) == 1:
                summary = diff_region(regions[0], cache, casefile, conn,
                                       sla_hours=s.qherlock_freshness_sla_hours)
            else:
                summary = diff_many(regions, cache, casefile, conn,
                                     sla_hours=s.qherlock_freshness_sla_hours)
        finally:
            conn.close()
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def patrol(scope: str = typer.Option("all", "--scope"),
           state: str = typer.Option("", "--state", help="deprecated alias for --scope"),
           objective: str = typer.Option("", "--objective")) -> None:
    """Run a full agentic patrol over SCOPE (calls the Anthropic API)."""
    regions = _resolve_scope(scope, state)  # early validation
    if state:
        scope = state.upper()
    if scope.lower() != "all":
        # Canonicalize so the recorded/prompted scope matches what was actually
        # patrolled (e.g. "ca" -> "CA"); "all" stays as-is rather than being
        # expanded into the full 51-region list.
        scope = ",".join(regions)
    s = _settings()
    try:
        report = asyncio.run(run_patrol(s, scope, objective))
    except PatrolFatalError as exc:
        typer.echo(f"fatal: {exc}")   # alert already posted inside run_patrol
        raise typer.Exit(code=2)
    typer.echo(report)


if __name__ == "__main__":
    app()
