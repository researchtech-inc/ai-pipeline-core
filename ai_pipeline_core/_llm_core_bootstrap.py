"""Boot-time integration glue between ``Settings`` and ``_llm_core``.

Imported once from ``ai_pipeline_core/__init__.py`` so ``_llm_core`` reads
its configuration (OpenAI base URL / API key, retry defaults) from the
framework's ``Settings`` without ``_llm_core`` importing the settings
module itself.
"""

from ai_pipeline_core._llm_core._config import LLMCoreConfig, configure
from ai_pipeline_core.settings import settings

__all__ = ["install_default_config"]


def install_default_config() -> None:
    """Install the active ``_llm_core`` configuration from framework Settings.

    Called at module import (side effect) so ``_llm_core`` reads framework
    Settings without importing them directly. Application code can
    subsequently use ``_llm_core._config.override_config`` to swap
    configuration per context (e.g., in tests or multi-tenant scenarios).
    """
    configure(
        LLMCoreConfig(
            openai_base_url=settings.openai_base_url,
            openai_api_key=settings.openai_api_key,
            project=settings.project,
            http_max_connections=settings.http_max_connections,
            http_max_keepalive_connections=settings.http_max_keepalive_connections,
            http_keepalive_expiry_s=float(settings.http_keepalive_expiry_s),
            conversation_retries=settings.conversation_retries,
            conversation_retry_delay_seconds=float(settings.conversation_retry_delay_seconds),
            conversation_retry_backoff_multiplier=float(settings.conversation_retry_backoff_multiplier),
            conversation_retry_max_delay_seconds=float(settings.conversation_retry_max_delay_seconds),
            prompt_contract_max_repair=settings.prompt_contract_max_repair,
        )
    )


install_default_config()
