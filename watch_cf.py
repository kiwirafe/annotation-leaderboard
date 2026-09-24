#!/usr/bin/env python3
"""Check multiple Codeforces users for new submissions and notify Discord."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CF_API = "https://codeforces.com/api/user.status"
CF_BASE = "https://codeforces.com"

DEFAULT_HANDLES = ["ngakanbagus18", "kavyasantha"]
DEFAULT_LOOKBACK_MINUTES = 10
DEFAULT_STATE_FILE = Path(".cf_state/cf_state.json")

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
FETCH_COUNT = 100
HTTP_TIMEOUT = 20


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE pairs from .env without an external dependency."""
    if not path.exists():
        return

    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[7:].lstrip()

        if "=" not in line:
            raise RuntimeError(
                f"Invalid .env line {line_number}: expected KEY=VALUE"
            )

        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise RuntimeError(f"Invalid .env line {line_number}: empty key")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def set_github_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "User-Agent": "CodeforcesDiscordWatcher/4.0",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error contacting {url}: {exc.reason}") from exc


def fetch_submissions(handle: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {"handle": handle, "from": 1, "count": FETCH_COUNT}
    )
    response = http_json(f"{CF_API}?{query}")

    if not isinstance(response, dict):
        raise RuntimeError(f"{handle}: unexpected response from Codeforces")
    if response.get("status") != "OK":
        raise RuntimeError(
            f"{handle}: Codeforces API error: "
            f"{response.get('comment', 'unknown error')}"
        )

    submissions = response.get("result")
    if not isinstance(submissions, list):
        raise RuntimeError(f"{handle}: invalid submission list")

    return submissions


def load_state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))

        # Current multi-user format.
        if isinstance(raw.get("last_seen"), dict):
            return {
                str(handle): int(submission_id)
                for handle, submission_id in raw["last_seen"].items()
            }

        # Backward compatibility with the old single-user state file.
        if "last_seen_submission_id" in raw:
            return {
                DEFAULT_HANDLES[0]: int(raw["last_seen_submission_id"])
            }

        raise ValueError("missing last_seen")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read state file {path}. "
            "Delete it to rebuild state from the lookback window."
        ) from exc


def save_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"last_seen": state}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    set_github_output("state_changed", "true")


def problem_name(submission: dict[str, Any]) -> str:
    problem = submission.get("problem") or {}
    return f"{problem.get('index', '?')}. {problem.get('name', 'Unknown problem')}"


def profile_url(handle: str) -> str:
    return f"{CF_BASE}/profile/{urllib.parse.quote(handle)}"


def problem_url(submission: dict[str, Any]) -> str | None:
    problem = submission.get("problem") or {}
    contest_id = problem.get("contestId", submission.get("contestId"))
    index = problem.get("index")

    if contest_id is None or index is None:
        return None

    return (
        f"{CF_BASE}/problemset/problem/"
        f"{contest_id}/{urllib.parse.quote(str(index))}"
    )


def verdict_text(submission: dict[str, Any]) -> str:
    verdict = submission.get("verdict")
    if not verdict:
        return "Testing"

    names = {
        "OK": "Accepted",
        "WRONG_ANSWER": "Wrong answer",
        "TIME_LIMIT_EXCEEDED": "Time limit exceeded",
        "MEMORY_LIMIT_EXCEEDED": "Memory limit exceeded",
        "RUNTIME_ERROR": "Runtime error",
        "COMPILATION_ERROR": "Compilation error",
        "IDLENESS_LIMIT_EXCEEDED": "Idleness limit exceeded",
        "SECURITY_VIOLATED": "Security violated",
        "CRASHED": "Crashed",
        "INPUT_PREPARATION_CRASHED": "Input preparation crashed",
        "CHALLENGED": "Challenged",
        "SKIPPED": "Skipped",
        "FAILED": "Failed",
        "PARTIAL": "Partial",
    }
    return names.get(str(verdict), str(verdict).replace("_", " ").title())


def submission_time_sydney(submission: dict[str, Any]) -> str:
    created = int(submission.get("creationTimeSeconds") or time.time())
    local = datetime.fromtimestamp(created, tz=SYDNEY_TZ)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%d %b %Y}, {hour}:{local:%M:%S %p %Z}"


def send_discord_notification(
    webhook_url: str,
    discord_user_id: str,
    handle: str,
    submission: dict[str, Any],
    *,
    test: bool = False,
) -> None:
    problem = problem_name(submission)
    p_url = problem_url(submission)

    title = f"{'[TEST] ' if test else ''}GET UP! {handle} MADE A SUBMISSION!"
    linked_handle = f"**[{handle}]({profile_url(handle)})**"
    problem_value = f"[{problem}]({p_url})" if p_url else problem

    payload = {
        "content": f"<@{discord_user_id}>",
        "allowed_mentions": {
            "parse": [],
            "users": [discord_user_id],
            "roles": [],
        },
        "embeds": [
            {
                "title": title,
                "description": f"{linked_handle} submitted **{problem}**.",
                "fields": [
                    {
                        "name": "Problem",
                        "value": problem_value,
                        "inline": False,
                    },
                    {
                        "name": "Verdict",
                        "value": verdict_text(submission),
                        "inline": True,
                    },
                    {
                        "name": "Language",
                        "value": str(
                            submission.get("programmingLanguage") or "Unknown"
                        ),
                        "inline": True,
                    },
                    {
                        "name": "Submission time",
                        "value": submission_time_sydney(submission),
                        "inline": True,
                    },
                ],
            }
        ],
    }

    http_json(webhook_url, method="POST", payload=payload)


def discord_config() -> tuple[str, str]:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    user_id = os.getenv("DISCORD_USER_ID", "")

    if not webhook_url.startswith(
        ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
    ):
        raise RuntimeError("DISCORD_WEBHOOK_URL is missing or invalid")

    if not user_id.isdigit():
        raise RuntimeError("DISCORD_USER_ID must be a numeric Discord user ID")

    return webhook_url, user_id


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc

    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")

    return number


def normalize_handles(values: list[str]) -> list[str]:
    """Accept space-separated handles and/or comma-separated groups."""
    handles: list[str] = []

    for value in values:
        for handle in value.split(","):
            handle = handle.strip()
            if handle and handle not in handles:
                handles.append(handle)

    if not handles:
        raise argparse.ArgumentTypeError("at least one handle is required")

    return handles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check multiple Codeforces users and notify Discord."
    )
    parser.add_argument(
        "--handles",
        nargs="+",
        default=DEFAULT_HANDLES,
        metavar="HANDLE",
        help=(
            "Codeforces handles to watch. "
            "Example: --handles ngakanbagus18 tourist Benq"
        ),
    )
    parser.add_argument(
        "--lookback-minutes",
        type=positive_int,
        default=DEFAULT_LOOKBACK_MINUTES,
        help="Fallback window used only for handles with no saved state.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="JSON file storing the last-seen submission ID for each handle.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send each watched user's latest submission as a test.",
    )

    args = parser.parse_args()
    args.handles = normalize_handles(args.handles)
    return args


def process_handle(
    handle: str,
    *,
    state: dict[str, int],
    state_file: Path,
    lookback_minutes: int,
    webhook_url: str,
    discord_user_id: str,
) -> int:
    submissions = fetch_submissions(handle)

    if not submissions:
        print(f"{handle}: no public submissions.")
        return 0

    last_seen = state.get(handle)

    if last_seen is None:
        cutoff = int(time.time()) - lookback_minutes * 60
        unseen = [
            submission
            for submission in submissions
            if int(submission.get("creationTimeSeconds", 0)) >= cutoff
        ]
        print(
            f"{handle}: no saved state; checking the last "
            f"{lookback_minutes} minute(s)."
        )
    else:
        unseen = [
            submission
            for submission in submissions
            if int(submission.get("id", 0)) > last_seen
        ]

    unseen.sort(key=lambda submission: int(submission["id"]))

    for submission in unseen:
        send_discord_notification(
            webhook_url,
            discord_user_id,
            handle,
            submission,
        )

        state[handle] = int(submission["id"])
        save_state(state_file, state)

        print(
            f"{handle}: notified {problem_name(submission)} — "
            f"{verdict_text(submission)} — "
            f"{submission_time_sydney(submission)}"
        )

    if not unseen and last_seen is None:
        # First time watching this handle: establish a baseline.
        state[handle] = max(int(submission["id"]) for submission in submissions)
        save_state(state_file, state)

    if not unseen:
        print(f"{handle}: no new submissions.")

    return len(unseen)


def main() -> int:
    load_dotenv()
    args = parse_args()
    webhook_url, discord_user_id = discord_config()
    set_github_output("state_changed", "false")

    state = load_state(args.state_file)

    if args.test:
        for handle in args.handles:
            submissions = fetch_submissions(handle)
            if not submissions:
                print(f"{handle}: no public submissions.")
                continue

            send_discord_notification(
                webhook_url,
                discord_user_id,
                handle,
                submissions[0],
                test=True,
            )
            print(f"{handle}: test notification sent.")

        return 0

    total = 0

    for handle in args.handles:
        total += process_handle(
            handle,
            state=state,
            state_file=args.state_file,
            lookback_minutes=args.lookback_minutes,
            webhook_url=webhook_url,
            discord_user_id=discord_user_id,
        )

    print(
        f"Done. Watched {len(args.handles)} user(s); "
        f"sent {total} notification(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise SystemExit(1)
