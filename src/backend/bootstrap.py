"""\file bootstrap.py
\brief Composition root that assembles and starts the application.
"""

import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from .application.check_service import CheckService
from .application.dashboard_service import DashboardService
from .application.ingest_service import IngestService
from .application.simulation_service import SimulationService
from .application.worker import RunWorker
from .config import parse_config
from .domain.validators import PayloadValidator
from .infrastructure.clock import SystemClock
from .infrastructure.event_hub import SseEventPublisher
from .infrastructure.memory_queue import MemoryRunQueue
from .infrastructure.sqlite_store import SLOTS, SqliteStore
from .interfaces.frontend import FrontendProvider
from .interfaces.http_server import handler_for


class Application:
    """\class Application
    \brief Construct the concrete adapters and application services.
    """

    def __init__(self, config):
        """\brief Assemble the application object graph.
        \param config Runtime configuration.
        """
        if config.reset and config.db != ":memory:" and os.path.exists(config.db):
            os.remove(config.db)
        self.clock = SystemClock()
        self.events = SseEventPublisher()
        self.queue = MemoryRunQueue()
        self.store = SqliteStore(config.db, max(config.keep, SLOTS), clock=self.clock)
        validator = PayloadValidator()
        self.ingest = IngestService(validator, self.store, self.queue)
        self.dashboard = DashboardService(self.store, self.store)
        self.checks = CheckService(self.store)
        self.simulation = SimulationService(self.store, self.store, self.queue, validator)
        self.frontend = FrontendProvider(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")))
        self.worker = RunWorker(self.queue, self.store, self.events)

    def seed(self, count=SLOTS):
        """\brief Seed an empty database with demo runs.
        \param count Number of demo runs to create.
        """
        now = datetime.now(timezone.utc)
        for index in range(count):
            timestamp = (now - timedelta(minutes=(count - index) * 23)).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = self.simulation._payload("Example")
            payload["timestamp"] = timestamp
            command = self.simulation.validator.validate(payload, self.store.check_kinds())
            self.store.add_run(command)

    def start(self):
        """\brief Start the asynchronous run worker."""
        self.worker.start()


def main(argv=None):
    """\brief Parse configuration, assemble services, and serve HTTP.
    \param argv Optional command-line arguments.
    """
    config = parse_config(argv)
    application = Application(config)
    if not config.no_seed and application.store.count_runs() == 0:
        application.seed()
    application.start()
    server = ThreadingHTTPServer((config.host, config.port), handler_for(application))
    server.daemon_threads = True
    shown = "localhost" if config.host in ("127.0.0.1", "0.0.0.0") else config.host
    print("Quality-gate dashboard")
    print("  dashboard : http://%s:%d/" % (shown, config.port))
    print("  ingest    : POST http://%s:%d/api/ingest" % (shown, config.port))
    print("  database  : %s   (%d pushes stored)" % (config.db, application.store.count_runs()))
    print("  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
