"""\file event_hub.py
\brief In-process event publisher used by the SSE interface.
"""

import queue
import threading


class SseEventPublisher:
    """\class SseEventPublisher
    \brief Fan out dashboard events to connected subscribers.
    """

    def __init__(self):
        """\brief Create an empty subscriber registry."""
        self._lock = threading.Lock()
        self._subscriptions = set()

    def subscribe(self):
        """\brief Register a subscriber queue.
        \return Queue receiving published event dictionaries.
        """
        subscription = queue.Queue(maxsize=50)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription):
        """\brief Remove a subscriber.
        \param subscription Queue previously returned by ``subscribe``.
        """
        with self._lock:
            self._subscriptions.discard(subscription)

    def publish(self, message):
        """\brief Publish an event without blocking slow subscribers.
        \param message Event dictionary.
        """
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            try:
                subscription.put_nowait(message)
            except queue.Full:
                pass
