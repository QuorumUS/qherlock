from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    legiscan_api_key: str
    anthropic_api_key: str = ""
    quorum_replica_dsn: str = ""
    sherlock_model: str = "claude-sonnet-5"
    sherlock_max_turns: int = 100
    data_dir: Path = Path("data")
    runs_dir: Path = Path("runs")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
