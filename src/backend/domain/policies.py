"""\file policies.py
\brief Pure business policies with no infrastructure dependencies.
"""

from .models import CheckDefinition, TrendVerdict


class TrendPolicy:
    """\class TrendPolicy
    \brief Calculates trend direction and severity.
    """

    def judge(self, check: CheckDefinition, delta):
        """\brief Evaluate a numeric change against a check definition.
        \param check Check configuration containing polarity and thresholds.
        \param delta Numeric difference from the baseline, or ``None``.
        \return TrendVerdict containing direction and severity.
        """
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
    """\class StatusPolicy
    \brief Normalizes statuses accepted by the public ingest API.
    """

    ALIASES = {
        "pass": "pass", "passed": "pass", "success": "pass", "ok": "pass",
        "fail": "fail", "failed": "fail", "failure": "fail", "error": "fail",
        "skip": "skip", "skipped": "skip", "cancelled": "skip", "canceled": "skip",
    }

    def normalize(self, value):
        """\brief Convert an external status alias to ``pass``, ``fail``, or ``skip``.
        \param value Raw status supplied by a pipeline.
        \return Normalized status, or ``None`` when unsupported.
        """
        return self.ALIASES.get(str(value or "").strip().lower())
