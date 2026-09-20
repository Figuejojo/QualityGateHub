"""Check configuration use case."""

import math
import re

from ..domain.models import POLARITIES


CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


class CheckService:
    def __init__(self, checks):
        self.checks = checks

    def upsert(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        name = payload.get("name")
        if not isinstance(name, str) or not CHECK_NAME_RE.match(name):
            raise ValueError("'name' must be 1-40 chars of letters, digits, _ . -")
        if "polarity" in payload and payload["polarity"] not in POLARITIES:
            raise ValueError("'polarity' must be higher_is_better or lower_is_better")
        if "kind" in payload and payload["kind"] not in ("pass_fail", "trend"):
            raise ValueError("'kind' must be pass_fail or trend")
        for field in ("tolerance", "fail_delta", "position"):
            if field in payload:
                value = payload[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("'%s' must be a finite number" % field)
                if field != "position" and value < 0:
                    raise ValueError("'%s' must be >= 0" % field)
        return self.checks.upsert(payload)
