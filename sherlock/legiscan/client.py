from collections.abc import Callable

import httpx

BASE_URL = "https://api.legiscan.com/"


class LegiScanError(RuntimeError):
    pass


class LegiScanClient:
    """Thin LegiScan Pull API client. One method per operation; unwraps payloads."""

    def __init__(
        self,
        api_key: str,
        http: httpx.Client | None = None,
        on_call: Callable[[str], None] | None = None,
    ):
        self._key = api_key
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=120)
        self._on_call = on_call or (lambda op: None)

    def close(self) -> None:
        self._http.close()

    def _get(self, op: str, **params) -> dict:
        self._on_call(op)
        resp = self._http.get("/", params={"key": self._key, "op": op, **params})
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "OK":
            raise LegiScanError(f"{op} failed: {payload.get('alert', payload)}")
        return payload

    def get_session_list(self, state: str) -> list[dict]:
        return self._get("getSessionList", state=state)["sessions"]

    def get_master_list_raw(self, session_id: int) -> dict:
        return self._get("getMasterListRaw", id=session_id)["masterlist"]

    def get_dataset_list(self, state: str) -> list[dict]:
        return self._get("getDatasetList", state=state)["datasetlist"]

    def get_dataset(self, session_id: int, access_key: str) -> dict:
        return self._get("getDataset", id=session_id, access_key=access_key)["dataset"]

    def get_bill(self, bill_id: int) -> dict:
        return self._get("getBill", id=bill_id)["bill"]
