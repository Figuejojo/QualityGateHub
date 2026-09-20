"""In-process event publisher used by the SSE interface."""

import queue
import threading


class SseEventPublisher:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscriptions = set()

    def subscribe(self):
        subscription = queue.Queue(maxsize=50)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription):
        with self._lock:
            self._subscriptions.discard(subscription)

    def publish(self, message):
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            try:
                subscription.put_nowait(message)
            except queue.Full:
                pass
