"""Regression test: CLI classes must not be function-local (breaks codec)."""


def test_cli_options_class_is_not_function_local() -> None:
    """_CliOptions defined inside run_cli_for_deployment() has <locals> in __qualname__,
    which the codec guard now rejects at encode time. The class should be at module scope
    or projected into a stable type before reaching the codec.

    This test proves the root cause still exists even though the codec guard catches it.
    """
    import ast
    from pathlib import Path

    cli_path = Path(__file__).resolve().parents[3] / "ai_pipeline_core" / "deployment" / "_cli.py"
    source = cli_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(cli_path))

    function_local_classes: list[str] = [
        f"{node.name}.<locals>.{child.name}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.walk(node)
        if isinstance(child, ast.ClassDef)
    ]

    assert not function_local_classes, (
        f"Classes defined inside functions will have <locals> in __qualname__ and fail codec encoding: "
        f"{function_local_classes}. Move them to module scope or project into deployment.options_type."
    )
