"""ClickHouse DDL statements for the span-based database schema."""

__all__ = [
    "BLOBS_DDL",
    "BLOBS_TABLE",
    "DDL_STATEMENTS",
    "DOCUMENTS_DDL",
    "DOCUMENTS_TABLE",
    "LOGS_DDL",
    "LOGS_TABLE",
    "LOGS_TTL_DAYS",
    "SCHEMA_META_DDL",
    "SCHEMA_META_TABLE",
    "SCHEMA_VERSION",
    "SPANS_DDL",
    "SPANS_TABLE",
]

SCHEMA_VERSION = 3

SPANS_TABLE = "spans"
DOCUMENTS_TABLE = "documents"
BLOBS_TABLE = "blobs"
LOGS_TABLE = "logs"
SCHEMA_META_TABLE = "schema_meta"

# DELETE-TTL retention for the logs table. Single source of truth: the DDL below
# interpolates this, and tests that stamp log rows assert their timestamps stay
# within this window so a fixed past date can never silently drift past the TTL.
LOGS_TTL_DAYS = 90

SPANS_DDL = f"""
CREATE TABLE IF NOT EXISTS {SPANS_TABLE} (
    span_id UUID,
    parent_span_id Nullable(UUID),
    deployment_id UUID,
    root_deployment_id UUID,
    run_id String,
    deployment_name LowCardinality(String) DEFAULT '',
    kind LowCardinality(String),
    name String,
    description String DEFAULT '',
    status LowCardinality(String),
    sequence_no UInt32,
    started_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC')),
    version UInt64,
    cache_key String DEFAULT '',
    previous_conversation_id Nullable(UUID),
    cost_usd Float64 DEFAULT 0.0,
    error_type LowCardinality(String) DEFAULT '',
    error_message String DEFAULT '' CODEC(ZSTD(3)),
    input_document_shas Array(String),
    output_document_shas Array(String),
    target String DEFAULT '',
    receiver_json String DEFAULT '' CODEC(ZSTD(3)),
    input_json String DEFAULT '' CODEC(ZSTD(3)),
    output_json String DEFAULT '' CODEC(ZSTD(3)),
    error_json String DEFAULT '' CODEC(ZSTD(3)),
    meta_json String DEFAULT '' CODEC(ZSTD(3)),
    metrics_json String DEFAULT '' CODEC(ZSTD(3)),
    input_blob_shas Array(String),
    output_blob_shas Array(String),
    INDEX idx_span_id span_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_parent parent_span_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_deployment_id deployment_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_run_id run_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_kind kind TYPE set(16) GRANULARITY 1,
    INDEX idx_status status TYPE set(16) GRANULARITY 1,
    INDEX idx_cache_key cache_key TYPE bloom_filter GRANULARITY 1,
    INDEX idx_input_docs input_document_shas TYPE bloom_filter GRANULARITY 1,
    INDEX idx_output_docs output_document_shas TYPE bloom_filter GRANULARITY 1,
    INDEX idx_input_blobs input_blob_shas TYPE bloom_filter GRANULARITY 1,
    INDEX idx_output_blobs output_blob_shas TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (root_deployment_id, deployment_id, span_id)
""".strip()

DOCUMENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DOCUMENTS_TABLE} (
    document_sha256 String,
    content_sha256 String,
    document_type LowCardinality(String),
    name String,
    description String DEFAULT '',
    mime_type LowCardinality(String) DEFAULT '',
    size_bytes UInt64 DEFAULT 0,
    summary String DEFAULT '',
    derived_from Array(String),
    triggered_by Array(String),
    attachments Nested(
        name String,
        description String,
        content_sha256 String,
        mime_type LowCardinality(String),
        size_bytes UInt64
    ),
    publicly_visible Bool DEFAULT 0,
    INDEX idx_document_type document_type TYPE set(128) GRANULARITY 1,
    INDEX idx_name name TYPE ngrambf_v1(3, 256, 2, 0) GRANULARITY 1,
    INDEX idx_derived_from derived_from TYPE bloom_filter GRANULARITY 1,
    INDEX idx_triggered_by triggered_by TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree()
ORDER BY (document_sha256)
""".strip()

BLOBS_DDL = f"""
CREATE TABLE IF NOT EXISTS {BLOBS_TABLE} (
    content_sha256 String,
    content String CODEC(ZSTD(3))
)
ENGINE = ReplacingMergeTree()
ORDER BY (content_sha256)
""".strip()

LOGS_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOGS_TABLE} (
    deployment_id UUID,
    span_id UUID,
    timestamp DateTime64(3, 'UTC'),
    sequence_no UInt32,
    level LowCardinality(String),
    category LowCardinality(String),
    event_type LowCardinality(String) DEFAULT '',
    logger_name String,
    message String CODEC(ZSTD(3)),
    fields_json String DEFAULT '{{}}' CODEC(ZSTD(3)),
    exception_text String DEFAULT '' CODEC(ZSTD(3))
)
ENGINE = MergeTree()
ORDER BY (deployment_id, span_id, timestamp, sequence_no)
TTL toDateTime(timestamp) + INTERVAL {LOGS_TTL_DAYS} DAY
""".strip()

SCHEMA_META_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_META_TABLE} (
    version UInt32,
    applied_at DateTime64(3, 'UTC'),
    framework_version String
)
ENGINE = MergeTree()
ORDER BY version
""".strip()

DDL_STATEMENTS = [
    SCHEMA_META_DDL,
    SPANS_DDL,
    DOCUMENTS_DDL,
    BLOBS_DDL,
    LOGS_DDL,
]
