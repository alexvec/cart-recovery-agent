from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://recovery:recovery@localhost:5433/recovery"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "cart-recovery"

    shopify_api_secret: SecretStr = SecretStr("")

    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_agent_id: str = ""
    elevenlabs_phone_number_id: str = ""
    elevenlabs_webhook_secret: SecretStr = SecretStr("")
    tool_secret: SecretStr = SecretStr("")

    pii_encryption_key: SecretStr = SecretStr("")
    phone_hash_key: SecretStr = SecretStr("")

    dialer: Literal["fake", "elevenlabs"] = "fake"
    call_delay_minutes: int = 20
    max_call_attempts: int = 2
    attribution_window_hours: int = 72
    holdout_pct: int = 15
    agent_version: str = "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
