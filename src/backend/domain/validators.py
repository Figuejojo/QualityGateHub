"""\file validators.py
\brief Validate and normalize payloads at the application boundary.
"""

import math
import re
from datetime import datetime, timezone

from .models import IngestCommand, PASS_FAIL, POLARITIES, TREND
from .policies import StatusPolicy


CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:/\-]{0,59}$")
DEFAULT_WORKFLOW = "Example"


def _number(value, field):
    """\brief Validate and convert a numeric input.
    \param value Candidate numeric value.
    \param field Field name used in validation errors.
    \return The value converted to ``float``.
    	hrows ValueError If the value is not finite numeric data.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("'%s' must be a finite number" % field)
    return float(value)


def utc_now_iso():
    """\brief Return the current UTC time in API ISO-8601 format.
    \return Timestamp ending in ``Z`` with millisecond precision.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_timestamp(value):
    """\brief Normalize an ISO-8601 or epoch timestamp.
    \param value Input timestamp, or ``None`` for the current time.
    \return UTC timestamp in the dashboard storage format.
    	hrows ValueError If the timestamp cannot be parsed.
    """
    if value is None:
        return utc_now_iso()
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        raise ValueError("'timestamp' must be ISO-8601 (e.g. 2026-09-19T14:03:22Z) or epoch seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PayloadValidator:
    """\class PayloadValidator
    \brief Convert raw JSON payloads into validated ingest commands.
    """

    def __init__(self, status_policy=None):
        """\brief Create a validator with an injectable status policy.
        \param status_policy Policy used to normalize status aliases.
        """
        self.status_policy = status_policy or StatusPolicy()

    def validate(self, obj, known_kinds):
        """\brief Validate and normalize one ingest payload.
        \param obj Raw JSON object from the HTTP boundary.
        \param known_kinds Mapping of registered check names to their kinds.
        \return Immutable ``IngestCommand`` for the queue.
        	hrows ValueError If the payload violates the API contract.
        """
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        results = obj.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError("'results' must be a non-empty list")
        if len(results) > 100:
            raise ValueError("'results' is limited to 100 entries")

        workflow = str(obj.get("workflow") or DEFAULT_WORKFLOW).strip()[:60]
        if not WORKFLOW_NAME_RE.match(workflow):
            raise ValueError("'workflow' must be 1-60 chars and start with a letter or number")
        normalized = []
        seen = set()
        for index, result in enumerate(results):
            where = "results[%d]" % index
            if not isinstance(result, dict):
                raise ValueError("%s must be an object" % where)
            name = result.get("check")
            if not isinstance(name, str) or not CHECK_NAME_RE.match(name):
                raise ValueError("%s: 'check' must be 1-40 chars of letters, digits, _ . -" % where)
            if name in seen:
                raise ValueError("%s: duplicate check '%s'" % (where, name))
            seen.add(name)
            kind = result.get("type") or known_kinds.get(name) or (PASS_FAIL if "status" in result else TREND)
            if kind not in (PASS_FAIL, TREND):
                raise ValueError("%s: 'type' must be pass_fail or trend" % where)
            if name in known_kinds and known_kinds[name] != kind:
                raise ValueError("%s: '%s' is registered as %s" % (where, name, known_kinds[name]))

            item = {"check": name, "kind": kind, "config": self._config(result, where)}
            if kind == PASS_FAIL:
                status = self.status_policy.normalize(result.get("status"))
                if not status:
                    raise ValueError("%s: 'status' must be pass, fail or skipped" % where)
                item["status"] = status
            else:
                if result.get("value") is None and result.get("delta") is None:
                    raise ValueError("%s: trend checks need 'value' or 'delta'" % where)
                for field in ("value", "baseline", "delta"):
                    item[field] = _number(result[field], field) if result.get(field) is not None else None
            normalized.append(item)

        return IngestCommand(
            workflow=workflow,
            commit=str(obj.get("commit") or "unknown")[:40],
            branch=str(obj.get("branch") or "")[:80],
            run_ref=str(obj.get("run_id") or obj.get("run_ref") or "")[:80],
            timestamp=parse_timestamp(obj.get("timestamp")),
            results=tuple(normalized),
        )

    @staticmethod
    def _config(result, where):
        """\brief Extract optional check configuration from one result.
        \param result Raw result object.
        \param where Location string used in validation messages.
        \return Configuration dictionary for check registration.
        """
        config = {}
        if "label" in result:
            config["label"] = str(result["label"])[:60]
        if "unit" in result:
            config["unit"] = str(result["unit"])[:20]
        if "polarity" in result:
            if result["polarity"] not in POLARITIES:
                raise ValueError("%s: 'polarity' must be higher_is_better or lower_is_better" % where)
            config["polarity"] = result["polarity"]
        for field in ("tolerance", "fail_delta"):
            if field in result:
                value = _number(result[field], field)
                if value < 0:
                    raise ValueError("%s: '%s' must be >= 0" % (where, field))
                config[field] = value
        return config
