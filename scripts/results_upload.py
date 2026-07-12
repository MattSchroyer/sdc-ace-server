#!/usr/bin/env python3
"""Monitor /data/results and send new JSON files to a webhook endpoint via POST request.

Polls the results directory every 5 seconds, reads new JSON files, and POST them to the
configured SERVER_RESULTS_POST_URL. Moves successfully uploaded files into a sibling
results_sent directory so they are not re-uploaded. Logs all errors to stderr without
skipping subsequent files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


POLL_INTERVAL_SECONDS = 5
RESULTS_DIR = Path(os.environ.get("SERVER_RESULTS_PATH", "/data/results"))
SENT_RESULTS_DIR = RESULTS_DIR.parent / "results_sent"
SERVER_RESULTS_POST_URL = os.environ.get("SERVER_RESULTS_POST_URL", "").strip()


def _archive_result(file_path: Path) -> bool:
    """Move a successfully uploaded result file into the sent-results directory."""
    try:
        SENT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = SENT_RESULTS_DIR / file_path.name
        if archive_path.exists():
            archive_path.unlink()
        shutil.move(str(file_path), str(archive_path))
        return True
    except Exception as e:
        print(f"WARNING: Failed to archive sent result {file_path.name}: {e}", file=sys.stderr)
        return False


def _send_result(url: str, file_path: Path) -> bool:
    """Send a result JSON file to the webhook via POST request.

    Args:
        url: The POST endpoint URL.
        file_path: Path to the JSON file to send.

    Returns:
        True if successful (2xx response), False otherwise.
    """
    try:
        # Read and parse the JSON file, then send it as a JSON-encoded request body.
        with open(file_path, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        json_body = json.dumps(json_data).encode("utf-8")

        # Prepare the POST request
        req = urllib.request.Request(
            url,
            data=json_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "User-Agent": "curl/8.7.0",
            },
        )

        print(f"Uploading {file_path.name} -> {url} ...", file=sys.stderr)

        # Send the request
        with urllib.request.urlopen(req, timeout=10) as response:
            status_code = response.status
            response_body = response.read().decode("utf-8")
            
            print(f"HTTP {status_code} response: {response_body}", file=sys.stderr)

            if 200 <= status_code < 300:
                print(f"SUCCESS: {file_path.name} -> {url} (HTTP {status_code})")
                if response_body:
                    print(f"Response body: {response_body}")
                return True
            else:
                print(
                    f"ERROR: {file_path.name} returned HTTP {status_code}",
                    file=sys.stderr,
                )
                if response_body:
                    print(f"Response body: {response_body}", file=sys.stderr)
                return False

    except json.JSONDecodeError as e:
        print(
            f"ERROR: {file_path.name} is not valid JSON: {e}",
            file=sys.stderr,
        )
        return False
    except urllib.error.HTTPError as e:
        try:
            response_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            response_body = ""

        headers = dict(e.headers.items())
        print(
            f"ERROR: {file_path.name} -> {url} HTTP {e.code}: {e.reason}",
            file=sys.stderr,
        )
        print(f"Response headers: {headers}", file=sys.stderr)
        if response_body:
            print(f"Response body: {response_body}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(
            f"ERROR: {file_path.name} -> {url} connection failed: {e.reason}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(
            f"ERROR: {file_path.name} upload failed: {e}",
            file=sys.stderr,
        )
        return False


def _poll_results_directory(webhook_url: str) -> None:
    """Continuously poll the results directory for new JSON files and upload them.

    Args:
        webhook_url: The POST endpoint URL to send results to.
    """
    print(
        f"Results uploader started: monitoring {RESULTS_DIR} -> {webhook_url} (archived to {SENT_RESULTS_DIR})",
        file=sys.stderr,
    )

    while True:
        try:
            # Ensure results directory exists
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)

            # Scan for JSON files
            json_files = sorted(RESULTS_DIR.glob("*.json"))

            for file_path in json_files:
                # Try to send; log errors but continue
                if _send_result(webhook_url, file_path):
                    _archive_result(file_path)

        except Exception as e:
            print(
                f"ERROR: Poll cycle failed: {e}",
                file=sys.stderr,
            )

        # Wait before next poll
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    """Main entry point."""
    if not SERVER_RESULTS_POST_URL:
        print(
            "WARNING: SERVER_RESULTS_POST_URL not set; results uploader is disabled.",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        _poll_results_directory(SERVER_RESULTS_POST_URL)
    except KeyboardInterrupt:
        print("Results uploader stopped.", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
