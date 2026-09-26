#!/usr/bin/env python3
"""Generate a self-contained HTML leaderboard for Temporal NoRA annotation progress.

The dashboard focuses on four human contribution views:
  1. Most canonical-action annotations overall
  2. Most fully annotated clips, measured against a configurable per-person target
  3. Most canonical-action annotations added during the current AEST day
  4. Most human action proposals

Output/history behaviour:
  - The current dashboard is overwritten on every run.
  - Every run also writes one compact JSON progress snapshot. Its filename contains
    the AEST hour, while its body contains ONLY the three per-person counters needed
    for history: actions annotated, clips fully annotated, and proposals.
  - "Actions annotated today" is calculated from the earliest saved progress JSON
    snapshot for the current AEST date. This does not depend on annotation timestamps,
    which are not present in the current export structure.

Counting rules:
  - One annotation = one canonical action with complete judgments for all five expected
    horizons. For each horizon:
      * action_judgment == "yes" requires reason_judgment to be present and not "unset";
      * action_judgment == "no" requires rejection_reasons to be non-empty.
    Individual horizon judgments are not separate annotations.
  - A clip is fully annotated by a person only when every action in that clip's
    canonical_action_pool has a complete judgment for all five expected horizons:
    H10, H60, H30M, H1H, H3H.
  - A proposal counts only when it has a non-empty reason and at least one associated fact_id.
  - Known model annotators and explicit exclusions are excluded from all human rankings.

The script can download /api/export using the same .env credentials as the
Temporal NoRA annotation pipeline, or analyse an existing JSON/JSONL export.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = "https://annotation.rainsproj.com"
DEFAULT_ANNOTATOR = "VLM Annotator"
DEFAULT_EXPORT = Path("inputs/annotation_export.jsonl")
DEFAULT_OUTPUT = Path("reports/annotation_leaderboard.html")
DEFAULT_TARGET_CLIPS = 10

# The user explicitly requested AEST. AEST is fixed UTC+10; it does not move to AEDT.
AEST = dt.timezone(dt.timedelta(hours=10), name="AEST")

REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
}

KNOWN_AI_ANNOTATORS = {
    "Claude-Opus-5",
    "GPT-5.6-Sol",
}

# Excluded from every statistic, leaderboard, target, and daily delta.
DEFAULT_EXCLUDED_CLIP_IDS = {
    "API-Test-Clip190",
}
DEFAULT_EXCLUDED_ANNOTATORS = {
    "Ning",
    "API-Test-Clip190",
}
AI_NAME_HINTS = (
    "gpt",
    "claude",
    "openai",
    "anthropic",
    "gemini",
    "llama",
    "qwen",
    "deepseek",
    "mistral",
)

EXPECTED_HORIZONS = ("H10", "H60", "H30M", "H1H", "H3H")
HORIZON_LABELS = {
    "H10": "0–10 seconds",
    "H60": "10 seconds–1 minute",
    "H30M": "1–30 minutes",
    "H1H": "30 minutes–1 hour",
    "H3H": "1–3 hours",
}
PROGRESS_JSON_PREFIX = "annotation_progress"


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def load_simple_dotenv(path: Path) -> None:
    """Load ordinary KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_project_env() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate not in seen:
            seen.add(candidate)
            load_simple_dotenv(candidate)


def make_authenticated_opener(
    base_url: str,
    annotator: str,
) -> urllib.request.OpenerDirector:
    site_password = os.getenv("ANNOTATION_SITE_PASSWORD")
    admin_password = os.getenv("ANNOTATION_ADMIN_PASSWORD")
    if not site_password or not admin_password:
        raise RuntimeError(
            "ANNOTATION_SITE_PASSWORD and ANNOTATION_ADMIN_PASSWORD must be set "
            "in the environment or a .env file"
        )

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def post_json(path: str, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={**REQUEST_HEADERS, "Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=90) as response:
            response.read()

    post_json(
        "/api/auth/login",
        {"annotatorName": annotator, "password": site_password},
    )
    post_json("/api/admin/login", {"password": admin_password})
    return opener


def download_export(args: argparse.Namespace) -> Path:
    output_path: Path = args.export_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    print("\n=== DOWNLOAD EXPORT ===")
    print(f"GET {args.url.rstrip('/')}/api/export")
    print(f"-> {output_path}")

    opener = make_authenticated_opener(args.url, args.annotator)
    request = urllib.request.Request(
        args.url.rstrip("/") + "/api/export",
        headers=REQUEST_HEADERS,
        method="GET",
    )

    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        with opener.open(request, timeout=180) as response, temp_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if temp_path.stat().st_size == 0:
            raise ValueError("Website export was empty")
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)

    print(f"Downloaded {output_path.stat().st_size:,} bytes")
    return output_path


def load_export_records(path: Path) -> list[dict[str, Any]]:
    """Read JSONL, one JSON object, or a JSON array of objects."""
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError(f"Export is empty: {path}")

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"JSONL line {line_number} is not an object")
            records.append(item)
        return records

    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise ValueError("Export must be a JSON object, array of objects, or JSONL")


def normalize_name(value: Any) -> str:
    return str(value or "Unknown").strip() or "Unknown"


def is_ai_annotator(name: str, explicit_ai_names: set[str]) -> bool:
    if name in explicit_ai_names:
        return True
    folded = name.casefold()
    return any(hint in folded for hint in AI_NAME_HINTS)


def is_excluded(value: str, excluded_values: set[str]) -> bool:
    """Case-insensitive exact-match exclusion."""
    folded = value.casefold()
    return any(folded == excluded.casefold() for excluded in excluded_values)


def horizon_judgment_is_complete(judgment: Any) -> bool:
    """Return True only when an action judgment has its required follow-up data.

    Rules:
      - yes -> reason_judgment must be present and not "unset"
      - no  -> rejection_reasons must contain at least one non-empty reason
    """
    if not isinstance(judgment, dict):
        return False

    action_judgment = str(judgment.get("action_judgment") or "").strip().casefold()
    if action_judgment == "yes":
        reason_judgment = str(judgment.get("reason_judgment") or "").strip().casefold()
        return bool(reason_judgment) and reason_judgment != "unset"

    if action_judgment == "no":
        rejection_reasons = judgment.get("rejection_reasons")
        if not isinstance(rejection_reasons, (list, tuple, set)):
            return False
        return any(str(reason).strip() for reason in rejection_reasons)

    return False


def action_is_started(action: dict[str, Any]) -> bool:
    """Return True when at least one horizon has an actual yes/no judgment."""
    judgments = action.get("horizon_judgments") or {}
    if not isinstance(judgments, dict):
        return False

    return any(
        str((judgment or {}).get("action_judgment") or "").strip().casefold()
        in {"yes", "no"}
        for judgment in judgments.values()
        if isinstance(judgment, dict)
    )


def action_is_complete(action: dict[str, Any]) -> bool:
    """Return True when all five expected horizons have complete judgments."""
    judgments = action.get("horizon_judgments") or {}
    if not isinstance(judgments, dict):
        return False
    return all(
        horizon_judgment_is_complete(judgments.get(horizon))
        for horizon in EXPECTED_HORIZONS
    )


def clip_is_fully_annotated(
    canonical_pool: list[Any],
    annotation: dict[str, Any],
) -> bool:
    """Return True when every canonical action is complete for this annotator."""
    pool_ids = {
        str(item.get("canonical_action_id")).strip()
        for item in canonical_pool
        if isinstance(item, dict) and item.get("canonical_action_id") is not None
    }
    pool_ids.discard("")
    if not pool_ids:
        return False

    action_map = {
        str(action.get("canonical_action_id")).strip(): action
        for action in (annotation.get("canonical_actions") or [])
        if isinstance(action, dict) and action.get("canonical_action_id") is not None
    }
    return all(
        canonical_id in action_map and action_is_complete(action_map[canonical_id])
        for canonical_id in pool_ids
    )

def proposal_is_complete(proposal: dict[str, Any]) -> bool:
    """Return True only when a proposal has a reason and at least one associated fact."""
    reason = str(proposal.get("reason") or "").strip()
    fact_ids = proposal.get("fact_ids")

    return (
        bool(reason)
        and isinstance(fact_ids, (list, tuple, set))
        and any(str(fact_id).strip() for fact_id in fact_ids)
    )


def audit_action_text(item: dict[str, Any]) -> str:
    for key in (
        "canonical_action",
        "action",
        "action_text",
        "text",
        "description",
        "representative_action",
        "canonical_text",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(action text unavailable)"


def audit_canonical_id(item: dict[str, Any]) -> str:
    value = item.get("canonical_action_id")
    return str(value).strip() if value is not None else ""


def audit_judgment(action: dict[str, Any] | None, horizon: str) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    judgments = action.get("horizon_judgments")
    if not isinstance(judgments, dict):
        return None
    value = judgments.get(horizon)
    return value if isinstance(value, dict) else None


def audit_judgment_state(judgment: dict[str, Any] | None) -> str:
    if not isinstance(judgment, dict):
        return "unset"
    value = str(judgment.get("action_judgment") or "").strip().casefold()
    return value if value in {"yes", "no"} else "unset"


def audit_missing_required_detail(judgment: dict[str, Any] | None) -> bool:
    state = audit_judgment_state(judgment)

    if state == "yes":
        reason = str((judgment or {}).get("reason_judgment") or "").strip().casefold()
        return not reason or reason == "unset"

    if state == "no":
        reasons = (judgment or {}).get("rejection_reasons")
        if not isinstance(reasons, (list, tuple, set)):
            return True
        return not any(str(reason).strip() for reason in reasons)

    return False


def compress_audit_findings(
    row_number: int,
    pool: list[dict[str, Any]],
    affected: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    if not pool or not affected:
        return []

    all_cells = {
        (ca_number, horizon)
        for ca_number in range(1, len(pool) + 1)
        for horizon in EXPECTED_HORIZONS
    }

    if affected == all_cells:
        return [{
            "row": row_number,
            "ca": "ALL",
            "action": "ALL",
            "band": "ALL",
        }]

    findings: list[dict[str, Any]] = []

    for ca_number, pool_item in enumerate(pool, start=1):
        affected_horizons = [
            horizon
            for horizon in EXPECTED_HORIZONS
            if (ca_number, horizon) in affected
        ]
        if not affected_horizons:
            continue

        action = audit_action_text(pool_item)

        if len(affected_horizons) == len(EXPECTED_HORIZONS):
            findings.append({
                "row": row_number,
                "ca": f"CA{ca_number}",
                "action": action,
                "band": "ALL",
            })
            continue

        for horizon in affected_horizons:
            findings.append({
                "row": row_number,
                "ca": f"CA{ca_number}",
                "action": action,
                "band": HORIZON_LABELS.get(horizon, horizon),
            })

    return findings


def analyse_missing_details(
    records: list[dict[str, Any]],
    *,
    start_row: int,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> dict[str, Any]:
    by_person: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    for original_index, record in enumerate(records, start=start_row):
        row_number = original_index + 1
        clip_id = normalize_name(record.get("clip_id"))

        if is_excluded(clip_id, excluded_clip_ids):
            continue

        pool_raw = record.get("canonical_action_pool") or []
        pool = [
            item for item in pool_raw if isinstance(item, dict)
        ] if isinstance(pool_raw, list) else []

        if not pool:
            continue

        for annotation in record.get("annotations") or []:
            if not isinstance(annotation, dict):
                continue

            name = normalize_name(annotation.get("annotator_id"))

            if is_excluded(name, excluded_annotators):
                continue
            if is_ai_annotator(name, explicit_ai_names):
                continue

            actions = annotation.get("canonical_actions") or []
            action_map = {
                audit_canonical_id(action): action
                for action in actions
                if isinstance(action, dict) and audit_canonical_id(action)
            } if isinstance(actions, list) else {}

            affected: set[tuple[int, str]] = set()

            for ca_number, pool_item in enumerate(pool, start=1):
                cid = audit_canonical_id(pool_item)
                action = action_map.get(cid) if cid else None

                for horizon in EXPECTED_HORIZONS:
                    judgment = audit_judgment(action, horizon)
                    state = audit_judgment_state(judgment)

                    if state in {"yes", "no"} and audit_missing_required_detail(judgment):
                        affected.add((ca_number, horizon))

            by_person[name].extend(
                compress_audit_findings(
                    row_number=row_number,
                    pool=pool,
                    affected=affected,
                )
            )

    issue_people = {
        name: findings
        for name, findings in sorted(
            by_person.items(),
            key=lambda item: item[0].casefold(),
        )
        if findings
    }

    return {
        "by_person": issue_people,
        "total_findings": sum(len(findings) for findings in issue_people.values()),
        "people_affected": len(issue_people),
    }



def parse_number_ranges(value: str) -> set[int]:
    """Parse clip numbers/ranges such as ``1-30, 38, 50-70``.

    Clip numbers are the 1-based row numbers shown in the proposal-details table.
    Whitespace is ignored. Overlapping ranges and duplicate numbers are harmless.
    """
    value = str(value or "").strip()
    if not value:
        return set()

    numbers: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue

        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if start <= 0 or end <= 0:
                raise ValueError("clip numbers must be >= 1")
            if start > end:
                raise ValueError(f"invalid clip range {part!r}: start is greater than end")
            numbers.update(range(start, end + 1))
            continue

        if re.fullmatch(r"\d+", part):
            number = int(part)
            if number <= 0:
                raise ValueError("clip numbers must be >= 1")
            numbers.add(number)
            continue

        raise ValueError(
            f"invalid clip/range {part!r}; expected values like '1-30, 38, 50-70'"
        )

    return numbers


def compress_number_ranges(numbers: list[int]) -> str:
    numbers = sorted(set(numbers))
    if not numbers:
        return ""

    ranges: list[str] = []
    start = previous = numbers[0]

    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(
            str(start) if start == previous else f"{start}-{previous}"
        )
        start = previous = number

    ranges.append(
        str(start) if start == previous else f"{start}-{previous}"
    )
    return ", ".join(ranges)


def analyse_proposal_details(
    records: list[dict[str, Any]],
    *,
    start_row: int,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
    ignored_incomplete_clip_numbers: set[int],
) -> dict[str, Any]:
    by_person: dict[str, dict[int, list[dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )

    for original_index, record in enumerate(records, start=start_row):
        row_number = original_index + 1
        clip_id = normalize_name(record.get("clip_id"))

        if is_excluded(clip_id, excluded_clip_ids):
            continue

        proposals = record.get("proposals") or []
        if not isinstance(proposals, list):
            continue

        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue

            name = normalize_name(proposal.get("annotator_id"))

            if is_excluded(name, excluded_annotators):
                continue
            if is_ai_annotator(name, explicit_ai_names):
                continue

            by_person[name][row_number].append(proposal)

    rows: list[dict[str, Any]] = []

    for name, clip_map in by_person.items():
        clip_numbers = sorted(clip_map)
        incomplete_clips = sorted(
            row_number
            for row_number, proposals in clip_map.items()
            if row_number not in ignored_incomplete_clip_numbers
            and (
                len(proposals) < 2
                or any(not proposal_is_complete(proposal) for proposal in proposals)
            )
        )

        action_count = sum(len(proposals) for proposals in clip_map.values())
        clip_count = len(clip_numbers)
        incomplete_count = len(incomplete_clips)

        # Ignored clips stay in the denominator and are treated as completed.
        completed_count = clip_count - incomplete_count
        percentage = (
            100.0 * completed_count / clip_count
            if clip_count else 0.0
        )

        rows.append({
            "name": name,
            "actions": action_count,
            "clips": clip_count,
            "incomplete": incomplete_count,
            "clip_ranges": compress_number_ranges(clip_numbers),
            "incomplete_ranges": compress_number_ranges(incomplete_clips),
            "percentage": percentage,
        })

    rows.sort(key=lambda row: (-row["actions"], row["name"].casefold()))

    return {
        "rows": rows,
        "proposers": len(rows),
        "incomplete_clips": sum(row["incomplete"] for row in rows),
        "ignored_incomplete_clip_numbers": sorted(ignored_incomplete_clip_numbers),
    }



def rank_counts(
    counts: collections.Counter[str] | dict[str, int],
    *,
    include_zero_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Competition ranking: 1, 2, 2, 4 for ties."""
    names = set(counts)
    if include_zero_names:
        names.update(include_zero_names)

    ordered = sorted(
        ((name, int(counts.get(name, 0))) for name in names),
        key=lambda item: (-item[1], item[0].casefold()),
    )

    rows: list[dict[str, Any]] = []
    previous_count: int | None = None
    previous_rank = 0
    for position, (name, count) in enumerate(ordered, start=1):
        if count != previous_count:
            previous_rank = position
            previous_count = count
        rows.append({"rank": previous_rank, "name": name, "count": count})
    return rows


def analyse(
    records: list[dict[str, Any]],
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> dict[str, Any]:
    annotation_counts: collections.Counter[str] = collections.Counter()
    started_action_counts: collections.Counter[str] = collections.Counter()
    proposal_counts: collections.Counter[str] = collections.Counter()
    full_clip_sets: dict[str, set[str]] = collections.defaultdict(set)

    human_names: set[str] = set()
    ai_names: set[str] = set()
    for record_index, record in enumerate(records):
        clip_id = normalize_name(record.get("clip_id"))
        if clip_id == "Unknown":
            clip_id = f"record_{record_index}"

        if is_excluded(clip_id, excluded_clip_ids):
            continue

        canonical_pool = record.get("canonical_action_pool") or []
        if not isinstance(canonical_pool, list):
            canonical_pool = []

        for proposal in record.get("proposals") or []:
            if not isinstance(proposal, dict):
                continue
            name = normalize_name(proposal.get("annotator_id"))
            if is_excluded(name, excluded_annotators):
                continue
            if is_ai_annotator(name, explicit_ai_names):
                ai_names.add(name)
                continue
            human_names.add(name)
            if proposal_is_complete(proposal):
                proposal_counts[name] += 1

        for annotation in record.get("annotations") or []:
            if not isinstance(annotation, dict):
                continue
            name = normalize_name(annotation.get("annotator_id"))
            if is_excluded(name, excluded_annotators):
                continue
            if is_ai_annotator(name, explicit_ai_names):
                ai_names.add(name)
                continue
            human_names.add(name)

            actions = annotation.get("canonical_actions") or []
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    if action_is_started(action):
                        started_action_counts[name] += 1
                    if action_is_complete(action):
                        annotation_counts[name] += 1

            if clip_is_fully_annotated(canonical_pool, annotation):
                full_clip_sets[name].add(clip_id)

    full_clip_counts = collections.Counter(
        {name: len(clip_ids) for name, clip_ids in full_clip_sets.items()}
    )

    return {
        "annotation_counts": dict(annotation_counts),
        "started_action_counts": dict(started_action_counts),
        "proposal_counts": dict(proposal_counts),
        "full_clip_counts": dict(full_clip_counts),
        "annotation_ranking": rank_counts(annotation_counts, include_zero_names=human_names),
        "full_clip_ranking": rank_counts(full_clip_counts, include_zero_names=human_names),
        "proposal_ranking": rank_counts(proposal_counts, include_zero_names=human_names),
        "summary": {
            "human_contributors": len(human_names),
            "total_annotations": sum(annotation_counts.values()),
            "total_proposals": sum(proposal_counts.values()),
            "full_clip_completions": sum(full_clip_counts.values()),
        },
        "human_names": sorted(human_names),
        "ai_names": sorted(ai_names),
        "excluded_clip_ids": sorted(excluded_clip_ids),
        "excluded_annotators": sorted(excluded_annotators),
    }


def make_today_counts(
    current_counts: dict[str, int],
    baseline_counts: dict[str, int],
    human_names: set[str],
) -> dict[str, int]:
    """Return net-new annotations for currently included human contributors only.

    Restricting to ``human_names`` prevents names present only in older progress JSON
    snapshots (for example a later-excluded test annotator) from leaking back into the
    current-day ranking.
    """
    return {
        name: max(0, int(current_counts.get(name, 0)) - int(baseline_counts.get(name, 0)))
        for name in human_names
    }


def make_progress_json_payload(stats: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Build the intentionally minimal progress JSON payload.

    The filename carries the timestamp. The JSON body contains only the three
    requested counters for each included human contributor.
    """
    names = sorted(set(stats["human_names"]), key=str.casefold)
    annotation_counts = stats["annotation_counts"]
    full_clip_counts = stats["full_clip_counts"]
    proposal_counts = stats["proposal_counts"]
    return {
        name: {
            "actions_annotated": int(annotation_counts.get(name, 0)),
            "clips_fully_annotated": int(full_clip_counts.get(name, 0)),
            "proposals": int(proposal_counts.get(name, 0)),
        }
        for name in names
    }


def progress_json_snapshot_path(progress_json_dir: Path, when_aest: dt.datetime) -> Path:
    """Return one deterministic snapshot path per AEST clock hour."""
    hour = when_aest.replace(minute=0, second=0, microsecond=0)
    day_dir = progress_json_dir / hour.date().isoformat()
    filename = f"{PROGRESS_JSON_PREFIX}_{hour:%Y-%m-%d_%H00}_AEST.json"
    return day_dir / filename


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(path, text)


def load_progress_json(path: Path) -> dict[str, dict[str, int]] | None:
    """Load and validate the compact three-counter progress JSON format."""
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None

    parsed: dict[str, dict[str, int]] = {}
    required = {"actions_annotated", "clips_fully_annotated", "proposals"}
    for raw_name, raw_metrics in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_metrics, dict):
            return None
        if set(raw_metrics) != required:
            return None
        try:
            parsed[raw_name] = {
                key: int(raw_metrics[key])
                for key in ("actions_annotated", "clips_fully_annotated", "proposals")
            }
        except (TypeError, ValueError, KeyError):
            return None
    return parsed


def find_day_baseline(
    progress_json_dir: Path,
    aest_date: dt.date,
) -> tuple[Path, dict[str, dict[str, int]]] | None:
    """Return the earliest valid progress JSON snapshot saved for an AEST date."""
    day_dir = progress_json_dir / aest_date.isoformat()
    if not day_dir.is_dir():
        return None

    pattern = f"{PROGRESS_JSON_PREFIX}_{aest_date.isoformat()}_*_AEST.json"
    for path in sorted(day_dir.glob(pattern), key=lambda item: item.name):
        payload = load_progress_json(path)
        if payload is not None:
            return path, payload
    return None


def counts_from_progress_json(
    payload: dict[str, dict[str, int]],
    metric: str,
) -> dict[str, int]:
    return {
        name: int(metrics.get(metric, 0))
        for name, metrics in payload.items()
    }


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def medal_for_rank(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "")


def top_person(rows: list[dict[str, Any]]) -> tuple[str, int] | None:
    if not rows:
        return None
    count = int(rows[0]["count"])
    if count <= 0:
        return None
    return str(rows[0]["name"]), count


def render_combined_activity_card(
    title: str,
    subtitle: str,
    total_rows: list[dict[str, Any]],
    today_rows: list[dict[str, Any]],
    noun: str,
    accent_class: str,
    icon: str,
) -> str:
    today_by_name = {
        str(row["name"]): int(row["count"])
        for row in today_rows
    }

    top = top_person(total_rows)
    if top:
        top_html = (
            '<div class="leader-strip"><span class="leader-crown">👑</span>'
            f'<span><b>{esc(top[0])}</b> leads overall with '
            f'<b>{fmt_int(top[1])}</b> {esc(noun)}.</span></div>'
        )
    else:
        top_html = '<div class="leader-strip muted-strip">No activity recorded yet.</div>'

    bars = []
    table_rows = []
    max_count = max((int(row["count"]) for row in total_rows), default=0) or 1

    for row in total_rows:
        rank = int(row["rank"])
        name = str(row["name"])
        total = int(row["count"])
        today = int(today_by_name.get(name, 0))
        width = 100.0 * total / max_count

        bars.append(
            '<div class="bar-row combined-bar-row">'
            f'<div class="bar-person" title="{esc(name)}">'
            f'<span class="bar-medal">{medal_for_rank(rank)}</span>'
            f'{esc(name)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width:.2f}%"></div>'
            '</div>'
            f'<div class="bar-count combined-bar-count">'
            f'<b>{fmt_int(total)}</b>'
            f'<span class="today-inline">+{fmt_int(today)}</span>'
            '</div>'
            '</div>'
        )

        table_rows.append(
            '<tr>'
            f'<td class="rank-cell"><span class="rank-number">{rank}</span>'
            f'<span class="medal">{medal_for_rank(rank)}</span></td>'
            f'<td><span class="person-name">{esc(name)}</span></td>'
            f'<td class="numeric combined-total">{fmt_int(total)}</td>'
            f'<td class="numeric today-value">+{fmt_int(today)}</td>'
            '</tr>'
        )

    table = (
        '<div class="empty">No recorded activity.</div>'
        if not table_rows
        else (
            '<div class="table-wrap"><table class="combined-table">'
            '<thead><tr><th>Rank</th><th>Person</th><th>Total</th><th>Today</th></tr></thead>'
            f'<tbody>{"".join(table_rows)}</tbody></table></div>'
        )
    )

    return f"""
    <section class="panel leaderboard-panel {esc(accent_class)}">
      <div class="panel-heading">
        <div class="panel-icon">{icon}</div>
        <div>
          <h2>{esc(title)}</h2>
          <p>{esc(subtitle)}</p>
        </div>
      </div>
      {top_html}
      <div class="bars">{"".join(bars[:12])}</div>
      <h3>Full ranking</h3>
      {table}
    </section>
    """


def render_completion_quality_card(stats: dict[str, Any]) -> str:
    rows = []
    for name in stats["human_names"]:
        started = int(stats["started_action_counts"].get(name, 0))
        completed = int(stats["annotation_counts"].get(name, 0))
        rate = (100.0 * completed / started) if started else 0.0
        rows.append(
            {
                "name": name,
                "started": started,
                "completed": completed,
                "rate": rate,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["rate"],
            -row["completed"],
            row["name"].casefold(),
        )
    )

    ranked_rows = []
    previous_rate: float | None = None
    previous_rank = 0
    for position, row in enumerate(rows, start=1):
        if previous_rate is None or abs(row["rate"] - previous_rate) > 1e-9:
            previous_rank = position
            previous_rate = row["rate"]
        ranked_rows.append({**row, "rank": previous_rank})

    if ranked_rows and ranked_rows[0]["started"] > 0:
        leader = ranked_rows[0]
        top_html = (
            '<div class="leader-strip"><span class="leader-crown">✨</span>'
            f'<span><b>{esc(leader["name"])}</b> has the highest completion quality at '
            f'<b>{leader["rate"]:.0f}%</b>.</span></div>'
        )
    else:
        top_html = '<div class="leader-strip muted-strip">No started actions recorded yet.</div>'

    bars = []
    table_rows = []

    for row in ranked_rows:
        rate = float(row["rate"])
        bars.append(
            '<div class="bar-row quality-bar-row">'
            f'<div class="bar-person" title="{esc(row["name"])}">'
            f'<span class="bar-medal">{medal_for_rank(int(row["rank"]))}</span>'
            f'{esc(row["name"])}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill quality-fill" style="width:{min(100.0, rate):.2f}%"></div>'
            '</div>'
            f'<div class="bar-count">{rate:.0f}%</div>'
            '</div>'
        )

        table_rows.append(
            '<tr>'
            f'<td class="rank-cell"><span class="rank-number">{int(row["rank"])}</span>'
            f'<span class="medal">{medal_for_rank(int(row["rank"]))}</span></td>'
            f'<td><span class="person-name">{esc(row["name"])}</span></td>'
            f'<td class="numeric">{fmt_int(row["started"])}</td>'
            f'<td class="numeric">{fmt_int(row["completed"])}</td>'
            f'<td class="numeric quality-rate">{rate:.0f}%</td>'
            '</tr>'
        )

    table = (
        '<div class="empty">No contributors detected.</div>'
        if not table_rows
        else (
            '<div class="table-wrap"><table class="quality-table">'
            '<thead><tr><th>Rank</th><th>Person</th><th>Started</th>'
            '<th>Completed</th><th>Quality</th></tr></thead>'
            f'<tbody>{"".join(table_rows)}</tbody></table></div>'
        )
    )

    return f"""
    <section class="panel leaderboard-panel accent-orange">
      <div class="panel-heading">
        <div class="panel-icon">✨</div>
        <div>
          <h2>Completion quality</h2>
          <p>Properly completed actions as a percentage of actions each person has started.</p>
        </div>
      </div>
      {top_html}
      <div class="bars">{"".join(bars[:12])}</div>
      <h3>Full ranking</h3>
      {table}
    </section>
    """

def render_target_card(
    rows: list[dict[str, Any]],
    target_clips: int,
) -> str:
    if rows:
        leader = rows[0]
        leader_pct = 100.0 * int(leader["count"]) / target_clips
        top_html = (
            '<div class="leader-strip"><span class="leader-crown">🎯</span>'
            f'<span><b>{esc(leader["name"])}</b> is at '
            f'<b>{fmt_int(leader["count"])}/{fmt_int(target_clips)}</b> clips '
            f'({leader_pct:.0f}%).</span></div>'
        )
    else:
        top_html = '<div class="leader-strip muted-strip">No clip completions recorded yet.</div>'

    bars = []
    table_rows = []
    for row in rows:
        count = int(row["count"])
        pct = 100.0 * count / target_clips
        width = min(100.0, pct)
        bars.append(
            '<div class="bar-row target-row">'
            f'<div class="bar-person" title="{esc(row["name"])}">'
            f'<span class="bar-medal">{medal_for_rank(int(row["rank"]))}</span>'
            f'{esc(row["name"])}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill target-fill" style="width:{width:.2f}%"></div>'
            '</div>'
            f'<div class="bar-count">{count}/{target_clips}</div>'
            '</div>'
        )
        table_rows.append(
            '<tr>'
            f'<td class="rank-cell"><span class="rank-number">{int(row["rank"])}</span>'
            f'<span class="medal">{medal_for_rank(int(row["rank"]))}</span></td>'
            f'<td><span class="person-name">{esc(row["name"])}</span></td>'
            f'<td class="numeric">{fmt_int(count)}</td>'
            f'<td class="numeric">{fmt_int(target_clips)}</td>'
            f'<td class="numeric">{pct:.0f}%</td>'
            '</tr>'
        )

    table = (
        '<div class="empty">No contributors detected.</div>'
        if not table_rows
        else (
            '<div class="table-wrap"><table class="target-table">'
            '<thead><tr><th>Rank</th><th>Person</th><th>Full clips</th>'
            '<th>Target</th><th>Progress</th></tr></thead>'
            f'<tbody>{"".join(table_rows)}</tbody></table></div>'
        )
    )

    return f'''
    <section class="panel leaderboard-panel accent-purple">
      <div class="panel-heading">
        <div class="panel-icon">🏁</div>
        <div>
          <h2>Fully annotated clips</h2>
          <p>Each person has a target of {fmt_int(target_clips)} fully annotated clips. </p>
        </div>
      </div>
      {top_html}
      <div class="bars">{"".join(bars[:12])}</div>
      <h3>Target progress</h3>
      {table}
    </section>
    '''


def render_compact_audit(audit: dict[str, Any]) -> str:
    total_findings = int(audit["total_findings"])
    people_affected = int(audit["people_affected"])

    if total_findings == 0:
        body = (
            '<div class="audit-clear">✓ All completed Yes/No judgments '
            'include their required details.</div>'
        )
    else:
        people_html = []

        for name, findings in audit["by_person"].items():
            rows = "".join(
                '<tr>'
                f'<td class="audit-row">{fmt_int(finding["row"])}</td>'
                f'<td class="audit-ca">{esc(finding["ca"])}</td>'
                f'<td class="audit-action" title="{esc(finding["action"])}">'
                f'{esc(finding["action"])}</td>'
                f'<td class="audit-band">{esc(finding["band"])}</td>'
                '</tr>'
                for finding in findings
            )

            people_html.append(
                '<details class="audit-person" open>'
                '<summary>'
                f'<span class="audit-name">{esc(name)}</span>'
                f'<span class="audit-count">{fmt_int(len(findings))} issue'
                f'{"s" if len(findings) != 1 else ""}</span>'
                '</summary>'
                '<div class="audit-table-wrap">'
                '<table class="audit-table">'
                '<thead><tr><th>Row</th><th>CA</th><th>Action</th><th>Time band</th></tr></thead>'
                f'<tbody>{rows}</tbody>'
                '</table>'
                '</div>'
                '</details>'
            )

        body = "".join(people_html)

    return f'''
    <section class="panel compact-audit">
      <div class="audit-heading">
        <div>
          <h2>Incomplete annotation details</h2>
          <p>Completed Yes/No judgments that still need their required supporting details.</p>
        </div>
        <div class="audit-summary">
          <span><b>{fmt_int(total_findings)}</b> findings</span>
          <span><b>{fmt_int(people_affected)}</b> people affected</span>
        </div>
      </div>
      {body}
    </section>
    '''



def render_proposal_details(details: dict[str, Any]) -> str:
    rows = details["rows"]
    ignored_clip_numbers = details.get("ignored_incomplete_clip_numbers", [])
    ignored_ranges = compress_number_ranges(list(ignored_clip_numbers))
    ignore_note = (
        f" Ignored for incompleteness: {esc(ignored_ranges)}."
        if ignored_ranges else ""
    )

    if not rows:
        table = (
            '<div class="proposal-clear">'
            'No human proposal records found in the selected rows.'
            '</div>'
        )
    else:
        body = "".join(
            '<tr>'
            f'<td class="proposal-name">{esc(row["name"])}</td>'
            f'<td class="numeric">{fmt_int(row["actions"])}</td>'
            f'<td class="numeric">{fmt_int(row["clips"])}</td>'
            f'<td class="numeric proposal-incomplete">{fmt_int(row["incomplete"])}</td>'
            f'<td class="proposal-clips">{esc(row["clip_ranges"]) or "—"}</td>'
            f'<td class="proposal-clips proposal-incomplete-ranges">'
            f'{esc(row["incomplete_ranges"]) or "—"}</td>'
            f'<td class="numeric">{row["percentage"]:.1f}%</td>'
            '</tr>'
            for row in rows
        )

        table = (
            '<div class="proposal-table-wrap">'
            '<table class="proposal-detail-table">'
            '<thead><tr>'
            '<th>Proposer</th>'
            '<th>Actions</th>'
            '<th>Clips</th>'
            '<th>Incomplete</th>'
            '<th>Clips</th>'
            '<th>Incomplete clips</th>'
            '<th>Complete</th>'
            '</tr></thead>'
            f'<tbody>{body}</tbody>'
            '</table>'
            '</div>'
        )

    return f"""
    <section class="panel proposal-details">
      <div class="proposal-details-heading">
        <div>
          <h2>Incomplete proposal details</h2>
          <p>Incomplete = fewer than 2 proposals, or missing reason/facts.{ignore_note}</p>
        </div>
        <div class="proposal-details-summary">
          <span><b>{fmt_int(details["proposers"])}</b> proposers</span>
          <span><b>{fmt_int(details["incomplete_clips"])}</b> incomplete</span>
        </div>
      </div>
      {table}
    </section>
    """



def render_dashboard(
    stats: dict[str, Any],
    today_ranking: list[dict[str, Any]],
    proposals_today_ranking: list[dict[str, Any]],
    audit: dict[str, Any],
    proposal_details: dict[str, Any],
    *,
    target_clips: int,
    generated_at_aest: dt.datetime,
) -> str:
    s = stats["summary"]
    ai_names = ", ".join(stats["ai_names"]) if stats["ai_names"] else "none detected"

    total_today = sum(int(row["count"]) for row in today_ranking)
    target_reached = sum(
        1 for row in stats["full_clip_ranking"] if int(row["count"]) >= target_clips
    )

    annotation_top = top_person(stats["annotation_ranking"])
    full_clip_top = top_person(stats["full_clip_ranking"])
    today_top = top_person(today_ranking)
    proposal_top = top_person(stats["proposal_ranking"])

    def mini_leader(label: str, leader: tuple[str, int] | None, suffix: str = "") -> str:
        if leader is None:
            return (
                f'<div class="mini-leader"><span>{esc(label)}</span>'
                '<b>—</b><small>No activity</small></div>'
            )
        return (
            f'<div class="mini-leader"><span>{esc(label)}</span>'
            f'<b>{esc(leader[0])}</b><small>{fmt_int(leader[1])}{esc(suffix)}</small></div>'
        )

    if full_clip_top:
        full_mini = (
            '<div class="mini-leader"><span>Full clip target</span>'
            f'<b>{esc(full_clip_top[0])}</b>'
            f'<small>{fmt_int(full_clip_top[1])}/{fmt_int(target_clips)} clips</small></div>'
        )
    else:
        full_mini = (
            '<div class="mini-leader"><span>Full clip target</span>'
            '<b>—</b><small>No completed clips</small></div>'
        )

    cards = [
        ("👥", "Contributors", s["human_contributors"], "human participants detected"),
        ("✅", "Annotations", s["total_annotations"], "actions annotated overall"),
        ("📅", "Annotated today", total_today, "since start of today"),
        ("🏁", "Target reached", target_reached, f'people at ≥ {target_clips:,} full clips'),
        ("💡", "Actions proposed", s["total_proposals"], "human proposals"),
    ]
    cards_html = "".join(
        f'''<div class="metric-card">
          <div class="metric-icon">{icon}</div>
          <div class="metric-label">{esc(label)}</div>
          <div class="metric-value">{fmt_int(value)}</div>
          <div class="metric-sub">{esc(sub)}</div>
        </div>'''
        for icon, label, value, sub in cards
    )

    annotation_activity_card = render_combined_activity_card(
        "Annotation progress",
        "Overall completed canonical actions with today's new annotations shown alongside.",
        stats["annotation_ranking"],
        today_ranking,
        "annotations",
        "accent-blue",
        "🏆",
    )
    proposal_activity_card = render_combined_activity_card(
        "Proposal progress",
        "Overall valid action proposals with today's new valid proposals shown alongside.",
        stats["proposal_ranking"],
        proposals_today_ranking,
        "proposals",
        "accent-green",
        "💡",
    )
    full_clip_card = render_target_card(stats["full_clip_ranking"], target_clips)
    completion_quality_card = render_completion_quality_card(stats)
    compact_audit = render_compact_audit(audit)
    proposal_details_html = render_proposal_details(proposal_details)

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team Annotation Progress</title>
<style>
:root {{
  color-scheme:dark;
  --bg:#090d18;
  --panel:#111827;
  --panel-soft:#151e31;
  --text:#f4f7ff;
  --muted:#9aa8c5;
  --line:#28344e;
  --blue:#78a7ff;
  --cyan:#62d6d2;
  --green:#67d89f;
  --purple:#bb91ff;
  --orange:#ffbd73;
}}
* {{ box-sizing:border-box; }}
html {{ scroll-behavior:smooth; }}
body {{
  margin:0;
  min-height:100vh;
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:var(--text);
  background:
    radial-gradient(circle at 12% -5%,rgba(83,119,255,.22),transparent 34rem),
    radial-gradient(circle at 92% 12%,rgba(99,214,159,.12),transparent 30rem),
    var(--bg);
}}
.container {{ max-width:1440px; margin:0 auto; padding:30px 24px 64px; }}
.hero {{
  position:relative;
  overflow:hidden;
  border:1px solid var(--line);
  border-radius:26px;
  padding:30px;
  background:linear-gradient(135deg,rgba(30,46,80,.98),rgba(16,24,41,.98));
  box-shadow:0 24px 70px rgba(0,0,0,.28);
}}
.hero:after {{
  content:"";
  position:absolute;
  width:340px;
  height:340px;
  right:-120px;
  top:-170px;
  border-radius:50%;
  background:radial-gradient(circle,rgba(120,167,255,.28),transparent 68%);
  pointer-events:none;
}}
.eyebrow {{ color:#b8c8eb; text-transform:uppercase; letter-spacing:.16em; font-size:11px; font-weight:800; }}
h1 {{ margin:8px 0 10px; font-size:clamp(32px,5vw,56px); line-height:1.03; letter-spacing:-.045em; }}
.hero-meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin-top:20px; color:#8fa2c7; font-size:12px; }}
.hero-meta b {{ color:#dce6fb; }}
.mode-pill {{ display:inline-block; padding:4px 9px; border:1px solid #42567e; border-radius:999px; background:#15233b; color:#dbe7ff; font-weight:800; }}
.metrics {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:13px; margin:18px 0; }}
.metric-card {{ border:1px solid var(--line); background:rgba(17,24,39,.94); border-radius:18px; padding:17px; min-width:0; }}
.metric-icon {{ font-size:18px; }}
.metric-label {{ margin-top:9px; color:#9fb0cf; text-transform:uppercase; letter-spacing:.08em; font-size:9px; font-weight:850; }}
.metric-value {{ margin:4px 0; font-size:27px; font-weight:900; letter-spacing:-.025em; }}
.metric-sub {{ color:#8394b5; font-size:10px; line-height:1.4; }}
.snapshot {{ margin-bottom:18px; }}
.panel {{ border:1px solid var(--line); border-radius:21px; padding:20px; background:rgba(17,24,39,.95); box-shadow:0 14px 34px rgba(0,0,0,.16); }}
.panel h2 {{ margin:0; font-size:19px; letter-spacing:-.02em; }}
.panel h3 {{ margin:18px 0 10px; color:#cbd7ef; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
.panel p {{ margin:5px 0 0; color:var(--muted); line-height:1.55; font-size:11px; }}
.momentum {{ display:grid; grid-template-columns:repeat(4,1fr); gap:11px; margin-top:15px; }}
.mini-leader {{ border:1px solid var(--line); border-radius:14px; background:#0d1422; padding:13px; min-width:0; }}
.mini-leader span {{ display:block; color:#8ea0c2; text-transform:uppercase; letter-spacing:.07em; font-size:8px; font-weight:850; }}
.mini-leader b {{ display:block; margin:5px 0 2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:13px; }}
.mini-leader small {{ color:#aebbd5; font-size:9px; }}
.dashboard-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }}
.leaderboard-panel {{ min-width:0; }}
.accent-blue {{ border-top:3px solid var(--blue); }}
.accent-purple {{ border-top:3px solid var(--purple); }}
.accent-green {{ border-top:3px solid var(--green); }}
.accent-orange {{ border-top:3px solid var(--orange); }}
.panel-heading {{ display:flex; gap:12px; align-items:flex-start; }}
.panel-icon {{ display:grid; place-items:center; width:38px; height:38px; flex:0 0 38px; border:1px solid var(--line); border-radius:12px; background:#0c1423; font-size:19px; }}
.leader-strip {{ display:flex; gap:8px; align-items:center; margin:15px 0 14px; padding:10px 12px; border:1px solid #31415f; border-radius:12px; background:#101a2d; color:#cbd8f1; font-size:11px; }}
.leader-crown {{ font-size:17px; }}
.muted-strip {{ color:var(--muted); }}
.bars {{ margin:6px 0 4px; }}
.bar-row {{ display:grid; grid-template-columns:minmax(110px,180px) 1fr 58px; gap:10px; align-items:center; margin:9px 0; }}
.bar-person {{ min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; color:#dce6fb; }}
.bar-medal {{ display:inline-block; width:20px; }}
.bar-track {{ height:11px; border-radius:999px; overflow:hidden; background:#202b42; }}
.bar-fill {{ height:100%; border-radius:999px; background:linear-gradient(90deg,var(--blue),var(--cyan)); }}
.target-fill {{ background:linear-gradient(90deg,var(--purple),#e1a5ff); }}
.bar-count {{ text-align:right; color:#bdc9e1; font-size:11px; font-weight:800; font-variant-numeric:tabular-nums; }}
.table-wrap {{ border:1px solid var(--line); border-radius:14px; overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
th,td {{ padding:10px 11px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; overflow-wrap:anywhere; }}
th {{ color:#95a8cc; background:#151e31; text-transform:uppercase; letter-spacing:.06em; font-size:9px; }}
td {{ color:#dce6fb; font-size:11px; }}
tbody tr:last-child td {{ border-bottom:0; }}
tbody tr:hover td {{ background:#141d30; }}
.target-table th:nth-child(1),.target-table td:nth-child(1) {{ width:66px; }}
.target-table th:nth-child(3),.target-table td:nth-child(3),
.target-table th:nth-child(4),.target-table td:nth-child(4),
.target-table th:nth-child(5),.target-table td:nth-child(5) {{ width:86px; text-align:right; }}
.combined-table th:nth-child(1),.combined-table td:nth-child(1) {{ width:72px; }}
.combined-table th:nth-child(3),.combined-table td:nth-child(3),
.combined-table th:nth-child(4),.combined-table td:nth-child(4) {{ width:92px; text-align:right; }}
.combined-total {{ font-size:13px; }}
.today-value {{ color:#c9f3df; font-weight:850; }}
.combined-bar-row {{ grid-template-columns:minmax(110px,180px) 1fr 92px; }}
.combined-bar-count {{ display:flex; justify-content:flex-end; gap:8px; align-items:baseline; }}
.combined-bar-count b {{ color:#dce6fb; }}
.today-inline {{ color:#8ee2b8; font-size:9px; }}
.quality-fill {{ background:linear-gradient(90deg,var(--orange),#ffe1a8); }}
.quality-table th:nth-child(1),.quality-table td:nth-child(1) {{ width:66px; }}
.quality-table th:nth-child(3),.quality-table td:nth-child(3),
.quality-table th:nth-child(4),.quality-table td:nth-child(4),
.quality-table th:nth-child(5),.quality-table td:nth-child(5) {{ width:88px; text-align:right; }}
.quality-rate {{ color:#ffe0a4; font-weight:850; }}
.compact-audit {{ margin-top:18px; padding:20px 22px; }}
.audit-heading {{ display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:12px; }}
.audit-heading h2 {{ margin:0; font-size:17px; }}
.audit-heading p {{ margin:4px 0 0; font-size:11px; line-height:1.5; }}
.audit-summary {{ display:flex; gap:7px; flex-wrap:wrap; justify-content:flex-end; }}
.audit-summary span {{ padding:6px 9px; border:1px solid #4b3d62; border-radius:999px; background:rgba(187,145,255,.08); color:#d8c2ff; font-size:10px; white-space:nowrap; }}
.audit-person {{ border-top:1px solid var(--line); }}
.audit-person:first-of-type {{ border-top:0; }}
.audit-person summary {{ cursor:pointer; list-style:none; display:flex; justify-content:space-between; gap:14px; align-items:center; padding:12px 2px; }}
.audit-person summary::-webkit-details-marker {{ display:none; }}
.audit-name {{ font-size:12px; font-weight:800; }}
.audit-count {{ color:#cdb8f2; font-size:10px; }}
.audit-table-wrap {{ overflow-x:auto; padding:2px 0 12px; }}
.audit-table {{ table-layout:fixed; min-width:620px; }}
.audit-table th,.audit-table td {{ padding:9px 10px; }}
.audit-table th {{ font-size:9px; }}
.audit-table td {{ font-size:11px; }}
.audit-table th:nth-child(1),.audit-table td:nth-child(1) {{ width:58px; }}
.audit-table th:nth-child(2),.audit-table td:nth-child(2) {{ width:64px; }}
.audit-table th:nth-child(4),.audit-table td:nth-child(4) {{ width:140px; }}
.audit-row,.audit-ca,.audit-band {{ white-space:nowrap; font-weight:700; }}
.audit-action {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.audit-clear {{ padding:11px 13px; border:1px dashed rgba(103,216,159,.28); border-radius:10px; color:#9be9bf; font-size:11px; }}
.proposal-details {{ margin-top:18px; padding:18px 20px; }}
.proposal-details-heading {{ display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:12px; }}
.proposal-details-heading h2 {{ margin:0; font-size:16px; }}
.proposal-details-heading p {{ margin:4px 0 0; font-size:10px; line-height:1.5; max-width:900px; }}
.proposal-details-summary {{ display:flex; gap:7px; flex-wrap:wrap; justify-content:flex-end; }}
.proposal-details-summary span {{ padding:5px 8px; border:1px solid #3c5f54; border-radius:999px; background:rgba(103,216,159,.07); color:#aee8cb; font-size:9px; white-space:nowrap; }}
.proposal-table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; }}
.proposal-detail-table {{ min-width:850px; table-layout:auto; }}
.proposal-detail-table th,.proposal-detail-table td {{ padding:9px 10px; }}
.proposal-detail-table .numeric {{ text-align:left; }}
.proposal-detail-table th {{ font-size:8px; }}
.proposal-detail-table td {{ font-size:10px; }}
.proposal-name {{ min-width:150px; font-weight:800; }}
.proposal-clips {{ min-width:150px; color:#cbd7ef; }}
.proposal-incomplete,.proposal-incomplete-ranges {{ color:#ffb5b5; font-weight:800; }}
.proposal-clear {{ padding:11px 13px; border:1px dashed var(--line); border-radius:10px; color:var(--muted); font-size:10px; }}
.rank-cell {{ white-space:nowrap; }}
.rank-number {{ display:inline-block; min-width:22px; font-weight:900; font-variant-numeric:tabular-nums; }}
.medal {{ display:inline-block; width:22px; }}
.person-name {{ font-weight:750; }}
.numeric {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:750; }}
.empty {{ border:1px dashed var(--line); border-radius:13px; color:var(--muted); padding:18px; font-size:12px; }}
.method {{ margin-top:18px; }}
.method-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:14px; }}
.method-item {{ padding:14px; border-radius:14px; background:#0d1422; border:1px solid var(--line); }}
.method-item b {{ display:block; margin-bottom:5px; font-size:11px; }}
.method-item span {{ color:var(--muted); font-size:10px; line-height:1.55; }}
footer {{ margin-top:24px; text-align:center; color:#70809f; font-size:10px; }}
@media (max-width:1120px) {{
  .metrics {{ grid-template-columns:repeat(3,1fr); }}
  .method-grid {{ grid-template-columns:repeat(2,1fr); }}
}}
@media (max-width:820px) {{
  .dashboard-grid {{ grid-template-columns:1fr; }}
  .momentum {{ grid-template-columns:repeat(2,1fr); }}
}}
@media (max-width:620px) {{
  .container {{ padding:14px 10px 36px; }}
  .hero,.panel {{ padding:16px; border-radius:17px; }}
  .metrics {{ grid-template-columns:1fr 1fr; }}
  .metric-card:last-child {{ grid-column:1 / -1; }}
  .momentum {{ grid-template-columns:1fr 1fr; }}
  .method-grid {{ grid-template-columns:1fr; }}
  .bar-row {{ grid-template-columns:95px 1fr 46px; gap:7px; }}
  .combined-bar-row {{ grid-template-columns:95px 1fr 72px; }}
  .combined-table th:nth-child(1),.combined-table td:nth-child(1) {{ width:55px; }}
  .combined-table th:nth-child(3),.combined-table td:nth-child(3),
  .combined-table th:nth-child(4),.combined-table td:nth-child(4) {{ width:64px; }}
  .quality-table {{ table-layout:auto; }}
  .quality-table th:nth-child(n),.quality-table td:nth-child(n) {{ width:auto; }}
  .audit-heading {{ flex-direction:column; }}
  .audit-summary {{ justify-content:flex-start; }}
  .proposal-details-heading {{ flex-direction:column; }}
  .proposal-details-summary {{ justify-content:flex-start; }}
  .target-table {{ table-layout:auto; }}
  .target-table th:nth-child(n),.target-table td:nth-child(n) {{ width:auto; }}
}}
@media print {{
  body {{ background:white; color:#111; }}
  .hero,.panel,.metric-card,.mini-leader {{ background:white; color:#111; box-shadow:none; }}
  .hero-meta,.panel p,.metric-label,.metric-sub,.mini-leader span,.method-item span {{ color:#555; }}
  .dashboard-grid {{ grid-template-columns:1fr 1fr; }}
}}
</style>
</head>
<body>
<div class="container">
  <section class="hero">
    <div class="eyebrow">Temporal NoRA · contribution board</div>
    <h1>Team Annotation Progress</h1>
    <div class="hero-meta">
      <span class="mode-pill"><b>Last Updated:</b> {esc(generated_at_aest.strftime("%d %B %Y, %I:%M %p AEST").lstrip("0"))}</span>
      <span class="mode-pill"><b>Target Clips:</b> {fmt_int(target_clips)}</span>
    </div>
  </section>

  <section class="metrics">{cards_html}</section>

  <section class="snapshot">
    <div class="panel">
      <h2>Current leaders</h2>
      <p>A quick snapshot before the full rankings below.</p>
      <div class="momentum">
        {mini_leader("Annotations", annotation_top)}
        {full_mini}
        {mini_leader("Annotated today", today_top)}
        {mini_leader("Proposals", proposal_top)}
      </div>
    </div>
  </section>

  <main class="dashboard-grid">
    {annotation_activity_card}
    {proposal_activity_card}
    {full_clip_card}
    {completion_quality_card}
  </main>

  {compact_audit}

  {proposal_details_html}

  <section class="panel method">
    <h2>How progress is counted</h2>
    <p>The daily metric is derived from the earliest saved progress JSON snapshot for the current AEST date because the annotation objects in the export do not contain reliable per-action timestamps.</p>
    <div class="method-grid">
      <div class="method-item">
        <b>One annotation = one canonical action</b>
        <span>A canonical action counts once only when all five time bands are complete. In each band, yes requires a non-unset reason_judgment; no requires at least one rejection reason. Individual time-band judgments are not separate annotations.</span>
      </div>
      <div class="method-item">
        <b>Fully annotated clip</b>
        <span>Every action in the clip's canonical_action_pool must have a complete judgment for H10, H60, H30M, H1H, and H3H. For yes, reason_judgment must be set; for no, rejection_reasons must be non-empty.</span>
      </div>
      <div class="method-item">
        <b>Today's annotations</b>
        <span>Current cumulative annotation count minus the earliest saved progress JSON snapshot for today's AEST date, per person. Negative differences are clamped to zero.</span>
      </div>
      <div class="method-item">
        <b>Human leaderboards only</b>
        <span>Known model annotators and common model-name patterns are excluded. Explicit exclusions: annotators {esc(", ".join(stats.get("excluded_annotators", [])) or "none")}; clips {esc(", ".join(stats.get("excluded_clip_ids", [])) or "none")}. Detected model names: {esc(ai_names)}.</span>
      </div>
    </div>
  </section>

  <footer>Powered by kiwirafe: kiwirafe.github.io</footer>
</div>
</body>
</html>'''


def print_ranking(title: str, rows: list[dict[str, Any]], limit: int = 10) -> None:
    print(f"\n{title}")
    if not rows:
        print("  No activity")
        return
    for row in rows[:limit]:
        print(f"  {row['rank']:>2}. {row['name']:<28} {row['count']:>6,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the current HTML dashboard plus compact progress JSON history."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Annotation site URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--annotator",
        default=DEFAULT_ANNOTATOR,
        help=f"Name used to log in (default: {DEFAULT_ANNOTATOR})",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Use an existing JSON/JSONL export instead of downloading /api/export.",
    )
    parser.add_argument(
        "--export-path",
        type=Path,
        default=DEFAULT_EXPORT,
        help=f"Downloaded export path (default: {DEFAULT_EXPORT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Current HTML output path, overwritten each run (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--progress-json-dir",
        type=Path,
        default=None,
        help=(
            "Directory for compact progress JSON history "
            "(default: <output parent>/progress_json)."
        ),
    )
    parser.add_argument(
        "--target-clips",
        type=int,
        default=DEFAULT_TARGET_CLIPS,
        help=f"Fully annotated clip target per person (default: {DEFAULT_TARGET_CLIPS}).",
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=0,
        help="0-based first clip row to analyse (default: 0).",
    )
    parser.add_argument(
        "-n",
        "--num-rows",
        type=int,
        default=None,
        help="Number of clip rows from --start-row (default: all remaining rows).",
    )
    parser.add_argument(
        "--ai-annotator",
        action="append",
        default=[],
        help="Additional annotator name to classify as AI/model. May be repeated.",
    )
    parser.add_argument(
        "--ignore-incomplete-proposal-clips",
        default=os.getenv("IGNORE_INCOMPLETE_PROPOSAL_CLIPS", ""),
        metavar="RANGES",
        help=(
            "1-based clip numbers/ranges to treat as completed in Incomplete "
            "proposal details, e.g. '1-30, 38, 50-70'. Can also be set with "
            "IGNORE_INCOMPLETE_PROPOSAL_CLIPS."
        ),
    )
    parser.add_argument(
        "--exclude-clip",
        action="append",
        default=[],
        help=(
            "Additional clip ID to exclude from all statistics. May be repeated. "
            "API-Test-Clip190 is excluded by default."
        ),
    )
    parser.add_argument(
        "--exclude-annotator",
        action="append",
        default=[],
        help=(
            "Additional annotator to exclude from all statistics. May be repeated. "
            "Ning and API-Test-Clip190 are excluded by default."
        ),
    )
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()

    if args.start_row < 0:
        raise ValueError("--start-row must be >= 0")
    if args.num_rows is not None and args.num_rows <= 0:
        raise ValueError("-n/--num-rows must be > 0")
    if args.target_clips <= 0:
        raise ValueError("--target-clips must be > 0")

    ignored_incomplete_proposal_clips = parse_number_ranges(
        args.ignore_incomplete_proposal_clips
    )

    if args.input is not None:
        export_path = args.input
        if not export_path.exists():
            raise FileNotFoundError(export_path)
    else:
        export_path = download_export(args)

    records = load_export_records(export_path)
    total_records = len(records)
    if args.start_row > total_records:
        raise ValueError(
            f"--start-row {args.start_row} is beyond the export ({total_records} records)"
        )

    end_row = (
        total_records
        if args.num_rows is None
        else min(total_records, args.start_row + args.num_rows)
    )
    records = records[args.start_row:end_row]

    print("\n=== ANALYSE ===")
    print(f"Loaded {total_records:,} clip record(s)")
    if records:
        print(f"Analysing rows {args.start_row}..{end_row - 1} ({len(records):,} record(s))")
    else:
        print("No records selected")

    ai_names = set(KNOWN_AI_ANNOTATORS)
    ai_names.update(args.ai_annotator)

    excluded_clip_ids = set(DEFAULT_EXCLUDED_CLIP_IDS)
    excluded_clip_ids.update(args.exclude_clip)
    excluded_annotators = set(DEFAULT_EXCLUDED_ANNOTATORS)
    excluded_annotators.update(args.exclude_annotator)

    stats = analyse(
        records,
        ai_names,
        excluded_clip_ids,
        excluded_annotators,
    )
    audit = analyse_missing_details(
        records,
        start_row=args.start_row,
        explicit_ai_names=ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
    )
    proposal_details = analyse_proposal_details(
        records,
        start_row=args.start_row,
        explicit_ai_names=ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
        ignored_incomplete_clip_numbers=ignored_incomplete_proposal_clips,
    )

    now_aest = dt.datetime.now(dt.timezone.utc).astimezone(AEST)
    output_path: Path = args.output
    progress_json_dir = args.progress_json_dir or (output_path.parent / "progress_json")

    # Persist this run's compact progress counters first. The timestamp belongs in
    # the filename so the JSON body contains only the requested per-person metrics.
    progress_payload = make_progress_json_payload(stats)
    progress_path = progress_json_snapshot_path(progress_json_dir, now_aest)
    progress_created = False
    if not progress_path.exists():
        write_json_atomic(progress_path, progress_payload)
        progress_created = True
    else:
        print(f"Progress JSON already exists; keeping immutable snapshot: {progress_path}")

    baseline_result = find_day_baseline(progress_json_dir, now_aest.date())
    if baseline_result is None:
        # This should only be reachable if the just-written JSON was unreadable.
        raise RuntimeError(f"Could not load a progress JSON baseline from {progress_path.parent}")
    baseline_path, baseline_payload = baseline_result
    baseline_counts = counts_from_progress_json(baseline_payload, "actions_annotated")
    baseline_proposal_counts = counts_from_progress_json(baseline_payload, "proposals")

    human_names = set(stats["human_names"])

    today_counts = make_today_counts(
        stats["annotation_counts"],
        baseline_counts,
        human_names,
    )
    today_ranking = rank_counts(today_counts, include_zero_names=human_names)

    proposals_today_counts = make_today_counts(
        stats["proposal_counts"],
        baseline_proposal_counts,
        human_names,
    )
    proposals_today_ranking = rank_counts(
        proposals_today_counts,
        include_zero_names=human_names,
    )

    current_html = render_dashboard(
        stats,
        today_ranking,
        proposals_today_ranking,
        audit,
        proposal_details,
        target_clips=args.target_clips,
        generated_at_aest=now_aest,
    )
    write_text_atomic(output_path, current_html)

    s = stats["summary"]
    target_reached = sum(
        1 for row in stats["full_clip_ranking"] if int(row["count"]) >= args.target_clips
    )
    total_today = sum(today_counts.values())

    print(f"Excluded clips: {', '.join(sorted(excluded_clip_ids)) or 'none'}")
    print(f"Excluded annotators: {', '.join(sorted(excluded_annotators)) or 'none'}")
    print(
        "Ignored incomplete proposal clips: "
        f"{compress_number_ranges(sorted(ignored_incomplete_proposal_clips)) or 'none'}"
    )
    print(f"Human contributors: {s['human_contributors']:,}")
    print(f"Canonical-action annotations: {s['total_annotations']:,}")
    print(f"Actions annotated today: {total_today:,}")
    print(f"Full clip completions: {s['full_clip_completions']:,}")
    print(f"People at target ({args.target_clips} clips): {target_reached:,}")
    print(f"Human proposals: {s['total_proposals']:,}")
    print(
        f"Missing-detail findings: {audit['total_findings']:,} "
        f"across {audit['people_affected']:,} people"
    )
    print(
        f"Incomplete proposal clips: {proposal_details['incomplete_clips']:,} "
        f"across {proposal_details['proposers']:,} proposers"
    )

    print_ranking("Most annotations", stats["annotation_ranking"])
    print_ranking("Most fully annotated clips", stats["full_clip_ranking"])
    print_ranking("Actions annotated today", today_ranking)
    print_ranking("Most actions proposed", stats["proposal_ranking"])
    print_ranking("Actions proposed today", proposals_today_ranking)

    print("\n=== REPORT ===")
    print(f"Current HTML:   {output_path}")
    print(f"Progress JSON:  {progress_path}{' (created)' if progress_created else ' (existing)'}")
    print(f"Today's baseline: {baseline_path}")
    print(f"AEST date:      {now_aest.date().isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("Interrupted")
        raise SystemExit(130)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        eprint(f"HTTP {exc.code}: {body or exc.reason}")
        raise SystemExit(1)
    except Exception as exc:
        eprint(f"ERROR: {exc}")
        raise SystemExit(1)