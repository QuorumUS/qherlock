import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings
from sherlock.diff.service import diff_state as run_diff_state
from sherlock.legiscan.cache import LegiScanCache
from sherlock.legiscan.client import LegiScanClient
from sherlock.legiscan.sync import sync_state
from sherlock.quorum import reader

TOOL_NAMES = (
    "mcp__sherlock__legiscan_sync",
    "mcp__sherlock__diff_state",
    "mcp__sherlock__list_anomalies",
    "mcp__sherlock__get_anomaly",
)

_EVIDENCE_CAP = 1500


def _text(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _bounded(payload: dict) -> dict:
    if "evidence" in payload and payload["evidence"] is not None:
        raw = json.dumps(payload["evidence"], default=str)
        if len(raw) > _EVIDENCE_CAP:
            payload["evidence"] = raw[:_EVIDENCE_CAP] + "…[truncated]"
    return payload


def build_toolkit(settings: Settings, return_handlers: bool = False):
    settings.ensure_dirs()
    cache_path = settings.data_dir / "cache.db"
    casefile_path = settings.data_dir / "casefile.db"

    @tool("legiscan_sync", "Refresh the local LegiScan cache for a state (datasets + "
          "masterlist change-hashes). Budget-aware; read-only.", {"state": str})
    async def legiscan_sync_handler(args: dict) -> dict:
        with LegiScanCache(cache_path) as cache:
            client = LegiScanClient(settings.legiscan_api_key, on_call=lambda op: cache.add_call(op))
            try:
                stats = sync_state(args["state"].upper(), client, cache)
            finally:
                client.close()
        return _text(stats)

    @tool("diff_state", "Diff LegiScan cache vs Quorum replica for a state's current "
          "sessions. Records missing_bill anomalies; returns summary + top cases.",
          {"state": str})
    async def diff_state_handler(args: dict) -> dict:
        if not settings.quorum_replica_dsn:
            return _text({"error": "no QUORUM_REPLICA_DSN configured — start a Teleport "
                                   "tunnel (tsh proxy db) and set it in .env"})
        with LegiScanCache(cache_path) as cache, CaseFileStore(casefile_path) as casefile:
            try:
                conn = reader.connect(settings.quorum_replica_dsn)
            except Exception as exc:
                return _text({"error": f"replica connection failed: {exc}"})
            try:
                ok, err = reader.check_schema(conn)
                if not ok:
                    return _text({"error": f"replica schema drift: {err}"})
                summary = run_diff_state(args["state"].upper(), cache, casefile, conn)
            finally:
                conn.close()
        return _text(summary)

    @tool("list_anomalies", "List recorded anomalies from case files. Optional filters: "
          "state, gap_type, status. Max 10 rows.",
          {"type": "object",
           "properties": {"state": {"type": "string"}, "gap_type": {"type": "string"},
                           "status": {"type": "string"}},
           "required": []})
    async def list_anomalies_handler(args: dict) -> dict:
        with CaseFileStore(casefile_path) as casefile:
            rows = casefile.list_anomalies(
                region=args.get("state") or None, gap_type=args.get("gap_type") or None,
                status=args.get("status") or None, limit=10,
            )
        return _text({"anomalies": rows})

    @tool("get_anomaly", "Fetch one anomaly with full (bounded) evidence by id.",
          {"anomaly_id": int})
    async def get_anomaly_handler(args: dict) -> dict:
        with CaseFileStore(casefile_path) as casefile:
            row = casefile.get_anomaly(int(args["anomaly_id"]))
        if row is None:
            return _text({"error": f"anomaly {args['anomaly_id']} not found"})
        return _text(_bounded(row))

    sdk_tools = [legiscan_sync_handler, diff_state_handler,
                 list_anomalies_handler, get_anomaly_handler]
    server = create_sdk_mcp_server(name="sherlock", version="0.1.0", tools=sdk_tools)
    if return_handlers:
        handlers = {"legiscan_sync": legiscan_sync_handler.handler,
                    "diff_state": diff_state_handler.handler,
                    "list_anomalies": list_anomalies_handler.handler,
                    "get_anomaly": get_anomaly_handler.handler}
        return server, handlers
    return server, TOOL_NAMES
