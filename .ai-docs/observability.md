# MODULE: observability
# PURPOSE: Observability system for AI pipelines.
# VERSION: 0.21.1
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Functions

```python
def main(argv: list[str] | None = None) -> int:
    """Run the ai-trace CLI."""
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
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1
```

## Examples

**Main download command writes span summary artifacts** (`tests/observability/test_trace_cli_spans.py:251`)

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

**Main show command reads span snapshot** (`tests/observability/test_trace_cli_spans.py:239`)

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

**No laminar span calls remain** (`tests/observability/test_laminar_sink.py:407`)

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

    assert len(sinks) == 1
    assert isinstance(sinks[0], LaminarSpanSink)
```

**Db path returns span filesystem database** (`tests/observability/test_trace_cli_spans.py:203`)

```python
def test_db_path_returns_span_filesystem_database(self, tmp_path: Path) -> None:
    FilesystemDatabase(tmp_path)
    args = type("Args", (), {"db_path": str(tmp_path)})()

    database = _resolve_connection(args)

    assert isinstance(database, FilesystemDatabase)
```

**Different key after failure uses error level** (`tests/observability/test_laminar_log_levels.py:28`)

```python
def test_different_key_after_failure_uses_error_level(self) -> None:
    source = inspect.getsource(LaminarSpanSink._initialization_state_allows_export)
    assert "logger.error" in source
```

**Initialization failure uses error level** (`tests/observability/test_laminar_log_levels.py:18`)

```python
def test_initialization_failure_uses_error_level(self) -> None:
    source = inspect.getsource(LaminarSpanSink._initialize_laminar)
    assert "logger.error" in source
    assert "logger.warning" not in source
```

**Project switch uses error level** (`tests/observability/test_laminar_log_levels.py:23`)

```python
def test_project_switch_uses_error_level(self) -> None:
    source = inspect.getsource(LaminarSpanSink._warn_project_switch)
    assert "logger.error" in source
    assert "logger.warning" not in source
```
