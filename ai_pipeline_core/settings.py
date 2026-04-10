"""Core configuration settings for pipeline operations."""

from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "Settings",
    "settings",
]


class Settings(BaseSettings):
    """Base configuration for AI Pipeline applications.

    Fields map to environment variables via Pydantic BaseSettings
    (e.g. ``clickhouse_host`` → ``CLICKHOUSE_HOST``). Uses ``.env`` file when present.

    Inherit to add application-specific fields::

        class ProjectSettings(Settings):
            app_name: str = "my-app"

        settings = ProjectSettings()
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,  # Settings are immutable after initialization
    )

    # LLM API Configuration
    openai_base_url: str = ""
    openai_api_key: str = ""

    # Prefect Configuration
    prefect_api_url: str = ""
    prefect_api_key: str = ""
    prefect_api_auth_string: str = ""
    prefect_work_pool_name: str = "default"
    prefect_work_queue_name: str = "default"
    prefect_gcs_bucket: str = ""

    # GCS (for Prefect deployment bundles)
    gcs_service_account_file: str = ""  # Path to GCS service account JSON file

    # ClickHouse tracking
    clickhouse_host: str = ""
    clickhouse_port: int = 8443
    clickhouse_database: str = "default"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_secure: bool = True
    clickhouse_connect_timeout: int = 30
    clickhouse_send_receive_timeout: int = 66
    clickhouse_connect_retries: int = 3
    clickhouse_retry_backoff_sec: int = 10

    # Document summary generation (store-level)
    doc_summary_enabled: bool = True
    doc_summary_model: str = "gemini-3.1-flash-lite"

    # Pub/Sub event delivery
    pubsub_project_id: str = ""
    pubsub_topic_id: str = ""

    # Laminar tracing (set LMNR_PROJECT_API_KEY to enable)
    lmnr_project_api_key: str = ""
    sentry_dsn: str = ""
    log_format: str = "text"
    orphan_reap_require_prefect_client: bool = True

    # Retry defaults (used when class-level retries/retry_delay_seconds is None)
    task_retries: int = 0
    task_retry_delay_seconds: int = 30
    flow_retries: int = 0
    flow_retry_delay_seconds: int = 30
    conversation_retries: int = 2
    conversation_retry_delay_seconds: int = 20

    @field_validator(
        "task_retries",
        "flow_retries",
        "conversation_retries",
        "task_retry_delay_seconds",
        "flow_retry_delay_seconds",
        "conversation_retry_delay_seconds",
    )
    @classmethod
    def _validate_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"Must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _disable_summary_without_llm(self) -> Self:
        """Auto-disable doc summary generation when LLM credentials are not configured."""
        if self.doc_summary_enabled and (not self.openai_api_key or not self.openai_base_url):
            object.__setattr__(self, "doc_summary_enabled", False)  # noqa: PLC2801 — frozen Pydantic model
        return self


settings = Settings()
