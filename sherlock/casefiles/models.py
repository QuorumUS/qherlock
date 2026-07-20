import hashlib
import dataclasses
from dataclasses import dataclass


@dataclass
class Anomaly:
    gap_type: str
    region: str
    session_key: str
    bill_number_norm: str
    field: str = ""
    legiscan_value: str = ""
    quorum_value: str = ""
    evidence: dict = dataclasses.field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [self.gap_type, self.region, self.session_key, self.bill_number_norm, self.field]
        )
        return hashlib.sha1(raw.encode()).hexdigest()
