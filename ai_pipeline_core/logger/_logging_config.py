"""Centralized logging configuration for AI Pipeline Core.

Provides logging configuration management for stdlib logging.
"""

import logging
import logging.config
import os
import threading
from pathlib import Path
from typing import Any

import yaml

# Default log levels for different components
_DEFAULT_LOG_LEVELS = {
    "ai_pipeline_core": "INFO",
    "ai_pipeline_core.documents": "INFO",
    "ai_pipeline_core.llm": "INFO",
    "ai_pipeline_core.pipeline": "INFO",
    "ai_pipeline_core.testing": "DEBUG",
}


class _LoggingConfig:
    """Manages logging configuration for the pipeline.

    Provides centralized logging configuration with stdlib logging.

    Configuration precedence:
        1. Explicit config_path parameter
        2. AI_PIPELINE_LOGGING_CONFIG environment variable
        3. PREFECT_LOGGING_SETTINGS_PATH environment variable
        4. Default configuration

    """

    def __init__(self, config_path: Path | None = None):
        """Initialize logging configuration.

        Args:
            config_path: Optional path to YAML configuration file.
        """
        self.config_path = config_path or self._get_default_config_path()
        self._config: dict[str, Any] | None = None

    @staticmethod
    def _get_default_config_path() -> Path | None:
        """Get default config path from environment variables.

        Returns:
            Path to the config file or None if not found.
        """
        # Check environment variable first
        if env_path := os.environ.get("AI_PIPELINE_LOGGING_CONFIG"):
            return Path(env_path)

        # Check Prefect's setting
        if prefect_path := os.environ.get("PREFECT_LOGGING_SETTINGS_PATH"):
            return Path(prefect_path)

        return None

    def load_config(self) -> dict[str, Any]:
        """Load logging configuration from file or defaults.

        Returns:
            Dictionary containing logging configuration.
        """
        if self._config is None:
            if self.config_path and self.config_path.exists():
                with open(self.config_path, encoding="utf-8") as f:
                    self._config = yaml.safe_load(f)
            else:
                self._config = self._get_default_config()
        # self._config cannot be None at this point
        assert self._config is not None
        return self._config

    @staticmethod
    def _get_default_config() -> dict[str, Any]:
        """Get default logging configuration.

        Returns:
            Default logging configuration dictionary.
        """
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s - %(message)s",
                    "datefmt": "%H:%M:%S",
                },
                "detailed": {
                    "format": ("%(asctime)s | %(levelname)-7s | %(name)s | %(funcName)s:%(lineno)d - %(message)s"),
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "level": "INFO",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "ai_pipeline_core": {
                    "level": os.environ.get("AI_PIPELINE_LOG_LEVEL", "DEBUG"),
                    "handlers": [],
                    "propagate": True,
                },
                "httpx": {
                    "level": "WARNING",
                    "propagate": True,
                },
                "httpcore": {
                    "level": "WARNING",
                    "propagate": True,
                },
                "LiteLLM": {
                    "level": "WARNING",
                    "propagate": True,
                },
                "clickhouse_connect": {
                    "level": "WARNING",
                    "propagate": True,
                },
            },
            "root": {
                "level": "DEBUG",
                "handlers": ["console"],
            },
        }

    def apply(self) -> None:
        """Apply the logging configuration."""
        config = self.load_config()
        logging.config.dictConfig(config)

        # Set Prefect logging environment variables if needed
        if "prefect" in config.get("loggers", {}):
            prefect_level = config["loggers"]["prefect"].get("level", "INFO")
            os.environ.setdefault("PREFECT_LOGGING_LEVEL", prefect_level)


# Global configuration instance
_logging_config: _LoggingConfig | None = None
_setup_lock = threading.Lock()


def setup_logging(config_path: Path | None = None, level: str | None = None) -> None:
    """Setup logging for the AI Pipeline Core library.

    Initializes logging configuration for the pipeline system.
    Called automatically at package import time (``ai_pipeline_core/__init__.py``).

    Args:
        config_path: Optional path to YAML logging configuration file.
        level: Optional log level override (INFO, DEBUG, WARNING, etc.).

    """
    global _logging_config  # noqa: PLW0603

    with _setup_lock:
        _logging_config = _LoggingConfig(config_path)
        _logging_config.apply()

        # Override level if provided
        if level:
            # Set for our loggers
            for logger_name in _DEFAULT_LOG_LEVELS:
                logger = logging.getLogger(logger_name)
                logger.setLevel(level)

            # Also set for Prefect
            os.environ["PREFECT_LOGGING_LEVEL"] = level
