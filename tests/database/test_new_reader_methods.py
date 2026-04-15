"""Tests for the 5 new DatabaseReader methods using _MemoryDatabase.

Covers:
- get_deployment_scoped_spans
- list_deployment_summaries
- list_tree_deployments
- get_document_producers
- get_document_events
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._types import (
    DeploymentSummaryRecord,
    DocumentEventPage,
    DocumentProducerRecord,
    SpanKind,
    SpanRecord,
    SpanStatus,
)

_BASE_TIME = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)


def _make_span(**kwargs: object) -> SpanRecord:
    deployment_id = kwargs.pop("deployment_id", uuid4())
    root_deployment_id = kwargs.pop("root_deployment_id", deployment_id)
    started_at: datetime = kwargs.pop("started_at", _BASE_TIME)
    defaults: dict[str, object] = {
        "span_id": kwargs.pop("span_id", uuid4()),
        "parent_span_id": kwargs.pop("parent_span_id", None),
        "deployment_id": deployment_id,
        "root_deployment_id": root_deployment_id,
        "run_id": kwargs.pop("run_id", f"run-{deployment_id}"),
        "deployment_name": kwargs.pop("deployment_name", "test-deploy"),
        "kind": SpanKind.TASK,
        "name": "TestSpan",
        "status": SpanStatus.COMPLETED,
        "sequence_no": 0,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=1),
        "version": 1,
    }
    defaults.update(kwargs)
    return SpanRecord(**defaults)


# ---------------------------------------------------------------------------
# get_deployment_scoped_spans
# ---------------------------------------------------------------------------


class TestGetDeploymentScopedSpans:
    """Verify get_deployment_scoped_spans returns only spans for the specified deployment_id."""

    async def test_returns_only_matching_deployment(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        dep_a = uuid4()
        dep_b = uuid4()
        span_a = _make_span(deployment_id=dep_a, root_deployment_id=root_id, name="InA")
        span_b = _make_span(deployment_id=dep_b, root_deployment_id=root_id, name="InB")
        await db.insert_span(span_a)
        await db.insert_span(span_b)

        result = await db.get_deployment_scoped_spans(root_id, dep_a)

        assert len(result) == 1
        assert result[0].span_id == span_a.span_id

    async def test_excludes_other_root_trees(self) -> None:
        db = _MemoryDatabase()
        root_a = uuid4()
        root_b = uuid4()
        dep = uuid4()
        span_a = _make_span(deployment_id=dep, root_deployment_id=root_a)
        span_b = _make_span(deployment_id=dep, root_deployment_id=root_b)
        await db.insert_span(span_a)
        await db.insert_span(span_b)

        result = await db.get_deployment_scoped_spans(root_a, dep)

        assert len(result) == 1
        assert result[0].root_deployment_id == root_a

    async def test_empty_tree(self) -> None:
        db = _MemoryDatabase()
        result = await db.get_deployment_scoped_spans(uuid4(), uuid4())
        assert result == []

    async def test_multiple_spans_same_deployment(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        dep_id = uuid4()
        s1 = _make_span(deployment_id=dep_id, root_deployment_id=root_id, name="S1", started_at=_BASE_TIME)
        s2 = _make_span(deployment_id=dep_id, root_deployment_id=root_id, name="S2", started_at=_BASE_TIME + timedelta(seconds=1))
        await db.insert_span(s1)
        await db.insert_span(s2)

        result = await db.get_deployment_scoped_spans(root_id, dep_id)

        assert len(result) == 2

    async def test_include_meta_true_returns_same_data(self) -> None:
        """Memory backend returns full data regardless of include_meta."""
        db = _MemoryDatabase()
        root_id = uuid4()
        dep_id = uuid4()
        span = _make_span(deployment_id=dep_id, root_deployment_id=root_id, meta_json='{"model": "gpt-4"}')
        await db.insert_span(span)

        result_default = await db.get_deployment_scoped_spans(root_id, dep_id)
        result_meta = await db.get_deployment_scoped_spans(root_id, dep_id, include_meta=True)
        result_no_meta = await db.get_deployment_scoped_spans(root_id, dep_id, include_meta=False)

        assert len(result_default) == 1
        assert len(result_meta) == 1
        assert len(result_no_meta) == 1
        assert result_default[0].span_id == span.span_id
        assert result_meta[0].span_id == span.span_id
        assert result_no_meta[0].span_id == span.span_id


# ---------------------------------------------------------------------------
# list_deployment_summaries
# ---------------------------------------------------------------------------


class TestListDeploymentSummaries:
    """Verify list_deployment_summaries respects root_only, status, limit, and aggregates cost."""

    async def _setup_tree(self, db: _MemoryDatabase) -> tuple[UUID, UUID, UUID]:
        """Create root + child deployment + task spans. Returns (root_id, child_dep_id, run_id_str)."""
        root_id = uuid4()
        child_id = uuid4()
        run_id = f"run-{root_id}"
        root = _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Root",
            run_id=run_id,
            status=SpanStatus.COMPLETED,
        )
        child = _make_span(
            span_id=child_id,
            parent_span_id=root_id,
            deployment_id=child_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            name="Child",
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
        task = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="Task1",
            cost_usd=0.5,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=2),
        )
        for s in (root, child, task):
            await db.insert_span(s)
        return root_id, child_id, root

    async def test_root_only_filter(self) -> None:
        db = _MemoryDatabase()
        root_id, child_id, _ = await self._setup_tree(db)

        results = await db.list_deployment_summaries(100, root_only=True)
        ids = {r.deployment_id for r in results}

        assert root_id in ids
        assert child_id not in ids

    async def test_includes_child_deployments(self) -> None:
        db = _MemoryDatabase()
        root_id, child_id, _ = await self._setup_tree(db)

        results = await db.list_deployment_summaries(100, root_only=False)
        ids = {r.deployment_id for r in results}

        assert root_id in ids
        assert child_id in ids

    async def test_status_filter(self) -> None:
        db = _MemoryDatabase()
        dep_completed = uuid4()
        dep_failed = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_completed,
                deployment_id=dep_completed,
                root_deployment_id=dep_completed,
                kind=SpanKind.DEPLOYMENT,
                status=SpanStatus.COMPLETED,
            )
        )
        await db.insert_span(
            _make_span(
                span_id=dep_failed,
                deployment_id=dep_failed,
                root_deployment_id=dep_failed,
                kind=SpanKind.DEPLOYMENT,
                status=SpanStatus.FAILED,
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )

        results = await db.list_deployment_summaries(100, status=SpanStatus.COMPLETED)
        ids = {r.deployment_id for r in results}

        assert dep_completed in ids
        assert dep_failed not in ids

    async def test_limit_respected(self) -> None:
        db = _MemoryDatabase()
        for i in range(5):
            dep_id = uuid4()
            await db.insert_span(
                _make_span(
                    span_id=dep_id,
                    deployment_id=dep_id,
                    root_deployment_id=dep_id,
                    kind=SpanKind.DEPLOYMENT,
                    started_at=_BASE_TIME + timedelta(seconds=i),
                )
            )

        results = await db.list_deployment_summaries(3)

        assert len(results) == 3

    async def test_zero_limit(self) -> None:
        db = _MemoryDatabase()
        dep_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
            )
        )

        assert await db.list_deployment_summaries(0) == []

    async def test_cost_aggregated_from_tree(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                cost_usd=0.1,
                run_id=run_id,
            )
        )
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                cost_usd=0.5,
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )

        results = await db.list_deployment_summaries(10, root_only=True)
        root_summary = next(r for r in results if r.deployment_id == root_id)

        assert root_summary.cost_usd == pytest.approx(0.6)

    async def test_zero_cost_deployment(self) -> None:
        db = _MemoryDatabase()
        dep_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
                cost_usd=0.0,
            )
        )

        results = await db.list_deployment_summaries(10, root_only=True)
        summary = next(r for r in results if r.deployment_id == dep_id)

        assert summary.cost_usd == 0.0

    async def test_returns_deployment_summary_records(self) -> None:
        db = _MemoryDatabase()
        dep_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
                name="TestDeploy",
                deployment_name="test-deploy",
                run_id="run-123",
                status=SpanStatus.COMPLETED,
                started_at=_BASE_TIME,
                ended_at=_BASE_TIME + timedelta(hours=1),
            )
        )

        results = await db.list_deployment_summaries(10)
        summary = next(r for r in results if r.deployment_id == dep_id)

        assert isinstance(summary, DeploymentSummaryRecord)
        assert summary.run_id == "run-123"
        assert summary.name == "TestDeploy"
        assert summary.status == SpanStatus.COMPLETED
        assert summary.started_at == _BASE_TIME
        assert summary.ended_at == _BASE_TIME + timedelta(hours=1)

    async def test_empty_database(self) -> None:
        db = _MemoryDatabase()
        assert await db.list_deployment_summaries(10) == []


# ---------------------------------------------------------------------------
# list_tree_deployments
# ---------------------------------------------------------------------------


class TestListTreeDeployments:
    """Verify list_tree_deployments returns deployment-kind spans with per-deployment cost."""

    async def test_returns_deployment_spans_only(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        root = _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
        )
        task = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
        await db.insert_span(root)
        await db.insert_span(task)

        results = await db.list_tree_deployments(root_id)

        assert len(results) == 1
        assert results[0].deployment_id == root_id

    async def test_per_deployment_cost(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        child_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                cost_usd=0.1,
                run_id=run_id,
            )
        )
        await db.insert_span(
            _make_span(
                span_id=child_id,
                parent_span_id=root_id,
                deployment_id=child_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                cost_usd=0.0,
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )
        # Task under root deployment
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                cost_usd=0.5,
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=2),
            )
        )
        # Task under child deployment
        await db.insert_span(
            _make_span(
                deployment_id=child_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                cost_usd=0.3,
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=3),
            )
        )

        results = await db.list_tree_deployments(root_id)
        cost_map = {r.deployment_id: r.cost_usd for r in results}

        # root_id cost = 0.1 (deployment) + 0.5 (task) = 0.6
        assert cost_map[root_id] == pytest.approx(0.6)
        # child_id cost = 0.0 (deployment) + 0.3 (task) = 0.3
        assert cost_map[child_id] == pytest.approx(0.3)

    async def test_ordered_by_started_at(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        child_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                run_id=run_id,
                started_at=_BASE_TIME,
            )
        )
        await db.insert_span(
            _make_span(
                span_id=child_id,
                parent_span_id=root_id,
                deployment_id=child_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=10),
            )
        )

        results = await db.list_tree_deployments(root_id)

        assert results[0].deployment_id == root_id
        assert results[1].deployment_id == child_id

    async def test_empty_tree(self) -> None:
        db = _MemoryDatabase()
        assert await db.list_tree_deployments(uuid4()) == []

    async def test_deployment_with_no_cost(self) -> None:
        db = _MemoryDatabase()
        dep_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
                cost_usd=0.0,
            )
        )

        results = await db.list_tree_deployments(dep_id)

        assert len(results) == 1
        assert results[0].cost_usd == 0.0


# ---------------------------------------------------------------------------
# get_document_producers
# ---------------------------------------------------------------------------


class TestGetDocumentProducers:
    """Verify get_document_producers returns earliest producer per document SHA."""

    async def test_returns_earliest_producer(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        early = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="EarlyTask",
            run_id=run_id,
            started_at=_BASE_TIME,
            output_document_shas=("doc-sha-1",),
        )
        late = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="LateTask",
            run_id=run_id,
            started_at=_BASE_TIME + timedelta(seconds=10),
            output_document_shas=("doc-sha-1",),
        )
        await db.insert_span(early)
        await db.insert_span(late)

        producers = await db.get_document_producers(root_id)

        assert "doc-sha-1" in producers
        assert producers["doc-sha-1"].span_name == "EarlyTask"
        assert producers["doc-sha-1"].started_at == _BASE_TIME

    async def test_excludes_deployment_and_attempt_spans(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        dep = _make_span(
            span_id=root_id,
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.DEPLOYMENT,
            run_id=run_id,
            output_document_shas=("doc-sha-dep",),
        )
        attempt = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.ATTEMPT,
            run_id=run_id,
            output_document_shas=("doc-sha-att",),
            started_at=_BASE_TIME + timedelta(seconds=1),
        )
        task = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            run_id=run_id,
            output_document_shas=("doc-sha-task",),
            started_at=_BASE_TIME + timedelta(seconds=2),
        )
        for s in (dep, attempt, task):
            await db.insert_span(s)

        producers = await db.get_document_producers(root_id)

        assert "doc-sha-dep" not in producers
        assert "doc-sha-att" not in producers
        assert "doc-sha-task" in producers

    async def test_multiple_documents(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        task = _make_span(
            deployment_id=root_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            run_id=run_id,
            output_document_shas=("sha-a", "sha-b"),
        )
        await db.insert_span(task)

        producers = await db.get_document_producers(root_id)

        assert len(producers) == 2
        assert "sha-a" in producers
        assert "sha-b" in producers

    async def test_empty_tree(self) -> None:
        db = _MemoryDatabase()
        assert await db.get_document_producers(uuid4()) == {}

    async def test_no_documents(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
            )
        )
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )

        assert await db.get_document_producers(root_id) == {}

    async def test_returns_document_producer_record(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        dep_id = uuid4()
        run_id = f"run-{root_id}"
        task = _make_span(
            deployment_id=dep_id,
            root_deployment_id=root_id,
            kind=SpanKind.TASK,
            name="ProducerTask",
            deployment_name="my-deploy",
            run_id=run_id,
            output_document_shas=("doc-sha",),
            started_at=_BASE_TIME,
        )
        await db.insert_span(task)

        producers = await db.get_document_producers(root_id)
        rec = producers["doc-sha"]

        assert isinstance(rec, DocumentProducerRecord)
        assert rec.document_sha256 == "doc-sha"
        assert rec.span_id == task.span_id
        assert rec.span_name == "ProducerTask"
        assert rec.span_kind == SpanKind.TASK
        assert rec.deployment_id == dep_id
        assert rec.deployment_name == "my-deploy"
        assert rec.started_at == _BASE_TIME


# ---------------------------------------------------------------------------
# get_document_events
# ---------------------------------------------------------------------------


class TestGetDocumentEvents:
    """Verify get_document_events filtering, ordering, and total_events counting."""

    async def _setup_events_db(self) -> tuple[_MemoryDatabase, UUID]:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        dep_a = uuid4()
        dep_b = uuid4()
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                run_id=run_id,
            )
        )
        # Task in dep_a: produces doc-1 (output), consumes doc-2 (input)
        await db.insert_span(
            _make_span(
                deployment_id=dep_a,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                name="TaskA",
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=1),
                ended_at=_BASE_TIME + timedelta(seconds=2),
                output_document_shas=("doc-1",),
                input_document_shas=("doc-2",),
            )
        )
        # Task in dep_b: produces doc-3
        await db.insert_span(
            _make_span(
                deployment_id=dep_b,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                name="TaskB",
                run_id=run_id,
                started_at=_BASE_TIME + timedelta(seconds=5),
                ended_at=_BASE_TIME + timedelta(seconds=6),
                output_document_shas=("doc-3",),
            )
        )
        return db, root_id

    async def test_returns_all_events(self) -> None:
        db, root_id = await self._setup_events_db()

        page = await db.get_document_events(root_id)

        # 1 output (doc-1) + 1 input (doc-2) + 1 output (doc-3) = 3 events
        assert page.total_events == 3
        assert len(page.events) == 3

    async def test_ordered_by_recency(self) -> None:
        db, root_id = await self._setup_events_db()

        page = await db.get_document_events(root_id)

        timestamps = [e.timestamp for e in page.events]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_limit_truncates(self) -> None:
        db, root_id = await self._setup_events_db()

        page = await db.get_document_events(root_id, limit=2)

        assert len(page.events) == 2
        assert page.total_events == 3

    async def test_deployment_id_scoping(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        dep_a = uuid4()
        dep_b = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                deployment_id=dep_a,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("doc-a",),
            )
        )
        await db.insert_span(
            _make_span(
                deployment_id=dep_b,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("doc-b",),
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )

        page = await db.get_document_events(root_id, deployment_id=dep_a)

        assert page.total_events == 1
        assert page.events[0].document_sha256 == "doc-a"

    async def test_since_filter(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("old-doc",),
                started_at=_BASE_TIME,
                ended_at=_BASE_TIME + timedelta(seconds=1),
            )
        )
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("new-doc",),
                started_at=_BASE_TIME + timedelta(hours=1),
                ended_at=_BASE_TIME + timedelta(hours=1, seconds=1),
            )
        )

        page = await db.get_document_events(root_id, since=_BASE_TIME + timedelta(minutes=30))

        assert page.total_events == 1
        assert page.events[0].document_sha256 == "new-doc"

    async def test_event_types_filter(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("doc-out",),
                input_document_shas=("doc-in",),
            )
        )

        # Only task_output events
        page = await db.get_document_events(root_id, event_types=["task_output"])

        assert page.total_events == 1
        assert page.events[0].direction == "output"

    async def test_event_types_input_only(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                output_document_shas=("doc-out",),
                input_document_shas=("doc-in",),
            )
        )

        page = await db.get_document_events(root_id, event_types=["task_input"])

        assert page.total_events == 1
        assert page.events[0].direction == "input"

    async def test_excludes_deployment_and_attempt_spans(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                span_id=root_id,
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.DEPLOYMENT,
                run_id=run_id,
                output_document_shas=("dep-doc",),
            )
        )
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.ATTEMPT,
                run_id=run_id,
                output_document_shas=("att-doc",),
                started_at=_BASE_TIME + timedelta(seconds=1),
            )
        )

        page = await db.get_document_events(root_id)

        assert page.total_events == 0

    async def test_empty_tree(self) -> None:
        db = _MemoryDatabase()
        page = await db.get_document_events(uuid4())
        assert page == DocumentEventPage(events=(), total_events=0)

    async def test_output_uses_ended_at_input_uses_started_at(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        started = _BASE_TIME
        ended = _BASE_TIME + timedelta(seconds=5)
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                started_at=started,
                ended_at=ended,
                output_document_shas=("out-doc",),
                input_document_shas=("in-doc",),
            )
        )

        page = await db.get_document_events(root_id)

        output_event = next(e for e in page.events if e.direction == "output")
        input_event = next(e for e in page.events if e.direction == "input")
        assert output_event.timestamp == ended
        assert input_event.timestamp == started

    async def test_output_falls_back_to_started_at_when_no_ended_at(self) -> None:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        await db.insert_span(
            _make_span(
                deployment_id=root_id,
                root_deployment_id=root_id,
                kind=SpanKind.TASK,
                run_id=run_id,
                started_at=_BASE_TIME,
                ended_at=None,
                output_document_shas=("doc",),
            )
        )

        page = await db.get_document_events(root_id)

        assert page.events[0].timestamp == _BASE_TIME


# ---------------------------------------------------------------------------
# Pagination (offset) tests
# ---------------------------------------------------------------------------


class TestListDeploymentsOffset:
    """Verify offset pagination on list_deployments."""

    async def _setup(self, db: _MemoryDatabase, count: int = 5) -> list[UUID]:
        ids = []
        for i in range(count):
            dep_id = uuid4()
            ids.append(dep_id)
            await db.insert_span(
                _make_span(
                    span_id=dep_id,
                    deployment_id=dep_id,
                    root_deployment_id=dep_id,
                    kind=SpanKind.DEPLOYMENT,
                    started_at=_BASE_TIME + timedelta(seconds=i),
                )
            )
        return ids

    async def test_offset_zero_is_default(self) -> None:
        db = _MemoryDatabase()
        await self._setup(db)

        default = await db.list_deployments(3)
        explicit = await db.list_deployments(3, offset=0)

        assert [s.span_id for s in default] == [s.span_id for s in explicit]

    async def test_offset_skips_rows(self) -> None:
        db = _MemoryDatabase()
        await self._setup(db, count=5)

        page1 = await db.list_deployments(2, offset=0)
        page2 = await db.list_deployments(2, offset=2)
        page3 = await db.list_deployments(2, offset=4)

        all_ids = [s.span_id for s in page1] + [s.span_id for s in page2] + [s.span_id for s in page3]
        assert len(set(all_ids)) == 5

    async def test_offset_past_end_returns_empty(self) -> None:
        db = _MemoryDatabase()
        await self._setup(db, count=3)

        result = await db.list_deployments(10, offset=100)

        assert result == []


class TestListDeploymentSummariesOffset:
    """Verify offset pagination on list_deployment_summaries."""

    async def test_offset_skips_rows(self) -> None:
        db = _MemoryDatabase()
        for i in range(5):
            dep_id = uuid4()
            await db.insert_span(
                _make_span(
                    span_id=dep_id,
                    deployment_id=dep_id,
                    root_deployment_id=dep_id,
                    kind=SpanKind.DEPLOYMENT,
                    started_at=_BASE_TIME + timedelta(seconds=i),
                )
            )

        page1 = await db.list_deployment_summaries(2, offset=0)
        page2 = await db.list_deployment_summaries(2, offset=2)

        ids1 = {r.deployment_id for r in page1}
        ids2 = {r.deployment_id for r in page2}
        assert len(ids1) == 2
        assert len(ids2) == 2
        assert ids1.isdisjoint(ids2)

    async def test_offset_with_filters(self) -> None:
        db = _MemoryDatabase()
        for i in range(4):
            dep_id = uuid4()
            await db.insert_span(
                _make_span(
                    span_id=dep_id,
                    deployment_id=dep_id,
                    root_deployment_id=dep_id,
                    kind=SpanKind.DEPLOYMENT,
                    status=SpanStatus.COMPLETED,
                    started_at=_BASE_TIME + timedelta(seconds=i),
                )
            )
        # Add one failed deployment
        failed_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=failed_id,
                deployment_id=failed_id,
                root_deployment_id=failed_id,
                kind=SpanKind.DEPLOYMENT,
                status=SpanStatus.FAILED,
                started_at=_BASE_TIME + timedelta(seconds=10),
            )
        )

        page = await db.list_deployment_summaries(2, status=SpanStatus.COMPLETED, offset=2)

        assert len(page) == 2
        assert all(r.status == SpanStatus.COMPLETED for r in page)

    async def test_offset_past_end(self) -> None:
        db = _MemoryDatabase()
        dep_id = uuid4()
        await db.insert_span(
            _make_span(
                span_id=dep_id,
                deployment_id=dep_id,
                root_deployment_id=dep_id,
                kind=SpanKind.DEPLOYMENT,
            )
        )

        assert await db.list_deployment_summaries(10, offset=100) == []


class TestGetDocumentEventsOffset:
    """Verify offset pagination on get_document_events."""

    async def _setup_events(self) -> tuple[_MemoryDatabase, UUID]:
        db = _MemoryDatabase()
        root_id = uuid4()
        run_id = f"run-{root_id}"
        for i in range(10):
            await db.insert_span(
                _make_span(
                    deployment_id=root_id,
                    root_deployment_id=root_id,
                    kind=SpanKind.TASK,
                    run_id=run_id,
                    started_at=_BASE_TIME + timedelta(seconds=i),
                    ended_at=_BASE_TIME + timedelta(seconds=i, milliseconds=500),
                    output_document_shas=(f"doc-{i}",),
                )
            )
        return db, root_id

    async def test_offset_zero_is_default(self) -> None:
        db, root_id = await self._setup_events()

        default = await db.get_document_events(root_id, limit=5)
        explicit = await db.get_document_events(root_id, limit=5, offset=0)

        assert default.events == explicit.events
        assert default.total_events == explicit.total_events

    async def test_offset_paginates(self) -> None:
        db, root_id = await self._setup_events()

        page1 = await db.get_document_events(root_id, limit=3, offset=0)
        page2 = await db.get_document_events(root_id, limit=3, offset=3)

        shas1 = {e.document_sha256 for e in page1.events}
        shas2 = {e.document_sha256 for e in page2.events}
        assert len(shas1) == 3
        assert len(shas2) == 3
        assert shas1.isdisjoint(shas2)

    async def test_total_events_independent_of_offset(self) -> None:
        db, root_id = await self._setup_events()

        page1 = await db.get_document_events(root_id, limit=3, offset=0)
        page2 = await db.get_document_events(root_id, limit=3, offset=6)

        assert page1.total_events == 10
        assert page2.total_events == 10

    async def test_offset_past_end_returns_empty_events(self) -> None:
        db, root_id = await self._setup_events()

        page = await db.get_document_events(root_id, limit=5, offset=100)

        assert len(page.events) == 0
        assert page.total_events == 10

    async def test_offset_with_since_filter(self) -> None:
        db, root_id = await self._setup_events()

        # since filters first, then offset paginates within filtered set
        cutoff = _BASE_TIME + timedelta(seconds=5)
        page = await db.get_document_events(root_id, limit=2, offset=0, since=cutoff)
        page2 = await db.get_document_events(root_id, limit=2, offset=2, since=cutoff)

        shas1 = {e.document_sha256 for e in page.events}
        shas2 = {e.document_sha256 for e in page2.events}
        assert shas1.isdisjoint(shas2)
