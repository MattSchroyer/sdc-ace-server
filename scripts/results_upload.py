#!/usr/bin/env python3
"""Monitor /data/results and send new JSON files to a webhook endpoint via POST request.

Polls the results directory every 5 seconds, reads new JSON files, and POST them to the
configured SERVER_RESULTS_POST_URL. Moves successfully uploaded files into a sibling
results_sent directory so they are not re-uploaded. Logs all errors to stderr without
skipping subsequent files.
"""

from __future__ import annotations

import errno
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
FAILED_RESULTS_DIR = RESULTS_DIR.parent / "results_failed"
LOG_FILE = Path(os.environ.get("SERVER_RESULTS_LOG_PATH", "/data/logs/results_upload.log"))
SERVER_RESULTS_POST_URL = os.environ.get("SERVER_RESULTS_POST_URL", "").strip()

_LOG_FILE_HANDLE = None
_ORIGINAL_STDOUT = None
_ORIGINAL_STDERR = None


def _configure_output_logging() -> None:
    """Redirect stdout and stderr to the configured log file."""
    global _LOG_FILE_HANDLE, _ORIGINAL_STDOUT, _ORIGINAL_STDERR

    if _LOG_FILE_HANDLE is not None:
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ORIGINAL_STDOUT = sys.stdout
    _ORIGINAL_STDERR = sys.stderr
    _LOG_FILE_HANDLE = LOG_FILE.open("a", encoding="utf-8")
    sys.stdout = _LOG_FILE_HANDLE
    sys.stderr = _LOG_FILE_HANDLE


def _restore_output_logging() -> None:
    """Restore stdout and stderr after logging is configured."""
    global _LOG_FILE_HANDLE

    if _LOG_FILE_HANDLE is None:
        return

    sys.stdout = _ORIGINAL_STDOUT
    sys.stderr = _ORIGINAL_STDERR
    _LOG_FILE_HANDLE.flush()
    _LOG_FILE_HANDLE.close()
    _LOG_FILE_HANDLE = None


def _move_result_to_directory(file_path: Path, destination_dir: Path, label: str, action_phrase: str) -> bool:
    """Move a result file into a destination directory and log the outcome."""
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        archive_path = destination_dir / file_path.name

        if archive_path.exists():
            if archive_path.is_dir():
                raise IsADirectoryError(f"Cannot replace directory target {archive_path}")
            archive_path.unlink()

        os.replace(file_path, archive_path)
        print(f"{label}: {file_path.name} -> {archive_path}", file=sys.stderr)
        return True
    except FileNotFoundError:
        print(
            f"WARNING: Failed to {action_phrase} {file_path.name}: source file disappeared before move",
            file=sys.stderr,
        )
        return False
    except OSError as e:
        if getattr(e, "errno", None) == errno.EXDEV:
            try:
                shutil.copy2(file_path, archive_path)
                file_path.unlink()
                print(f"{label}: {file_path.name} -> {archive_path} (copied across filesystems)", file=sys.stderr)
                return True
            except OSError as copy_error:
                print(
                    f"WARNING: Failed to {action_phrase} {file_path.name} across filesystems: {copy_error}",
                    file=sys.stderr,
                )
                return False

        print(
            f"WARNING: Failed to {action_phrase} {file_path.name} -> {archive_path}: {e}",
            file=sys.stderr,
        )
        return False
    except Exception as e:
        print(
            f"WARNING: Failed to {action_phrase} {file_path.name} -> {archive_path}: {e}",
            file=sys.stderr,
        )
        return False


def _archive_result(file_path: Path) -> bool:
    """Move a successfully uploaded result file into the sent-results directory."""
    return _move_result_to_directory(file_path, SENT_RESULTS_DIR, "Archived", "archive sent result")


def _move_failed_result(file_path: Path) -> bool:
    """Move a failed upload result file into the failed-results directory."""
    return _move_result_to_directory(file_path, FAILED_RESULTS_DIR, "Moved to failed", "move failed result")


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
        f"[UPLOADER] Results uploader started: monitoring {RESULTS_DIR} -> {webhook_url} (archived to {SENT_RESULTS_DIR})",
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
                else:
                    _move_failed_result(file_path)

        except KeyboardInterrupt:
            print("Results uploader stopped.", file=sys.stderr)
            return
        except Exception as e:
            print(
                f"ERROR: Poll cycle failed: {e}",
                file=sys.stderr,
            )

        try:
            # Wait before next poll
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("Results uploader stopped.", file=sys.stderr)
            return


def main() -> None:
    """Main entry point."""
    _configure_output_logging()

    if not SERVER_RESULTS_POST_URL:
        print(
            "WARNING: SERVER_RESULTS_POST_URL not set; results uploader is disabled.",
            file=sys.stderr,
        )
        _restore_output_logging()
        sys.exit(0)

    try:
        _poll_results_directory(SERVER_RESULTS_POST_URL)
    except KeyboardInterrupt:
        print("Results uploader stopped.", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        _restore_output_logging()


if __name__ == "__main__":
    main()
