from qherlock.diff.matchers import (
    is_deliberately_unimported, legiscan_number_norm, normalize_bill_number,
    match_sessions, quorum_number_norm, parse_extraordinary_number,
    extract_session_ordinal, select_sibling_special_sessions,
)
from qherlock.quorum.reader import SessionRow


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


def test_ny_amendment_suffix_stripped():
    # NY session 3596 real rows: amended Senate/Assembly bills carry a trailing letter.
    assert quorum_number_norm("S.115A", 115, 2, state="NY") == "S115"
    assert quorum_number_norm("S.156A", 156, 2, state="NY") == "S156"
    # Non-amended rows and other prefixes are untouched.
    assert quorum_number_norm("A.115", 115, 3, state="NY") == "A115"
    assert quorum_number_norm("J.115", 115, 4, state="NY") == "J115"
    assert quorum_number_norm("K.115", 115, 1, state="NY") == "K115"


def test_amendment_suffix_only_for_configured_states():
    # Same label in a non-configured state keeps the trailing letter.
    assert quorum_number_norm("S.115A", 115, 2, state="CA") == "S115A"
    assert quorum_number_norm("S.115A", 115, 2) == "S115A"  # no state -> no strip


def test_amendment_suffix_leaves_plain_numbers_alone():
    # No trailing letter -> unchanged even in NY.
    assert quorum_number_norm("AB 12", 12, None, state="NY") == "AB12"


def test_ma_extension_order_suffix_suppressed():
    assert is_deliberately_unimported("MA", "Financial Services -- Extension Order")
    assert is_deliberately_unimported("MA", "Revenue - Extension Order")
    assert is_deliberately_unimported("MA", "The Judiciary -- Extension Order")
    # Existing prefix rule still holds:
    assert is_deliberately_unimported("MA", "Order relative to X")


def test_ma_non_extension_orders_still_flagged():
    # These two real cases are NOT extension orders -> not suppressed.
    assert not is_deliberately_unimported("MA", "Communication from the Gaming Commission")
    assert not is_deliberately_unimported("MA", "Resolutions responding to the SJC order of May 7")
    # Other states unaffected:
    assert not is_deliberately_unimported("CA", "Some Extension Order")


def test_parse_extraordinary_number_basic():
    # CA fuses the session marker into the number: ABX11 = Assembly Bill, X1, bill 1.
    assert parse_extraordinary_number("ABX11", {1}) == [(1, "AB1")]
    assert parse_extraordinary_number("ABX110", {1}) == [(1, "AB10")]
    assert parse_extraordinary_number("SBX14", {1}) == [(1, "SB4")]
    assert parse_extraordinary_number("ACAX11", {1}) == [(1, "ACA1")]


def test_parse_extraordinary_number_no_marker_or_no_ordinal():
    assert parse_extraordinary_number("AB1", {1}) == []      # plain number, no marker
    assert parse_extraordinary_number("ABX11", set()) == []  # no known ordinals
    assert parse_extraordinary_number(None, {1}) == []


def test_parse_extraordinary_number_ambiguous_returns_all_candidates():
    # ordinals {1, 11} both parse 'ABX110'; the caller disambiguates by which
    # base actually exists in that ordinal's session (Task 4).
    got = parse_extraordinary_number("ABX110", {1, 11})
    assert (1, "AB10") in got and (11, "AB0") in got


def test_extract_session_ordinal():
    assert extract_session_ordinal("2025 Spec Session 1 - X1") == 1
    assert extract_session_ordinal("2024 Spec Session 2 - X2") == 2
    assert extract_session_ordinal("2025 Special Session 3") == 3
    assert extract_session_ordinal("California 2025-2026 Regular Session") is None
    assert extract_session_ordinal("2025-2026") is None
    assert extract_session_ordinal(None) is None


def test_select_sibling_special_sessions_keeps_biennium_rejects_stub_and_prior():
    reg = SessionRow(3570, "ca", "2025-2026", "2025-2026", 2025, True, True)
    specials = [
        SessionRow(3736, "ca", "2025 Spec Session 1 - X1", "2025 Spec Session 1 - X1",
                   2025, False, False),
        SessionRow(3809, "ca", "California 2025-2026 Regular Session", None,
                   2025, False, False),   # no X-ordinal -> rejected
        SessionRow(3665, "ca", "2024 Spec Session 1 - X1", "2024 Spec Session 1 - X1",
                   2024, False, False),   # prior biennium -> rejected
    ]
    got = select_sibling_special_sessions(reg, specials)
    assert set(got) == {1}
    assert got[1].id == 3736


def test_select_sibling_includes_second_biennium_year_and_first_wins():
    reg = SessionRow(3570, "ca", "2025-2026", "2025-2026", 2025, True, True)
    specials = [
        SessionRow(3736, "ca", "2025 Spec Session 1 - X1", "2025 Spec Session 1 - X1",
                   2025, False, False),
        SessionRow(3800, "ca", "2026 Spec Session 2 - X2", "2026 Spec Session 2 - X2",
                   2026, False, False),   # starts in the 2nd year of the biennium -> included
        SessionRow(3999, "ca", "2025 Spec Session 1 - X1", "2025 Spec Session 1 - X1",
                   2025, False, False),   # duplicate ordinal 1 -> first (3736) wins
    ]
    got = select_sibling_special_sessions(reg, specials)
    assert set(got) == {1, 2}
    assert got[1].id == 3736        # first session wins on duplicate ordinal
    assert got[2].id == 3800        # second-biennium-year sibling included
