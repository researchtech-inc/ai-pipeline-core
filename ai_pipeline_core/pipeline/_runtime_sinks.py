"""Runtime sink construction helpers."""

from dataclasses import dataclass

from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.observability._laminar_sink import LaminarSpanSink
from ai_pipeline_core.observability._sentry_init import ensure_sentry_initialized
from ai_pipeline_core.observability._sentry_sink import SentrySpanSink
from ai_pipeline_core.pipeline._log_sink import DatabaseLogSink
from ai_pipeline_core.pipeline._span_sink import DatabaseSpanSink, SpanSink
from ai_pipeline_core.pipeline._types import LogSink
from ai_pipeline_core.settings import Settings

__all__ = ["RuntimeSinks", "build_runtime_sinks"]


@dataclass(frozen=True, slots=True)
class RuntimeSinks:
    """Concrete sink bundles for one runtime boundary."""

    span_sinks: tuple[SpanSink, ...]
    log_sinks: tuple[LogSink, ...]


def build_runtime_sinks(
    *,
    database: DatabaseWriter | None,
    settings_obj: Settings,
) -> RuntimeSinks:
    """Build runtime span/log sinks for one execution boundary."""
    span_sinks: list[SpanSink] = []
    log_sinks: list[LogSink] = []
    if settings_obj.sentry_dsn:
        ensure_sentry_initialized(settings_obj.sentry_dsn)
        span_sinks.append(SentrySpanSink())
    if database is not None:
        span_sinks.insert(0, DatabaseSpanSink(database))
        log_sinks.insert(0, DatabaseLogSink(database))
    if settings_obj.lmnr_project_api_key:
        span_sinks.append(LaminarSpanSink(settings_obj))
    return RuntimeSinks(span_sinks=tuple(span_sinks), log_sinks=tuple(log_sinks))
