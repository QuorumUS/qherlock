from sherlock.casefiles.models import Anomaly
from sherlock.casefiles.store import CaseFileStore


def make_anomaly(number="AB12"):
    return Anomaly(gap_type="missing_bill", region="CA", session_key="2172",
                   bill_number_norm=number, legiscan_value="AB12",
                   evidence={"legiscan_bill_id": 111, "title": "An act"})


def test_fingerprint_is_stable_and_field_sensitive():
    a, b = make_anomaly(), make_anomaly()
    assert a.fingerprint == b.fingerprint
    c = make_anomaly()
    c.field = "status"
    assert c.fingerprint != a.fingerprint


def test_upsert_new_then_recurring(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        kind1, aid1 = store.upsert_anomaly(make_anomaly())
        kind2, aid2 = store.upsert_anomaly(make_anomaly())
        assert (kind1, kind2) == ("new", "recurring")
        assert aid1 == aid2
        row = store.get_anomaly(aid1)
        assert row["status"] == "new"
        assert row["evidence"]["legiscan_bill_id"] == 111
        assert row["last_seen"] >= row["first_seen"]


def test_list_anomalies_filters_and_limits(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        for i in range(15):
            store.upsert_anomaly(make_anomaly(number=f"AB{i}"))
        assert len(store.list_anomalies(region="CA")) == 10  # default cap
        assert store.list_anomalies(region="TX") == []
        assert len(store.list_anomalies(gap_type="missing_bill", limit=3)) == 3


def test_patrol_lifecycle(tmp_path):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        pid = store.start_patrol(scope="CA")
        store.finish_patrol(pid, {"anomalies_new": 2}, "runs/1.jsonl")
        assert pid == 1


def test_upsert_race_falls_back_to_recurring(tmp_path, monkeypatch):
    with CaseFileStore(tmp_path / "casefile.db") as store:
        a = make_anomaly()
        # Simulate losing the check-then-insert race: SELECT sees nothing,
        # but the row appears before our INSERT executes.
        real_execute = store._execute
        state = {"primed": False}

        def racing_execute(sql, params=()):
            if sql.strip().startswith("INSERT INTO anomalies") and not state["primed"]:
                state["primed"] = True
                with CaseFileStore(tmp_path / "casefile.db") as rival:
                    rival.upsert_anomaly(make_anomaly())
            return real_execute(sql, params)

        monkeypatch.setattr(store, "_execute", racing_execute)
        kind, aid = store.upsert_anomaly(a)
        assert kind == "recurring"
        assert store.get_anomaly(aid)["fingerprint"] == a.fingerprint
