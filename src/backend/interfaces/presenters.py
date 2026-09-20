"""Protocol presenters for HTTP responses."""

import json


class JsonPresenter:
    content_type = "application/json; charset=utf-8"

    def render(self, value):
        return json.dumps(value).encode("utf-8")


class SsePresenter:
    content_type = "text/event-stream"

    def render(self, event_type, message):
        return ("event: %s\ndata: %s\n\n" % (event_type, json.dumps(message))).encode("utf-8")
