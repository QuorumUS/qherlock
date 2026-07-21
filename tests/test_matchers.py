from sherlock.diff.matchers import (
    is_deliberately_unimported, legiscan_number_norm, normalize_bill_number,
    match_sessions, quorum_number_norm,
)
from sherlock.quorum.reader import SessionRow


def test_normalize_pure_rules():
    assert normalize_bill_number(" a b 12 ") == "AB12"
    assert normalize_bill_number("A.B. 0012") == "AB12"
    assert normalize_bill_number(None) == ""
    assert normalize_bill_number(24) == "24"


def test_legiscan_side_federal_prefixes():
    cases = {"HB24": "HR24", "SB5": "S5", "HR7": "HRES7", "SR2": "SRES2",
             "HJR3": "HJRES3", "SJR4": "SJRES4", "HCR10": "HCONRES10",
             "SCR9": "SCONRES9"}
    for raw, want in cases.items():
        assert legiscan_number_norm("US", raw) == want


def test_quorum_side_is_never_translated():
    # THE golden guard: H.R. 24 must stay HR24, not become HRES24.
    assert quorum_number_norm("H.R. 24", 24) == "HR24"
    assert quorum_number_norm("S. 100", 100) == "S100"


def test_quorum_null_label_bill_type_fallback():
    assert quorum_number_norm(None, 24, 3) == "HR24"      # bill_type 3 = hr
    assert quorum_number_norm(None, 3, 8) == "SJRES3"     # bill_type 8 = sjres
    assert quorum_number_norm(None, 3, 999) == ""         # unknown type, no label
    assert quorum_number_norm(None, None, 3) == ""


def test_ca_prefix_still_legiscan_side_only():
    assert legiscan_number_norm("CA", "AR10") == "HR10"
    assert legiscan_number_norm("TX", "AR10") == "AR10"
    assert quorum_number_norm("AR10", 10) == "AR10"


def test_ma_ignored_title_prefixes():
    assert is_deliberately_unimported("MA", "Order relative to procedure") is True
    assert is_deliberately_unimported("MA", "Study Order concerning X") is True
    assert is_deliberately_unimported("MA", "An Act to do things") is False
    assert is_deliberately_unimported("CA", "Order of business") is False
    assert is_deliberately_unimported("MA", None) is False


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
