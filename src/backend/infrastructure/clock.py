"""\file clock.py
\brief System clock infrastructure adapter.
"""

from datetime import datetime, timezone


class SystemClock:
    """\class SystemClock
    \brief Provide current UTC timestamps to application services.
    """

    def now_iso(self):
        """\brief Return the current UTC timestamp in storage format.
        \return ISO-8601 timestamp ending in ``Z``.
        """
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
