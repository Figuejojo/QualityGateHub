"""Demo run generation use case."""

import random

from ..domain.models import EnqueueResult
from ..domain.validators import DEFAULT_WORKFLOW, PayloadValidator


class SimulationService:
    def __init__(self, runs, checks, queue, validator=None):
        self.runs = runs
        self.checks = checks
        self.queue = queue
        self.validator = validator or PayloadValidator()

    def simulate(self, workflow=DEFAULT_WORKFLOW):
        payload = self._payload(workflow)
        command = self.validator.validate(payload, self.checks.check_kinds())
        self.queue.put(command)
        return EnqueueResult(command.workflow, command.commit, self.queue.qsize())

    def _payload(self, workflow):
        previous = self.runs.last_values(workflow)
        static_analysis = previous.get("static_analysis", 42.0)
        coverage = previous.get("coverage", 71.0)
        static_analysis = max(0, static_analysis + random.choices([-3, -2, -1, 0, 1, 2, 4, 8], [1, 2, 3, 5, 3, 2, 1, 1])[0])
        coverage = min(98.0, max(40.0, round(coverage + random.choice([-2.5, -1.0, -0.4, 0, 0, 0.1, 0.2, 0.5, 1.2, 2.4]), 1)))
        build_ok = random.random() > 0.08
        test_ok = random.random() > 0.12
        results = [{"check": "build", "status": "pass" if build_ok else "fail"}]
        if build_ok:
            results.extend([
                {"check": "unit_test", "status": "pass" if test_ok else "fail"},
                {"check": "static_analysis", "value": static_analysis},
                {"check": "coverage", "value": coverage},
            ])
        else:
            results.append({"check": "unit_test", "status": "skipped"})
        return {
            "workflow": workflow,
            "commit": "%07x" % random.getrandbits(28),
            "branch": random.choice(["develop", "develop", "develop", "feature/uart-dma", "fix/adc-offset"]),
            "results": results,
        }
