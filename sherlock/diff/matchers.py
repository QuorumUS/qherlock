import re

from sherlock.quorum.reader import SessionRow

# Salvaged seed from quorum-site app/management/scraper/legiscan/comparison.py
PREFIX_MAP: dict[str, dict[str, str]] = {"CA": {"AR": "HR"}}

_CLEAN_RE = re.compile(r"[\s. ]")
_NUM_RE = re.compile(r"^([A-Z]+)0*(\d+)$")


def normalize_bill_number(state: str, raw: str | int | None) -> str:
    s = _CLEAN_RE.sub("", str(raw or "").upper())
    m = _NUM_RE.match(s)
    if not m:
        return s
    prefix, num = m.group(1), m.group(2)
    prefix = PREFIX_MAP.get(state.upper(), {}).get(prefix, prefix)
    return f"{prefix}{num}"


def match_sessions(
    legiscan_sessions: list[dict], quorum_sessions: list[SessionRow]
) -> tuple[list[tuple[dict, SessionRow]], list[str]]:
    matched: list[tuple[dict, SessionRow]] = []
    warnings: list[str] = []
    for ls in legiscan_sessions:
        want_regular = (ls.get("special", 0) == 0)
        candidates = [q for q in quorum_sessions
                      if q.regular_session == want_regular
                      and q.start_year == ls.get("year_start")]
        if len(candidates) == 1:
            matched.append((ls, candidates[0]))
        else:
            warnings.append(
                f"LegiScan session {ls['session_id']} ({ls.get('session_name')!r}, "
                f"years {ls.get('year_start')}-{ls.get('year_end')}, special={ls.get('special', 0)}): "
                f"{len(candidates)} Quorum candidates — skipped"
            )
    return matched, warnings
