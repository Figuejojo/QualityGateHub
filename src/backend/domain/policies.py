"""Pure domain policies."""

from .models import CheckDefinition, TrendVerdict


class TrendPolicy:
    """Calculates trend direction and severity without infrastructure knowledge."""

    def judge(self, check: CheckDefinition, delta):
        if delta is None:
            return TrendVerdict(None, "none")
        if abs(delta) <= check.tolerance + 1e-9:
            return TrendVerdict("same", "neutral")
        direction = "up" if delta > 0 else "down"
        worse = (delta < 0) == (check.polarity == "higher_is_better")
        if not worse:
            return TrendVerdict(direction, "good")
        severity = "bad" if abs(delta) >= check.fail_delta else "warn"
        return TrendVerdict(direction, severity)


class StatusPolicy:
    """Normalizes statuses accepted by the public ingest API."""

    ALIASES = {
        "pass": "pass", "passed": "pass", "success": "pass", "ok": "pass",
        "fail": "fail", "failed": "fail", "failure": "fail", "error": "fail",
        "skip": "skip", "skipped": "skip", "cancelled": "skip", "canceled": "skip",
    }

    def normalize(self, value):
        return self.ALIASES.get(str(value or "").strip().lower())
