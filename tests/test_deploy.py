import plistlib
from pathlib import Path

PLIST = Path(__file__).parent.parent / "deploy" / "us.quorum.querlock.plist"


def test_plist_parses_with_expected_schedule():
    with PLIST.open("rb") as fh:
        d = plistlib.load(fh)
    assert d["Label"] == "us.quorum.querlock"
    assert d["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert "patrol --scope all" in " ".join(d["ProgramArguments"])
    assert d["RunAtLoad"] is False
