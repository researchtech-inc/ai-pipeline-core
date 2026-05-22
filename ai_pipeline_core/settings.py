"""Core configuration settings for pipeline operations."""

from collections.abc import Mapping
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_pipeline_core._llm_core.types import AIModel

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
    doc_summary_model: AIModel | None = None
    doc_summary_concurrency: int = 50

    # Pub/Sub event delivery
    pubsub_project_id: str = ""
    pubsub_topic_id: str = ""

    sentry_dsn: str = ""
    log_format: str = "text"
    orphan_reap_require_prefect_client: bool = True

    # Retry defaults (used when class-level retries/retry_delay_seconds is None)
    task_retries: int = 0
    task_retry_delay_seconds: int = 30
    flow_retries: int = 0
    flow_retry_delay_seconds: int = 30
    conversation_retries: int = 2
    conversation_retry_delay_seconds: int = 30
    conversation_retry_backoff_multiplier: int = 3
    conversation_retry_max_delay_seconds: int = 300

    # PromptContract repair-loop ceiling (1 = no repair). 2 means: one initial attempt + one repair.
    prompt_contract_max_repair: int = 2

    @field_validator(
        "task_retries",
        "flow_retries",
        "conversation_retries",
        "task_retry_delay_seconds",
        "flow_retry_delay_seconds",
        "conversation_retry_delay_seconds",
        "conversation_retry_backoff_multiplier",
        "conversation_retry_max_delay_seconds",
    )
    @classmethod
    def _validate_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"Must be >= 0, got {v}")
        return v

    @field_validator("prompt_contract_max_repair")
    @classmethod
    def _validate_prompt_contract_max_repair(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"prompt_contract_max_repair must be >= 1, got {v}")
        return v

    @field_validator("doc_summary_concurrency")
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"doc_summary_concurrency must be >= 1, got {v}")
        return v

    @field_validator("doc_summary_model", mode="before")
    @classmethod
    def _coerce_doc_summary_model(cls, value: Any) -> AIModel | None:
        if value is None or (isinstance(value, str) and not value):
            return None
        if isinstance(value, AIModel):
            return value
        if isinstance(value, str):
            return AIModel(name=value)
        if isinstance(value, dict):
            return AIModel(**value)
        raise TypeError(
            "doc_summary_model must be empty, a model name string, an AIModel, or an AIModel-shaped mapping. Set DOC_SUMMARY_MODEL to an AIPL model name."
        )

    @model_validator(mode="before")
    @classmethod
    def _disable_summary_without_llm(cls, data: Any) -> Any:
        """Auto-disable doc summary generation when LLM credentials are not configured."""
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        enabled = _coerce_bool(values.get("doc_summary_enabled", True))
        has_credentials = bool(values.get("openai_api_key")) and bool(values.get("openai_base_url"))
        summary_model = values.get("doc_summary_model")
        has_summary_model = summary_model is not None and (not isinstance(summary_model, str) or bool(summary_model))
        if enabled and (not has_credentials or not has_summary_model):
            values["doc_summary_enabled"] = False
        return values


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


settings = Settings()
