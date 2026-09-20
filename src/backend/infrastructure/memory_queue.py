"""In-process queue adapter."""

import queue


class MemoryRunQueue:
    def __init__(self):
        self._queue = queue.Queue()

    def put(self, command):
        self._queue.put(command)

    def get(self):
        return self._queue.get()

    def task_done(self):
        self._queue.task_done()

    def qsize(self):
        return self._queue.qsize()
