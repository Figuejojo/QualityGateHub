#!/usr/bin/env python3
"""Post a JSON test payload to the quality-gate dashboard."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_URL = "http://127.0.0.1:8080/api/ingest"


def main():
    parser = argparse.ArgumentParser(description="Post dashboard test data from a JSON file")
    parser.add_argument("json_file", help="path to the JSON payload file")
    parser.add_argument("--url", default=DEFAULT_URL, help="ingest URL (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10, help="request timeout in seconds (default: %(default)s)")
    args = parser.parse_args()

    try:
        with open(args.json_file, "r", encoding="utf-8") as payload_file:
            payload = json.load(payload_file)
        if not isinstance(payload, dict):
            raise ValueError("the JSON file must contain an object")

        request = Request(
            args.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=args.timeout) as response:
            body = response.read().decode("utf-8")
            print("HTTP %d" % response.status)
            if body:
                try:
                    print(json.dumps(json.loads(body), indent=2))
                except json.JSONDecodeError:
                    print(body)
    except (OSError, ValueError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print("HTTP %d: %s" % (exc.code, detail), file=sys.stderr)
        return 1
    except URLError as exc:
        print("Could not connect to %s: %s" % (args.url, exc.reason), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())