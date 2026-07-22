from datetime import date

import pytest

from qherlock.diff.detectors import (
    GENERAL_STATUS_RANK, LEGISCAN_MIN_RANK, compute_severity, detect_bill_anomalies,
)
from qherlock.quorum.reader import BillCounts, BillRow

TODAY = date(2026, 7, 21)


def _q(status=1, mrad="2026-07-20", **kw):
    base = dict(id=10, label="AB 1", number=1, bill_type=None,
                current_general_status=status, current_status_date="2026-07-01",
                most_recent_action_date=mrad, introduced_date="2026-01-05",
                missing_data=False, last_quorum_update=None, source="legiscan")
    base.update(kw)
    return BillRow(**base)


def _ls(status=1, last="2026-07-20", **counts):
    d = {"bill_id": 1, "number": "AB1", "status": status, "status_date": "2026-07-01",
         "last_action_date": last, "n_sponsors": 1, "n_actions": 1, "n_texts": 1, "n_votes": 0}
    d.update({f"n_{k}": v for k, v in counts.items()})
    return d


def _run(ls, q, counts=None, sla=72):
    return detect_bill_anomalies("CA", "2172", "AB1", ls, q, counts or BillCounts(
        actions=1, texts=1, sponsors=1, votes=0), sla_hours=sla, today=TODAY)


def test_incomplete_fields_one_anomaly_per_zero_field():
    out = _run(_ls(sponsors=2, votes=1), _q(), BillCounts(actions=1, texts=1))
    got = {(a.gap_type, a.field) for a in out}
    assert ("incomplete_fields", "sponsors") in got
    assert ("incomplete_fields", "votes") in got
    assert ("incomplete_fields", "actions") not in got
    fps = [a.fingerprint for a in out]
    assert len(fps) == len(set(fps))


def test_incomplete_never_fires_when_legiscan_also_zero_or_quorum_nonzero():
    assert _run(_ls(votes=0), _q(), BillCounts(actions=1, texts=1, sponsors=1)) == []
    assert _run(_ls(votes=2), _q(), BillCounts(actions=1, texts=1, sponsors=1, votes=1)) == []


def test_stale_beyond_sla_grace():
    out = _run(_ls(last="2026-07-20"), _q(mrad="2026-07-16"))  # 4-day lag > 72h
    assert [a.gap_type for a in out] == ["stale"]
    assert out[0].field == "most_recent_action_date"
    assert out[0].evidence["lag_days"] == 4


def test_stale_grace_window_and_quorum_ahead():
    assert _run(_ls(last="2026-07-20"), _q(mrad="2026-07-17")) == []   # exactly 72h: grace
    assert _run(_ls(last="2026-07-10"), _q(mrad="2026-07-20")) == []   # Quorum fresher


def test_no_date_detectors_when_quorum_mrad_null():
    # incomplete_fields:actions already covers empty Quorum history
    out = _run(_ls(last="2026-07-20"), _q(mrad=None), BillCounts(texts=1, sponsors=1))
    assert {a.gap_type for a in out} == {"incomplete_fields"}


def test_wrong_data_quorum_rank_below_minimum():
    out = _run(_ls(status=2), _q(status=1))  # Engrossed needs >= passed_first(3)
    assert [a.gap_type for a in out] == ["wrong_data"]
    assert out[0].field == "status"
    assert (out[0].legiscan_value, out[0].quorum_value) == ("2", "1")


@pytest.mark.parametrize("ls_status,q_status", [
    (2, 3),   # at minimum -> ok
    (4, 6),   # passed vs enacted -> ok
    (4, 7),   # passed vs effective -> ok
    (1, 9),   # failed is terminal = ahead
    (2, 8),   # unknown -> skip
    (2, None),  # NULL -> skip
    (6, 1),   # LS Failed -> never compared
    (2, 12),  # nomination status -> skip
])
def test_wrong_data_never_fires(ls_status, q_status):
    assert _run(_ls(status=ls_status), _q(status=q_status)) == []


def test_precedence_stale_wins_over_wrong_data():
    out = _run(_ls(status=4, last="2026-07-20"), _q(status=1, mrad="2026-07-01"))
    assert [a.gap_type for a in out] == ["stale"]


def test_severity_rubric():
    assert compute_severity("missing_bill", "", days_since_ls_activity=5) == "P1"
    assert compute_severity("missing_bill", "", days_since_ls_activity=90) == "P2"
    assert compute_severity("missing_bill", "", days_since_ls_activity=None) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=10) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=60, lag_days=45) == "P2"
    assert compute_severity("stale", "most_recent_action_date", days_since_ls_activity=60, lag_days=5) == "P3"
    assert compute_severity("wrong_data", "status", days_since_ls_activity=5) == "P2"
    assert compute_severity("incomplete_fields", "sponsors", days_since_ls_activity=None) == "P3"
    assert compute_severity("incomplete_fields", "texts", days_since_ls_activity=None) == "P4"


def test_status_maps_have_no_non_us_ids():
    assert all(k < 100 for k in GENERAL_STATUS_RANK)
    assert 6 not in LEGISCAN_MIN_RANK  # LS Failed deliberately unmapped


def _qbill(status, mrad="2025-05-20"):
    return BillRow(id=1, label="x", number="1", bill_type=7,
                   current_general_status=status, current_status_date=mrad,
                   most_recent_action_date=mrad, introduced_date=mrad,
                   missing_data=False, last_quorum_update=mrad, source="")


def test_resolution_passed_at_adopted_rank_not_flagged():
    ls = {"number": "AJR143", "status": 4, "last_action_date": "2025-05-20",
          "bill_id": 1, "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies("WI", "2197", "AJR143", ls, _qbill(4), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert [a for a in out if a.gap_type == "wrong_data"] == []


def test_regular_bill_passed_but_introduced_still_flagged():
    ls = {"number": "SB10", "status": 4, "last_action_date": "2025-05-20",
          "bill_id": 2, "n_sponsors": 0, "n_actions": 0, "n_texts": 0, "n_votes": 0}
    out = detect_bill_anomalies("WI", "2197", "SB10", ls, _qbill(1), BillCounts(),
                                sla_hours=72, today=date(2025, 5, 21))
    assert any(a.gap_type == "wrong_data" for a in out)
