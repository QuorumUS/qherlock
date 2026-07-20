from sherlock.diff.matchers import match_sessions, normalize_bill_number
from sherlock.quorum.reader import SessionRow


def test_normalize_strips_and_uppercases():
    assert normalize_bill_number("CA", "ab 0012") == "AB12"
    assert normalize_bill_number("CA", "S.B. 5") == "SB5"
    assert normalize_bill_number("TX", None) == ""
    assert normalize_bill_number("CA", 12) == "12"
    assert normalize_bill_number("CA", 0) == "0"


def test_normalize_applies_ca_prefix_map():
    assert normalize_bill_number("CA", "AR 10") == "HR10"
    assert normalize_bill_number("TX", "AR 10") == "AR10"  # map is per-state


def make_qsession(id=10, start_year=2025, regular=True):
    return SessionRow(id=id, region_abbrev="ca", title=None, session_name=None,
                      start_year=start_year, current=True, regular_session=regular)


def test_match_sessions_pairs_regular_by_year():
    ls = [{"session_id": 2172, "year_start": 2025, "year_end": 2026, "special": 0,
           "session_name": "2025-2026 Regular Session"}]
    matched, warnings = match_sessions(ls, [make_qsession()])
    assert len(matched) == 1 and matched[0][1].id == 10
    assert warnings == []


def test_match_sessions_warns_on_no_candidate():
    ls = [{"session_id": 2173, "year_start": 2025, "year_end": 2025, "special": 1,
           "session_name": "First Extraordinary Session"}]
    matched, warnings = match_sessions(ls, [make_qsession()])  # only a regular session
    assert matched == []
    assert len(warnings) == 1 and "2173" in warnings[0]
