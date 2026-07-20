import json

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from sherlock.agent.tools import TOOL_NAMES, build_toolkit
from sherlock.casefiles.store import CaseFileStore
from sherlock.config import Settings

DOCTRINE = """You are Sherlock, a data-integrity detective auditing Quorum's legislative \
database against LegiScan.

Patrol procedure (M0 — read-only, one state):
1. Call legiscan_sync for the target state to refresh the local LegiScan cache.
2. Call diff_state to compare LegiScan against Quorum's replica and record anomalies.
3. Inspect the most interesting cases with list_anomalies / get_anomaly.
4. Finish with a patrol report in markdown: session match warnings, anomaly counts \
(new vs recurring), the top cases with bill number, title, and your read on the likely \
cause (ingestion gap? session mismatch? bill-number normalization quirk?), and what you \
would do next.

Rules:
- Never invent data. Every claim in your report must trace to a tool result.
- If a tool returns an error payload, report the error and continue with what you have.
- Session-match warnings usually mean false positives downstream — say so prominently.
- You have no write tools. You observe and report."""


def build_options(settings: Settings, server) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.sherlock_model,
        max_turns=settings.sherlock_max_turns,
        system_prompt=DOCTRINE,
        mcp_servers={"sherlock": server},
        allowed_tools=list(TOOL_NAMES),
        permission_mode="bypassPermissions",  # only our 4 read-only tools are allowed
    )


def write_transcript_line(fh, msg) -> None:
    fh.write(json.dumps({"type": type(msg).__name__, "repr": str(msg)}) + "\n")


async def run_patrol(settings: Settings, state: str, objective: str = "") -> str:
    settings.ensure_dirs()
    server, _ = build_toolkit(settings)
    options = build_options(settings, server)

    with CaseFileStore(settings.data_dir / "casefile.db") as casefile:
        patrol_id = casefile.start_patrol(scope=state)
        transcript_path = settings.runs_dir / f"patrol-{patrol_id}.jsonl"
        prompt = f"Patrol {state}." + (f" Objective: {objective}" if objective else "")

        result_text = ""
        with open(transcript_path, "w") as fh:
            async for msg in query(prompt=prompt, options=options):
                write_transcript_line(fh, msg)
                if isinstance(msg, ResultMessage):
                    result_text = msg.result or ""

        casefile.finish_patrol(patrol_id, {"result_chars": len(result_text)},
                               str(transcript_path))
    return result_text
