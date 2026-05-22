"""Prove retry and cache environment variable overrides don't exist.

No PIPELINE_DISABLE_CACHE or PIPELINE_TASK_RETRIES environment variables
are read from the execution context or task configuration.
"""

import inspect

import ai_pipeline_core.pipeline._execution_context as ctx_mod
import ai_pipeline_core.pipeline._task as task_mod


class TestNoEnvOverrides:
    """Prove: environment-based overrides are not implemented."""

    def test_no_pipeline_disable_cache_env_handling(self) -> None:
        """Passes on current code — no env variable for cache disable."""
        source = inspect.getsource(ctx_mod)
        assert "PIPELINE_DISABLE_CACHE" not in source

    def test_no_pipeline_task_retries_env_handling(self) -> None:
        """Passes on current code — no env variable for retry override."""
        source = inspect.getsource(task_mod)
        assert "PIPELINE_TASK_RETRIES" not in source
