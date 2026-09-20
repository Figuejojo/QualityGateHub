"""\file models.py
\brief Domain entities and immutable value objects.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


PASS_FAIL = "pass_fail"
TREND = "trend"
POLARITIES = ("higher_is_better", "lower_is_better")


@dataclass(frozen=True)
class CheckDefinition:
    """\class CheckDefinition
    \brief Configuration and display metadata for one quality check.
    """
    name: str
    label: str
    kind: str
    polarity: str = "higher_is_better"
    unit: str = ""
    tolerance: float = 0.0
    fail_delta: float = 0.0
    position: int = 0


@dataclass(frozen=True)
class TrendVerdict:
    """\class TrendVerdict
    \brief Direction and severity calculated for a trend result.
    """
    direction: Optional[str]
    severity: str


@dataclass
class CheckResult:
    """\class CheckResult
    \brief Observed result for one check in a run.
    """
    check_name: str
    status: Optional[str] = None
    value: Optional[float] = None
    baseline: Optional[float] = None
    delta: Optional[float] = None
    direction: Optional[str] = None
    severity: str = "none"


@dataclass
class Run:
    """\class Run
    \brief A workflow execution containing check results.
    """
    workflow: str
    commit: str
    branch: str
    run_ref: str
    timestamp: str
    results: List[CheckResult] = field(default_factory=list)
    id: Optional[int] = None
    received_at: Optional[str] = None


@dataclass(frozen=True)
class IngestCommand:
    """\class IngestCommand
    \brief Validated, immutable command placed on the run queue.
    """
    workflow: str
    commit: str
    branch: str
    run_ref: str
    timestamp: str
    results: tuple


@dataclass(frozen=True)
class EnqueueResult:
    """\class EnqueueResult
    \brief Public result returned after a run is queued.
    """
    workflow: str
    commit: str
    queue_depth: int


@dataclass(frozen=True)
class DashboardView:
    """\class DashboardView
    \brief Data required to render a dashboard workflow.
    """
    checks: list
    runs: list
    total_runs: int
    slots: int
    workflow: str
    workflows: list
