# MODULE: replay
# CLASSES: ExperimentResult, ExperimentOverrides
# PURPOSE: Generic replay and experimentation entry points.
# VERSION: 0.21.1
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import ExperimentOverrides, ExperimentResult, execute_span, experiment_batch, experiment_span
from ai_pipeline_core.replay import find_experiment_span_ids
```

## Public API

```python
@dataclass(frozen=True, slots=True)
class ExperimentResult:
    source_span_id: UUID
    replay_run_id: str
    replay_root_span_id: UUID | None
    original: OriginalOutput
    result: Any
    duration_seconds: float
    cost_usd: float
    model_used: str
    tokens: TokenSummary | None
    recording_degraded: bool = False


@dataclass(frozen=True, slots=True)
class ExperimentOverrides:
    model: str | None = None
    model_options: dict[str, Any] | None = None
    tools: Mapping[str, Tool] | None = None
    response_format: type[BaseModel] | None = None
```

## Functions

```python
def main(argv: list[str] | None = None) -> int:
    """Run the replay CLI."""
    parser = argparse.ArgumentParser(prog="ai-replay", description="Execute or inspect replayable spans")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Execute one replayable span")
    run_parser.add_argument("replay_file", nargs="?", help="Path to a span JSON file from a snapshot")
    run_parser.add_argument("--from-db", type=str, help="Load a span by span ID from the database")
    run_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")
    run_parser.add_argument("--model", type=str, default=None, help="Override model for replayed conversation spans")
    run_parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="Set ExperimentOverrides fields or model_options values")
    run_parser.add_argument("--output-dir", type=str, default=None, help="Output directory for replay results")
    run_parser.add_argument("--import", dest="modules", action="append", metavar="MODULE", help="Import a module before replay")

    show_parser = subparsers.add_parser("show", help="Inspect one replayable span")
    show_parser.add_argument("replay_file", nargs="?", help="Path to a span JSON file from a snapshot")
    show_parser.add_argument("--from-db", type=str, help="Load a span by span ID from the database")
    show_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")

    batch_parser = subparsers.add_parser("batch", help="Run replay experiments over many spans")
    batch_parser.add_argument("--from-deployment", required=True, type=str, help="Deployment/root_deployment_id to search")
    batch_parser.add_argument("--db-path", type=str, help="Use a FilesystemDatabase at this path instead of ClickHouse")
    batch_parser.add_argument("--kind", type=str, default=None, help="Filter spans by kind")
    batch_parser.add_argument("--purpose", type=str, default=None, help="Filter conversation spans by purpose")
    batch_parser.add_argument("--task-class", type=str, default=None, help="Filter task spans by module:Class path")
    batch_parser.add_argument("--model", type=str, default=None, help="Override model for replayed conversation spans")
    batch_parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="Set ExperimentOverrides fields or model_options values")
    batch_parser.add_argument("--concurrency", type=int, default=5, help="Maximum concurrent experiments")
    batch_parser.add_argument("--import", dest="modules", action="append", metavar="MODULE", help="Import a module before replay")

    args = parser.parse_args(argv)

    if args.command == "run":
        if args.replay_file is None and args.from_db is None:
            parser.error("run requires either a replay_file or --from-db <span_id>")
        if args.replay_file is not None and args.from_db is not None:
            parser.error("run accepts a replay_file or --from-db <span_id>, not both")
        return _cmd_run(args)
    if args.command == "show":
        if args.replay_file is None and args.from_db is None:
            parser.error("show requires either a replay_file or --from-db <span_id>")
        if args.replay_file is not None and args.from_db is not None:
            parser.error("show accepts a replay_file or --from-db <span_id>, not both")
        return _cmd_show(args)
    if args.command == "batch":
        return _cmd_batch(args)

    parser.print_help()
    return 1


async def execute_span(
    span_id: UUID,
    *,
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None = None,
) -> Any:
    """Replay one recorded span against live code."""
    outcome = await _execute_span_internal(
        span_id,
        source_db=source_db,
        sink_db=sink_db,
    )
    return outcome.result


async def experiment_span(
    span_id: UUID,
    *,
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None = None,
    overrides: ExperimentOverrides | None = None,
) -> ExperimentResult:
    source_span = await source_db.get_span(span_id)
    if source_span is None:
        raise FileNotFoundError(f"Span {span_id} was not found in the source database.")
    original = _extract_original_output(source_span)

    started_at = time.monotonic()
    outcome = await _execute_span_internal(
        span_id,
        source_db=source_db,
        sink_db=sink_db,
        overrides=overrides,
    )
    duration_seconds = time.monotonic() - started_at

    replay_spans: list[SpanRecord] = []
    root_id = outcome.context.root_deployment_id
    if sink_db is not None and isinstance(sink_db, DatabaseReader) and root_id is not None:
        replay_spans = await sink_db.get_deployment_tree(root_id)

    return ExperimentResult(
        source_span_id=span_id,
        replay_run_id=outcome.context.run_id,
        replay_root_span_id=outcome.context.replay_root_span_id or _root_span_id(replay_spans, outcome.context.run_id),
        original=original,
        result=outcome.result,
        duration_seconds=duration_seconds,
        cost_usd=_tree_cost(replay_spans) if replay_spans else 0.0,
        model_used=_model_used(outcome.result, original, overrides),
        tokens=_tree_token_summary(replay_spans),
        recording_degraded=outcome.context.recording_degraded,
    )


async def experiment_batch(
    span_ids: Sequence[UUID],
    source_db: DatabaseReader,
    *,
    overrides: ExperimentOverrides | None = None,
    concurrency: int = 5,
    sink_db: DatabaseWriter | None = None,
) -> list[ExperimentResult]:
    if concurrency < 1:
        raise ValueError("experiment_batch concurrency must be at least 1.")

    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(span_id: UUID) -> ExperimentResult:
        async with semaphore:
            return await experiment_span(
                span_id,
                source_db=source_db,
                sink_db=sink_db,
                overrides=overrides,
            )

    results = await safe_gather_indexed(
        *(_run_one(span_id) for span_id in span_ids),
        label="experiment_batch",
        raise_if_all_fail=True,
    )
    return [result for result in results if result is not None]


async def find_experiment_span_ids(
    database: DatabaseReader,
    deployment_id: UUID,
    *,
    kind: str | None = None,
    purpose: str | None = None,
    task_class: str | None = None,
) -> list[UUID]:
    spans = await database.get_deployment_tree(deployment_id)
    matched: list[UUID] = []
    for span in sorted(spans, key=lambda item: (item.started_at, item.sequence_no, str(item.span_id))):
        if span.kind == SpanKind.ATTEMPT:
            continue  # structural grouping, not directly replayable
        if kind is not None and span.kind != kind:
            continue
        meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json")
        if purpose is not None and meta.get("purpose") != purpose:
            continue
        if task_class is not None:
            target_task_class = _target_task_class(span.target)
            if target_task_class != task_class:
                continue
        matched.append(span.span_id)
    return matched
```

## Examples

**Main show from db displays meta and metrics** (`tests/replay/test_cli_usage.py:42`)

```python
def test_main_show_from_db_displays_meta_and_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _database, span = _conversation_span(tmp_path)

    exit_code = main(["show", "--from-db", str(span.span_id), "--db-path", str(tmp_path / "bundle")])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "meta_json" in output
    assert "metrics_json" in output
    assert "gemini-3-flash" in output
```

**Main run from file writes output** (`tests/replay/test_cli_usage.py:55`)

```python
class _MockResult:
    def __init__(self, content: str = "LLM response") -> None:
        self.content = content
        self.usage = SimpleNamespace(total_tokens=150, model_dump=lambda: {"total_tokens": 150})
        self.cost = 0.003


def test_main_run_from_file_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _database, span = _conversation_span(tmp_path)
    replay_file = tmp_path / "bundle" / "spans" / f"{span.span_id}.json"

    async def fake_execute(span_id: Any, *, source_db: Any, sink_db: Any = None) -> _MockResult:
        _ = (span_id, source_db, sink_db)
        return _MockResult()

    monkeypatch.setattr("ai_pipeline_core.replay.cli.execute_span", fake_execute)

    exit_code = main(["run", str(replay_file), "--db-path", str(tmp_path / "bundle")])

    assert exit_code == 0
    output_dir = replay_file.parent / f"{replay_file.stem}_replay"
    assert (output_dir / "output.yaml").exists()
```

**Main run with overrides uses experiment span** (`tests/replay/test_cli_usage.py:73`)

```python
class _MockResult:
    def __init__(self, content: str = "LLM response") -> None:
        self.content = content
        self.usage = SimpleNamespace(total_tokens=150, model_dump=lambda: {"total_tokens": 150})
        self.cost = 0.003


def test_main_run_with_overrides_uses_experiment_span(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _database, span = _conversation_span(tmp_path)
    replay_file = tmp_path / "bundle" / "spans" / f"{span.span_id}.json"

    async def fail_execute(span_id: Any, *, source_db: Any, sink_db: Any = None) -> _MockResult:
        _ = (span_id, source_db, sink_db)
        raise AssertionError("run with overrides should use experiment_span")

    async def fake_experiment_span(
        span_id: Any,
        *,
        source_db: Any,
        sink_db: Any = None,
        overrides: Any = None,
    ) -> Any:
        _ = (source_db, sink_db)
        assert span_id == span.span_id
        assert overrides is not None
        assert overrides.model == "gemini-3-flash"
        assert overrides.model_options == {"reasoning_effort": "low"}
        return SimpleNamespace(result=_MockResult("override response"))

    monkeypatch.setattr("ai_pipeline_core.replay.cli.execute_span", fail_execute)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_span", fake_experiment_span)

    exit_code = main([
        "run",
        str(replay_file),
        "--db-path",
        str(tmp_path / "bundle"),
        "--model",
        "gemini-3-flash",
        "--set",
        "reasoning_effort=low",
    ])

    assert exit_code == 0
    output_dir = replay_file.parent / f"{replay_file.stem}_replay"
    assert (output_dir / "output.yaml").exists()
```

**Main batch uses find and experiment helpers** (`tests/replay/test_cli_usage.py:115`)

```python
def test_main_batch_uses_find_and_experiment_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _database, span = _conversation_span(tmp_path)
    deployment_id = span.root_deployment_id

    async def fake_find(
        database: Any,
        deployment_id_arg: Any,
        *,
        kind: Any = None,
        purpose: Any = None,
        task_class: Any = None,
    ) -> list[Any]:
        _ = database
        assert deployment_id_arg == deployment_id
        assert kind == "conversation"
        assert purpose == "summary"
        assert task_class is None
        return [span.span_id]

    async def fake_batch(
        span_ids: Any,
        source_db: Any,
        *,
        overrides: Any = None,
        concurrency: int = 5,
        sink_db: Any = None,
    ) -> list[Any]:
        _ = (source_db, sink_db)
        assert span_ids == [span.span_id]
        assert overrides is not None
        assert overrides.model == "gemini-3-flash"
        assert overrides.model_options == {"reasoning_effort": "low"}
        assert concurrency == 3
        return [
            SimpleNamespace(
                source_span_id=span.span_id,
                model_used="gemini-3-flash",
                duration_seconds=0.5,
                cost_usd=0.0,
            )
        ]

    monkeypatch.setattr("ai_pipeline_core.replay.cli.find_experiment_span_ids", fake_find)
    monkeypatch.setattr("ai_pipeline_core.replay.cli.experiment_batch", fake_batch)

    exit_code = main([
        "batch",
        "--from-deployment",
        str(deployment_id),
        "--db-path",
        str(tmp_path / "bundle"),
        "--kind",
        "conversation",
        "--purpose",
        "summary",
        "--model",
        "gemini-3-flash",
        "--set",
        "reasoning_effort=low",
        "--concurrency",
        "3",
    ])

    assert exit_code == 0
    assert "Ran 1 replay experiments" in capsys.readouterr().out
```

**Execute span installs replay execution context** (`tests/replay/test_execute_span.py:121`)

```python
@pytest.mark.asyncio
async def test_execute_span_installs_replay_execution_context(memory_database) -> None:
    span = make_span(
        kind="task",
        name="function",
        target=f"function:{__name__}:execute_function",
        input_value={"value": "context"},
    )
    await memory_database.insert_span(span)

    result = await execute_span(span.span_id, source_db=memory_database)

    assert result == "function:context"
    assert _SEEN_CONTEXT["run_id"].startswith(f"replay-{str(span.span_id)[:8]}-")
    assert _SEEN_CONTEXT["publisher_type"] is _NoopPublisher
    assert _SEEN_CONTEXT["disable_cache"] is True
```

**Execute span copies input artifacts when source and sink differ** (`tests/replay/test_replay_portability.py:17`)

```python
@pytest.mark.asyncio
async def test_execute_span_copies_input_artifacts_when_source_and_sink_differ(
    memory_database,
    sample_text_doc: ReplayTextDocument,
) -> None:
    sink_database = type(memory_database)()
    await store_document_in_database(memory_database, sample_text_doc)
    payload = b"binary-payload"
    payload_sha = compute_content_sha256(payload)
    await memory_database.save_blob(_BlobRecord(content_sha256=payload_sha, content=payload))

    span = make_span(
        kind="task",
        name="portable",
        target=f"function:{__name__}:portability_function",
        input_value={"document": sample_text_doc, "payload": payload},
    )
    await memory_database.insert_span(span)

    result = await execute_span(
        span.span_id,
        source_db=memory_database,
        sink_db=sink_database,
    )

    assert result == f"{sample_text_doc.name}:{len(payload)}"
    assert await sink_database.get_document(sample_text_doc.sha256) is not None
    assert await sink_database.get_blob(payload_sha) is not None
```


## Error Examples

**Replay tool conversation without override tools raises** (`tests/replay/test_bugs_replay.py:152`)

```python
def test_replay_tool_conversation_without_override_tools_raises() -> None:
    """Replaying recorded conversation that used tools without override_tools raises TypeError."""
    recorded_tools = [{"name": "search_tool", "class_path": "tests.replay.test_bugs_replay:SearchTool"}]
    arguments: dict[str, Any] = {"tools": recorded_tools, "content": "test"}

    with pytest.raises(TypeError, match="override_tools"):
        _apply_overrides(receiver=None, arguments=arguments, overrides=None)
```

**Replay tool conversation without override tools with overrides raises** (`tests/replay/test_bugs_replay.py:161`)

```python
def test_replay_tool_conversation_without_override_tools_with_overrides_raises() -> None:
    """Even with model overrides, missing override_tools raises TypeError."""
    recorded_tools = [{"name": "search_tool", "class_path": "tests.replay.test_bugs_replay:SearchTool"}]
    arguments: dict[str, Any] = {"tools": recorded_tools, "content": "test"}
    overrides = FakeOverrides(model="new-model")

    with pytest.raises(TypeError, match="override_tools"):
        _apply_overrides(receiver=None, arguments=arguments, overrides=overrides)
```
