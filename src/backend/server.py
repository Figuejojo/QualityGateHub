#!/usr/bin/env python3
"""
Quality-gate dashboard backend
==============================

Python 3.8+, standard library only (works on a locked-down Windows box).

    python src/backend/server.py                  # http://127.0.0.1:8080
    python src/backend/server.py --port 9000
    python src/backend/server.py --host 0.0.0.0   # reachable from the lab network
    python src/backend/server.py --reset          # wipe the DB and start fresh

How it works
------------
  POST /api/ingest  ->  validate  ->  in-memory queue  ->  worker thread  ->  SQLite
                                                                         \\->  SSE push to browsers
  GET  /            ->  dashboard page (15 most recent pushes)
    GET  /api/dashboard?limit=15&workflow=DailyCheck
  GET  /events      ->  Server-Sent Events stream (browser re-renders on each new push)
  POST /api/checks  ->  create / tune a check (label, polarity, noise band, ...)
  POST /api/simulate->  enqueue a random run (used by the demo buttons)

Ingest payload
--------------
  {
    "workflow": "DailyCheck", "commit": "a1b2c3d", "branch": "develop", # workflow optional
    "run_id": "gha-123456", "timestamp": "2026-09-19T14:03:22Z",   # both optional
    "results": [
      {"check": "build",           "status": "pass"},          # pass_fail  (pass | fail | skipped)
      {"check": "unit_test",       "status": "fail"},
      {"check": "static_analysis", "value": 41},               # trend: baseline = previous push
      {"check": "coverage",        "value": 78.4, "baseline": 77.9}   # ...or send your own baseline
    ]
  }

  * A trend result needs "value" (compared with "baseline", or with the previous push if
    no baseline is sent) or a ready-made "delta".
  * A check name the server has not seen before becomes a new row automatically. On its first
    push you may add: "label", "type" (pass_fail | trend), "unit", "polarity"
    (higher_is_better | lower_is_better), "tolerance", "fail_delta".

Trend colours (shape = direction, colour = verdict)
---------------------------------------------------
  |delta| <= tolerance                  -> "same"      (blue)
  moved the good way                    -> up/down     (green)
  moved the bad way, < fail_delta       -> up/down     (yellow)
  moved the bad way, >= fail_delta      -> up/down     (red)
"""

import argparse
import json
import math
import os
import queue
import random
import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SLOTS = 15  # number of pushes the board shows

# --------------------------------------------------------------------------------------
# Checks that exist from the start. Anything else is added at runtime.
# --------------------------------------------------------------------------------------
DEFAULT_CHECKS = [
    dict(name="build", label="Build", kind="pass_fail"),
    dict(name="unit_test", label="Unit Testing", kind="pass_fail"),
    dict(name="static_analysis", label="Static Code Analysis", kind="trend",
         polarity="lower_is_better", unit="violations", tolerance=0.0, fail_delta=5.0),
    dict(name="coverage", label="Coverage", kind="trend",
         polarity="higher_is_better", unit="%", tolerance=0.3, fail_delta=2.0),
]

CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:/\-]{0,59}$")
STATUS_ALIASES = {
    "pass": "pass", "passed": "pass", "success": "pass", "ok": "pass",
    "fail": "fail", "failed": "fail", "failure": "fail", "error": "fail",
    "skip": "skip", "skipped": "skip", "cancelled": "skip", "canceled": "skip",
}
POLARITIES = ("higher_is_better", "lower_is_better")
DEFAULT_WORKFLOW = "Example"


# --------------------------------------------------------------------------------------
# Verdict logic - the one place that decides icon direction + colour for trend checks
# --------------------------------------------------------------------------------------
def judge(check, delta):
    """Return (direction, severity) for a trend delta. severity: good|neutral|warn|bad|none"""
    if delta is None:
        return None, "none"
    if abs(delta) <= check["tolerance"] + 1e-9:
        return "same", "neutral"
    direction = "up" if delta > 0 else "down"
    worse = (delta < 0) == (check["polarity"] == "higher_is_better")
    if not worse:
        return direction, "good"
    return direction, ("bad" if abs(delta) >= check["fail_delta"] else "warn")


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
def _num(v, field):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("'%s' must be a finite number" % field)
    return float(v)


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(v):
    if v is None:
        return utc_now_iso()
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            dt = datetime.fromtimestamp(v, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        raise ValueError("'timestamp' must be ISO-8601 (e.g. 2026-09-19T14:03:22Z) or epoch seconds")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def validate_payload(obj, known_kinds):
    if not isinstance(obj, dict):
        raise ValueError("body must be a JSON object")
    results = obj.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("'results' must be a non-empty list")
    if len(results) > 100:
        raise ValueError("'results' is limited to 100 entries")

    out = {
        "workflow": str(obj.get("workflow") or DEFAULT_WORKFLOW).strip()[:60],
        "commit": str(obj.get("commit") or "unknown")[:40],
        "branch": str(obj.get("branch") or "")[:80],
        "run_ref": str(obj.get("run_id") or obj.get("run_ref") or "")[:80],
        "timestamp": parse_ts(obj.get("timestamp")),
        "results": [],
    }
    if not WORKFLOW_NAME_RE.match(out["workflow"]):
        raise ValueError("'workflow' must be 1-60 chars and start with a letter or number")
    seen = set()
    for i, r in enumerate(results):
        where = "results[%d]" % i
        if not isinstance(r, dict):
            raise ValueError("%s must be an object" % where)
        name = r.get("check")
        if not isinstance(name, str) or not CHECK_NAME_RE.match(name):
            raise ValueError("%s: 'check' must be 1-40 chars of letters, digits, _ . -" % where)
        if name in seen:
            raise ValueError("%s: duplicate check '%s'" % (where, name))
        seen.add(name)

        kind = r.get("type") or known_kinds.get(name) or ("pass_fail" if "status" in r else "trend")
        if kind not in ("pass_fail", "trend"):
            raise ValueError("%s: 'type' must be pass_fail or trend" % where)
        if name in known_kinds and known_kinds[name] != kind:
            raise ValueError("%s: '%s' is registered as %s" % (where, name, known_kinds[name]))

        item = {"check": name, "kind": kind}
        if kind == "pass_fail":
            status = STATUS_ALIASES.get(str(r.get("status", "")).strip().lower())
            if not status:
                raise ValueError("%s: 'status' must be pass, fail or skipped" % where)
            item["status"] = status
        else:
            if r.get("value") is None and r.get("delta") is None:
                raise ValueError("%s: trend checks need 'value' or 'delta'" % where)
            for f in ("value", "baseline", "delta"):
                item[f] = _num(r[f], f) if r.get(f) is not None else None

        cfg = {}
        if "label" in r:
            cfg["label"] = str(r["label"])[:60]
        if "unit" in r:
            cfg["unit"] = str(r["unit"])[:20]
        if "polarity" in r:
            if r["polarity"] not in POLARITIES:
                raise ValueError("%s: 'polarity' must be higher_is_better or lower_is_better" % where)
            cfg["polarity"] = r["polarity"]
        for f in ("tolerance", "fail_delta"):
            if f in r:
                v = _num(r[f], f)
                if v < 0:
                    raise ValueError("%s: '%s' must be >= 0" % (where, f))
                cfg[f] = v
        item["config"] = cfg
        out["results"].append(item)
    return out


# --------------------------------------------------------------------------------------
# Storage (SQLite, one connection guarded by a lock)
# --------------------------------------------------------------------------------------
class Store:
    def __init__(self, path, keep):
        self.keep = keep
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    name TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL,
                    polarity TEXT NOT NULL DEFAULT 'higher_is_better',
                    unit TEXT NOT NULL DEFAULT '',
                    tolerance REAL NOT NULL DEFAULT 0,
                    fail_delta REAL NOT NULL DEFAULT 0,
                    position INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, received_at TEXT NOT NULL,
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
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
            if "workflow" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN workflow TEXT NOT NULL DEFAULT 'Others'")
            for i, c in enumerate(DEFAULT_CHECKS):
                self.db.execute(
                    "INSERT OR IGNORE INTO checks(name,label,kind,polarity,unit,tolerance,fail_delta,position)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (c["name"], c["label"], c["kind"], c.get("polarity", "higher_is_better"),
                     c.get("unit", ""), c.get("tolerance", 0.0), c.get("fail_delta", 0.0), i + 1),
                )
            self.db.commit()

    # -- checks ------------------------------------------------------------------------
    def check_kinds(self):
        with self.lock:
            return {r["name"]: r["kind"] for r in self.db.execute("SELECT name, kind FROM checks")}

    def _get_check(self, name):
        row = self.db.execute("SELECT * FROM checks WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def _register(self, name, kind, cfg):
        pos = self.db.execute("SELECT COALESCE(MAX(position),0)+1 FROM checks").fetchone()[0]
        label = cfg.get("label") or name.replace("_", " ").replace("-", " ").title()
        self.db.execute(
            "INSERT INTO checks(name,label,kind,polarity,unit,tolerance,fail_delta,position)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name, label, kind, cfg.get("polarity", "higher_is_better"), cfg.get("unit", ""),
             cfg.get("tolerance", 0.0), cfg.get("fail_delta", 0.0), cfg.get("position", pos)),
        )
        return self._get_check(name)

    def upsert_check(self, body):
        """Create or tune a check (POST /api/checks). Missing fields keep their value."""
        name = body.get("name")
        if not isinstance(name, str) or not CHECK_NAME_RE.match(name):
            raise ValueError("'name' must be 1-40 chars of letters, digits, _ . -")
        cfg = {}
        if "label" in body:
            cfg["label"] = str(body["label"])[:60]
        if "unit" in body:
            cfg["unit"] = str(body["unit"])[:20]
        if "polarity" in body:
            if body["polarity"] not in POLARITIES:
                raise ValueError("'polarity' must be higher_is_better or lower_is_better")
            cfg["polarity"] = body["polarity"]
        for f in ("tolerance", "fail_delta"):
            if f in body:
                v = _num(body[f], f)
                if v < 0:
                    raise ValueError("'%s' must be >= 0" % f)
                cfg[f] = v
        if "position" in body:
            cfg["position"] = int(_num(body["position"], "position"))
        with self.lock:
            existing = self._get_check(name)
            if existing is None:
                kind = body.get("kind", "trend")
                if kind not in ("pass_fail", "trend"):
                    raise ValueError("'kind' must be pass_fail or trend")
                self._register(name, kind, cfg)
            elif cfg:
                sets = ", ".join("%s=?" % k for k in cfg)
                self.db.execute("UPDATE checks SET %s WHERE name=?" % sets, list(cfg.values()) + [name])
            self.db.commit()
            return self._get_check(name)

    # -- runs --------------------------------------------------------------------------
    def add_run(self, p):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO runs(ts, received_at, commit_sha, branch, run_ref, workflow) VALUES (?,?,?,?,?,?)",
                (p["timestamp"], utc_now_iso(), p["commit"], p["branch"], p["run_ref"], p["workflow"]),
            )
            run_id = cur.lastrowid
            for r in p["results"]:
                chk = self._get_check(r["check"]) or self._register(r["check"], r["kind"], r["config"])
                if chk["kind"] == "pass_fail":
                    self.db.execute(
                        "INSERT INTO results(run_id, check_name, status, severity) VALUES (?,?,?,?)",
                        (run_id, chk["name"], r["status"], {"pass": "good", "fail": "bad"}.get(r["status"], "none")),
                    )
                    continue
                value, baseline, delta = r["value"], r["baseline"], r["delta"]
                if baseline is None and delta is None and value is not None:
                    row = self.db.execute(
                        "SELECT value FROM results WHERE check_name=? AND value IS NOT NULL AND run_id<? "
                        "AND run_id IN (SELECT id FROM runs WHERE workflow=?) "
                        "ORDER BY run_id DESC LIMIT 1", (chk["name"], run_id, p["workflow"])).fetchone()
                    baseline = row["value"] if row else None
                if delta is None and value is not None and baseline is not None:
                    delta = value - baseline
                if delta is not None:
                    delta = round(delta, 4)
                direction, severity = judge(chk, delta)
                self.db.execute(
                    "INSERT INTO results(run_id, check_name, value, baseline, delta, direction, severity)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (run_id, chk["name"], value, baseline, delta, direction, severity),
                )
            # Retain an independent history for each workflow.
            workflows = [r[0] for r in self.db.execute("SELECT DISTINCT workflow FROM runs")]
            for workflow in workflows:
                old = self.db.execute(
                    "SELECT id FROM runs WHERE workflow=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (workflow, self.keep)).fetchall()
                old_ids = [r[0] for r in old]
                if old_ids:
                    marks = ",".join("?" * len(old_ids))
                    self.db.execute("DELETE FROM results WHERE run_id IN (%s)" % marks, old_ids)
                    self.db.execute("DELETE FROM runs WHERE id IN (%s)" % marks, old_ids)
            self.db.commit()
            return run_id

    def count_runs(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def last_values(self, workflow=DEFAULT_WORKFLOW):
        """Most recent value seen for each trend check (used by the simulator)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT check_name, value FROM results WHERE value IS NOT NULL AND run_id IN "
                "(SELECT MAX(run_id) FROM results WHERE value IS NOT NULL AND run_id IN "
                "(SELECT id FROM runs WHERE workflow=?) GROUP BY check_name)", (workflow,)).fetchall()
            return {r["check_name"]: r["value"] for r in rows}

    def dashboard(self, limit, workflow=DEFAULT_WORKFLOW):
        with self.lock:
            checks = [dict(r) for r in self.db.execute("SELECT * FROM checks ORDER BY position, name")]
            workflows = [r[0] for r in self.db.execute("SELECT DISTINCT workflow FROM runs")]
            if DEFAULT_WORKFLOW not in workflows:
                workflows.append(DEFAULT_WORKFLOW)
            workflows.sort(key=lambda name: (name == DEFAULT_WORKFLOW, name.lower()))
            if workflow not in workflows:
                workflow = DEFAULT_WORKFLOW
            runs = [dict(r) for r in self.db.execute(
                "SELECT * FROM runs WHERE workflow=? ORDER BY id DESC LIMIT ?", (workflow, limit))]
            runs.reverse()
            by_id = {}
            for r in runs:
                by_id[r["id"]] = {"id": r["id"], "ts": r["ts"], "received_at": r["received_at"],
                                  "commit": r["commit_sha"], "branch": r["branch"], "run_ref": r["run_ref"],
                                  "workflow": r["workflow"],
                                  "results": {}}
            if by_id:
                marks = ",".join("?" * len(by_id))
                for row in self.db.execute("SELECT * FROM results WHERE run_id IN (%s)" % marks, list(by_id)):
                    by_id[row["run_id"]]["results"][row["check_name"]] = {
                        k: row[k] for k in ("status", "value", "baseline", "delta", "direction", "severity")}
            return {"checks": checks, "runs": [by_id[r["id"]] for r in runs],
                    "total_runs": self.db.execute("SELECT COUNT(*) FROM runs WHERE workflow=?",
                                   (workflow,)).fetchone()[0],
                    "slots": SLOTS, "workflow": workflow, "workflows": workflows}


# --------------------------------------------------------------------------------------
# Live-update hub (fan-out to connected browsers over SSE)
# --------------------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = set()

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, msg):
        with self.lock:
            targets = list(self.subs)
        for q in targets:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


STORE = None
HUB = Hub()
INGEST_Q = queue.Queue()


def worker():
    """Single consumer: everything that arrives is written in order, then browsers are told."""
    while True:
        payload = INGEST_Q.get()
        try:
            run_id = STORE.add_run(payload)
            HUB.publish({"type": "run", "run_id": run_id})
            print("[ingest] run #%d  %s  %d results" % (run_id, payload["commit"], len(payload["results"])))
        except Exception as exc:  # keep the worker alive whatever happens
            print("[ingest] FAILED: %s" % exc, file=sys.stderr)
        finally:
            INGEST_Q.task_done()


# --------------------------------------------------------------------------------------
# Demo data generator
# --------------------------------------------------------------------------------------
def simulate_payload(ts=None, workflow=DEFAULT_WORKFLOW):
    last = STORE.last_values(workflow)
    sa = last.get("static_analysis", 42.0)
    cov = last.get("coverage", 71.0)
    sa = max(0, sa + random.choices([-3, -2, -1, 0, 1, 2, 4, 8], [1, 2, 3, 5, 3, 2, 1, 1])[0])
    cov = min(98.0, max(40.0, round(cov + random.choice([-2.5, -1.0, -0.4, 0, 0, 0.1, 0.2, 0.5, 1.2, 2.4]), 1)))
    build_ok = random.random() > 0.08
    test_ok = random.random() > 0.12
    results = [{"check": "build", "status": "pass" if build_ok else "fail"}]
    if build_ok:
        results.append({"check": "unit_test", "status": "pass" if test_ok else "fail"})
        results.append({"check": "static_analysis", "value": sa})
        results.append({"check": "coverage", "value": cov})
    else:  # a broken build skips everything downstream
        results.append({"check": "unit_test", "status": "skipped"})
    payload = {
        "workflow": workflow,
        "commit": "%07x" % random.getrandbits(28),
        "branch": random.choice(["develop", "develop", "develop", "feature/uart-dma", "fix/adc-offset"]),
        "results": results,
    }
    if ts:
        payload["timestamp"] = ts
    return payload


def seed(n=SLOTS):
    now = datetime.now(timezone.utc)
    for i in range(n):
        when = now - timedelta(minutes=(n - i) * 23)
        p = validate_payload(simulate_payload(when.strftime("%Y-%m-%dT%H:%M:%SZ")), STORE.check_kinds())
        STORE.add_run(p)


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "GateBoard/0.1"

    def log_message(self, fmt, *args):  # quiet; the worker logs what matters
        pass

    # helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length <= 0:
            raise ValueError("empty body")
        if length > 1_000_000:
            raise ValueError("body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON: %s" % exc)

    # routes
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, load_frontend(), "text/html; charset=utf-8")
        elif url.path == "/api/dashboard":
            try:
                limit = int(parse_qs(url.query).get("limit", [SLOTS])[0])
            except ValueError:
                limit = SLOTS
            workflow = parse_qs(url.query).get("workflow", [DEFAULT_WORKFLOW])[0]
            self._send(200, STORE.dashboard(max(1, min(limit, 100)), workflow))
        elif url.path == "/api/health":
            self._send(200, {"ok": True, "queued": INGEST_Q.qsize(), "runs": STORE.count_runs()})
        elif url.path == "/events":
            self._sse()
        elif url.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/ingest":
                payload = validate_payload(self._read_json(), STORE.check_kinds())
                INGEST_Q.put(payload)
                self._send(202, {"queued": True, "workflow": payload["workflow"], "commit": payload["commit"],
                                 "queue_depth": INGEST_Q.qsize()})
            elif path == "/api/simulate":
                body = self._read_json() if self.headers.get("Content-Length") else {}
                workflow = body.get("workflow") or DEFAULT_WORKFLOW
                if not isinstance(workflow, str) or not WORKFLOW_NAME_RE.match(workflow):
                    raise ValueError("'workflow' must be 1-60 chars and start with a letter or number")
                payload = validate_payload(simulate_payload(workflow=workflow), STORE.check_kinds())
                INGEST_Q.put(payload)
                self._send(202, {"queued": True, "workflow": payload["workflow"], "commit": payload["commit"]})
            elif path == "/api/checks":
                self._send(200, STORE.upsert_check(self._read_json()))
                HUB.publish({"type": "checks"})
            else:
                self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = HUB.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\nevent: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(("event: %s\ndata: %s\n\n" % (msg["type"], json.dumps(msg))).encode("utf-8"))
                self.wfile.flush()
        except OSError:  # browser went away
            pass
        finally:
            HUB.unsubscribe(q)


# --------------------------------------------------------------------------------------
# Frontend asset
# --------------------------------------------------------------------------------------
FRONTEND_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html"))

def load_frontend():
    with open(FRONTEND_PATH, "rb") as frontend_file:
        return frontend_file.read()


# --------------------------------------------------------------------------------------
def main():
    global STORE
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    ap = argparse.ArgumentParser(description="Quality-gate dashboard prototype")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.path.join(project_root, "ci_dashboard.db"), help="SQLite file, or :memory:")
    ap.add_argument("--keep", type=int, default=500, help="pushes to retain in the DB (board shows the last 15)")
    ap.add_argument("--no-seed", action="store_true", help="do not pre-fill 15 demo pushes into an empty DB")
    ap.add_argument("--reset", action="store_true", help="delete the DB file before starting")
    args = ap.parse_args()

    if args.reset and args.db != ":memory:" and os.path.exists(args.db):
        os.remove(args.db)
    STORE = Store(args.db, max(args.keep, SLOTS))
    if not args.no_seed and STORE.count_runs() == 0:
        seed()
    threading.Thread(target=worker, daemon=True, name="ingest-worker").start()

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit("Cannot listen on %s:%d - %s" % (args.host, args.port, exc))
    srv.daemon_threads = True

    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print("Quality-gate dashboard")
    print("  dashboard : http://%s:%d/" % (shown, args.port))
    print("  ingest    : POST http://%s:%d/api/ingest" % (shown, args.port))
    print("  database  : %s   (%d pushes stored)" % (args.db, STORE.count_runs()))
    print("  Ctrl+C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
