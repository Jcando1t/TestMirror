"""Validated settings loaded from environment variables and .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TESTMIRROR_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: str = Field(default="data/testmirror.db", min_length=1)
    max_retries: int = Field(default=2, ge=0, le=20)
    use_deepseek: bool = False
