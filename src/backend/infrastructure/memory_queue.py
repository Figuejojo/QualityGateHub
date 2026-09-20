"""\file memory_queue.py
\brief Thread-safe in-process queue adapter.
"""

import queue


class MemoryRunQueue:
    """\class MemoryRunQueue
    \brief Adapt ``queue.Queue`` to the run queue port.
    """

    def __init__(self):
        """\brief Create an empty queue."""
        self._queue = queue.Queue()

    def put(self, command):
        """\brief Add a validated command to the queue.
        \param command Validated ingest command.
        """
        self._queue.put(command)

    def get(self):
        """\brief Block until a command is available.
        \return Next queued command.
        """
        return self._queue.get()

    def task_done(self):
        """\brief Mark the current command as processed."""
        self._queue.task_done()

    def qsize(self):
        """\brief Return the approximate number of queued commands.
        \return Queue depth.
        """
        return self._queue.qsize()
