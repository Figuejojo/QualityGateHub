"""Dashboard query use case."""


class DashboardService:
    def __init__(self, checks, runs):
        self.checks = checks
        self.runs = runs

    def get_dashboard(self, limit, workflow):
        return self.runs.dashboard(limit, workflow)

    def count_runs(self):
        return self.runs.count_runs()
