"""\file presenters.py
\brief JSON and Server-Sent Events response presenters.
"""

import json


class JsonPresenter:
    """\class JsonPresenter
    \brief Serialize JSON-compatible values for HTTP responses.
    """
    content_type = "application/json; charset=utf-8"

    def render(self, value):
        """\brief Encode a value as UTF-8 JSON.
        \param value JSON-compatible value.
        \return Encoded JSON bytes.
        """
        return json.dumps(value).encode("utf-8")


class SsePresenter:
    """\class SsePresenter
    \brief Format event dictionaries using the SSE wire protocol.
    """
    content_type = "text/event-stream"

    def render(self, event_type, message):
        """\brief Render one named SSE event.
        \param event_type Event name.
        \param message JSON-compatible event payload.
        \return Encoded SSE message bytes.
        """
        return ("event: %s\ndata: %s\n\n" % (event_type, json.dumps(message))).encode("utf-8")
