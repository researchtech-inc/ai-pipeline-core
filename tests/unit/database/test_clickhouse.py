"""Tests for ClickHouseDatabase DDL, schema versioning, and basic availability checks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from clickhouse_connect.driver.exceptions import OperationalError as ClickHouseOperationalError

from ai_pipeline_core.database.clickhouse._ddl import (
    BLOBS_DDL,
    DDL_STATEMENTS,
    DOCUMENTS_DDL,
    LOGS_DDL,
    SCHEMA_META_DDL,
    SCHEMA_META_TABLE,
    SCHEMA_VERSION,
    SPANS_DDL,
)
from ai_pipeline_core.database.clickhouse._connection import (
    SchemaVersionError,
    _create_client,
    _ensure_schema,
    reset_schema_check,
)
from ai_pipeline_core.settings import Settings


def _extract_table_body(ddl: str) -> str:
    start = ddl.find("(")
    if start == -1:
        raise AssertionError(f"DDL is missing a column list: {ddl}")
    depth = 0
    for index in range(start, len(ddl)):
        char = ddl[index]
        if char == "(":
            depth += 1
            continue
        if char != ")":
            continue
        depth -= 1
        if depth == 0:
            return ddl[start + 1 : index]
    raise AssertionError(f"DDL has unbalanced parentheses: {ddl}")


def _extract_column_lines(ddl: str) -> list[str]:
    lines: list[str] = []
    for raw_line in _extract_table_body(ddl).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("INDEX "):
            continue
        lines.append(line.rstrip(","))
    return lines


def _extract_index_lines(ddl: str) -> list[str]:
    return [
        line.strip().rstrip(",") for line in _extract_table_body(ddl).splitlines() if line.strip().startswith("INDEX ")
    ]


def test_ddl_statement_list_includes_all_tables() -> None:
    assert DDL_STATEMENTS == [SCHEMA_META_DDL, SPANS_DDL, DOCUMENTS_DDL, BLOBS_DDL, LOGS_DDL]


def test_spans_ddl_matches_expected_shape() -> None:
    assert len(_extract_column_lines(SPANS_DDL)) == 30
    assert "ENGINE = ReplacingMergeTree(version)" in SPANS_DDL
    assert "ORDER BY (root_deployment_id, deployment_id, span_id)" in SPANS_DDL
    assert len(_extract_index_lines(SPANS_DDL)) == 11
    assert "detail_json" not in SPANS_DDL


def test_documents_ddl_matches_expected_shape() -> None:
    assert "description String DEFAULT ''" in DOCUMENTS_DDL
    assert "mime_type LowCardinality(String) DEFAULT ''" in DOCUMENTS_DDL
    assert "attachments Nested(" in DOCUMENTS_DDL
    assert "ENGINE = ReplacingMergeTree()" in DOCUMENTS_DDL
    assert "detail_json" not in DOCUMENTS_DDL
    assert "version" not in DOCUMENTS_DDL
    assert "CODEC(ZSTD(3))" not in DOCUMENTS_DDL.split("summary String DEFAULT ''", 1)[1].split("\n", 1)[0]
    assert len(_extract_index_lines(DOCUMENTS_DDL)) == 4


def test_blobs_and_logs_ddl_match_expected_shape() -> None:
    assert len(_extract_column_lines(BLOBS_DDL)) == 2
    assert "ORDER BY (content_sha256)" in BLOBS_DDL
    assert len(_extract_column_lines(LOGS_DDL)) == 11
    assert "ORDER BY (deployment_id, span_id, timestamp, sequence_no)" in LOGS_DDL


def test_schema_meta_ddl_shape() -> None:
    assert f"CREATE TABLE IF NOT EXISTS {SCHEMA_META_TABLE}" in SCHEMA_META_DDL
    assert "version UInt32" in SCHEMA_META_DDL
    assert "applied_at DateTime64(3, 'UTC')" in SCHEMA_META_DDL
    assert "framework_version String" in SCHEMA_META_DDL
    assert "ENGINE = MergeTree()" in SCHEMA_META_DDL
    assert "ORDER BY version" in SCHEMA_META_DDL


def test_schema_version_is_positive_integer() -> None:
    assert isinstance(SCHEMA_VERSION, int)
    assert SCHEMA_VERSION >= 1


def test_schema_meta_ddl_is_first_in_ddl_statements() -> None:
    assert DDL_STATEMENTS[0] is SCHEMA_META_DDL


# ---------------------------------------------------------------------------
# Unit tests for _ensure_schema (mocked ClickHouse client)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_schema_flag():
    """Reset the process-level schema check flag before and after each test."""
    reset_schema_check()
    yield
    reset_schema_check()


def _mock_client(*, table_exists: bool, db_version: int | None = None) -> AsyncMock:
    """Build a mock AsyncClient for _ensure_schema tests."""
    client = AsyncMock()

    # _table_exists queries system.tables
    query_result = MagicMock()
    if table_exists:
        if db_version is not None:
            query_result.result_rows = [(db_version,)]
        else:
            query_result.result_rows = [(0,)]
        # First call: system.tables check (returns rows = table exists)
        # Second call: SELECT max(version)
        system_result = MagicMock()
        system_result.result_rows = [(1,)]
        client.query = AsyncMock(side_effect=[system_result, query_result])
    else:
        # system.tables check returns no rows = table doesn't exist
        system_result = MagicMock()
        system_result.result_rows = []
        client.query = AsyncMock(return_value=system_result)
    return client


@pytest.mark.asyncio
async def test_ensure_schema_creates_tables_on_fresh_db() -> None:
    client = _mock_client(table_exists=False)

    await _ensure_schema(client, "default")

    # DDL_STATEMENTS has 5 entries + 1 INSERT for the version stamp
    assert client.command.call_count == len(DDL_STATEMENTS) + 1
    last_call_sql = client.command.call_args_list[-1].args[0]
    assert SCHEMA_META_TABLE in last_call_sql
    assert "INSERT" in last_call_sql


@pytest.mark.asyncio
async def test_ensure_schema_passes_on_matching_version() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION)

    await _ensure_schema(client, "default")

    # No DDL should have been run — only 2 queries (system.tables + max(version))
    client.command.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_schema_raises_on_outdated_db() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION - 1)

    with pytest.raises(SchemaVersionError, match="older than the framework expects"):
        await _ensure_schema(client, "default")


@pytest.mark.asyncio
async def test_ensure_schema_raises_on_newer_db() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION + 1)

    with pytest.raises(SchemaVersionError, match="newer than the framework supports"):
        await _ensure_schema(client, "default")


@pytest.mark.asyncio
async def test_ensure_schema_runs_only_once_per_process() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION)

    await _ensure_schema(client, "default")
    initial_query_count = client.query.call_count

    # Second call should be a no-op (process-level flag is set)
    await _ensure_schema(client, "default")
    assert client.query.call_count == initial_query_count


@pytest.mark.asyncio
async def test_ensure_schema_recovers_incomplete_bootstrap() -> None:
    """When schema_meta exists but has no version row (db_version=0), re-run bootstrap."""
    client = _mock_client(table_exists=True, db_version=0)

    await _ensure_schema(client, "default")

    # Should have run CREATE IF NOT EXISTS DDLs + INSERT version stamp
    assert client.command.call_count == len(DDL_STATEMENTS) + 1
    last_call_sql = client.command.call_args_list[-1].args[0]
    assert "INSERT" in last_call_sql
    assert SCHEMA_META_TABLE in last_call_sql


@pytest.mark.asyncio
async def test_reset_schema_check_allows_recheck() -> None:
    client = _mock_client(table_exists=True, db_version=SCHEMA_VERSION)

    await _ensure_schema(client, "default")
    reset_schema_check()

    # After reset, a new client should trigger a fresh check
    client2 = _mock_client(table_exists=True, db_version=SCHEMA_VERSION)
    await _ensure_schema(client2, "default")
    assert client2.query.call_count == 2  # system.tables + max(version)


# ---------------------------------------------------------------------------
# _create_client retry tests (mocked get_async_client)
# ---------------------------------------------------------------------------


def _retry_settings(*, retries: int = 3, backoff: int = 0) -> Settings:
    return Settings(
        clickhouse_host="test-host",
        clickhouse_connect_retries=retries,
        clickhouse_retry_backoff_sec=backoff,
    )


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_succeeds_on_first_attempt(mock_get_client: AsyncMock) -> None:
    mock_client = AsyncMock()
    mock_get_client.return_value = mock_client

    result = await _create_client(_retry_settings())

    assert result is mock_client
    assert mock_get_client.call_count == 1


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_retries_on_operational_error(mock_get_client: AsyncMock) -> None:
    mock_client = AsyncMock()
    mock_get_client.side_effect = [
        ClickHouseOperationalError("Read timed out"),
        mock_client,
    ]

    result = await _create_client(_retry_settings(retries=3, backoff=0))

    assert result is mock_client
    assert mock_get_client.call_count == 2


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_retries_on_os_error(mock_get_client: AsyncMock) -> None:
    mock_client = AsyncMock()
    mock_get_client.side_effect = [
        OSError("Connection refused"),
        mock_client,
    ]

    result = await _create_client(_retry_settings(retries=2, backoff=0))

    assert result is mock_client
    assert mock_get_client.call_count == 2


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_raises_after_all_retries_exhausted(mock_get_client: AsyncMock) -> None:
    mock_get_client.side_effect = ClickHouseOperationalError("Read timed out")

    with pytest.raises(ClickHouseOperationalError, match="Read timed out"):
        await _create_client(_retry_settings(retries=3, backoff=0))

    assert mock_get_client.call_count == 3


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_succeeds_on_last_attempt(mock_get_client: AsyncMock) -> None:
    mock_client = AsyncMock()
    mock_get_client.side_effect = [
        ClickHouseOperationalError("timeout 1"),
        OSError("timeout 2"),
        mock_client,
    ]

    result = await _create_client(_retry_settings(retries=3, backoff=0))

    assert result is mock_client
    assert mock_get_client.call_count == 3


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_no_retry_on_non_retryable_error(mock_get_client: AsyncMock) -> None:
    mock_get_client.side_effect = ValueError("bad config")

    with pytest.raises(ValueError, match="bad config"):
        await _create_client(_retry_settings(retries=3, backoff=0))

    assert mock_get_client.call_count == 1


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.asyncio.sleep", new_callable=AsyncMock)
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_backoff_increases_with_attempt(mock_get_client: AsyncMock, mock_sleep: AsyncMock) -> None:
    mock_client = AsyncMock()
    mock_get_client.side_effect = [
        ClickHouseOperationalError("timeout 1"),
        ClickHouseOperationalError("timeout 2"),
        mock_client,
    ]

    await _create_client(_retry_settings(retries=3, backoff=10))

    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(10)  # attempt 1: 10 * 1
    mock_sleep.assert_any_call(20)  # attempt 2: 10 * 2


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection.get_async_client")
async def test_create_client_retries_at_least_once_with_zero_retries_setting(mock_get_client: AsyncMock) -> None:
    """clickhouse_connect_retries=0 should still attempt once (max(0, 1) = 1)."""
    mock_get_client.side_effect = ClickHouseOperationalError("timeout")

    with pytest.raises(ClickHouseOperationalError):
        await _create_client(_retry_settings(retries=0, backoff=0))

    assert mock_get_client.call_count == 1


# ---------------------------------------------------------------------------
# get_async_clickhouse_client — client lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection._ensure_schema", new_callable=AsyncMock)
@patch("ai_pipeline_core.database.clickhouse._connection._create_client", new_callable=AsyncMock)
async def test_get_async_clickhouse_client_closes_client_on_schema_version_error(
    mock_create: AsyncMock, mock_schema: AsyncMock
) -> None:
    mock_client = AsyncMock()
    mock_create.return_value = mock_client
    mock_schema.side_effect = SchemaVersionError("version mismatch")

    with pytest.raises(SchemaVersionError):
        from ai_pipeline_core.database.clickhouse._connection import get_async_clickhouse_client

        await get_async_clickhouse_client(_retry_settings())

    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
@patch("ai_pipeline_core.database.clickhouse._connection._ensure_schema", new_callable=AsyncMock)
@patch("ai_pipeline_core.database.clickhouse._connection._create_client", new_callable=AsyncMock)
async def test_get_async_clickhouse_client_closes_client_on_os_error(
    mock_create: AsyncMock, mock_schema: AsyncMock
) -> None:
    mock_client = AsyncMock()
    mock_create.return_value = mock_client
    mock_schema.side_effect = OSError("connection lost during schema check")

    with pytest.raises(OSError):
        from ai_pipeline_core.database.clickhouse._connection import get_async_clickhouse_client

        await get_async_clickhouse_client(_retry_settings())

    mock_client.close.assert_awaited_once()
