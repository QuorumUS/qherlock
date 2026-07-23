import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from querlock import slack
from querlock.agent.tools import TOOL_NAMES, build_toolkit
from querlock.casefiles.store import CaseFileStore
from querlock.config import Settings
from querlock.legiscan.cache import LegiScanCache
from querlock.quorum import reader

DOCTRINE = """You are Querlock, a data-integrity detective auditing Quorum's legislative \
database against LegiScan across all 50 US states plus Congress (region code "US").

Patrol procedure (M1 — read-only shadow mode):
1. Sync: call legiscan_sync with the patrol scope (usually {"scope": "all"}). It is \
quota-aware and deterministic; note degraded or errored regions for the digest.
2. Diff: call diff with the same scope. It runs every detector across the scope and \
returns a rollup: counts by gap type and region (new vs recurring), top cases by \
severity, session-match warnings, and per-region errors.
3. Triage: pick the most significant cases (at most ~8) using list_anomalies and \
get_anomaly. Severity guide: P1 = missing bill in an active session with recent \
LegiScan activity; P2 = significant or clustered gaps; P3 = isolated single-bill \
gaps; P4 = cosmetic.
4. Investigate: call investigate_bill(state, session, number) — session is the \
LegiScan session_id shown as session_key on the anomaly — for at most 5 cases where \
the recorded evidence is ambiguous. Each live call spends LegiScan quota; do not \
investigate what the diff evidence already explains.
5. Digest: call post_slack with kind "digest" and ONE compact message (target \
under 1,000 characters — smaller is better; the full detail belongs in the report, \
not Slack). Include: one-line scope; counts by gap type as new/recurring/resolved; \
the top 3 case families each with region + one-line diagnosis; degraded or errored \
regions only if any; LegiScan calls_this_month. Do not paste per-bill lists.
6. Report: finish with a full markdown patrol report: everything in the digest plus \
counts by gap type (new vs recurring vs resolved), session-match warnings, cluster \
diagnoses, and recommended next steps.

Triage rules:
- Session-match warnings usually mean false positives downstream — say so prominently \
and downgrade the affected cases.
- Many anomalies sharing one region and session are usually one root cause (session \
mismatch, prefix quirk, ingestion gap). Diagnose the cluster, not each bill.
- LegiScan is a recall oracle only: Quorum being ahead of LegiScan is never an anomaly.

Rules:
- Never invent data. Every claim in your digest and report must trace to a tool result.
- If a tool returns an error payload, report it and continue with what you have.
- If sync reports degraded regions, work from cached data and say so in the digest.
- If post_slack returns ok=false, note the delivery failure in your report and continue.
- You have no write tools. You observe and report."""


class PatrolFatalError(RuntimeError):
    """Replica unreachable / schema drift — patrol must not start (spec §12)."""


def build_options(settings: Settings, server) -> ClaudeAgentOptions:
    kwargs: dict = dict(
        model=settings.querlock_model,
        max_turns=settings.querlock_max_turns,
        system_prompt=DOCTRINE,
        mcp_servers={"querlock": server},
        tools=[],  # disable every built-in tool (Bash, Write, Edit, ...) — only our MCP tools remain
        allowed_tools=list(TOOL_NAMES),  # auto-approve our 6 read-only tools; nothing else is offered
    )
    if settings.claude_code_oauth_token:
        kwargs["env"] = {"CLAUDE_CODE_OAUTH_TOKEN": settings.claude_code_oauth_token}
    elif settings.anthropic_api_key:
        kwargs["env"] = {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    # else: the spawned claude CLI uses the host's logged-in OAuth credentials (Virgil pattern)
    return ClaudeAgentOptions(**kwargs)


def write_transcript_line(fh, msg) -> None:
    fh.write(json.dumps({"type": type(msg).__name__, "repr": str(msg)}) + "\n")


async def run_patrol(settings: Settings, scope: str, objective: str = "") -> str:
    settings.ensure_dirs()

    if settings.quorum_replica_dsn:
        try:
            conn = reader.connect(settings.quorum_replica_dsn)
        except Exception as exc:
            msg = f"replica unreachable: {type(exc).__name__}"
            slack.post(settings.slack_bot_token, settings.slack_channel_id,
                       "alert", f"Patrol aborted — {msg}")
            raise PatrolFatalError(msg) from None
        try:
            ok, err = reader.check_schema(conn)
        finally:
            conn.close()
        if not ok:
            msg = f"replica schema drift: {err}"
            slack.post(settings.slack_bot_token, settings.slack_channel_id,
                       "alert", f"Patrol aborted — {msg}")
            raise PatrolFatalError(msg)
    # DSN unset: dev flow — log and continue; the diff tool reports its own error payload.

    server, _ = build_toolkit(settings)
    options = build_options(settings, server)

    with CaseFileStore(settings.data_dir / "casefile.db") as casefile:
        patrol_id = casefile.start_patrol(scope=scope)
        transcript_path = settings.runs_dir / f"patrol-{patrol_id}.jsonl"
        prompt = f"Patrol scope: {scope}." + (f" Objective: {objective}" if objective else "")

        result_text = ""
        result_msg = None
        error: str | None = None
        try:
            with open(transcript_path, "w") as fh:
                async for msg in query(prompt=prompt, options=options):
                    write_transcript_line(fh, msg)
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
                        result_text = msg.result or ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            slack.post(settings.slack_bot_token, settings.slack_channel_id, "alert",
                       f"Patrol {patrol_id} ({scope}) FAILED: {error}")
            raise
        finally:
            try:
                with LegiScanCache(settings.data_dir / "cache.db") as cache:
                    ls_calls = cache.calls_this_month()
            except Exception:
                # A cache.db failure here must never mask an in-flight patrol
                # exception (or clobber the finally block's own bookkeeping).
                ls_calls = None
            stats: dict = {"result_chars": len(result_text),
                           "legiscan_calls_month": ls_calls}
            if result_msg is not None:
                stats.update(num_turns=result_msg.num_turns,
                             duration_ms=result_msg.duration_ms,
                             total_cost_usd=result_msg.total_cost_usd,
                             usage=result_msg.usage)
            if error is not None:
                stats["error"] = error
            casefile.finish_patrol(patrol_id, stats, str(transcript_path))

    # Deterministic stats footer (spec §11 "quota + token spend"): the agent
    # finishes before ResultMessage exists, so this line comes from code. Also
    # doubles as heartbeat if the agent skipped its digest. Never fatal.
    if result_msg is not None:
        cost = (f"${result_msg.total_cost_usd:.2f}" if result_msg.total_cost_usd is not None
                else "n/a (subscription)")
        ls_calls_display = ls_calls if ls_calls is not None else "?"
        slack.post(settings.slack_bot_token, settings.slack_channel_id, "digest",
                   f"Patrol {patrol_id} ({scope}) done: {result_msg.num_turns} turns, "
                   f"{(result_msg.duration_ms or 0) // 60000}m, cost {cost}, "
                   f"LegiScan {ls_calls_display}/{settings.legiscan_monthly_budget} this month.")
    return result_text
