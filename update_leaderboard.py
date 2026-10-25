#!/usr/bin/env python3
"""Generate a self-contained HTML audit for Temporal NoRA annotations.

Audits:
1. Strict 2 Yes + 1 No disagreement at (clip, canonical action, time band).
   Shows existing annotators, original proposers and eligible independent reviewers.
2. Human annotator judging a canonical action derived from their own proposal.

The self-contained report filters open disagreements by eligible reviewer and
tracks independently reviewed time-band units toward a per-person target.
Review attribution is based on a persistent pre-review snapshot, because the
export does not reliably record the chronology of individual judgments.
Confirmed duplicate groups are included in proposer provenance.

Designed to run repeatedly (including via GitHub Actions). The review state is
stored separately from the published HTML, and a repeated run never adds
another credit for an already-attributed fourth judgment. The first run creates
the three-voter baseline; previous fourth reviews cannot be inferred safely.
Do not discard the review state between GitHub Actions runs.
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
DEFAULT_OUTPUT = Path("reports/annotation_conflict.html")

REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
}

KNOWN_AI_ANNOTATORS = {"Claude-Opus-5", "GPT-5.6-Sol"}
AI_NAME_HINTS = (
    "gpt", "claude", "openai", "anthropic", "gemini",
    "llama", "qwen", "deepseek", "mistral",
)
DEFAULT_EXCLUDED_CLIP_IDS = {"API-Test-Clip190"}
DEFAULT_EXCLUDED_ANNOTATORS = {"Ning", "API-Test-Clip190"}

# Project's six human reviewers; all appear as filter options even with no eligible actions.
# Change this tuple, or pass --reviewer multiple times, if team membership changes.
DEFAULT_REVIEWERS = (
    "Hanyu",
    "Jiayang Wang",
    "Khai",
    "Naman Kansal",
    "Ngakan Putu Bagus Aditya",
    "Shantelle So",
)

EXPECTED_HORIZONS = ("H10", "H60", "H30M", "H1H", "H3H")
HORIZON_LABELS = {
    "H10": "0–10 seconds",
    "H60": "10 seconds–1 minute",
    "H30M": "1–30 minutes",
    "H1H": "30 minutes–1 hour",
    "H3H": "1–3 hours",
}
VALID_ACTION_JUDGMENTS = {"yes", "no"}
DEFAULT_REVIEW_TARGET = 700
REVIEW_PROGRESS_FILENAME = "annotation_review_progress.json"
# Kept outside the Pages output directory, so the review ledger is not public.
# On ephemeral runners, pass this path explicitly and commit it between runs.
DEFAULT_PROGRESS_STATE = Path("review_state") / REVIEW_PROGRESS_FILENAME


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def load_simple_dotenv(path: Path) -> None:
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
        key, value = key.strip(), value.strip()
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


def make_authenticated_opener(base_url: str, annotator: str) -> urllib.request.OpenerDirector:
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

    post_json("/api/auth/login", {"annotatorName": annotator, "password": site_password})
    post_json("/api/admin/login", {"password": admin_password})
    return opener


def download_export(args: argparse.Namespace) -> Path:
    output_path: Path = args.export_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise ValueError("Website export was empty")
        temp_path.replace(output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    print(f"Downloaded {output_path.stat().st_size:,} bytes")
    return output_path


def load_export_records(path: Path) -> list[dict[str, Any]]:
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
    folded = value.casefold()
    return any(folded == excluded.casefold() for excluded in excluded_values)


def canonical_id(item: dict[str, Any]) -> str:
    value = item.get("canonical_action_id")
    return str(value).strip() if value is not None else ""


def action_text(item: dict[str, Any]) -> str:
    for key in (
        "action", "canonical_action", "action_text", "text",
        "description", "representative_action", "canonical_text",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(action text unavailable)"


def valid_binary_judgment(judgment: Any) -> str | None:
    if not isinstance(judgment, dict):
        return None
    value = str(judgment.get("action_judgment") or "").strip().casefold()
    return value if value in VALID_ACTION_JUDGMENTS else None


def completed_horizon_judgment(judgment: Any) -> bool:
    """Require the same complete Yes/No evidence as the annotation leaderboard.

    A Yes must have a non-unset reason_judgment; a No must include at least
    one rejection reason. Merely setting Yes/No does not complete a review.
    """
    state = valid_binary_judgment(judgment)
    if state == "yes":
        reason = str(judgment.get("reason_judgment") or "").strip().casefold()
        return bool(reason) and reason != "unset"
    if state == "no":
        reasons = judgment.get("rejection_reasons")
        return isinstance(reasons, (list, tuple, set)) and any(
            str(reason).strip() for reason in reasons
        )
    return False


def rejection_reason_text(judgment: Any) -> str:
    if not isinstance(judgment, dict):
        return "—"
    reasons = judgment.get("rejection_reasons")
    if not isinstance(reasons, (list, tuple, set)):
        return "—"
    cleaned = [str(reason).strip() for reason in reasons if str(reason).strip()]
    return ", ".join(cleaned) if cleaned else "—"


def proposal_id_set(item: dict[str, Any]) -> set[str]:
    raw = item.get("proposal_ids")
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in raw if str(value).strip()}


def build_duplicate_expansion(
    duplicate_groups: list[Any],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Expand confirmed groups from original/representative/merged IDs."""
    expansion: dict[str, set[str]] = {}
    group_name_by_seed: dict[str, str] = {}
    for raw_group in duplicate_groups:
        if not isinstance(raw_group, dict):
            continue
        if str(raw_group.get("status") or "").strip().casefold() != "confirmed":
            continue
        members = {
            str(value).strip()
            for value in (raw_group.get("proposal_ids") or [])
            if str(value).strip()
        }
        if not members:
            continue
        seeds = set(members)
        for key in ("representative_proposal_id", "merged_proposal_id"):
            value = str(raw_group.get(key) or "").strip()
            if value:
                seeds.add(value)
        group_label = (
            str(raw_group.get("name") or "").strip()
            or str(raw_group.get("group_id") or "").strip()
            or "confirmed duplicate group"
        )
        for seed in seeds:
            expansion.setdefault(seed, set()).update(members)
            group_name_by_seed[seed] = group_label
    return expansion, group_name_by_seed


def expand_canonical_proposal_ids(
    canonical_proposal_ids: set[str],
    duplicate_expansion: dict[str, set[str]],
) -> set[str]:
    expanded = set(canonical_proposal_ids)
    while True:
        before = len(expanded)
        for proposal_id in list(expanded):
            expanded.update(duplicate_expansion.get(proposal_id, set()))
        if len(expanded) == before:
            return expanded


def build_canonical_provenance(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map canonical actions to *all* original proposals, including confirmed merges.

    This shared provenance is used by both the disagreement reviewer eligibility
    check and the existing self-annotation check.
    """
    proposals = record.get("proposals") or []
    proposal_by_id = {
        str(proposal.get("proposal_id") or "").strip(): proposal
        for proposal in proposals if isinstance(proposal, dict)
        and str(proposal.get("proposal_id") or "").strip()
    } if isinstance(proposals, list) else {}
    duplicate_groups = record.get("duplicate_groups") or []
    expansion, _ = build_duplicate_expansion(
        duplicate_groups if isinstance(duplicate_groups, list) else []
    )
    pool = record.get("canonical_action_pool") or []
    provenance: dict[str, dict[str, Any]] = {}
    if not isinstance(pool, list):
        return provenance

    for item in pool:
        if not isinstance(item, dict):
            continue
        cid = canonical_id(item)
        if not cid:
            continue
        proposal_ids = expand_canonical_proposal_ids(proposal_id_set(item), expansion)
        proposals_by_person: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for proposal_id in sorted(proposal_ids):
            proposal = proposal_by_id.get(proposal_id)
            if proposal is not None:
                proposals_by_person[normalize_name(proposal.get("annotator_id"))].append(proposal)
        provenance[cid] = {
            "pool_item": item,
            "proposals_by_person": proposals_by_person,
            "proposers": sorted(proposals_by_person, key=str.casefold),
        }
    return provenance


def analyse_two_yes_one_no(
    records: list[dict[str, Any]],
    *,
    start_row: int,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
    reviewers: tuple[str, ...] = DEFAULT_REVIEWERS,
) -> dict[str, Any]:
    """Find strict 2 Yes / 1 No units and eligible reviewers for each action.

    A reviewer is eligible only if they have never submitted a valid Yes/No
    judgment on *any band of that canonical action*, and are not among the
    original proposers, including contributors to confirmed duplicate groups.
    """
    unit_rows: list[dict[str, Any]] = []
    tracking_units: list[dict[str, Any]] = []
    conflicting_duplicate_ratings = 0

    for local_index, record in enumerate(records):
        row_number = start_row + local_index + 1
        clip_id = normalize_name(record.get("clip_id"))
        if is_excluded(clip_id, excluded_clip_ids):
            continue

        provenance = build_canonical_provenance(record)
        pool_map = {cid: info["pool_item"] for cid, info in provenance.items()}
        values: dict[tuple[str, str], dict[str, set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        action_annotators: dict[str, set[str]] = collections.defaultdict(set)

        annotations = record.get("annotations") or []
        if not isinstance(annotations, list):
            annotations = []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            name = normalize_name(annotation.get("annotator_id"))
            if is_excluded(name, excluded_annotators) or is_ai_annotator(name, explicit_ai_names):
                continue
            actions = annotation.get("canonical_actions") or []
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, dict):
                    continue
                cid = canonical_id(action)
                if cid not in pool_map:
                    continue
                judgments = action.get("horizon_judgments") or {}
                if not isinstance(judgments, dict):
                    continue
                for horizon, judgment in judgments.items():
                    state = valid_binary_judgment(judgment)
                    if state is None:
                        continue
                    values[(cid, str(horizon))][name].add(state)
                    action_annotators[cid].add(name)

        for (cid, horizon), annotator_values in values.items():
            clean: dict[str, str] = {}
            for name, states in annotator_values.items():
                if len(states) == 1:
                    clean[name] = next(iter(states))
                else:
                    conflicting_duplicate_ratings += 1
            if len(clean) != 3:
                continue
            counts = collections.Counter(clean.values())
            if counts["yes"] != 2 or counts["no"] != 1:
                continue

            # Record the *original* three voters and action-wide exclusions.
            # This is persisted before fourth reviews are added, so attribution
            # does not depend on (typically alphabetical) export list order.
            tracking_units.append({
                "clip_id": clip_id,
                "canonical_action_id": cid,
                "horizon": horizon,
                "original_raters": sorted(clean, key=str.casefold),
                "ineligible_people": sorted(
                    set(action_annotators.get(cid, set()))
                    | set(provenance[cid]["proposers"]),
                    key=str.casefold,
                ),
            })
            unit_rows.append({
                "row": row_number,
                "canonical_action_id": cid,
                "action": action_text(pool_map[cid]),
                "horizon": horizon,
                "band": HORIZON_LABELS.get(horizon, horizon),
                # Existing report behaviour: people with any valid judgment on this action.
                "annotators": sorted(action_annotators.get(cid, set()), key=str.casefold),
                "proposers": provenance[cid]["proposers"],
            })

    # Keep one displayed row per canonical action, with all disputed bands together.
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for unit in unit_rows:
        key = (unit["row"], unit["canonical_action_id"])
        row = grouped.setdefault(key, {
            "row": unit["row"],
            "canonical_action_id": unit["canonical_action_id"],
            "action": unit["action"],
            "disagreements": [],
            "proposers": unit["proposers"],
        })
        row["disagreements"].append({
            "horizon": unit["horizon"],
            "band": unit["band"],
            "annotators": unit["annotators"],
        })

    horizon_index = {h: i for i, h in enumerate(EXPECTED_HORIZONS)}
    rows = list(grouped.values())
    for row in rows:
        row["disagreements"].sort(
            key=lambda item: (horizon_index.get(item["horizon"], 999), item["horizon"])
        )
        row["annotators"] = sorted(
            {name for item in row["disagreements"] for name in item["annotators"]},
            key=str.casefold,
        )
        ineligible = {name.casefold() for name in row["annotators"]}
        ineligible.update(name.casefold() for name in row["proposers"])
        row["eligible_reviewers"] = [
            name for name in reviewers if name.casefold() not in ineligible
        ]

    rows.sort(key=lambda row: (row["row"], row["canonical_action_id"]))
    return {
        "rows": rows,
        "units": len(unit_rows),
        "affected_actions": len(rows),
        "reviewers": reviewers,
        "tracking_units": tracking_units,
        "conflicting_duplicate_ratings_excluded": conflicting_duplicate_ratings,
    }


def review_unit_key(clip_id: str, canonical_action_id: str, horizon: str) -> str:
    """Use stable record identifiers rather than export row indexes."""
    return json.dumps([clip_id, canonical_action_id, horizon],
                      ensure_ascii=False, separators=(",", ":"))


def load_review_progress(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "tracked_units": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read review progress file {path}: {exc}") from exc
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("tracked_units"), dict)):
        raise ValueError(f"Unsupported or invalid review progress state: {path}")
    for key, entry in data["tracked_units"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError(f"Invalid tracked review unit in {path}")
        if (not isinstance(entry.get("original_raters"), list)
                or not isinstance(entry.get("ineligible_people"), list)):
            raise ValueError(f"Invalid baseline for tracked review unit {key} in {path}")
    return data


def write_review_progress(path: Path, data: dict[str, Any]) -> None:
    """Write only provenance/attribution, atomically; never touch the export."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def collect_current_review_ratings(
    records: list[dict[str, Any]],
    *,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> dict[str, dict[str, bool]]:
    """Map unit -> human -> completion, excluding inconsistent duplicate votes."""
    states: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(set)
    )
    completions: dict[str, set[str]] = collections.defaultdict(set)
    for record in records:
        clip_id = normalize_name(record.get("clip_id"))
        if is_excluded(clip_id, excluded_clip_ids):
            continue
        pool = record.get("canonical_action_pool") or []
        valid_ids = {canonical_id(item) for item in pool if isinstance(item, dict)} \
            if isinstance(pool, list) else set()
        annotations = record.get("annotations") or []
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            name = normalize_name(annotation.get("annotator_id"))
            if (is_excluded(name, excluded_annotators)
                    or is_ai_annotator(name, explicit_ai_names)):
                continue
            actions = annotation.get("canonical_actions") or []
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, dict):
                    continue
                cid = canonical_id(action)
                if not cid or cid not in valid_ids:
                    continue
                judgments = action.get("horizon_judgments") or {}
                if not isinstance(judgments, dict):
                    continue
                for horizon in EXPECTED_HORIZONS:
                    judgment = judgments.get(horizon)
                    state = valid_binary_judgment(judgment)
                    if state is None:
                        continue
                    key = review_unit_key(clip_id, cid, horizon)
                    states[key][name].add(state)
                    if completed_horizon_judgment(judgment):
                        completions[key].add(name)

    return {
        key: {
            name: name in completions[key]
            for name, values in people.items()
            if len(values) == 1
        }
        for key, people in states.items()
    }


def analyse_review_progress(
    records: list[dict[str, Any]],
    *,
    dispute_audit: dict[str, Any],
    progress_path: Path,
    target: int,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> dict[str, Any]:
    """Attribute a finished fourth judgment using a historical 3-voter snapshot.

    Exports lack reliable per-judgment timestamps and often store annotators in
    alphabetical order. A new reviewer is therefore identified by comparing
    current observations to the original three-voter disagreement, retained in
    a persistent JSON file. Existing 4+ ratings with no previous baseline are
    deliberately *not* guessed/attributed to anyone.
    """
    progress = load_review_progress(progress_path)
    tracked = progress["tracked_units"]
    changed = not progress_path.exists()
    for unit in dispute_audit["tracking_units"]:
        key = review_unit_key(unit["clip_id"], unit["canonical_action_id"], unit["horizon"])
        if key not in tracked:
            tracked[key] = {
                "original_raters": unit["original_raters"],
                "ineligible_people": unit["ineligible_people"],
                "reviewer": None,
            }
            changed = True

    ratings = collect_current_review_ratings(
        records,
        explicit_ai_names=explicit_ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
    )
    # Count only selected/current records even if the progress ledger contains
    # baselines for other previously examined row ranges.
    selected_clip_ids = {
        normalize_name(record.get("clip_id")) for record in records
        if not is_excluded(normalize_name(record.get("clip_id")), excluded_clip_ids)
    }
    roster = tuple(dispute_audit["reviewers"])
    roster_by_casefold = {name.casefold(): name for name in roster}
    counts = dict.fromkeys(roster, 0)
    tracked_visible = 0
    ambiguous = 0

    for key, entry in tracked.items():
        try:
            clip_id, _cid, _horizon = json.loads(key)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid unit identifier in review state: {key}") from exc
        if clip_id not in selected_clip_ids:
            continue
        tracked_visible += 1
        current = ratings.get(key, {})
        current_by_casefold = {name.casefold(): (name, complete)
                               for name, complete in current.items()}
        original = {name.casefold() for name in entry["original_raters"]}
        if not original.issubset(current_by_casefold):
            continue
        blocked = {name.casefold() for name in entry["ineligible_people"]}
        attributed = entry.get("reviewer")
        if attributed:
            # A previously established fourth reviewer keeps credit while
            # their completed judgment and the original three still exist.
            match = current_by_casefold.get(str(attributed).casefold())
            if match is not None and match[1]:
                roster_name = roster_by_casefold.get(str(attributed).casefold())
                if roster_name is not None:
                    counts[roster_name] += 1
            continue

        # Only a *single* new fourth valid voter can be inferred. If two
        # newcomers appear between exports, attribution cannot be recovered.
        if len(current_by_casefold) != 4:
            if len(current_by_casefold) > 4:
                ambiguous += 1
            continue
        newcomers = [
            roster_by_casefold[name]
            for name, (_display, complete) in current_by_casefold.items()
            if name not in blocked and name not in original and name in roster_by_casefold
            and complete
        ]
        if len(newcomers) != 1:
            continue
        reviewer = newcomers[0]
        entry["reviewer"] = reviewer
        counts[reviewer] += 1
        changed = True

    if changed:
        write_review_progress(progress_path, progress)

    return {
        "counts": counts,
        "target": target,
        "reviewers": roster,
        "tracked_units": tracked_visible,
        "ambiguous_units": ambiguous,
        "progress_path": progress_path,
    }


def analyse_self_annotations(
    records: list[dict[str, Any]],
    *,
    start_row: int,
    explicit_ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    involved_people: set[str] = set()

    for local_index, record in enumerate(records):
        row_number = start_row + local_index + 1
        clip_id = normalize_name(record.get("clip_id"))
        if is_excluded(clip_id, excluded_clip_ids):
            continue

        provenance = build_canonical_provenance(record)

        annotations = record.get("annotations") or []
        if not isinstance(annotations, list):
            annotations = []

        action_annotators: dict[str, set[str]] = collections.defaultdict(set)
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            name = normalize_name(annotation.get("annotator_id"))
            if is_excluded(name, excluded_annotators) or is_ai_annotator(name, explicit_ai_names):
                continue
            actions = annotation.get("canonical_actions") or []
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, dict):
                    continue
                cid = canonical_id(action)
                if cid not in provenance:
                    continue
                judgments = action.get("horizon_judgments") or {}
                if not isinstance(judgments, dict):
                    continue
                if any(valid_binary_judgment(judgment) is not None for judgment in judgments.values()):
                    action_annotators[cid].add(name)

        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            annotator = normalize_name(annotation.get("annotator_id"))
            if is_excluded(annotator, excluded_annotators) or is_ai_annotator(annotator, explicit_ai_names):
                continue
            actions = annotation.get("canonical_actions") or []
            if not isinstance(actions, list):
                continue

            for action in actions:
                if not isinstance(action, dict):
                    continue
                cid = canonical_id(action)
                info = provenance.get(cid)
                if info is None:
                    continue
                matched = info["proposals_by_person"].get(annotator, [])
                if not matched:
                    continue

                judgments = action.get("horizon_judgments") or {}
                if not isinstance(judgments, dict):
                    continue
                valid_by_horizon = {
                    str(horizon): state
                    for horizon, judgment in judgments.items()
                    if (state := valid_binary_judgment(judgment)) is not None
                }
                if not valid_by_horizon:
                    continue

                rows.append({
                    "row": row_number,
                    "canonical_action_id": cid,
                    "action": action_text(info["pool_item"]),
                    "annotator": annotator,
                    "annotators": sorted(action_annotators.get(cid, set()), key=str.casefold),
                })
                involved_people.add(annotator)

    rows.sort(key=lambda row: (row["row"], row["canonical_action_id"], row["annotator"].casefold()))
    return {
        "rows": rows,
        "events": len(rows),
        "affected_actions": len({(r["row"], r["canonical_action_id"]) for r in rows}),
        "people": sorted(involved_people, key=str.casefold),
    }


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def badge(value: str, css_class: str = "") -> str:
    return f'<span class="badge {esc(css_class)}">{esc(value)}</span>'


def render_two_yes_one_no_table(audit: dict[str, Any]) -> str:
    if not audit["rows"]:
        return '<div class="clear-box">✓ No strict 2 Yes / 1 No units found.</div>'

    options = "".join(
        f'<option value="{esc(name)}">{esc(name)}</option>'
        for name in audit["reviewers"]
    )
    filter_html = (
        '<div class="filter-toolbar">'
        '<label for="eligible-reviewer-filter">Show actions eligible for</label>'
        '<select id="eligible-reviewer-filter" aria-controls="disagreement-table">'
        '<option value="">All reviewers (all actions)</option>'
        f'{options}'
        '</select>'
        f'<span class="filter-status" id="eligible-filter-status" role="status" aria-live="polite">'
        f'Showing all {fmt_int(len(audit["rows"]))} actions '
        f'({fmt_int(audit["units"])} disputed time bands)</span>'
        '</div>'
    )
    body = []
    for row in audit["rows"]:
        bands_html = "".join(
            f'<div class="band-line">{esc(item["band"])}</div>'
            for item in row["disagreements"]
        )
        annotators_html = " ".join(badge(name) for name in row["annotators"]) or "—"
        proposers_html = " ".join(badge(name, "proposer") for name in row["proposers"]) or "—"
        eligible_html = " ".join(
            badge(name, "eligible") for name in row["eligible_reviewers"]
        ) or '<span class="none-eligible">None</span>'
        # Escaped JSON in a data attribute keeps exact names intact (including
        # punctuation and non-ASCII characters), without relying on delimiters.
        eligible_json = esc(json.dumps(row["eligible_reviewers"], ensure_ascii=False))
        body.append(
            f'<tr data-eligible="{eligible_json}" data-band-count="{len(row["disagreements"])}">'
            f'<td class="num">{fmt_int(row["row"])}</td>'
            f'<td class="ca">{esc(row["canonical_action_id"])}</td>'
            f'<td class="action">{esc(row["action"])}</td>'
            f'<td class="band">{bands_html}</td>'
            f'<td class="annotators">{annotators_html}</td>'
            f'<td class="proposers">{proposers_html}</td>'
            f'<td class="eligible-reviewers">{eligible_html}</td>'
            "</tr>"
        )
    table_html = (
        '<div class="table-wrap"><table id="disagreement-table" '
        'class="audit-table disagreement-table">'
        '<thead><tr><th>Row</th><th>CA</th><th>Action</th><th>Time bands</th>'
        '<th>Annotated by</th><th>Proposed by</th><th>Eligible reviewers</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )
    return (
        filter_html + table_html +
        '<div class="filter-empty" id="eligible-filter-empty" role="status" hidden></div>'
    )


# The report is self-contained: filtering runs entirely in the generated HTML.
REVIEWER_FILTER_SCRIPT = """<script>
(() => {
  'use strict';
  const filter = document.getElementById('eligible-reviewer-filter');
  if (!filter) return;
  const rows = Array.from(document.querySelectorAll('#disagreement-table tbody tr[data-eligible]'));
  const status = document.getElementById('eligible-filter-status');
  const empty = document.getElementById('eligible-filter-empty');
  const format = (count) => new Intl.NumberFormat().format(count);

  function applyReviewerFilter() {
    const reviewer = filter.value;
    let actions = 0;
    let bands = 0;
    for (const row of rows) {
      const eligible = JSON.parse(row.dataset.eligible || '[]');
      const visible = !reviewer || eligible.includes(reviewer);
      row.hidden = !visible;
      if (visible) {
        actions += 1;
        bands += Number(row.dataset.bandCount || 0);
      }
    }
    status.textContent = reviewer
      ? `Showing ${format(actions)} eligible actions (${format(bands)} disputed time bands)`
      : `Showing all ${format(actions)} actions (${format(bands)} disputed time bands)`;
    empty.hidden = !reviewer || actions > 0;
    if (!empty.hidden) empty.textContent = `No disputed actions are eligible for ${reviewer}.`;
  }

  filter.addEventListener('change', applyReviewerFilter);
  applyReviewerFilter();
})();
</script>"""


def render_review_progress_chart(progress: dict[str, Any]) -> str:
    target = progress["target"]
    items = sorted(
        ((name, int(progress["counts"].get(name, 0))) for name in progress["reviewers"]),
        key=lambda item: (-item[1], item[0].casefold()),
    )
    bars = []
    for name, count in items:
        percent = 100 * count / target
        bars.append(
            '<div class="progress-bar-row">'
            f'<div class="progress-person" title="{esc(name)}">{esc(name)}</div>'
            f'<div class="progress-track" role="progressbar" aria-label="{esc(name)}" '
            f'aria-valuemin="0" aria-valuemax="{target}" '
            f'aria-valuenow="{min(target, count)}">'
            f'<div class="progress-fill" style="width:{min(100., percent):.2f}%"></div>'
            '</div>'
            f'<div class="progress-count"><b>{fmt_int(count)}</b>'
            f'<span> / {fmt_int(target)}</span></div>'
            '</div>'
        )
    return (
        '<section class="panel review-progress">'
        '<div class="progress-heading"><h2>Adjudication progress</h2>'
        f'<span class="progress-target">Target: {fmt_int(target)} units per person</span></div>'
        '<p>One unit is one previously disputed time band completed by an eligible fourth reviewer. '
        'Attribution begins after an original three-rater baseline is saved.</p>'
        f'<div class="progress-bars">{"".join(bars)}</div>'
        '</section>'
    )


def render_self_annotation_table(audit: dict[str, Any]) -> str:
    if not audit["rows"]:
        return '<div class="clear-box">✓ No self-annotation/proposer overlap found.</div>'
    body = []
    for row in audit["rows"]:
        annotators_html = " ".join(badge(name) for name in row.get("annotators", [])) or "—"
        body.append(
            "<tr>"
            f'<td class="num">{fmt_int(row["row"])}</td>'
            f'<td class="ca">{esc(row["canonical_action_id"])}</td>'
            f'<td class="action">{esc(row["action"])}</td>'
            f'<td class="person">{esc(row["annotator"])}</td>'
            f'<td class="annotators">{annotators_html}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="audit-table self-table">'
        '<thead><tr><th>Row</th><th>CA</th><th>Canonical action</th><th>Annotator / proposer</th>'
        '<th>Annotated by</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def render_dashboard(
    two_one: dict[str, Any],
    self_audit: dict[str, Any],
    review_progress: dict[str, Any],
    *,
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
    explicit_ai_names: set[str],
) -> str:
    generated = dt.datetime.now().astimezone()
    people = ", ".join(self_audit["people"]) if self_audit["people"] else "none"
    exclusions = (
        f"Annotators: {', '.join(sorted(excluded_annotators)) or 'none'} · "
        f"Clips: {', '.join(sorted(excluded_clip_ids)) or 'none'}"
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Annotation Conflict Audit</title>
<style>
:root{{--bg:#090d18;--panel:#111827;--text:#f4f7ff;--muted:#9aa8c5;--line:#28344e;--green:#67d89f;--red:#ff7f8c;--orange:#ffbd73;--purple:#bb91ff}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;min-height:100vh;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 12% -5%,rgba(83,119,255,.22),transparent 34rem),radial-gradient(circle at 92% 12%,rgba(255,127,140,.10),transparent 30rem),var(--bg)}}
.container{{max-width:1560px;margin:0 auto;padding:30px 24px 64px}}.hero{{border:1px solid var(--line);border-radius:26px;padding:30px;background:linear-gradient(135deg,rgba(30,46,80,.98),rgba(16,24,41,.98));box-shadow:0 24px 70px rgba(0,0,0,.28)}}
.eyebrow{{color:#b8c8eb;text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:800}}h1{{margin:8px 0 10px;font-size:clamp(32px,5vw,54px);letter-spacing:-.045em}}.hero-meta{{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:20px;color:#8fa2c7;font-size:12px}}.hero-meta b{{color:#dce6fb}}
.panel{{margin-top:18px;border:1px solid var(--line);border-radius:21px;padding:20px;background:rgba(17,24,39,.95);box-shadow:0 14px 34px rgba(0,0,0,.16)}}.panel h2{{margin:0;font-size:20px}}.panel p{{margin:6px 0 14px;color:var(--muted);line-height:1.58;font-size:11px}}.section-one{{border-top:3px solid var(--orange)}}.section-two{{border-top:3px solid var(--purple)}}
.summary-line{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 16px}}.summary-pill{{border:1px solid var(--line);border-radius:999px;background:#0d1422;color:#cbd8f1;padding:6px 10px;font-size:10px}}.table-wrap{{width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:14px}}.audit-table{{width:100%;border-collapse:collapse;table-layout:auto;min-width:760px}}.audit-table th,.audit-table td{{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}}.audit-table th{{position:sticky;top:0;z-index:1;color:#95a8cc;background:#151e31;text-transform:uppercase;letter-spacing:.055em;font-size:9px}}.audit-table td{{color:#dce6fb;font-size:11px;line-height:1.45}}.audit-table tbody tr:last-child td{{border-bottom:0}}.audit-table tbody tr:hover td{{background:#141d30}}.disagreement-table{{min-width:1320px}}.disagreement-table th:first-child{{min-width:56px;white-space:nowrap}}.disagreement-table .annotators,.disagreement-table .proposers,.disagreement-table .eligible-reviewers{{min-width:160px;max-width:250px}}
.audit-table tr[hidden],.filter-empty[hidden]{{display:none}}.filter-toolbar{{display:flex;align-items:center;flex-wrap:wrap;gap:10px 14px;margin:14px 0 16px;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#0d1422}}.filter-toolbar label{{font-size:11px;font-weight:800;color:#dbe6fa}}.filter-toolbar select{{min-width:230px;max-width:100%;border:1px solid #425573;border-radius:9px;background:#1b2840;color:#f4f7ff;padding:8px 12px;font-size:12px;cursor:pointer}}.filter-toolbar select:focus-visible{{outline:2px solid #77afff;outline-offset:2px}}.filter-status{{font-size:11px;color:var(--muted);margin-left:auto}}.filter-empty{{margin-top:12px;padding:13px 15px;border:1px dashed var(--line);border-radius:12px;font-size:11px;color:#cbd8f1}}.num,.ca,.band,.person{{white-space:nowrap;font-weight:800}}.num{{text-align:right;font-variant-numeric:tabular-nums}}.action{{min-width:300px;max-width:520px}}.band-line{{white-space:nowrap;margin:1px 0}}.vote-line{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin:3px 0}}.subband{{display:inline-block;min-width:112px;color:#9fb0cf;font-size:9px;font-weight:800}}
.badge{{display:inline-block;margin:1px 4px 1px 0;padding:4px 8px;border-radius:999px;border:1px solid var(--line);background:#0d1422;color:#dce6fb;font-size:9px;font-weight:800}}.badge.yes{{color:#aee8cb;border-color:#356653;background:rgba(103,216,159,.08)}}.badge.no{{color:#ffb5bc;border-color:#70404a;background:rgba(255,127,140,.08)}}.badge.direct{{color:#b8d1ff;border-color:#425f8e;background:rgba(120,167,255,.09)}}.badge.duplicate{{color:#dfcaff;border-color:#59477a;background:rgba(187,145,255,.09)}}.badge.proposer{{color:#c6b8fc;border-color:#514775;background:rgba(187,145,255,.08)}}.badge.eligible{{color:#a5e6ca;border-color:#326251;background:rgba(103,216,159,.07)}}.none-eligible{{color:#8695ae;font-style:italic}}.judgment{{display:inline-block;margin:2px 4px 2px 0;padding:4px 7px;border-radius:8px;border:1px solid var(--line);font-size:9px;white-space:nowrap}}.judgment.yes{{color:#aee8cb;border-color:#356653;background:rgba(103,216,159,.07)}}.judgment.no{{color:#ffb5bc;border-color:#70404a;background:rgba(255,127,140,.07)}}
.review-progress{{border-top:3px solid #78a7ff}}.progress-heading{{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}}.progress-target{{color:#d9e6ff;border:1px solid #405778;background:#182742;padding:6px 10px;border-radius:999px;font-size:11px;font-weight:800}}.progress-bars{{margin:18px 0 4px}}.progress-bar-row{{display:grid;grid-template-columns:minmax(145px,240px) minmax(100px,1fr) 108px;align-items:center;gap:14px;margin:14px 0}}.progress-person{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:700;color:#e1eafa}}.progress-track{{height:13px;border-radius:999px;overflow:hidden;background:#202b42}}.progress-fill{{height:100%;border-radius:999px;background:linear-gradient(90deg,#78a7ff,#62d6d2)}}.progress-count{{text-align:right;white-space:nowrap;color:#9eafcd;font-size:12px;font-variant-numeric:tabular-nums}}.progress-count b{{color:#e1eafa;font-size:14px}}.clear-box{{padding:13px 15px;border:1px dashed rgba(103,216,159,.34);border-radius:12px;color:#9be9bf;background:rgba(103,216,159,.035);font-size:11px}}.method-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}}.method-item{{padding:14px;border-radius:14px;background:#0d1422;border:1px solid var(--line)}}.method-item b{{display:block;margin-bottom:5px;font-size:11px}}.method-item span{{color:var(--muted);font-size:10px;line-height:1.55}}code{{background:#0a1120;border:1px solid var(--line);border-radius:6px;padding:2px 5px}}footer{{margin-top:24px;text-align:center;color:#70809f;font-size:10px}}
@media(max-width:1120px){{.method-grid{{grid-template-columns:1fr}}}}@media(max-width:700px){{.container{{padding:14px 10px 36px}}.hero,.panel{{padding:16px;border-radius:17px}}.progress-bar-row{{grid-template-columns:minmax(100px,145px) minmax(70px,1fr) 79px;gap:7px}}.progress-person{{font-size:10px}}.progress-count{{font-size:10px}}.progress-count b{{font-size:11px}}}}
</style></head><body><div class="container">
<section class="hero"><div class="eyebrow">Temporal NoRA · annotation audit</div><h1>Annotation Conflict Audit</h1><div class="hero-meta"><span><b>Generated:</b> {esc(generated.strftime("%d %B %Y, %I:%M %p %Z").lstrip("0"))}</span></div></section>
{render_review_progress_chart(review_progress)}
<section class="panel section-one"><h2>2 Yes + 1 No disagreements</h2>{render_two_yes_one_no_table(two_one)}</section>
<section class="panel section-two"><h2>Annotator rated their own proposed action</h2><p>A finding requires at least one valid Yes/No judgment by the same included human who proposed the action. The table also lists everyone who supplied at least one valid Yes/No judgment on that action. Confirmed duplicate groups are expanded so a proposal remains attributable to its original proposer even when another proposal is selected as the representative canonical action.</p><div class="summary-line"><span class="summary-pill"><b>{fmt_int(self_audit["events"])}</b> self-annotation events</span><span class="summary-pill"><b>{fmt_int(self_audit["affected_actions"])}</b> affected canonical actions</span><span class="summary-pill"><b>{fmt_int(len(self_audit["people"]))}</b> people</span><span class="summary-pill"><b>People:</b> {esc(people)}</span></div>{render_self_annotation_table(self_audit)}</section>
<section class="panel"><h2>Method & filters</h2><div class="method-grid"><div class="method-item"><b>Human-only audit</b><span>Known/common model names are excluded. Explicit model names: {esc(", ".join(sorted(explicit_ai_names, key=str.casefold)) or "none")}.</span></div><div class="method-item"><b>Default exclusions</b><span>{esc(exclusions)}. Use <code>--include-default-exclusions</code> to disable the built-in Ning/API-Test exclusions.</span></div><div class="method-item"><b>Proposal provenance & reviewer roster</b><span>Canonical <code>proposal_ids</code> are expanded only through <code>confirmed</code> duplicate groups. The six reviewers are listed in <code>DEFAULT_REVIEWERS</code>; use <code>--reviewer NAME</code> (repeatable) to override the roster.</span></div></div></section>
<footer>Generated by annotation_conflict_audit.py · self-contained HTML</footer></div>{REVIEWER_FILTER_SCRIPT}</body></html>'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit 2-Yes/1-No disagreements and annotator/proposer overlap, then generate HTML."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Annotation site URL (default: {DEFAULT_URL})")
    parser.add_argument("--annotator", default=DEFAULT_ANNOTATOR, help=f"Name used to log in (default: {DEFAULT_ANNOTATOR})")
    parser.add_argument("--input", type=Path, help="Use an existing JSON/JSONL export instead of downloading /api/export.")
    parser.add_argument("--export-path", type=Path, default=DEFAULT_EXPORT, help=f"Downloaded export path (default: {DEFAULT_EXPORT})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"HTML output path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--start-row", type=int, default=0, help="0-based first clip row to analyse (default: 0).")
    parser.add_argument("-n", "--num-rows", type=int, default=None, help="Number of clip rows from --start-row (default: all remaining rows).")
    parser.add_argument("--ai-annotator", action="append", default=[], help="Additional annotator name to classify as AI/model. May be repeated.")
    parser.add_argument("--reviewer", action="append", default=[], help="Replace the default six-person reviewer roster (repeat for each person).")
    parser.add_argument("--review-target", type=int, default=DEFAULT_REVIEW_TARGET, help=f"Completed adjudication units targeted per person (default: {DEFAULT_REVIEW_TARGET}).")
    parser.add_argument("--progress-state", type=Path, default=DEFAULT_PROGRESS_STATE,
                        help=f"Persistent pre-review baseline file (default: {DEFAULT_PROGRESS_STATE}). Keep it between scheduled runs.")
    parser.add_argument("--exclude-clip", action="append", default=[], help="Additional clip ID to exclude. May be repeated.")
    parser.add_argument("--exclude-annotator", action="append", default=[], help="Additional annotator name to exclude. May be repeated.")
    parser.add_argument("--include-default-exclusions", action="store_true", help="Do not automatically exclude Ning or API-Test-Clip190. AI/model exclusions still apply.")
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()
    if args.start_row < 0:
        raise ValueError("--start-row must be >= 0")
    if args.num_rows is not None and args.num_rows <= 0:
        raise ValueError("-n/--num-rows must be > 0")
    if args.review_target <= 0:
        raise ValueError("--review-target must be > 0")

    if args.input is not None:
        export_path: Path = args.input
        if not export_path.exists():
            raise FileNotFoundError(export_path)
    else:
        export_path = download_export(args)

    all_records = load_export_records(export_path)
    total_records = len(all_records)
    if args.start_row > total_records:
        raise ValueError(f"--start-row {args.start_row} is beyond the export ({total_records} records)")
    end_row = total_records if args.num_rows is None else min(total_records, args.start_row + args.num_rows)
    records = all_records[args.start_row:end_row]

    ai_names = set(KNOWN_AI_ANNOTATORS)
    ai_names.update(args.ai_annotator)
    excluded_clip_ids: set[str] = set()
    excluded_annotators: set[str] = set()
    if not args.include_default_exclusions:
        excluded_clip_ids.update(DEFAULT_EXCLUDED_CLIP_IDS)
        excluded_annotators.update(DEFAULT_EXCLUDED_ANNOTATORS)
    excluded_clip_ids.update(args.exclude_clip)
    excluded_annotators.update(args.exclude_annotator)

    reviewers = tuple(dict.fromkeys(name.strip() for name in args.reviewer if name.strip())) if args.reviewer else DEFAULT_REVIEWERS
    # Names excluded by the audit cannot be counted as eligible reviewers.
    reviewers = tuple(
        name for name in reviewers
        if not is_excluded(name, excluded_annotators)
        and not is_ai_annotator(name, ai_names)
    )
    if not reviewers:
        raise ValueError("Reviewer roster is empty after applying exclusions")

    print("\n=== ANALYSE ===")
    print(f"Loaded {total_records:,} clip record(s)")
    if records:
        print(f"Analysing source rows {args.start_row + 1}..{end_row} ({len(records):,} record(s))")
    else:
        print("No records selected")

    two_one = analyse_two_yes_one_no(
        records,
        start_row=args.start_row,
        explicit_ai_names=ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
        reviewers=reviewers,
    )
    progress_path = args.progress_state
    review_progress = analyse_review_progress(
        records,
        dispute_audit=two_one,
        progress_path=progress_path,
        target=args.review_target,
        explicit_ai_names=ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
    )
    self_audit = analyse_self_annotations(
        records,
        start_row=args.start_row,
        explicit_ai_names=ai_names,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
    )

    report = render_dashboard(
        two_one,
        self_audit,
        review_progress,
        excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
        explicit_ai_names=ai_names,
    )

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(report, encoding="utf-8")
    temp_path.replace(output_path)

    print(f"Completed independent adjudications:     {sum(review_progress['counts'].values()):,}")
    print(f"Per-person review target:                {args.review_target:,} units")
    for person in reviewers:
        print(f"  {person}: {review_progress['counts'][person]:,}/{args.review_target:,}")
    if review_progress["ambiguous_units"]:
        print(f"Ambiguous 5+ voter units (not attributed): {review_progress['ambiguous_units']:,}")
    print(f"Strict 2 Yes + 1 No units:               {two_one['units']:,}")
    print(f"Canonical actions affected by 2/1 split: {two_one['affected_actions']:,}")
    print(f"Self-annotation events:                  {self_audit['events']:,}")
    print(f"People with self-annotation overlap:     {len(self_audit['people']):,}")
    if self_audit["people"]:
        print(f"  {', '.join(self_audit['people'])}")
    print("\n=== REPORT ===")
    print(f"HTML: {output_path}")
    print(f"Review progress state: {progress_path}")
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
