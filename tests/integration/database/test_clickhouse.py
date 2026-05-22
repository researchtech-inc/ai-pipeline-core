"""ClickHouseDatabase integration tests requiring a running ClickHouse container."""

import asyncio
from uuid import uuid4

import pytest

from ai_pipeline_core.database.clickhouse._connection import SchemaVersionError, reset_schema_check
from ai_pipeline_core.database.clickhouse._ddl import SCHEMA_META_TABLE, SCHEMA_VERSION
from ai_pipeline_core.settings import Settings

HTTP_PORT = 8123


@pytest.fixture(scope="module")
def clickhouse_container(require_docker):
    """Start a ClickHouse container once per module, exposing the HTTP port."""
    from testcontainers.clickhouse import ClickHouseContainer

    container = ClickHouseContainer(port=HTTP_PORT)
    container.with_exposed_ports(HTTP_PORT)
    with container:
        yield container


@pytest.fixture(scope="module")
def clickhouse_settings(clickhouse_container) -> Settings:
    """Build Settings pointing at the testcontainer ClickHouse instance."""
    return Settings(
        clickhouse_host=clickhouse_container.get_container_host_ip(),
        clickhouse_port=int(clickhouse_container.get_exposed_port(HTTP_PORT)),
        clickhouse_database=clickhouse_container.dbname,
        clickhouse_user=clickhouse_container.username,
        clickhouse_password=clickhouse_container.password,
        clickhouse_secure=False,
    )


def test_clickhouse_database_can_connect(clickhouse_settings) -> None:
    from ai_pipeline_core.database.clickhouse._backend import ClickHouseDatabase

    database = ClickHouseDatabase(settings=clickhouse_settings)
    assert database is not None


def test_clickhouse_schema_auto_init_creates_tables_and_stamps_version(clickhouse_settings) -> None:
    """On a fresh database, get_async_clickhouse_client auto-creates all tables and stamps the schema version."""
    from clickhouse_connect import get_async_client

    from ai_pipeline_core.database.clickhouse._connection import get_async_clickhouse_client

    test_database = f"schema_test_{uuid4().hex[:12]}"

    async def _run() -> None:
        admin_client = await get_async_client(
            host=clickhouse_settings.clickhouse_host,
            port=clickhouse_settings.clickhouse_port,
            database=clickhouse_settings.clickhouse_database,
            username=clickhouse_settings.clickhouse_user,
            password=clickhouse_settings.clickhouse_password,
            secure=clickhouse_settings.clickhouse_secure,
            connect_timeout=clickhouse_settings.clickhouse_connect_timeout,
            send_receive_timeout=clickhouse_settings.clickhouse_send_receive_timeout,
        )
        try:
            await admin_client.command(f"CREATE DATABASE IF NOT EXISTS {test_database}")
        finally:
            await admin_client.close()

        try:
            reset_schema_check()
            test_settings = Settings(
                clickhouse_host=clickhouse_settings.clickhouse_host,
                clickhouse_port=clickhouse_settings.clickhouse_port,
                clickhouse_database=test_database,
                clickhouse_user=clickhouse_settings.clickhouse_user,
                clickhouse_password=clickhouse_settings.clickhouse_password,
                clickhouse_secure=clickhouse_settings.clickhouse_secure,
            )
            client = await get_async_clickhouse_client(test_settings)
            try:
                result = await client.query(f"SELECT max(version) FROM {SCHEMA_META_TABLE}")
                assert result.result_rows[0][0] == SCHEMA_VERSION

                for table in ("spans", "documents", "blobs", "logs"):
                    exists_result = await client.query(
                        "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
                        parameters={"db": test_database, "tbl": table},
                    )
                    assert len(exists_result.result_rows) > 0, f"Table {table} was not created"
            finally:
                await client.close()
        finally:
            cleanup_client = await get_async_client(
                host=clickhouse_settings.clickhouse_host,
                port=clickhouse_settings.clickhouse_port,
                database=clickhouse_settings.clickhouse_database,
                username=clickhouse_settings.clickhouse_user,
                password=clickhouse_settings.clickhouse_password,
                secure=clickhouse_settings.clickhouse_secure,
                connect_timeout=clickhouse_settings.clickhouse_connect_timeout,
                send_receive_timeout=clickhouse_settings.clickhouse_send_receive_timeout,
            )
            try:
                await cleanup_client.command(f"DROP DATABASE IF EXISTS {test_database} SYNC")
            finally:
                await cleanup_client.close()
            reset_schema_check()

    asyncio.run(_run())


def test_clickhouse_schema_version_mismatch_raises(clickhouse_settings) -> None:
    """When schema_meta exists with a higher version than the framework, SchemaVersionError is raised."""
    from clickhouse_connect import get_async_client

    from ai_pipeline_core.database.clickhouse._connection import get_async_clickhouse_client

    test_database = f"schema_mismatch_{uuid4().hex[:12]}"

    async def _run() -> None:
        admin_client = await get_async_client(
            host=clickhouse_settings.clickhouse_host,
            port=clickhouse_settings.clickhouse_port,
            database=clickhouse_settings.clickhouse_database,
            username=clickhouse_settings.clickhouse_user,
            password=clickhouse_settings.clickhouse_password,
            secure=clickhouse_settings.clickhouse_secure,
            connect_timeout=clickhouse_settings.clickhouse_connect_timeout,
            send_receive_timeout=clickhouse_settings.clickhouse_send_receive_timeout,
        )
        try:
            await admin_client.command(f"CREATE DATABASE IF NOT EXISTS {test_database}")
            await admin_client.command(
                f"CREATE TABLE {test_database}.{SCHEMA_META_TABLE} "
                "(version UInt32, applied_at DateTime64(3, 'UTC'), framework_version String) "
                "ENGINE = MergeTree() ORDER BY version"
            )
            future_version = SCHEMA_VERSION + 99
            await admin_client.command(f"INSERT INTO {test_database}.{SCHEMA_META_TABLE} VALUES ({future_version}, now64(3), 'future-version')")
        finally:
            await admin_client.close()

        try:
            reset_schema_check()
            test_settings = Settings(
                clickhouse_host=clickhouse_settings.clickhouse_host,
                clickhouse_port=clickhouse_settings.clickhouse_port,
                clickhouse_database=test_database,
                clickhouse_user=clickhouse_settings.clickhouse_user,
                clickhouse_password=clickhouse_settings.clickhouse_password,
                clickhouse_secure=clickhouse_settings.clickhouse_secure,
            )
            with pytest.raises(SchemaVersionError, match="newer than the framework supports"):
                await get_async_clickhouse_client(test_settings)
        finally:
            cleanup_client = await get_async_client(
                host=clickhouse_settings.clickhouse_host,
                port=clickhouse_settings.clickhouse_port,
                database=clickhouse_settings.clickhouse_database,
                username=clickhouse_settings.clickhouse_user,
                password=clickhouse_settings.clickhouse_password,
                secure=clickhouse_settings.clickhouse_secure,
                connect_timeout=clickhouse_settings.clickhouse_connect_timeout,
                send_receive_timeout=clickhouse_settings.clickhouse_send_receive_timeout,
            )
            try:
                await cleanup_client.command(f"DROP DATABASE IF EXISTS {test_database} SYNC")
            finally:
                await cleanup_client.close()
            reset_schema_check()

    asyncio.run(_run())
