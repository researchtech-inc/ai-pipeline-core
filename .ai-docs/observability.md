# MODULE: observability
# PURPOSE: Observability system for AI pipelines.
# VERSION: 0.22.3
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Functions

```python
def main(argv: list[str] | None = None) -> int:
    """Run the ai-trace CLI."""
    setup_logging()
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db-path", type=str, default=None, help="Use a FilesystemDatabase snapshot instead of ClickHouse")

    parser = argparse.ArgumentParser(prog="ai-trace", description="Inspect deployment execution trees")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", parents=[db_parent], help="List recent deployments")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum number of deployments to show")
    list_parser.add_argument("--status", type=str, default=None, help="Filter deployments by status")

    show_parser = subparsers.add_parser("show", parents=[db_parent], help="Show deployment summary and logs")
    show_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    download_parser = subparsers.add_parser("download", parents=[db_parent], help="Download a deployment as a FilesystemDatabase snapshot")
    download_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")
    download_parser.add_argument("-o", "--output-dir", type=str, required=True, help="Output directory for the snapshot")

    llm_parser = subparsers.add_parser("llm", parents=[db_parent], help="Show all LLM calls for a deployment")
    llm_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    docs_parser = subparsers.add_parser("docs", parents=[db_parent], help="List all documents in a deployment")
    docs_parser.add_argument("identifier", help="Deployment/span UUID or deployment run_id")

    doc_parser = subparsers.add_parser("doc", parents=[db_parent], help="Show a single document by SHA256")
    doc_parser.add_argument("sha256", help="Document SHA256 identifier")

    recover_parser = subparsers.add_parser("recover", parents=[db_parent], help="Mark orphaned running deployments as failed")
    recover_parser.add_argument(
        "--i-accept-nondeterministic-reconciliation",
        action="store_true",
        default=False,
        help=("Explicitly bypass Prefect-state reconciliation and force nondeterministic wall-clock reconciliation after operator investigation."),
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    try:
        database = _resolve_connection(args)
        if args.command == "list":
            return asyncio.run(_list_deployments_async(database, args.limit, args.status))
        if args.command == "show":
            return asyncio.run(_show_deployment_async(database, args.identifier))
        if args.command == "download":
            return asyncio.run(_download_deployment_async(database, args.identifier, Path(args.output_dir).resolve()))
        if args.command == "llm":
            return asyncio.run(_show_llm_calls_async(database, args.identifier))
        if args.command == "docs":
            return asyncio.run(_show_docs_async(database, args.identifier))
        if args.command == "doc":
            return asyncio.run(_show_doc_async(database, args.sha256))
        if args.command == "recover":
            print(_RECOVER_SCOPE_WARNING)
            return asyncio.run(
                _recover_orphans_async(
                    database,
                    accept_nondeterministic_reconciliation=args.i_accept_nondeterministic_reconciliation,
                )
            )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1
```

## Examples

**Main download command writes span summary artifacts** (`tests/observability/test_trace_cli_spans.py:309`)

```python
def test_main_download_command_writes_span_summary_artifacts(
    self,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "download"
    asyncio.run(_seed_span_snapshot(source_dir))

    result = main(["download", "span-run", "--db-path", str(source_dir), "--output-dir", str(output_dir)])

    assert result == 0
    assert "Downloaded deployment" in capsys.readouterr().out
    assert (output_dir / "logs.jsonl").exists()
```

**Main show command reads span snapshot** (`tests/observability/test_trace_cli_spans.py:297`)

```python
def test_main_show_command_reads_span_snapshot(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(_seed_span_snapshot(tmp_path))

    result = main(["show", "span-run", "--db-path", str(tmp_path)])

    assert result == 0
    output = capsys.readouterr().out
    assert "Deployment span-cli-pipeline / span-run" in output
    assert "conversation: analyze_document" in output
    assert "task started" in output
    assert '"rounds": 1' in output
```

**No laminar span calls remain** (`tests/observability/test_laminar_sink.py:406`)

```python
def test_no_laminar_span_calls_remain() -> None:
    needle = "laminar" + "_span("
    hits: list[str] = []
    for root in (Path("ai_pipeline_core"), Path("tests")):
        for path in root.rglob("*.py"):
            if path.name == "test_laminar_sink.py":
                continue
            if needle in path.read_text(encoding="utf-8"):
                hits.append(str(path))

    assert hits == []
```

**Build runtime sinks includes laminar sink when key is set** (`tests/observability/test_laminar_sink.py:131`)

```python
def test_build_runtime_sinks_includes_laminar_sink_when_key_is_set() -> None:
    sinks = build_runtime_sinks(database=None, settings_obj=Settings(lmnr_project_api_key="secret"))

    assert any(isinstance(sink, LaminarSpanSink) for sink in sinks.span_sinks)
```


## Error Examples

**Kwarg alone does not bypass require client** (`tests/observability/test_recovery.py:318`)

```python
@pytest.mark.asyncio
async def test_kwarg_alone_does_not_bypass_require_client() -> None:
    """M1: Passing require_prefect_client=False alone is not enough when env var is default."""
    database = _MemoryDatabase()

    with pytest.raises(RuntimeError, match="refuses to run without a Prefect client"):
        await recover_orphaned_spans(database, prefect_client=None, require_prefect_client=False)
```

**Refuses to run without prefect client by default** (`tests/observability/test_recovery.py:122`)

```python
@pytest.mark.asyncio
async def test_refuses_to_run_without_prefect_client_by_default() -> None:
    database = _MemoryDatabase()

    with pytest.raises(RuntimeError, match="refuses to run without a Prefect client"):
        await recover_orphaned_spans(database, prefect_client=None)
```

**Recover command rejects deleted flags** (`tests/observability/test_trace_cli_spans.py:268`)

```python
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--without-prefect", None),
        ("--heartbeat-stale-seconds", "30"),
        ("--fallback-max-hours", "48"),
    ],
)
def test_recover_command_rejects_deleted_flags(
    self,
    tmp_path: Path,
    flag: str,
    value: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FilesystemDatabase(tmp_path)
    argv = ["recover", "--db-path", str(tmp_path), flag]
    if value is not None:
        argv.append(value)

    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    assert exc_info.value.code == 2
    assert flag in capsys.readouterr().err
```

**Recovery propagates typeerror not swallow** (`tests/observability/test_recovery.py:281`)

```python
@pytest.mark.asyncio
async def test_recovery_propagates_typeerror_not_swallow() -> None:
    database = _BrokenDatabase()
    root = await _seed_running_root(
        database,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        prefect_flow_run_id=uuid4(),
    )
    await _add_task_child(database, root=root, status=SpanStatus.RUNNING)

    with pytest.raises(TypeError, match="broken activity reader"):
        await recover_orphaned_spans(
            database,
            prefect_client=_FakePrefectClient(_prefect_flow_run(state_type="FAILED")),
        )
```
