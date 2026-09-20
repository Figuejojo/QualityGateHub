"""Background run processing."""

import sys
import threading


class RunWorker:
    def __init__(self, queue, runs, events):
        self.queue = queue
        self.runs = runs
        self.events = events

    def process(self, command):
        run_id = self.runs.add_run(command)
        self.events.publish({"type": "run", "run_id": run_id})
        print("[ingest] run #%d  %s  %d results" % (run_id, command.commit, len(command.results)))
        return run_id

    def run_forever(self):
        while True:
            command = self.queue.get()
            try:
                self.process(command)
            except Exception as exc:
                print("[ingest] FAILED: %s" % exc, file=sys.stderr)
            finally:
                self.queue.task_done()

    def start(self):
        thread = threading.Thread(target=self.run_forever, daemon=True, name="ingest-worker")
        thread.start()
        return thread
