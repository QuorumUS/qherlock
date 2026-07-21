import pytest

from sherlock.regions import ALL_REGIONS, parse_scope


def test_all_regions_inventory():
    assert len(ALL_REGIONS) == 51
    assert ALL_REGIONS[0] == "US"
    assert "CA" in ALL_REGIONS
    assert "PR" not in ALL_REGIONS  # territories out of scope (spec §17)
    assert len(set(ALL_REGIONS)) == 51


def test_parse_scope_all_case_insensitive():
    assert parse_scope("all") == list(ALL_REGIONS)
    assert parse_scope("ALL") == list(ALL_REGIONS)


def test_parse_scope_single_list_dedup():
    assert parse_scope("ca") == ["CA"]
    assert parse_scope("ca, tx") == ["CA", "TX"]
    assert parse_scope("CA,ca") == ["CA"]


def test_parse_scope_invalid_names_every_bad_code():
    with pytest.raises(ValueError) as exc:
        parse_scope("CA,XX,YY")
    assert "XX" in str(exc.value) and "YY" in str(exc.value)


def test_parse_scope_empty_raises():
    with pytest.raises(ValueError):
        parse_scope("")
