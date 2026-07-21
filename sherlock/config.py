from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    legiscan_api_key: str
    claude_code_oauth_token: str = ""
    anthropic_api_key: str = ""
    quorum_replica_dsn: str = ""
    slack_webhook_url: str = ""
    sherlock_freshness_sla_hours: int = 72   # stale-detector SLA grace (spec §1)
    legiscan_monthly_budget: int = 30000     # free-tier cap; sync degrades at 80%
    sherlock_model: str = "claude-sonnet-5"
    sherlock_max_turns: int = 100
    data_dir: Path = Path("data")
    runs_dir: Path = Path("runs")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
