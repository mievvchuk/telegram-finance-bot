from __future__ import annotations

import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: SecretStr
    zai_api_key: SecretStr
    zai_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    zai_text_model: str = "glm-4.7-flash"
    zai_vision_model: str = "glm-4.6v-flash"
    zai_timeout_seconds: float = Field(default=45, gt=0, le=180)

    database_path: Path = Path("./data/finance_bot.sqlite3")
    app_timezone: str = "Europe/Kyiv"
    allowed_telegram_user_ids: str = ""

    draft_ttl_seconds: int = Field(default=900, ge=60, le=604800)
    duplicate_window_seconds: int = Field(default=30, ge=0, le=3600)
    max_image_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=5 * 1024 * 1024)
    log_level: str = "INFO"

    @field_validator("telegram_bot_token", "zai_api_key")
    @classmethod
    def required_secret_is_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("required secret cannot be blank")
        return value

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone: {value}") from error
        return value

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level: {value}")
        return normalized

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        if not self.allowed_telegram_user_ids.strip():
            return frozenset()
        try:
            values = {
                int(value.strip())
                for value in self.allowed_telegram_user_ids.split(",")
                if value.strip()
            }
        except ValueError as error:
            raise ValueError(
                "ALLOWED_TELEGRAM_USER_IDS must be comma-separated integers"
            ) from error
        if any(value <= 0 for value in values):
            raise ValueError("Telegram user IDs must be positive")
        return frozenset(values)