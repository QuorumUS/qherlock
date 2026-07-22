import re

from qherlock.quorum.reader import SessionRow

# LegiScan prefix -> Quorum prefix, applied to the LEGISCAN side ONLY.
# Quorum's own numbers must never be translated (H.R. 24 -> HR24 must not
# become HRES24). Salvage precedent: quorum-site comparison.py:125.
PREFIX_MAP: dict[str, dict[str, str]] = {
    "CA": {"AR": "HR"},
    "US": {"HB": "HR", "SB": "S", "HR": "HRES", "SR": "SRES",
           "HJR": "HJRES", "SJR": "SJRES", "HCR": "HCONRES", "SCR": "SCONRES"},
}

# Salvaged (comparison.py:26): LegiScan bills whose TITLE marks a type Quorum
# deliberately does not import. MA procedural Orders come two ways: leading
# "order"/"study order" (prefix) and trailing "... Extension Order" (suffix).
IGNORED_TITLE_PREFIXES: dict[str, tuple[str, ...]] = {
    "MA": ("order", "study order"),
}
IGNORED_TITLE_SUBSTRINGS: dict[str, tuple[str, ...]] = {
    "MA": ("extension order",),
}

# Quorum BillType id -> normalized prefix (quorum-site app/bill/models.py:1923),
# for federal bills whose label is NULL (identity = bill_type + number).
BILL_TYPE_PREFIX: dict[int, str] = {
    1: "HRES", 2: "S", 3: "HR", 4: "SRES",
    5: "HCONRES", 6: "SCONRES", 7: "HJRES", 8: "SJRES",
}

# States where Quorum stores an amended bill under a suffixed label (NY: S.115A)
# while LegiScan reports the base number (S115). Strip the trailing letter so the
# two sides match. Prefix-preserving: only a single trailing [A-Z] is removed.
AMENDMENT_SUFFIX_STATES: frozenset[str] = frozenset({"NY"})
_SUFFIX_RE = re.compile(r"^([A-Z]+\d+)([A-Z])$")

# States where LegiScan folds extraordinary-session bills into the biennium
# dataset while Quorum keeps a separate special session. Marker: a letter
# prefix ending in 'X' (ABX/SBX) before the number.
EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})

_CLEAN_RE = re.compile(r"[\s. ]")
_NUM_RE = re.compile(r"^([A-Z]+)0*(\d+)$")


def normalize_bill_number(raw: str | int | None) -> str:
    """Pure normalization: uppercase, strip spaces/dots, drop leading zeros.
    No prefix translation — see legiscan_number_norm for that."""
    s = _CLEAN_RE.sub("", ("" if raw is None else str(raw)).upper())
    m = _NUM_RE.match(s)
    if not m:
        return s
    return f"{m.group(1)}{m.group(2)}"


def is_extraordinary_number(state: str, raw_number: str | int | None) -> bool:
    """True when the raw LegiScan number marks an extraordinary-session bill
    (CA ABX1.../SBX2...). Uses the pure-normalized form; no prefix translation."""
    if state.upper() not in EXTRAORDINARY_SESSION_STATES:
        return False
    norm = normalize_bill_number(raw_number)
    m = _NUM_RE.match(norm)
    return bool(m) and m.group(1).endswith("X")


def legiscan_number_norm(state: str, raw: str | int | None) -> str:
    """Normalize + per-state prefix translation. LegiScan side only."""
    s = normalize_bill_number(raw)
    m = _NUM_RE.match(s)
    if not m:
        return s
    prefix, num = m.group(1), m.group(2)
    prefix = PREFIX_MAP.get(state.upper(), {}).get(prefix, prefix)
    return f"{prefix}{num}"


def quorum_number_norm(label: str | None, number, bill_type: int | None = None,
                       state: str | None = None) -> str:
    """Quorum-side identity: normalized label; federal NULL-label fallback via
    bill_type + number; '' when no identity can be derived (caller skips).
    For AMENDMENT_SUFFIX_STATES, a single trailing amendment letter is dropped
    (S.115A -> S115) so amended bills match LegiScan's base number."""
    if label:
        norm = normalize_bill_number(label)
        if state and state.upper() in AMENDMENT_SUFFIX_STATES:
            m = _SUFFIX_RE.match(norm)
            if m:
                return m.group(1)
        return norm
    if bill_type in BILL_TYPE_PREFIX and number is not None:
        return f"{BILL_TYPE_PREFIX[bill_type]}{number}"
    return ""


def is_deliberately_unimported(state: str, title: str | None) -> bool:
    """Salvaged MA order rule (comparison.py:31), extended for suffix-form
    '... Extension Order' titles."""
    t = (title or "").strip().lower()
    if not t:
        return False
    st = state.upper()
    prefixes = IGNORED_TITLE_PREFIXES.get(st)
    if prefixes and t.startswith(prefixes):
        return True
    substrings = IGNORED_TITLE_SUBSTRINGS.get(st)
    if substrings and any(s in t for s in substrings):
        return True
    return False


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
