"""Centralized logging configuration for AI Pipeline Core."""

import logging
import logging.config
import os
import threading
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ai_pipeline_core.settings import settings

__all__ = ["setup_logging"]

_DEFAULT_LOG_LEVELS = {
    "ai_pipeline_core": "INFO",
    "ai_pipeline_core.documents": "INFO",
    "ai_pipeline_core.llm": "INFO",
    "ai_pipeline_core.pipeline": "INFO",
    "ai_pipeline_core.testing": "DEBUG",
}


class _LoggingConfig:
    """Loads and applies stdlib logging config."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or self._get_default_config_path()
        self._config: dict[str, Any] | None = None

    @staticmethod
    def _get_default_config_path() -> Path | None:
        if env_path := os.environ.get("AI_PIPELINE_LOGGING_CONFIG"):
            return Path(env_path)
        if prefect_path := os.environ.get("PREFECT_LOGGING_SETTINGS_PATH"):
            return Path(prefect_path)
        return None

    def load_config(self) -> dict[str, Any]:
        """Load YAML config or construct the framework default."""
        if self._config is None:
            if self.config_path is not None and self.config_path.exists():
                with self.config_path.open(encoding="utf-8") as file_obj:
                    loaded = YAML(typ="safe").load(file_obj)
                self._config = loaded if isinstance(loaded, dict) else self._default_config()
            else:
                self._config = self._default_config()
        return self._config

    @staticmethod
    def _default_config() -> dict[str, Any]:
        formatter_name = "json" if settings.log_format == "json" else "standard"
        formatter_config: dict[str, Any] = {
            "standard": {
                "format": "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)s - %(message)s",
                "datefmt": "%H:%M:%S",
            },
            "json": {
                "()": "ai_pipeline_core.logger._json_formatter.JSONConsoleFormatter",
            },
        }
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "execution_context": {
                    "()": "ai_pipeline_core.logger._context_filter.ExecutionContextFilter",
                },
            },
            "formatters": formatter_config,
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                    "filters": ["execution_context"],
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
                "httpx": {"level": "WARNING", "propagate": True},
                "httpcore": {"level": "WARNING", "propagate": True},
                "LiteLLM": {"level": "WARNING", "propagate": True},
                "clickhouse_connect": {"level": "WARNING", "propagate": True},
            },
            "root": {
                "level": "DEBUG",
                "handlers": ["console"],
            },
        }

    def apply(self) -> None:
        """Apply the loaded logging config to stdlib logging."""
        config = self.load_config()
        logging.config.dictConfig(config)
        prefect_logger = config.get("loggers", {}).get("prefect")
        if isinstance(prefect_logger, dict):
            prefect_level = prefect_logger.get("level")
            if isinstance(prefect_level, str):
                os.environ.setdefault("PREFECT_LOGGING_LEVEL", prefect_level)


_logging_config: _LoggingConfig | None = None
_setup_lock = threading.Lock()


def setup_logging(config_path: Path | None = None, level: str | None = None) -> None:
    """Configure stdlib logging for framework and CLI entry points."""
    global _logging_config  # noqa: PLW0603

    with _setup_lock:
        root_logger = logging.getLogger()
        prefect_path = os.environ.get("PREFECT_LOGGING_SETTINGS_PATH")
        if prefect_path and root_logger.handlers:
            logging.captureWarnings(capture=True)
            return

        _logging_config = _LoggingConfig(config_path)
        _logging_config.apply()
        logging.captureWarnings(capture=True)

        if level is not None:
            for logger_name in _DEFAULT_LOG_LEVELS:
                logging.getLogger(logger_name).setLevel(level)
            os.environ["PREFECT_LOGGING_LEVEL"] = level
