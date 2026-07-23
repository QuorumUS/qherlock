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
IGNORED_TITLE_SUFFIXES: dict[str, tuple[str, ...]] = {
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

# States where LegiScan fuses the extraordinary-session marker into the bill
# number (CA: 'ABX110' = Assembly Bill, extraordinary session X1, bill 10) while
# Quorum keeps the base number in a separate, often non-current, special session.
EXTRAORDINARY_SESSION_STATES: frozenset[str] = frozenset({"CA"})

_SUFFIX_RE = re.compile(r"^([A-Z]+\d+)([A-Z])$")

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


def legiscan_number_norm(state: str, raw: str | int | None) -> str:
    """Normalize + per-state prefix translation. LegiScan side only."""
    s = normalize_bill_number(raw)
    m = _NUM_RE.match(s)
    if not m:
        return s
    prefix, num = m.group(1), m.group(2)
    prefix = PREFIX_MAP.get(state.upper(), {}).get(prefix, prefix)
    return f"{prefix}{num}"


def parse_extraordinary_number(raw_number: str | int | None, ordinals) -> list[tuple[int, str]]:
    """For a LegiScan number that fuses the extraordinary-session marker into the
    number, return every (ordinal, base_norm) candidate for the given ordinals
    (e.g. 'ABX110' with ordinals {1} -> [(1, 'AB10')]). Empty when the number
    carries no recognized marker. Returning all candidates (not one) lets the
    caller disambiguate by which base actually exists in that ordinal's session."""
    norm = normalize_bill_number(raw_number)
    out: list[tuple[int, str]] = []
    for o in ordinals:
        m = re.match(rf"^([A-Z]+)X{o}(\d+)$", norm)
        if m:
            out.append((o, f"{m.group(1)}{int(m.group(2))}"))
    return out


_ORDINAL_RE = re.compile(r"\bX(\d+)\b")
_SPEC_SESSION_RE = re.compile(r"SPEC(?:IAL)?\s+SESSION\s+(\d+)")


def extract_session_ordinal(title: str | None) -> int | None:
    """Extraordinary-session ordinal from a Quorum session title/name
    ('2025 Spec Session 1 - X1' -> 1). Prefers the 'X<n>' marker; falls back to
    'Spec[ial] Session <n>'. Returns None when neither is present."""
    if not title:
        return None
    up = title.upper()
    m = _ORDINAL_RE.search(up)
    if m:
        return int(m.group(1))
    m = _SPEC_SESSION_RE.search(up)
    return int(m.group(1)) if m else None


def select_sibling_special_sessions(regular: SessionRow,
                                    specials: list[SessionRow]) -> dict[int, SessionRow]:
    """Map ordinal -> the Quorum special session that belongs to `regular`'s
    biennium. A sibling is a special session whose start_year is within the
    biennium window [y, y+1] (y = regular.start_year) AND whose title/name carries
    an X-ordinal. Includes non-current sessions. First session wins per ordinal."""
    if regular.start_year is None:
        return {}
    window = {regular.start_year, regular.start_year + 1}
    out: dict[int, SessionRow] = {}
    for s in specials:
        if s.start_year not in window:
            continue
        ordinal = extract_session_ordinal(s.title)
        if ordinal is None:
            ordinal = extract_session_ordinal(s.session_name)
        if ordinal is None or ordinal in out:
            continue
        out[ordinal] = s
    return out


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
    suffixes = IGNORED_TITLE_SUFFIXES.get(st)
    if suffixes:
        t_stripped = t.rstrip()
        if t_stripped.endswith("."):
            t_stripped = t_stripped[:-1].rstrip()
        if t_stripped.endswith(suffixes):
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
