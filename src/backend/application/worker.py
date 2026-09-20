"""\file worker.py
\brief Background processing for queued ingest commands.
"""

import sys
import threading


class RunWorker:
    """\class RunWorker
    \brief Consume commands, persist runs, and publish live events.
    """

    def __init__(self, queue, runs, events):
        """\brief Construct a worker with injected runtime ports.
        \param queue Run queue.
        \param runs Run repository.
        \param events Event publisher.
        """
        self.queue = queue
        self.runs = runs
        self.events = events

    def process(self, command):
        """\brief Process one command synchronously.
        \param command Validated ingest command.
        \return Assigned run identifier.
        """
        run_id = self.runs.add_run(command)
        self.events.publish({"type": "run", "run_id": run_id})
        print("[ingest] run #%d  %s  %d results" % (run_id, command.commit, len(command.results)))
        return run_id

    def run_forever(self):
        """\brief Consume commands until the process exits."""
        while True:
            command = self.queue.get()
            try:
                self.process(command)
            except Exception as exc:
                print("[ingest] FAILED: %s" % exc, file=sys.stderr)
            finally:
                self.queue.task_done()

    def start(self):
        """\brief Start the daemon worker thread.
        \return The started thread.
        """
        thread = threading.Thread(target=self.run_forever, daemon=True, name="ingest-worker")
        thread.start()
        return thread
