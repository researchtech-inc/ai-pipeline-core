"""Shared lane policy for the dev CLI and pytest hooks."""

from pathlib import Path

LANES = ("unit", "integration", "qualification")

LANE_PATHS: dict[str, tuple[str, ...]] = {
    "unit": ("tests/unit",),
    "integration": ("tests/integration",),
    "qualification": ("tests/qualification",),
}

LANE_XDIST_ARGS: dict[str, tuple[str, ...]] = {
    "unit": ("-n", "auto", "--dist", "worksteal"),
    "integration": ("-n", "4", "--dist", "loadfile"),
    "qualification": ("-n", "2"),
}

LANE_TIMEOUTS: dict[str, int] = {
    "unit": 30,
    "integration": 180,
    "qualification": 900,
    "capabilities": 900,
}

TESTMON_LANES = frozenset({"unit", "integration"})

EXAMPLE_PATHS: dict[str, str] = {
    "static": "examples/tests/test_static.py",
    "smoke": "examples/tests/test_smoke.py",
    "live": "examples/tests/test_live.py",
}


def lane_from_path(path: str | Path) -> str | None:
    """Return the lane encoded by a repository-relative path."""
    path_text = Path(path).as_posix()
    for lane, roots in LANE_PATHS.items():
        if any(path_text == root or path_text.startswith(f"{root}/") for root in roots):
            return lane
    if path_text.startswith("examples/tests/"):
        return "unit"
    if path_text.startswith("benchmarks/llm_matrix/"):
        return "benchmark"
    if path_text == "tests/capabilities" or path_text.startswith("tests/capabilities/"):
        return "capabilities"
    return None
