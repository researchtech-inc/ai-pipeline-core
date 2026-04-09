# MODULE: settings
# CLASSES: Settings
# DEPENDS: BaseSettings
# VERSION: 0.21.3
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
    pubsub_project_id: str = ""
    pubsub_topic_id: str = ""
    lmnr_project_api_key: str = ""
    sentry_dsn: str = ""
    log_format: str = "text"
    orphan_span_max_age_minutes: int = 120
    task_retries: int = 0
    task_retry_delay_seconds: int = 30
    flow_retries: int = 0
    flow_retry_delay_seconds: int = 30
    conversation_retries: int = 2
    conversation_retry_delay_seconds: int = 20
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

**Env variable loading** (`tests/test_settings.py:25`)

```python
@patch.dict(
    os.environ,
    {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_API_KEY": "sk-test123",
        "PREFECT_API_URL": "https://api.prefect.io",
        "PREFECT_API_KEY": "pf-key456",
    },
)
def test_env_variable_loading(self):
    """Test loading settings from environment variables."""
    s = Settings()
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert s.openai_api_key == "sk-test123"
    assert s.prefect_api_url == "https://api.prefect.io"
    assert s.prefect_api_key == "pf-key456"
```

**Extra env ignored** (`tests/test_settings.py:41`)

```python
@patch.dict(
    os.environ,
    {
        "OPENAI_API_KEY": "test-key",
        "UNKNOWN_SETTING": "should-be-ignored",
        "RANDOM_VAR": "also-ignored",
    },
)
def test_extra_env_ignored(self):
    """Test that unknown environment variables are ignored."""
    # Should not raise even with unknown env vars (extra="ignore")
    s = Settings()
    assert s.openai_api_key == "test-key"
    # Unknown vars are not added as attributes
    assert not hasattr(s, "unknown_setting")
    assert not hasattr(s, "random_var")
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
