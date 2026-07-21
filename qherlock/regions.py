"""Region inventory: 50 US states + federal ("US" — LegiScan's code for Congress).

Quorum's federal LegSession rows carry region_abbrev 'us' (quorum-site
app/models.py Region.federal), so "US" flows through the existing
case-insensitive session query unchanged. No territories (spec §17).
"""

ALL_REGIONS: tuple[str, ...] = (
    "US",
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)


def parse_scope(scope: str) -> list[str]:
    """'all' -> every region; 'ca' -> ['CA']; 'ca, tx' -> ['CA', 'TX'].

    Dedups preserving order. Raises ValueError naming every invalid code.
    """
    s = (scope or "").strip()
    if s.lower() == "all":
        return list(ALL_REGIONS)
    out: list[str] = []
    bad: list[str] = []
    for part in s.split(","):
        code = part.strip().upper()
        if not code:
            continue
        if code not in ALL_REGIONS:
            bad.append(code)
        elif code not in out:
            out.append(code)
    if bad:
        raise ValueError(
            f"unknown region code(s): {', '.join(bad)} — use 'all', USPS state codes, or 'US'"
        )
    if not out:
        raise ValueError("empty scope — use 'all', USPS state codes, or 'US'")
    return out
