from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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

    google_service_account_file: Path | None = None
    google_service_account_json: SecretStr | None = None
    google_service_account_json_base64: SecretStr | None = None
    default_google_sheet_id: str | None = None
    transactions_worksheet: str = Field(default="Операції", min_length=1, max_length=100)

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

    @field_validator("google_service_account_file", mode="before")
    @classmethod
    def empty_credentials_path_is_none(cls, value: Any) -> Any:
        return None if value is None or not str(value).strip() else value

    @field_validator("default_google_sheet_id", mode="before")
    @classmethod
    def empty_sheet_id_is_none(cls, value: Any) -> Any:
        return None if value is None or not str(value).strip() else str(value).strip()

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

    @model_validator(mode="after")
    def exactly_one_google_credential_source(self) -> Settings:
        values = [
            bool(self.google_service_account_file),
            bool(
                self.google_service_account_json
                and self.google_service_account_json.get_secret_value().strip()
            ),
            bool(
                self.google_service_account_json_base64
                and self.google_service_account_json_base64.get_secret_value().strip()
            ),
        ]
        if sum(values) != 1:
            raise ValueError(
                "set exactly one of GOOGLE_SERVICE_ACCOUNT_FILE, "
                "GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SERVICE_ACCOUNT_JSON_BASE64"
            )
        return self

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

    def google_credentials(self) -> dict[str, Any] | Path:
        if self.google_service_account_file:
            path = self.google_service_account_file.expanduser()
            if not path.is_file():
                raise ValueError(f"Google service-account file does not exist: {path}")
            return path
        if self.google_service_account_json:
            raw = self.google_service_account_json.get_secret_value()
        elif self.google_service_account_json_base64:
            encoded = self.google_service_account_json_base64.get_secret_value()
            try:
                raw = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 is invalid") from error
        else:
            raise ValueError("Google service-account credentials are not configured")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("Google service-account JSON is invalid") from error
        if (
            not isinstance(result, dict)
            or not result.get("client_email")
            or not result.get("private_key")
        ):
            raise ValueError("Google credentials must contain client_email and private_key")
        return result
