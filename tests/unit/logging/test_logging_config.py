"""Tests for logging configuration."""

import importlib
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from ai_pipeline_core.database import LogRecord
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.logger._buffer import ExecutionLogBuffer
from ai_pipeline_core.logger._handler import ExecutionLogHandler
from ai_pipeline_core.logger._logging_config import _LoggingConfig


class Test_LoggingConfig:
    """Test _LoggingConfig class."""

    def test_default_config_path_from_env(self):
        """Test getting config path from environment."""
        with patch.dict(os.environ, {"AI_PIPELINE_LOGGING_CONFIG": "/path/to/config.yml"}):
            config = _LoggingConfig()
            assert config.config_path == Path("/path/to/config.yml")

    def test_default_config_path_from_prefect_env(self):
        """Test getting config path from Prefect environment."""
        with patch.dict(os.environ, {"PREFECT_LOGGING_SETTINGS_PATH": "/prefect/config.yml"}):
            config = _LoggingConfig()
            assert config.config_path == Path("/prefect/config.yml")

    def test_no_config_path_returns_none(self):
        """Test that no env vars results in None config path."""
        with patch.dict(os.environ, clear=True):
            config = _LoggingConfig()
            assert config.config_path is None

    def test_load_config_from_file(self, tmp_path: Path) -> None:
        """Test loading config from YAML file."""
        config_file = tmp_path / "logging.yml"
        config_file.write_text("""
version: 1
disable_existing_loggers: false
handlers:
  console:
    class: logging.StreamHandler
""")

        config = _LoggingConfig(config_path=config_file)
        loaded = config.load_config()

        assert loaded["version"] == 1
        assert loaded["disable_existing_loggers"] is False
        assert "console" in loaded["handlers"]

    def test_load_default_config_when_no_file(self):
        """Test loading default config when no file exists."""
        config = _LoggingConfig()
        loaded = config.load_config()

        assert loaded["version"] == 1
        assert "formatters" in loaded
        assert "handlers" in loaded
        assert "loggers" in loaded
        assert loaded["root"]["level"] == "DEBUG"
        assert loaded["handlers"]["console"]["level"] == "INFO"
        assert loaded["loggers"]["ai_pipeline_core"]["level"] == "DEBUG"
        assert loaded["loggers"]["ai_pipeline_core"]["propagate"] is True
        assert loaded["loggers"]["ai_pipeline_core"]["handlers"] == []

    def test_default_config_suppresses_noisy_third_party_loggers(self):
        """httpx and httpcore INFO logs must be suppressed by default."""
        config = _LoggingConfig()
        loaded = config.load_config()
        loggers = loaded["loggers"]

        assert loggers["httpx"]["level"] == "WARNING"
        assert loggers["httpcore"]["level"] == "WARNING"

    @patch("logging.config.dictConfig")
    def test_apply_config(self, mock_dict_config: Mock) -> None:
        """Test applying logging configuration."""
        config = _LoggingConfig()
        config.apply()

        mock_dict_config.assert_called_once()
        call_args = mock_dict_config.call_args[0][0]
        assert call_args["version"] == 1

    @patch("logging.config.dictConfig")
    def test_apply_with_prefect_settings(self, mock_dict_config: Mock) -> None:
        """Test applying config with Prefect settings."""
        with patch.dict(os.environ, clear=True):
            # Use patch to inject the config during initialization
            custom_config = {"version": 1, "loggers": {"prefect": {"level": "DEBUG"}}}
            with patch.object(_LoggingConfig, "load_config", return_value=custom_config):
                config = _LoggingConfig()
                config.apply()

                # Should set Prefect env var
                assert os.environ.get("PREFECT_LOGGING_LEVEL") == "DEBUG"


class TestSetupLogging:
    """Test setup_logging function."""

    @patch("ai_pipeline_core.logger._logging_config._LoggingConfig.apply")
    def test_setup_logging_basic(self, mock_apply: Mock) -> None:
        """Test basic setup_logging call."""
        setup_logging()
        mock_apply.assert_called_once()

    @patch("ai_pipeline_core.logger._logging_config.logging.getLogger")
    @patch("ai_pipeline_core.logger._logging_config._LoggingConfig.apply")
    def test_setup_logging_with_level(self, mock_apply: Mock, mock_get_logger: Mock) -> None:
        """Test setup_logging with custom level."""
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        setup_logging(level="DEBUG")

        # Should set level on loggers
        assert mock_get_logger.call_count > 0
        mock_logger.setLevel.assert_called_with("DEBUG")

        # Should set Prefect env
        assert os.environ["PREFECT_LOGGING_LEVEL"] == "DEBUG"

    @patch("ai_pipeline_core.logger._logging_config._LoggingConfig")
    def test_setup_logging_with_config_path(self, mock_config_class: Mock, tmp_path: Path) -> None:
        """Test setup_logging with custom config path."""
        config_file = tmp_path / "custom.yml"
        mock_instance = MagicMock()
        mock_config_class.return_value = mock_instance

        setup_logging(config_path=config_file)

        mock_config_class.assert_called_once_with(config_file)
        mock_instance.apply.assert_called_once()


class TestImportTimeSetup:
    """Verify the framework no longer relies on import-time logging setup."""

    def test_import_does_not_trigger_setup(self) -> None:
        """Importing the module should not configure logging as a side effect."""
        import ai_pipeline_core.logger._logging_config as cfg

        reloaded = importlib.reload(cfg)
        assert reloaded._logging_config is None


def test_logging_module_exports_handler_and_buffer_for_logrecord_runtime() -> None:
    buffer = ExecutionLogBuffer()
    buffer.append(
        LogRecord(
            deployment_id=uuid4(),
            span_id=uuid4(),
            timestamp=datetime.now(UTC),
            sequence_no=0,
            level="INFO",
            category="framework",
            logger_name="ai_pipeline_core.tests",
            message="export check",
        )
    )

    [stored_log] = buffer.drain()
    assert stored_log.sequence_no == 0
    assert ExecutionLogHandler.__name__ == "ExecutionLogHandler"
