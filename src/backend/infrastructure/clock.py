"""System time adapter."""

from datetime import datetime, timezone


class SystemClock:
    def now_iso(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
