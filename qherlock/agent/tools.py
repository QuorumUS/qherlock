import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from qherlock import slack
from qherlock.casefiles.store import CaseFileStore
from qherlock.config import Settings
from qherlock.diff.service import diff_many, diff_region
from qherlock.investigate import investigate
from qherlock.legiscan.cache import LegiScanCache
from qherlock.legiscan.client import LegiScanClient
from qherlock.legiscan.sync import sync_many, sync_state
from qherlock.quorum import reader
from qherlock.regions import parse_scope

TOOL_NAMES = (
    "mcp__qherlock__legiscan_sync",
    "mcp__qherlock__diff",
    "mcp__qherlock__list_anomalies",
    "mcp__qherlock__get_anomaly",
    "mcp__qherlock__investigate_bill",
    "mcp__qherlock__post_slack",
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

    @tool("legiscan_sync", "Refresh the local LegiScan cache. scope: 'all' (50 states + "
          "US federal), one region ('CA'), or a comma list ('CA,TX'). Budget-aware; "
          "read-only.", {"scope": str})
    async def legiscan_sync_handler(args: dict) -> dict:
        try:
            regions = parse_scope(args["scope"])
        except ValueError as exc:
            return _text({"error": str(exc)})
        with LegiScanCache(cache_path) as cache:
            client = LegiScanClient(settings.legiscan_api_key, on_call=lambda op: cache.add_call(op))
            try:
                if len(regions) == 1:
                    result = sync_state(regions[0], client, cache,
                                         budget_limit=settings.legiscan_monthly_budget)
                else:
                    result = sync_many(regions, client, cache,
                                        budget_limit=settings.legiscan_monthly_budget)
            finally:
                client.close()
        return _text(result)

    @tool("diff", "Run all four detectors (missing_bill, incomplete_fields, stale, "
          "wrong_data) for the scope's current sessions vs the Quorum replica. Records "
          "anomalies; returns a bounded rollup with counts by gap type and region plus "
          "top cases by severity.", {"scope": str})
    async def diff_handler(args: dict) -> dict:
        try:
            regions = parse_scope(args["scope"])
        except ValueError as exc:
            return _text({"error": str(exc)})
        if not settings.quorum_replica_dsn:
            return _text({"error": "no QUORUM_REPLICA_DSN configured — start a Teleport "
                                   "tunnel (tsh proxy db) and set it in .env"})
        with LegiScanCache(cache_path) as cache, CaseFileStore(casefile_path) as casefile:
            try:
                conn = reader.connect(settings.quorum_replica_dsn)
            except Exception as exc:
                return _text({"error": f"replica connection failed: {type(exc).__name__}"})
            try:
                ok, err = reader.check_schema(conn)
                if not ok:
                    return _text({"error": f"replica schema drift: {err}"})
                if len(regions) == 1:
                    summary = diff_region(regions[0], cache, casefile, conn,
                                          sla_hours=settings.qherlock_freshness_sla_hours)
                else:
                    summary = diff_many(regions, cache, casefile, conn,
                                        sla_hours=settings.qherlock_freshness_sla_hours)
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
                region=(args.get("state") or "").upper() or None,
                gap_type=args.get("gap_type") or None,
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

    @tool("investigate_bill", "Targeted single-bill deep-dive: live LegiScan getBill "
          "(budget-permitting) plus a Quorum replica lookup, side by side.",
          {"state": str, "session": str, "number": str})
    async def investigate_bill_handler(args: dict) -> dict:
        state = args["state"].upper()
        try:
            session_id = int(args["session"])
        except ValueError:
            return _text({"error": "session must be the LegiScan session_id (shown as "
                                   "session_key on anomalies)"})
        if not settings.quorum_replica_dsn:
            return _text({"error": "no QUORUM_REPLICA_DSN configured — start a Teleport "
                                   "tunnel (tsh proxy db) and set it in .env"})
        with LegiScanCache(cache_path) as cache:
            try:
                conn = reader.connect(settings.quorum_replica_dsn)
            except Exception as exc:
                return _text({"error": f"replica connection failed: {type(exc).__name__}"})
            try:
                ok, err = reader.check_schema(conn)
                if not ok:
                    return _text({"error": f"replica schema drift: {err}"})
                client = LegiScanClient(settings.legiscan_api_key,
                                        on_call=lambda op: cache.add_call(op))
                try:
                    result = investigate(state, session_id, args["number"], client, cache, conn,
                                         budget_limit=settings.legiscan_monthly_budget)
                finally:
                    client.close()
            finally:
                conn.close()
        return _text(_bounded(result))

    @tool("post_slack", "Post a digest or alert to the configured Slack webhook. "
          "kind: 'digest' or 'alert'.", {"kind": str, "text": str})
    async def post_slack_handler(args: dict) -> dict:
        result = slack.post(settings.slack_webhook_url, args["kind"], args["text"])
        return _text(result)

    sdk_tools = [legiscan_sync_handler, diff_handler, list_anomalies_handler,
                 get_anomaly_handler, investigate_bill_handler, post_slack_handler]
    server = create_sdk_mcp_server(name="qherlock", version="0.1.0", tools=sdk_tools)
    if return_handlers:
        handlers = {"legiscan_sync": legiscan_sync_handler.handler,
                    "diff": diff_handler.handler,
                    "list_anomalies": list_anomalies_handler.handler,
                    "get_anomaly": get_anomaly_handler.handler,
                    "investigate_bill": investigate_bill_handler.handler,
                    "post_slack": post_slack_handler.handler}
        return server, handlers
    return server, TOOL_NAMES
