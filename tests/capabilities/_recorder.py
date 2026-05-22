"""Capability matrix recorder + artifact writer."""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Status = Literal["PASS", "FAIL", "ERROR", "TERMINAL"]


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """One row in the capability matrix.

    ``declared`` is ``None`` for the text baseline (no flag gates text).
    ``probed_supported`` is True iff the probe achieved its invariant.
    ``match`` is True when ``declared`` was ``None`` or matched ``probed_supported``.
    """

    model: str
    capability: str
    declared: bool | None
    probed_supported: bool
    match: bool
    status: Status
    detail: str
    cost_usd: float
    elapsed_s: float
    deployment_id: str | None


@dataclass(slots=True)
class CapabilityMatrix:
    """Mutable in-memory aggregator that flushes JSON + Markdown at session end."""

    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    results: list[CapabilityResult] = field(default_factory=list)

    def record(self, result: CapabilityResult) -> None:
        """Append one probe result to the matrix."""
        self.results.append(result)

    def to_json_payload(self) -> dict[str, object]:
        """Return a JSON-serializable dict view of the matrix."""
        return {
            "started_at": self.started_at,
            "results": [asdict(result) for result in self.results],
        }

    def write_json(self, path: Path) -> None:
        """Write the matrix as JSON, creating parent directories as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json_payload(), indent=2, sort_keys=True))

    def to_markdown(self) -> str:
        """Render the matrix as a Markdown table."""
        lines = [
            "# Capability Matrix",
            "",
            f"Started: {self.started_at}",
            "",
            "| Model | Capability | Declared | Probed | Match | Status | Cost (USD) | Elapsed (s) | Detail |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        if not self.results:
            lines.append("| _(empty)_ |  |  |  |  |  |  |  |  |")
        for result in self.results:
            declared = "—" if result.declared is None else str(result.declared)
            match_cell = "—" if result.declared is None else ("✓" if result.match else "✗")
            detail = (result.detail or "").replace("|", "\\|").replace("\n", " ")[:160]
            lines.append(
                f"| {result.model} | {result.capability} | {declared} | "
                f"{result.probed_supported} | {match_cell} | {result.status} | "
                f"{result.cost_usd:.4f} | {result.elapsed_s:.2f} | {detail} |"
            )
        return "\n".join(lines) + "\n"

    def write_markdown(self, path: Path) -> None:
        """Write the Markdown view to disk, creating parent directories as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown())

    def per_model_summary(self) -> dict[str, dict[str, int]]:
        """Aggregate PASS / total / divergence counts per model."""
        summary: dict[str, dict[str, int]] = {}
        for result in self.results:
            bucket = summary.setdefault(result.model, {"pass": 0, "total": 0, "divergences": 0})
            bucket["total"] += 1
            if result.status == "PASS":
                bucket["pass"] += 1
            if result.declared is not None and not result.match:
                bucket["divergences"] += 1
        return summary
