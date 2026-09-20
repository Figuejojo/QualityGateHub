"""\file ports.py
\brief Protocol interfaces consumed by application services.
"""

from typing import Protocol


class CheckRepository(Protocol):
    """\class CheckRepository
    \brief Persistence contract for check definitions.
    """
    def check_kinds(self):
        """\brief Return registered check names and kinds."""
        ...

    def get(self, name):
        """\brief Retrieve a check by name."""
        ...

    def upsert(self, body):
        """\brief Create or update a check definition."""
        ...

    def list_checks(self):
        """\brief List registered checks in display order."""
        ...


class RunRepository(Protocol):
    """\class RunRepository
    \brief Persistence contract for workflow runs and dashboard queries.
    """
    def add_run(self, command):
        """\brief Persist one validated run command."""
        ...

    def dashboard(self, limit, workflow):
        """\brief Query dashboard data for a workflow."""
        ...

    def last_values(self, workflow):
        """\brief Return latest trend values for a workflow."""
        ...

    def count_runs(self):
        """\brief Count stored runs."""
        ...


class RunQueue(Protocol):
    """\class RunQueue
    \brief Queue contract for validated ingest commands.
    """
    def put(self, command):
        """\brief Enqueue a validated command."""
        ...

    def get(self):
        """\brief Dequeue the next command."""
        ...

    def task_done(self):
        """\brief Mark the current command complete."""
        ...

    def qsize(self):
        """\brief Return queue depth."""
        ...


class EventPublisher(Protocol):
    """\class EventPublisher
    \brief Publish and subscribe contract for live dashboard events.
    """
    def publish(self, message):
        """\brief Publish an event message."""
        ...

    def subscribe(self):
        """\brief Register an event subscriber."""
        ...

    def unsubscribe(self, subscription):
        """\brief Remove an event subscriber."""
        ...


class Clock(Protocol):
    """\class Clock
    \brief Clock contract used to make timestamps replaceable in tests.
    """
    def now_iso(self):
        """\brief Return the current UTC timestamp."""
        ...
