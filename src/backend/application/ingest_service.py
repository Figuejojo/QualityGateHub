"""\file ingest_service.py
\brief Application service for accepting pipeline run payloads.
"""

from ..domain.models import EnqueueResult


class IngestService:
    """\class IngestService
    \brief Validate incoming data and enqueue it for asynchronous persistence.
    """

    def __init__(self, validator, checks, queue):
        """\brief Construct the ingest use case with injected ports.
        \param validator Payload validation service.
        \param checks Check repository used to determine known check kinds.
        \param queue Run queue receiving validated commands.
        """
        self.validator = validator
        self.checks = checks
        self.queue = queue

    def ingest(self, payload):
        """\brief Validate and enqueue a raw ingest payload.
        \param payload JSON-compatible pipeline payload.
        \return EnqueueResult describing the accepted command.
        	hrows ValueError If validation fails.
        """
        command = self.validator.validate(payload, self.checks.check_kinds())
        self.queue.put(command)
        return EnqueueResult(command.workflow, command.commit, self.queue.qsize())
