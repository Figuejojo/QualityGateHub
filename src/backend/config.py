"""Application configuration."""

import argparse
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db: str
    keep: int
    no_seed: bool
    reset: bool


def parse_config(argv=None):
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    parser = argparse.ArgumentParser(description="Quality-gate dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 = this machine only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=os.path.join(project_root, "ci_dashboard.db"), help="SQLite file, or :memory:")
    parser.add_argument("--keep", type=int, default=500, help="pushes to retain in the DB (board shows the last 15)")
    parser.add_argument("--no-seed", action="store_true", help="do not pre-fill 15 demo pushes into an empty DB")
    parser.add_argument("--reset", action="store_true", help="delete the DB file before starting")
    args = parser.parse_args(argv)
    return Config(args.host, args.port, args.db, args.keep, args.no_seed, args.reset)
