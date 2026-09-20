"""Ingest use case."""

from ..domain.models import EnqueueResult


class IngestService:
    def __init__(self, validator, checks, queue):
        self.validator = validator
        self.checks = checks
        self.queue = queue

    def ingest(self, payload):
        command = self.validator.validate(payload, self.checks.check_kinds())
        self.queue.put(command)
        return EnqueueResult(command.workflow, command.commit, self.queue.qsize())
