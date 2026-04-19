# MODULE: settings
# CLASSES: Settings
# DEPENDS: BaseSettings
# VERSION: 0.22.4
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import Settings
```

## Public API

```python
class Settings(BaseSettings):
    """Base configuration for AI Pipeline applications.

    Fields map to environment variables via Pydantic BaseSettings
    (e.g. ``clickhouse_host`` → ``CLICKHOUSE_HOST``). Uses ``.env`` file when present.

    Inherit to add application-specific fields::

        class ProjectSettings(Settings):
            app_name: str = "my-app"

        settings = ProjectSettings()"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True)  # Settings are immutable after initialization
    openai_base_url: str = ""
    openai_api_key: str = ""
    prefect_api_url: str = ""
    prefect_api_key: str = ""
    prefect_api_auth_string: str = ""
    prefect_work_pool_name: str = "default"
    prefect_work_queue_name: str = "default"
    prefect_gcs_bucket: str = ""
    gcs_service_account_file: str = ""  # Path to GCS service account JSON file
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
    doc_summary_enabled: bool = True
    doc_summary_model: str = "gemini-3.1-flash-lite"
    doc_summary_concurrency: int = 50
    pubsub_project_id: str = ""
    pubsub_topic_id: str = ""
    lmnr_project_api_key: str = ""
    sentry_dsn: str = ""
    log_format: str = "text"
    orphan_reap_require_prefect_client: bool = True
    task_retries: int = 2
    task_retry_delay_seconds: int = 30
    flow_retries: int = 0
    flow_retry_delay_seconds: int = 30
    conversation_retries: int = 3
    conversation_retry_delay_seconds: int = 30
    conversation_retry_backoff_multiplier: int = 3
    conversation_retry_max_delay_seconds: int = 300
```

## Examples

**Settings singleton** (`tests/test_settings.py:50`)

```python
def test_settings_singleton(self):
    """Test that the module provides a settings singleton."""
    # The module exports a pre-created instance
    assert isinstance(settings, Settings)

    # It should be the same instance
    from ai_pipeline_core.settings import settings as settings2

    assert settings is settings2
```

**Settings singleton is settings instance** (`tests/test_settings_singleton.py:21`)

```python
def test_settings_singleton_is_settings_instance() -> None:
    assert isinstance(settings, Settings)
```

**Removed orphan reap threshold settings are absent** (`tests/test_settings.py:123`)

```python
def test_removed_orphan_reap_threshold_settings_are_absent(self) -> None:
    """Test that the heuristic orphan-reaper settings were deleted."""
    s = Settings()

    assert "orphan_reap_heartbeat_stale_seconds" not in Settings.model_fields
    assert "orphan_reap_fallback_max_hours" not in Settings.model_fields
    assert not hasattr(s, "orphan_reap_heartbeat_stale_seconds")
    assert not hasattr(s, "orphan_reap_fallback_max_hours")
```

**Execution context does not replace settings singleton** (`tests/test_settings_singleton.py:25`)

```python
def test_execution_context_does_not_replace_settings_singleton() -> None:
    with set_execution_context(_build_context()):
        assert isinstance(settings, Settings)
```

**Model config attributes** (`tests/test_settings.py:110`)

```python
def test_model_config_attributes(self):
    """Test that model_config is properly set."""
    assert Settings.model_config.get("env_file") == ".env"
    assert Settings.model_config.get("env_file_encoding") == "utf-8"
    assert Settings.model_config.get("extra") == "ignore"
    assert Settings.model_config.get("frozen") is True
```

**Orphan reap defaults** (`tests/test_settings.py:117`)

```python
def test_orphan_reap_defaults(self) -> None:
    """Test the remaining orphan-reaper safety default."""
    s = Settings()

    assert s.orphan_reap_require_prefect_client is True
```

**Partial configuration** (`tests/test_settings.py:92`)

```python
def test_partial_configuration(self):
    """Test that partial configuration works."""
    # Only some settings provided
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": ""}, clear=True):
        s = Settings()

        assert s.openai_api_key == "test-key"
        assert s.openai_base_url == ""  # Default
```


## Error Examples

**Settings immutable config** (`tests/test_settings.py:101`)

```python
def test_settings_immutable_config(self):
    """Test that Settings uses proper Pydantic configuration."""
    s = Settings()

    # Settings should be immutable (frozen=True)
    with pytest.raises(ValidationError) as exc_info:
        s.openai_api_key = "new-key"
    assert "frozen" in str(exc_info.value).lower()
```
