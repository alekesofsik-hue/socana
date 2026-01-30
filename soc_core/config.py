from __future__ import annotations

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # enable_decoding=False:
    # pydantic-settings tries to JSON-decode "complex" env values (like list[int]).
    # If systemd EnvironmentFile provides an empty string (e.g. TELEGRAM_ADMIN_CHAT_IDS=),
    # JSON decoding fails with JSONDecodeError. We parse CSV ourselves in validators below.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", enable_decoding=False
    )

    # IMAP
    imap_host: str = Field(default="imap.timeweb.ru", alias="IMAP_HOST")
    imap_port: int = Field(default=993, alias="IMAP_PORT")
    imap_username: str = Field(alias="IMAP_USERNAME")
    imap_password: str = Field(alias="IMAP_PASSWORD")
    imap_from_filter: str = Field(default="cloud_noreply@kaspersky.com", alias="IMAP_FROM_FILTER")
    imap_mailbox: str = Field(default="INBOX", alias="IMAP_MAILBOX")
    imap_mark_seen: bool = Field(default=True, alias="IMAP_MARK_SEEN")
    imap_poll_interval_seconds: int = Field(default=60, alias="IMAP_POLL_INTERVAL_SECONDS")

    # Telegram
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: int | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_admin_chat_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ADMIN_CHAT_IDS")
    telegram_allowed_user_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ALLOWED_USER_IDS")
    telegram_admin_user_ids: list[int] = Field(default_factory=list, alias="TELEGRAM_ADMIN_USER_IDS")

    # DB
    sqlite_path: str = Field(default="socana.sqlite3", alias="SQLITE_PATH")

    # Dedup
    anti_spam_window_seconds: int = Field(default=600, alias="ANTI_SPAM_WINDOW_SECONDS")
    anti_spam_repeat_threshold: int = Field(default=3, alias="ANTI_SPAM_REPEAT_THRESHOLD")

    # AI
    enable_llm: bool = Field(default=False, alias="ENABLE_LLM")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    prompts_path: str | None = Field(default=None, alias="PROMPTS_PATH")

    # Tools
    serper_api_key: str | None = Field(default=None, alias="SERPER_API_KEY")
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _empty_chat_id_to_none(cls, v):
        # allow TELEGRAM_CHAT_ID= (empty) in .env
        if v is None:
            return None
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_ids(cls, v):
        # Supports:
        # - empty
        # - "123,456 789"
        # - [123, 456] (if provided as JSON array)
        if v is None:
            return []
        if isinstance(v, list):
            out = []
            for x in v:
                try:
                    out.append(int(x))
                except Exception:
                    continue
            return out
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            parts = [p for p in s.replace(";", ",").replace(" ", ",").split(",") if p.strip()]
            out = []
            for p in parts:
                try:
                    out.append(int(p.strip()))
                except Exception:
                    continue
            return out
        try:
            return [int(v)]
        except Exception:
            return []

    @field_validator("telegram_admin_user_ids", mode="before")
    @classmethod
    def _parse_admin_user_ids(cls, v):
        return cls._parse_allowed_ids(v)

    @field_validator("telegram_admin_chat_ids", mode="before")
    @classmethod
    def _parse_admin_chat_ids(cls, v):
        return cls._parse_allowed_ids(v)

    @field_validator("imap_poll_interval_seconds", mode="before")
    @classmethod
    def _poll_interval_min_5(cls, v):
        # Avoid too aggressive polling by accident
        try:
            iv = int(v)
        except Exception:
            return 60
        return max(iv, 5)


def load_settings() -> Settings:
    return Settings()

