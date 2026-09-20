"""\file dashboard_service.py
\brief Application service for dashboard queries.
"""


class DashboardService:
    """\class DashboardService
    \brief Coordinate read-only dashboard queries.
    """

    def __init__(self, checks, runs):
        """\brief Construct the service with repository dependencies.
        \param checks Check repository.
        \param runs Run repository.
        """
        self.checks = checks
        self.runs = runs

    def get_dashboard(self, limit, workflow):
        """\brief Return dashboard data for a workflow.
        \param limit Maximum number of runs.
        \param workflow Workflow name.
        \return Dashboard view data.
        """
        return self.runs.dashboard(limit, workflow)

    def count_runs(self):
        """\brief Return the number of stored runs.
        \return Stored run count.
        """
        return self.runs.count_runs()
