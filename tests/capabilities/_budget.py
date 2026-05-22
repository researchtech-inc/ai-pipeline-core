"""Mid-run cost budget used by the capability suite.

Lives in a sibling module (not the conftest) so unit tests can import the
dataclass without triggering pytest's conftest collection machinery.
"""

from dataclasses import dataclass


@dataclass(slots=True)
class CapabilityCostBudget:
    """Cost limit for one capability suite run; separate from the integration budget.

    ``add`` accumulates a probe cost and raises immediately when the running total
    exceeds ``limit_usd``. Mid-run aborts prevent a too-low limit from quietly
    funding every remaining probe before a teardown-only check could fire.
    """

    limit_usd: float
    total_usd: float = 0.0

    def add(self, cost: float | None) -> None:
        """Accumulate a probe cost; raise immediately when the running total exceeds the limit."""
        if cost is None or cost < 0:
            return
        self.total_usd += float(cost)
        if self.total_usd > self.limit_usd:
            raise RuntimeError(
                f"Capability suite cost ${self.total_usd:.4f} exceeded "
                f"budget ${self.limit_usd:.2f}; aborting remaining probes. "
                "Raise the limit with `dev probe capabilities -- --cost-limit <usd>` "
                "or shrink the probe set."
            )
