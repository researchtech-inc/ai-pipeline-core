"""Unified pipeline run response contract.

Single source of truth for the response shape shared with unified-middleware.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Discriminator


class RunState(StrEnum):
    """Pipeline run lifecycle state."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CRASHED = "CRASHED"
    CANCELLED = "CANCELLED"


class FlowStatus(StrEnum):
    """Individual flow step status within a pipeline run."""

    STARTED = "started"
    COMPLETED = "completed"
    CACHED = "cached"
    PROGRESS = "progress"


class _RunBase(BaseModel):
    """Common fields on every run response variant."""

    flow_run_id: UUID
    run_id: str
    state: RunState
    timestamp: datetime

    model_config = ConfigDict(frozen=True)


class PendingRun(_RunBase):
    """Pipeline queued or running but no progress reported yet."""

    type: Literal["pending"] = "pending"


class ProgressRun(_RunBase):
    """Pipeline running with step-level progress data."""

    type: Literal["progress"] = "progress"
    step: int
    total_steps: int
    flow_name: str
    status: FlowStatus
    progress: float  # overall 0.0-1.0
    step_progress: float  # within step 0.0-1.0
    message: str


class DeploymentResultData(BaseModel):
    """Typed result payload — always has success + optional error."""

    success: bool
    error: str | None = None

    model_config = ConfigDict(frozen=True, extra="allow")


class CompletedRun(_RunBase):
    """Pipeline finished (Prefect COMPLETED). Check result.success for business outcome."""

    type: Literal["completed"] = "completed"
    result: DeploymentResultData | None


class FailedRun(_RunBase):
    """Pipeline crashed — execution error, not business logic."""

    type: Literal["failed"] = "failed"
    error: str
    result: DeploymentResultData | None = None


RunResponse = Annotated[
    PendingRun | ProgressRun | CompletedRun | FailedRun,
    Discriminator("type"),
]

__all__ = [
    "CompletedRun",
    "DeploymentResultData",
    "FailedRun",
    "FlowStatus",
    "PendingRun",
    "ProgressRun",
    "RunResponse",
    "RunState",
]
