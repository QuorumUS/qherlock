import json
from datetime import date
from pathlib import Path

from qherlock.diff.detectors import detect_bill_anomalies
from qherlock.diff.matchers import (is_deliberately_unimported,
                                    legiscan_number_norm, parse_extraordinary_number,
                                    quorum_number_norm)
from qherlock.quorum.reader import BillCounts, BillRow

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_ny_amendment_family_all_match():
    fx = _load("ny_amendment.json")
    for p in fx["pairs"]:
        ls_norm = legiscan_number_norm(fx["state"], p["legiscan_number"])
        q_norm = quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                    p["quorum_bill_type"], state=fx["state"])
        assert ls_norm == q_norm, f"{p['legiscan_number']} != {p['quorum_label']}"


def test_ny_genuine_missing_has_no_quorum_side():
    fx = _load("ny_amendment.json")
    # Mirror qherlock/diff/service.py's index build: normalize every Quorum-side
    # pair into the same q_by_norm keyspace used for matching.
    q_by_norm = {
        quorum_number_norm(p["quorum_label"], p["quorum_number"], p["quorum_bill_type"],
                            state=fx["state"])
        for p in fx["pairs"]
    }
    ls_norm = legiscan_number_norm(fx["state"], fx["genuine_missing"]["legiscan_number"])
    # The planted genuine gap (A33878) must not collide with any amendment-suffix
    # pair after normalization -> it would genuinely be reported missing.
    assert ls_norm not in q_by_norm


def _qbill(status):
    return BillRow(id=1, label="x", number="1", bill_type=7,
                   current_general_status=status, current_status_date="2025-05-20",
                   most_recent_action_date="2025-05-20", introduced_date="2025-05-20",
                   missing_data=False, last_quorum_update="2025-05-20", source="")


def test_wi_resolutions_not_flagged():
    fx = _load("wi_resolutions.json")
    for r in fx["resolutions_passed_adopted"]:
        ls = {"number": r["number"], "status": r["ls_status"],
              "last_action_date": "2025-05-20", "bill_id": 1,
              "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
        out = detect_bill_anomalies(fx["state"], fx["session_key"], r["number"], ls,
                                    _qbill(r["quorum_status"]), BillCounts(),
                                    sla_hours=72, today=date(2025, 5, 21))
        assert not [a for a in out if a.gap_type == "wrong_data"], r["number"]


def test_wi_genuine_regular_bill_still_flagged():
    fx = _load("wi_resolutions.json")
    g = fx["genuine_regular_bill_behind"]
    ls = {"number": g["number"], "status": g["ls_status"],
          "last_action_date": "2025-05-20", "bill_id": 2,
          "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies(fx["state"], fx["session_key"], g["number"], ls,
                                _qbill(g["quorum_status"]), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert any(a.gap_type == "wrong_data" for a in out)


def test_ma_orders_suppressed_and_genuine_flagged():
    fx = _load("ma_orders.json")
    for t in fx["suppressed_titles"]:
        assert is_deliberately_unimported(fx["state"], t), t
    for t in fx["still_flagged_titles"]:
        assert not is_deliberately_unimported(fx["state"], t), t


def test_ca_extraordinary_family_all_match():
    fx = _load("ca_extraordinary.json")
    for p in fx["pairs"]:
        q_norm = quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                    p["quorum_bill_type"], state=fx["state"])
        cands = parse_extraordinary_number(p["legiscan_number"], {fx["ordinal"]})
        assert any(base == q_norm for _, base in cands), \
            f"{p['legiscan_number']} did not map to {p['quorum_label']}"


def test_ca_extraordinary_genuine_missing_absent():
    fx = _load("ca_extraordinary.json")
    q_bases = {quorum_number_norm(p["quorum_label"], p["quorum_number"],
                                  p["quorum_bill_type"], state=fx["state"])
               for p in fx["pairs"]}
    cands = parse_extraordinary_number(fx["genuine_missing"]["legiscan_number"], {fx["ordinal"]})
    assert cands, "planted bill must still parse as extraordinary"
    assert all(base not in q_bases for _, base in cands), \
        "planted genuine-missing bill must not match any sibling base"
