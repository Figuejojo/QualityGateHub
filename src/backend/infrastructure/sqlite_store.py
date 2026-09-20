"""SQLite repository adapter."""

import sqlite3
import threading

from ..domain.models import CheckDefinition, PASS_FAIL
from ..domain.policies import TrendPolicy
from ..domain.validators import DEFAULT_WORKFLOW, utc_now_iso

SLOTS = 15
DEFAULT_CHECKS = [
    CheckDefinition("build", "Build", PASS_FAIL, position=1),
    CheckDefinition("unit_test", "Unit Testing", PASS_FAIL, position=2),
    CheckDefinition("static_analysis", "Static Code Analysis", "trend", "lower_is_better", "violations", 0.0, 5.0, 3),
    CheckDefinition("coverage", "Coverage", "trend", "higher_is_better", "%", 0.3, 2.0, 4),
]


class SqliteStore:
    """Implements focused check and run repository protocols over SQLite."""

    def __init__(self, path, keep, trend_policy=None, clock=None):
        self.keep = keep
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.trend_policy = trend_policy or TrendPolicy()
        self.clock = clock
        self._initialize()

    def _initialize(self):
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    name TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL,
                    polarity TEXT NOT NULL DEFAULT 'higher_is_better', unit TEXT NOT NULL DEFAULT '',
                    tolerance REAL NOT NULL DEFAULT 0, fail_delta REAL NOT NULL DEFAULT 0,
                    position INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, received_at TEXT NOT NULL,
                    commit_sha TEXT, branch TEXT, run_ref TEXT,
                    workflow TEXT NOT NULL DEFAULT 'Others'
                );
                CREATE TABLE IF NOT EXISTS results (
                    run_id INTEGER NOT NULL, check_name TEXT NOT NULL,
                    status TEXT, value REAL, baseline REAL, delta REAL,
                    direction TEXT, severity TEXT,
                    PRIMARY KEY (run_id, check_name)
                );
                """
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "workflow" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN workflow TEXT NOT NULL DEFAULT 'Others'")
            for check in DEFAULT_CHECKS:
                self.db.execute(
                    "INSERT OR IGNORE INTO checks(name,label,kind,polarity,unit,tolerance,fail_delta,position) VALUES (?,?,?,?,?,?,?,?)",
                    (check.name, check.label, check.kind, check.polarity, check.unit, check.tolerance, check.fail_delta, check.position),
                )
            self.db.commit()

    def check_kinds(self):
        with self.lock:
            return {row["name"]: row["kind"] for row in self.db.execute("SELECT name, kind FROM checks")}

    def get(self, name):
        with self.lock:
            row = self.db.execute("SELECT * FROM checks WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def list_checks(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM checks ORDER BY position, name")]

    def _register(self, name, kind, config):
        position = self.db.execute("SELECT COALESCE(MAX(position),0)+1 FROM checks").fetchone()[0]
        label = config.get("label") or name.replace("_", " ").replace("-", " ").title()
        self.db.execute(
            "INSERT INTO checks(name,label,kind,polarity,unit,tolerance,fail_delta,position) VALUES (?,?,?,?,?,?,?,?)",
            (name, label, kind, config.get("polarity", "higher_is_better"), config.get("unit", ""),
             config.get("tolerance", 0.0), config.get("fail_delta", 0.0), config.get("position", position)),
        )
        return self.get(name)

    def upsert(self, body):
        name = body.get("name")
        with self.lock:
            existing = self.get(name)
            config = {field: body[field] for field in ("label", "unit", "polarity", "tolerance", "fail_delta", "position") if field in body}
            if existing is None:
                kind = body.get("kind", "trend")
                if kind not in ("pass_fail", "trend"):
                    raise ValueError("'kind' must be pass_fail or trend")
                self._register(name, kind, config)
            elif config:
                assignments = ", ".join("%s=?" % field for field in config)
                self.db.execute("UPDATE checks SET %s WHERE name=?" % assignments, list(config.values()) + [name])
            self.db.commit()
            return self.get(name)

    def add_run(self, command):
        with self.lock:
            received_at = self.clock.now_iso() if self.clock else utc_now_iso()
            cursor = self.db.execute(
                "INSERT INTO runs(ts, received_at, commit_sha, branch, run_ref, workflow) VALUES (?,?,?,?,?,?)",
                (command.timestamp, received_at, command.commit, command.branch, command.run_ref, command.workflow),
            )
            run_id = cursor.lastrowid
            for result in command.results:
                check = self.get(result["check"]) or self._register(result["check"], result["kind"], result["config"])
                if check["kind"] == PASS_FAIL:
                    severity = {"pass": "good", "fail": "bad"}.get(result["status"], "none")
                    self.db.execute("INSERT INTO results(run_id, check_name, status, severity) VALUES (?,?,?,?)",
                                    (run_id, check["name"], result["status"], severity))
                    continue
                value, baseline, delta = result["value"], result["baseline"], result["delta"]
                if baseline is None and delta is None and value is not None:
                    row = self.db.execute(
                        "SELECT value FROM results WHERE check_name=? AND value IS NOT NULL AND run_id<? "
                        "AND run_id IN (SELECT id FROM runs WHERE workflow=?) ORDER BY run_id DESC LIMIT 1",
                        (check["name"], run_id, command.workflow),
                    ).fetchone()
                    baseline = row["value"] if row else None
                if delta is None and value is not None and baseline is not None:
                    delta = value - baseline
                if delta is not None:
                    delta = round(delta, 4)
                verdict = self.trend_policy.judge(CheckDefinition(**{key: check[key] for key in CheckDefinition.__dataclass_fields__}), delta)
                self.db.execute(
                    "INSERT INTO results(run_id, check_name, value, baseline, delta, direction, severity) VALUES (?,?,?,?,?,?,?)",
                    (run_id, check["name"], value, baseline, delta, verdict.direction, verdict.severity),
                )
            self._trim_history()
            self.db.commit()
            return run_id

    def _trim_history(self):
        workflows = [row[0] for row in self.db.execute("SELECT DISTINCT workflow FROM runs")]
        for workflow in workflows:
            old = self.db.execute("SELECT id FROM runs WHERE workflow=? ORDER BY id DESC LIMIT -1 OFFSET ?", (workflow, self.keep)).fetchall()
            old_ids = [row[0] for row in old]
            if old_ids:
                marks = ",".join("?" * len(old_ids))
                self.db.execute("DELETE FROM results WHERE run_id IN (%s)" % marks, old_ids)
                self.db.execute("DELETE FROM runs WHERE id IN (%s)" % marks, old_ids)

    def count_runs(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def last_values(self, workflow=DEFAULT_WORKFLOW):
        with self.lock:
            rows = self.db.execute(
                "SELECT check_name, value FROM results WHERE value IS NOT NULL AND run_id IN "
                "(SELECT MAX(run_id) FROM results WHERE value IS NOT NULL AND run_id IN "
                "(SELECT id FROM runs WHERE workflow=?) GROUP BY check_name)", (workflow,)).fetchall()
            return {row["check_name"]: row["value"] for row in rows}

    def dashboard(self, limit, workflow=DEFAULT_WORKFLOW):
        with self.lock:
            workflows = [row[0] for row in self.db.execute("SELECT DISTINCT workflow FROM runs")]
            if DEFAULT_WORKFLOW not in workflows:
                workflows.append(DEFAULT_WORKFLOW)
            workflows.sort(key=lambda name: (name == DEFAULT_WORKFLOW, name.lower()))
            if workflow not in workflows:
                workflow = DEFAULT_WORKFLOW
            runs = [dict(row) for row in self.db.execute("SELECT * FROM runs WHERE workflow=? ORDER BY id DESC LIMIT ?", (workflow, limit))]
            runs.reverse()
            by_id = {run["id"]: {"id": run["id"], "ts": run["ts"], "received_at": run["received_at"],
                                  "commit": run["commit_sha"], "branch": run["branch"], "run_ref": run["run_ref"],
                                  "workflow": run["workflow"], "results": {}} for run in runs}
            if by_id:
                marks = ",".join("?" * len(by_id))
                for row in self.db.execute("SELECT * FROM results WHERE run_id IN (%s)" % marks, list(by_id)):
                    by_id[row["run_id"]]["results"][row["check_name"]] = {
                        key: row[key] for key in ("status", "value", "baseline", "delta", "direction", "severity")
                    }
            return {"checks": self.list_checks(), "runs": [by_id[run["id"]] for run in runs],
                    "total_runs": self.db.execute("SELECT COUNT(*) FROM runs WHERE workflow=?", (workflow,)).fetchone()[0],
                    "slots": SLOTS, "workflow": workflow, "workflows": workflows}
