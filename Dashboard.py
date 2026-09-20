#!/usr/bin/env python3
"""
Quality-gate dashboard - prototype
==================================

One file, Python 3.8+, standard library only (works on a locked-down Windows box).

    python ci_dashboard.py                  # http://127.0.0.1:8080
    python ci_dashboard.py --port 9000
    python ci_dashboard.py --host 0.0.0.0   # reachable from the lab network
    python ci_dashboard.py --reset          # wipe the DB and start fresh

How it works
------------
  POST /api/ingest  ->  validate  ->  in-memory queue  ->  worker thread  ->  SQLite
                                                                         \\->  SSE push to browsers
  GET  /            ->  dashboard page (15 most recent pushes)
  GET  /api/dashboard?limit=15
  GET  /events      ->  Server-Sent Events stream (browser re-renders on each new push)
  POST /api/checks  ->  create / tune a check (label, polarity, noise band, ...)
  POST /api/simulate->  enqueue a random run (used by the demo buttons)

Ingest payload
--------------
  {
    "commit": "a1b2c3d", "branch": "develop",            # both optional
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
STATUS_ALIASES = {
    "pass": "pass", "passed": "pass", "success": "pass", "ok": "pass",
    "fail": "fail", "failed": "fail", "failure": "fail", "error": "fail",
    "skip": "skip", "skipped": "skip", "cancelled": "skip", "canceled": "skip",
}
POLARITIES = ("higher_is_better", "lower_is_better")


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
        "commit": str(obj.get("commit") or "unknown")[:40],
        "branch": str(obj.get("branch") or "")[:80],
        "run_ref": str(obj.get("run_id") or obj.get("run_ref") or "")[:80],
        "timestamp": parse_ts(obj.get("timestamp")),
        "results": [],
    }
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
                    commit_sha TEXT, branch TEXT, run_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS results (
                    run_id INTEGER NOT NULL, check_name TEXT NOT NULL,
                    status TEXT, value REAL, baseline REAL, delta REAL,
                    direction TEXT, severity TEXT,
                    PRIMARY KEY (run_id, check_name)
                );
                """
            )
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
                "INSERT INTO runs(ts, received_at, commit_sha, branch, run_ref) VALUES (?,?,?,?,?)",
                (p["timestamp"], utc_now_iso(), p["commit"], p["branch"], p["run_ref"]),
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
                        "ORDER BY run_id DESC LIMIT 1", (chk["name"], run_id)).fetchone()
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
            # retention: keep the newest `keep` pushes
            self.db.execute(
                "DELETE FROM results WHERE run_id IN (SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (self.keep,))
            self.db.execute("DELETE FROM runs WHERE id IN (SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?)",
                            (self.keep,))
            self.db.commit()
            return run_id

    def count_runs(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def last_values(self):
        """Most recent value seen for each trend check (used by the simulator)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT check_name, value FROM results WHERE value IS NOT NULL AND run_id IN "
                "(SELECT MAX(run_id) FROM results WHERE value IS NOT NULL GROUP BY check_name)").fetchall()
            return {r["check_name"]: r["value"] for r in rows}

    def dashboard(self, limit):
        with self.lock:
            checks = [dict(r) for r in self.db.execute("SELECT * FROM checks ORDER BY position, name")]
            runs = [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
            runs.reverse()
            by_id = {}
            for r in runs:
                by_id[r["id"]] = {"id": r["id"], "ts": r["ts"], "received_at": r["received_at"],
                                  "commit": r["commit_sha"], "branch": r["branch"], "run_ref": r["run_ref"],
                                  "results": {}}
            if by_id:
                marks = ",".join("?" * len(by_id))
                for row in self.db.execute("SELECT * FROM results WHERE run_id IN (%s)" % marks, list(by_id)):
                    by_id[row["run_id"]]["results"][row["check_name"]] = {
                        k: row[k] for k in ("status", "value", "baseline", "delta", "direction", "severity")}
            return {"checks": checks, "runs": [by_id[r["id"]] for r in runs],
                    "total_runs": self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                    "slots": SLOTS}


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
def simulate_payload(ts=None):
    last = STORE.last_values()
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
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif url.path == "/api/dashboard":
            try:
                limit = int(parse_qs(url.query).get("limit", [SLOTS])[0])
            except ValueError:
                limit = SLOTS
            self._send(200, STORE.dashboard(max(1, min(limit, 100))))
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
                self._send(202, {"queued": True, "commit": payload["commit"], "queue_depth": INGEST_Q.qsize()})
            elif path == "/api/simulate":
                payload = validate_payload(simulate_payload(), STORE.check_kinds())
                INGEST_Q.put(payload)
                self._send(202, {"queued": True, "commit": payload["commit"]})
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
# The page
# --------------------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quality gates</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E%3Ccircle cx='14' cy='14' r='11' fill='%23d4ebd9' stroke='%234c9b62' stroke-width='2'/%3E%3C/svg%3E">
<style>
  :root {
    --bg: #EEF1F4; --panel: #FFFFFF; --ink: #17212B; --muted: #66727F; --line: #D5DBE1; --hair: #EDF0F3;
    --accent: #2F5DA8;
    --label-w: 210px; --col-w: 62px; --row-h: 66px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.45 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
  }
  .page { max-width: 1240px; margin: 0 auto; padding: 28px 24px 56px; }

  .top { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
  h1 { font-size: 26px; font-weight: 600; letter-spacing: -0.01em; margin: 0; }
  .sub { color: var(--muted); margin: 2px 0 0; }
  .status { display: flex; align-items: center; gap: 10px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #98A3AE; flex: none; }
  .dot.live { background: #4C9B62; box-shadow: 0 0 0 3px rgba(76,155,98,.22); }
  .dot.retry { background: #B58400; }

  .tools { display: flex; gap: 18px; align-items: center; flex-wrap: wrap; margin: 0 0 12px; }
  button {
    font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--ink);
    border-radius: 3px; padding: 6px 12px; cursor: pointer;
  }
  button:hover { background: #F3F6F9; }
  button:focus-visible, input:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .tools label { display: inline-flex; gap: 8px; align-items: center; cursor: pointer; }
  input[type=checkbox] { accent-color: var(--accent); width: 16px; height: 16px; margin: 0; }

  /* ---- the board ---- */
  .board { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; overflow-x: auto; }
  .grid {
    display: grid; grid-template-columns: var(--label-w) repeat(var(--cols, 15), var(--col-w));
    width: max-content; min-width: 100%;
  }
  .corner, .rowlabel { position: sticky; left: 0; z-index: 2; background: var(--panel); }
  .rowlabel {
    display: flex; flex-direction: column; justify-content: center; gap: 4px;
    padding: 0 16px; border-top: 1px solid var(--hair); height: var(--row-h);
  }
  .labelbox {
    border: 1px solid var(--ink); border-radius: 2px; padding: 6px 10px; text-align: center; font-weight: 500;
    background: var(--panel);
  }
  .labelbox.ghost { border: 1px dashed #98A3AE; color: var(--muted); font-weight: 400; }
  .cap { font-size: 11.5px; color: var(--muted); text-align: center; line-height: 1.2; }

  .head {
    display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 1px;
    padding: 14px 0 8px; font-size: 12px; line-height: 1.25; font-variant-numeric: tabular-nums;
  }
  .head .d { font-weight: 600; }
  .head .t, .head .sha { color: var(--muted); }
  .head .sha { font-size: 11px; }

  .cell { display: flex; align-items: center; justify-content: center; height: var(--row-h); border-top: 1px solid var(--hair); }
  .cell.empty::after { content: ""; width: 5px; height: 5px; border-radius: 50%; background: #DDE2E7; }
  .cell.none::after { content: ""; width: 10px; height: 2px; background: #DDE2E7; }
  .cell.ghost::after { content: ""; width: 5px; height: 5px; border-radius: 50%; background: #E9EDF1; }
  .cell[tabindex]:focus-visible { outline: 2px solid var(--accent); outline-offset: -4px; border-radius: 6px; }
  .cell[tabindex] { cursor: default; }

  .fresh { animation: arrive 2.4s ease-out; }
  @keyframes arrive { 0% { background: rgba(91,130,192,.30); } 100% { background: transparent; } }
  @media (prefers-reduced-motion: reduce) { .fresh { animation: none; } }

  /* ---- icons: shape = what happened, colour = how much to worry ---- */
  .ico { width: 26px; height: 26px; display: block; }
  .ico .shape { fill: var(--fill); stroke: var(--stroke); stroke-width: 1.6; }
  .ico .glyph { fill: none; stroke: var(--stroke); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .ico .glyph-fill { fill: var(--stroke); }
  .sev-good    { --fill: #D4EBD9; --stroke: #4C9B62; }
  .sev-bad     { --fill: #F6D0CF; --stroke: #C8514F; }
  .sev-neutral { --fill: #D8E5F8; --stroke: #5B82C0; }
  .sev-warn    { --fill: #FBEFC0; --stroke: #B58400; }
  .sev-none, .sev-skip { --fill: #EEF1F4; --stroke: #9AA5B0; }
  .sev-skip .shape { stroke-dasharray: 3 2.5; }
  .mini .ico { width: 18px; height: 18px; }

  /* ---- tooltip ---- */
  #tip {
    position: fixed; z-index: 10; max-width: 260px; pointer-events: none;
    background: var(--ink); color: #fff; border-radius: 4px; padding: 9px 11px; font-size: 12.5px; line-height: 1.4;
  }
  #tip[hidden] { display: none; }
  #tip b { font-weight: 600; }
  #tip .m { color: #B9C3CD; }

  /* ---- legend + how-to ---- */
  .legend {
    display: flex; flex-wrap: wrap; gap: 14px 40px; margin-top: 16px; padding: 14px 18px;
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
  }
  .legend h2 { font-size: 13px; font-weight: 600; margin: 0 0 8px; }
  .legend ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
  .legend li { display: flex; align-items: center; gap: 9px; color: var(--muted); }
  .howto { margin-top: 16px; }
  .howto summary { cursor: pointer; font-weight: 500; width: max-content; }
  .howto p { color: var(--muted); max-width: 70ch; margin: 8px 0; }
  pre {
    margin: 8px 0 0; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
    overflow-x: auto; font: 12.5px/1.5 Consolas, "Cascadia Mono", monospace;
  }
</style>
</head>
<body>
<div class="page">
  <header class="top">
    <div>
      <h1>Quality gates</h1>
      <p class="sub">Last 15 pushes, oldest on the left</p>
    </div>
    <div class="status" aria-live="polite">
      <span class="dot" id="dot"></span><span id="conn">Connecting</span>
      <span id="last"></span>
    </div>
  </header>

  <div class="tools">
    <button id="send" type="button">Send simulated push</button>
    <label><input type="checkbox" id="auto"> Auto-send every 4 s</label>
  </div>

  <div class="board"><div class="grid" id="grid"></div></div>

  <section class="legend" aria-label="Legend">
    <div><h2>Pass / fail</h2><ul id="lg-pf"></ul></div>
    <div><h2>Direction vs baseline</h2><ul id="lg-dir"></ul></div>
    <div><h2>Colour of a direction icon</h2><ul id="lg-col"></ul></div>
  </section>

  <details class="howto">
    <summary>Push data from a pipeline</summary>
    <p>POST JSON to <b id="url"></b>. Trend values are compared with the previous push unless you send a
       <code>baseline</code>. A new check name adds a row; on its first push you can also send
       <code>label</code>, <code>unit</code>, <code>polarity</code>, <code>tolerance</code> and <code>fail_delta</code>.</p>
<pre>{
  "commit": "a1b2c3d",
  "branch": "develop",
  "results": [
    { "check": "build",           "status": "pass" },
    { "check": "unit_test",       "status": "fail" },
    { "check": "static_analysis", "value": 41 },
    { "check": "coverage",        "value": 78.4, "baseline": 77.9 },
    { "check": "flash_usage", "value": 412.5, "unit": "KB", "polarity": "lower_is_better",
      "tolerance": 0.5, "fail_delta": 8, "label": "Flash usage" }
  ]
}</pre>
  </details>
</div>
<div id="tip" role="tooltip" hidden></div>

<script>
const SLOTS = 15;
const grid = document.getElementById('grid');
const tip = document.getElementById('tip');
const state = { data: null, lastId: null, lastReceived: null };

/* ---------- icons ---------- */
const SHAPES = {
  circle: '<circle class="shape" cx="14" cy="14" r="11"/>',
  up:     '<path class="shape" d="M14 3.5 L25.5 24.5 H2.5 Z" stroke-linejoin="round"/>',
  down:   '<path class="shape" d="M2.5 3.5 H25.5 L14 24.5 Z" stroke-linejoin="round"/>',
  same:   '<rect class="shape" x="3" y="5" width="22" height="8" rx="1.5"/><rect class="shape" x="3" y="15" width="22" height="8" rx="1.5"/>'
};
const GLYPHS = {
  check: '<path class="glyph" d="M9 14.5 l3.6 3.6 L19 10.5"/>',
  x:     '<path class="glyph" d="M10 10 L18 18 M18 10 L10 18"/>',
  dash:  '<path class="glyph" d="M10 14 H18"/>',
  dot:   '<circle class="glyph-fill" cx="14" cy="14" r="2.2"/>'
};
function svg(shape, sev, glyph) {
  return '<svg class="ico sev-' + sev + '" viewBox="0 0 28 28" aria-hidden="true">' +
         SHAPES[shape] + (glyph ? GLYPHS[glyph] : '') + '</svg>';
}
function iconFor(check, r) {
  if (check.kind === 'pass_fail') {
    if (r.status === 'pass') return svg('circle', 'good', 'check');
    if (r.status === 'fail') return svg('circle', 'bad', 'x');
    return svg('circle', 'skip', 'dash');
  }
  if (!r.direction) return svg('circle', 'none', 'dot');          // first value, nothing to compare with
  const shape = r.direction === 'up' ? 'up' : r.direction === 'down' ? 'down' : 'same';
  return svg(shape, r.severity);
}

/* ---------- helpers ---------- */
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = n => Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
const dateOf = iso => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const timeOf = iso => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
const fullTime = iso => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

function caption(c) {
  if (c.kind === 'pass_fail') return 'pass / fail';
  return (c.unit ? c.unit + ', ' : '') + (c.polarity === 'higher_is_better' ? 'higher is better' : 'lower is better');
}

function describe(c, run, r) {
  const lines = [];
  const unit = c.unit ? ' ' + c.unit : '';
  if (c.kind === 'pass_fail') {
    lines.push(r.status === 'pass' ? 'Passed' : r.status === 'fail' ? 'Failed' : 'Skipped');
  } else {
    lines.push(r.value != null ? fmtNum(r.value) + unit : 'No value');
    if (r.delta == null) {
      lines.push('No baseline yet');
    } else if (r.direction === 'same') {
      lines.push('Unchanged, within the noise band (' + fmtNum(c.tolerance) + unit + ')');
    } else {
      const verdict = { good: 'better than baseline', warn: 'worse, under the fail limit', bad: 'worse, at or over the fail limit' }[r.severity];
      lines.push((r.direction === 'up' ? 'Up ' : 'Down ') + fmtNum(Math.abs(r.delta)) + unit + ', ' + verdict);
    }
    if (r.baseline != null) lines.push('Baseline ' + fmtNum(r.baseline) + unit);
  }
  return lines;
}

/* ---------- render ---------- */
function headCell(run, fresh) {
  const t = run.ts;
  return '<div class="head' + (fresh ? ' fresh' : '') + '"><span class="d">' + esc(dateOf(t)) + '</span>' +
         '<span class="t">' + esc(timeOf(t)) + '</span><span class="sha">' + esc((run.commit || '').slice(0, 7)) + '</span></div>';
}

function cellHtml(c, run, fresh) {
  if (!run) return '<div class="cell empty"></div>';
  const r = run.results[c.name];
  const f = fresh ? ' fresh' : '';
  if (!r) return '<div class="cell none' + f + '"></div>';
  const plain = c.label + ': ' + describe(c, run, r).join('. ');
  return '<div class="cell' + f + '" tabindex="0" data-run="' + run.id + '" data-check="' + esc(c.name) + '" aria-label="' + esc(plain) + '">' +
         iconFor(c, r) + '</div>';
}

function render(fresh) {
  const d = state.data;
  const pad = Math.max(SLOTS - d.runs.length, 0);
  const cols = Array(pad).fill(null).concat(d.runs);         // newest run is always the right-most column
  const lastIdx = cols.length - 1;
  grid.style.setProperty('--cols', cols.length);

  let h = '<div class="corner"></div>';
  cols.forEach((r, i) => { h += r ? headCell(r, fresh && i === lastIdx) : '<div class="head"></div>'; });

  d.checks.forEach(c => {
    h += '<div class="rowlabel"><div class="labelbox">' + esc(c.label) + '</div><div class="cap">' + esc(caption(c)) + '</div></div>';
    cols.forEach((r, i) => { h += cellHtml(c, r, fresh && i === lastIdx); });
  });

  h += '<div class="rowlabel"><div class="labelbox ghost">Others</div><div class="cap">new checks appear here</div></div>';
  cols.forEach(() => { h += '<div class="cell ghost"></div>'; });

  grid.innerHTML = h;
  hideTip();

  const newest = d.runs[d.runs.length - 1];
  state.lastReceived = newest ? newest.received_at : null;
  renderLast();
}

function renderLast() {
  const el = document.getElementById('last');
  const d = state.data;
  if (!d || !d.runs.length) { el.textContent = 'No pushes yet'; return; }
  const newest = d.runs[d.runs.length - 1];
  const secs = Math.max(0, Math.round((Date.now() - new Date(state.lastReceived)) / 1000));
  const ago = secs < 60 ? secs + ' s ago' : secs < 3600 ? Math.round(secs / 60) + ' min ago' : fullTime(state.lastReceived);
  el.textContent = 'Last push ' + (newest.commit || '').slice(0, 7) + ', ' + ago + '. ' + d.total_runs + ' stored';
}

async function load() {
  const res = await fetch('/api/dashboard?limit=' + SLOTS, { cache: 'no-store' });
  state.data = await res.json();
  const runs = state.data.runs;
  const newest = runs.length ? runs[runs.length - 1].id : 0;
  const fresh = state.lastId !== null && newest > state.lastId;
  state.lastId = newest;
  render(fresh);
}

/* ---------- tooltip ---------- */
function showTip(cell) {
  const d = state.data; if (!d) return;
  const run = d.runs.find(r => String(r.id) === cell.dataset.run);
  const c = d.checks.find(x => x.name === cell.dataset.check);
  const r = run && run.results[cell.dataset.check];
  if (!run || !c || !r) return;
  const body = describe(c, run, r);
  tip.innerHTML = '<b>' + esc(c.label) + '</b><br>' + body.map(esc).join('<br>') +
    '<div class="m">' + esc(run.commit) + (run.branch ? ' on ' + esc(run.branch) : '') + '<br>' + esc(fullTime(run.ts)) + '</div>';
  tip.hidden = false;
  const b = cell.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  let x = b.left + b.width / 2 - w / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
  let y = b.bottom + 6;
  if (y + h > window.innerHeight - 8) y = b.top - h - 6;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { tip.hidden = true; }
grid.addEventListener('mouseover', e => { const c = e.target.closest('.cell[data-run]'); if (c) showTip(c); });
grid.addEventListener('mouseout',  e => { if (e.target.closest('.cell[data-run]')) hideTip(); });
grid.addEventListener('focusin',   e => { const c = e.target.closest('.cell[data-run]'); if (c) showTip(c); });
grid.addEventListener('focusout',  hideTip);

/* ---------- live updates ---------- */
function setConn(on) {
  document.getElementById('dot').className = 'dot ' + (on ? 'live' : 'retry');
  document.getElementById('conn').textContent = on ? 'Listening for pushes' : 'Reconnecting';
}
function connect() {
  const es = new EventSource('/events');
  es.addEventListener('hello', () => { setConn(true); load(); });
  es.addEventListener('run', load);
  es.addEventListener('checks', load);
  es.onerror = () => setConn(false);
}

/* ---------- demo controls ---------- */
const send = () => fetch('/api/simulate', { method: 'POST' });
document.getElementById('send').addEventListener('click', send);
let timer = null;
document.getElementById('auto').addEventListener('change', e => {
  clearInterval(timer);
  if (e.target.checked) { send(); timer = setInterval(send, 4000); }
});

/* ---------- legend ---------- */
const li = (icon, text) => '<li><span class="mini">' + icon + '</span>' + text + '</li>';
document.getElementById('lg-pf').innerHTML =
  li(svg('circle', 'good', 'check'), 'Pass') + li(svg('circle', 'bad', 'x'), 'Fail') + li(svg('circle', 'skip', 'dash'), 'Skipped');
document.getElementById('lg-dir').innerHTML =
  li(svg('up', 'neutral'), 'Increase') + li(svg('same', 'neutral'), 'Same, within the noise band') + li(svg('down', 'neutral'), 'Decrease') +
  li(svg('circle', 'none', 'dot'), 'No baseline yet');
document.getElementById('lg-col').innerHTML =
  li(svg('up', 'good'), 'Moved the good way') + li(svg('up', 'neutral'), 'Same') +
  li(svg('up', 'warn'), 'Moved the bad way, under the fail limit') + li(svg('up', 'bad'), 'Moved the bad way, at or over the limit');
document.getElementById('url').textContent = location.origin + '/api/ingest';

setInterval(renderLast, 5000);
load().then(connect);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------
def main():
    global STORE
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Quality-gate dashboard prototype")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.path.join(here, "ci_dashboard.db"), help="SQLite file, or :memory:")
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