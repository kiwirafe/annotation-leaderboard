#!/usr/bin/env python3
"""Generate a self-contained HTML audit for Temporal NoRA annotations.

Build and persist a fair action-level review queue for Temporal NoRA.

Two types of review are merged into one queue:
- Strict 2 Yes / 1 No disagreements: their disputed time bands need a new
  independent human annotation (fourth reviewer).
- Self-annotations: all five bands need a *new*, independent fourth human
  reviewer who neither proposed nor originally annotated the action.

An entire canonical action always belongs to one reviewer, never split by band.
Assignments are balanced by assigned unit count while preferring to keep clips
together. The assignments and three-voter baseline are persisted in a JSON
state file, so each GitHub Actions run retains earlier decisions.

For both review types, completion requires a complete *new* judgment from
its assigned independent reviewer. Historical annotators never automatically
receive credit for self-annotation review. Self-review baselines are frozen in
the saved state because the export does not record review chronology.
The self-contained HTML has a required reviewer selector and green/red status
per time band. No third-party packages are needed.
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
# The reference leaderboard displays its update time in fixed AEST (UTC+10),
# even when GitHub Actions runs on a UTC server.
AEST = dt.timezone(dt.timedelta(hours=10), name="AEST")

DEFAULT_REVIEW_TARGET = 700  # Legacy CLI flag, retained for old workflows.
REVIEW_PROGRESS_FILENAME = "annotation_review_progress.json"
# Stored under docs/review_state to persist alongside the GitHub Pages output.
# WARNING: On a public Pages site, the JSON review ledger is publicly accessible.
# On ephemeral runners, commit this file between scheduled runs.
DEFAULT_PROGRESS_STATE = Path("docs") / "review_state" / REVIEW_PROGRESS_FILENAME


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


def review_unit_key(clip_id: str, action_id: str, horizon: str) -> str:
    """JSON keys avoid collisions and remain stable across export row changes."""
    return json.dumps([clip_id, action_id, horizon], ensure_ascii=False,
                      separators=(",", ":"))


def review_action_key(clip_id: str, action_id: str) -> str:
    return json.dumps([clip_id, action_id], ensure_ascii=False,
                      separators=(",", ":"))


def load_review_progress(path: Path) -> dict[str, Any]:
    """Open old v1 progress files without discarding their tracked votes."""
    if not path.is_file():
        return {"version": 1, "tracked_units": {}, "self_actions": {}, "assignments": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read review state {path}: {exc}") from exc
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("tracked_units"), dict)):
        raise ValueError(f"Invalid/unsupported review state {path}")
    for section in ("self_actions", "assignments"):
        if section not in data:
            data[section] = {}
        if not isinstance(data[section], dict):
            raise ValueError(f"Invalid {section} section in {path}")
    for key, unit in data["tracked_units"].items():
        if (not isinstance(key, str) or not isinstance(unit, dict)
                or not isinstance(unit.get("original_raters"), list)
                or not isinstance(unit.get("ineligible_people"), list)):
            raise ValueError(f"Invalid tracked unit {key} in {path}")
    for key, action in data["self_actions"].items():
        if (not isinstance(key, str) or not isinstance(action, dict)
                or not isinstance(action.get("eligible_reviewers"), list)):
            raise ValueError(f"Invalid self-review action {key} in {path}")
        # Older ledgers lack original_annotators. They are upgraded using the
        # current export before any task can receive fourth-review credit.
        if "original_annotators" in action and (
            not isinstance(action["original_annotators"], list)
            or not all(isinstance(name, str) for name in action["original_annotators"])
        ):
            raise ValueError(f"Invalid self-review baseline {key} in {path}")
    for key, assignment in data["assignments"].items():
        if (not isinstance(key, str) or not isinstance(assignment, dict)
                or not isinstance(assignment.get("reviewer"), str)):
            raise ValueError(f"Invalid assignment {key} in {path}")
    return data


def write_review_progress(path: Path, data: dict[str, Any]) -> None:
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


def collect_action_observations(
    records: list[dict[str, Any]],
    *,
    start_row: int,
    ai_names: set[str],
    excluded_clip_ids: set[str],
    excluded_annotators: set[str],
) -> tuple[dict[str, dict[str, Any]], int]:
    """Index human votes once; don't use export order as fourth-vote chronology."""
    actions_by_key: dict[str, dict[str, Any]] = {}
    duplicate_conflicts = 0
    seen_clips: set[str] = set()
    for idx, record in enumerate(records):
        clip_id = normalize_name(record.get("clip_id"))
        if is_excluded(clip_id, excluded_clip_ids):
            continue
        if clip_id in seen_clips:
            raise ValueError(f"Duplicate clip_id {clip_id!r}: stable assignments would collide")
        seen_clips.add(clip_id)
        provenance = build_canonical_provenance(record)
        per_action = {}
        for cid, info in provenance.items():
            key = review_action_key(clip_id, cid)
            per_action[cid] = {
                "key": key, "row": idx + start_row + 1, "clip_id": clip_id,
                "canonical_action_id": cid,
                "action": action_text(info["pool_item"]),
                "proposers": list(info["proposers"]),
                "annotators": set(),
                "states": collections.defaultdict(lambda: collections.defaultdict(set)),
                "completions": collections.defaultdict(set),
                "disputed": {},
                "self_annotators": set(),
            }
            if key in actions_by_key:
                raise ValueError(f"Duplicate canonical action: {key}")
            actions_by_key[key] = per_action[cid]

        annotations = record.get("annotations") or []
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            name = normalize_name(annotation.get("annotator_id"))
            if (is_excluded(name, excluded_annotators)
                    or is_ai_annotator(name, ai_names)):
                continue
            rated_actions = annotation.get("canonical_actions") or []
            if not isinstance(rated_actions, list):
                continue
            for rated_action in rated_actions:
                if not isinstance(rated_action, dict):
                    continue
                item = per_action.get(canonical_id(rated_action))
                if item is None:
                    continue
                judgments = rated_action.get("horizon_judgments") or {}
                if not isinstance(judgments, dict):
                    continue
                for horizon in EXPECTED_HORIZONS:
                    judgment = judgments.get(horizon)
                    state = valid_binary_judgment(judgment)
                    if state is None:
                        continue
                    item["annotators"].add(name)
                    item["states"][horizon][name].add(state)
                    if completed_horizon_judgment(judgment):
                        item["completions"][horizon].add(name)

    for item in actions_by_key.values():
        # Conflicting duplicate votes by the *same* person never count as
        # an independent 2/1 judgment, even if one entry is otherwise complete.
        for horizon, by_person in item.pop("states").items():
            clean = {}
            for name, states in by_person.items():
                if len(states) == 1:
                    clean[name] = next(iter(states))
                else:
                    duplicate_conflicts += 1
                    item["completions"][horizon].discard(name)
            item.setdefault("ratings", {})[horizon] = clean
            counts = collections.Counter(clean.values())
            if len(clean) == 3 and counts["yes"] == 2 and counts["no"] == 1:
                item["disputed"][horizon] = sorted(clean, key=str.casefold)
        proposer_names = {name.casefold() for name in item["proposers"]}
        item["self_annotators"] = {
            name for name in item["annotators"] if name.casefold() in proposer_names
        }
        item["annotators"] = sorted(item["annotators"], key=str.casefold)
    return actions_by_key, duplicate_conflicts


def discover_tasks(
    actions: dict[str, dict[str, Any]],
    state: dict[str, Any],
    reviewers: tuple[str, ...],
) -> tuple[list[dict[str, Any]], bool]:
    """Create stable action-level tasks by merging disagreements and self-audits.

    Self-annotation overrides the disagreement route for the same canonical
    action: five bands, with a NEW fourth reviewer who did not participate in
    the original annotation and is not a proposer. No band can be split.
    """
    changed = False
    tracked = state["tracked_units"]
    self_actions = state["self_actions"]
    roster = {name.casefold(): name for name in reviewers}
    history_by_action: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)

    # Load previous 2/1 baselines. Their tasks stay visible when fourth ratings
    # arrive (at that point the current unit is no longer a strict 2/1 split).
    for unit_key, baseline in tracked.items():
        try:
            clip_id, cid, horizon = json.loads(unit_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Malformed persisted unit key {unit_key}") from exc
        if horizon not in EXPECTED_HORIZONS:
            continue
        action_key = review_action_key(clip_id, cid)
        if action_key in actions:
            history_by_action[action_key][horizon] = baseline

    for action in actions.values():
        for horizon, original_raters in action["disputed"].items():
            unit_key = review_unit_key(action["clip_id"], action["canonical_action_id"], horizon)
            if unit_key not in tracked:
                blocked = sorted(set(action["annotators"]) | set(action["proposers"]),
                                 key=str.casefold)
                tracked[unit_key] = {
                    "original_raters": original_raters,
                    "ineligible_people": blocked,
                    "reviewer": None,  # Backward compatible with the old review ledger.
                }
                changed = True
            history_by_action[action["key"]][horizon] = tracked[unit_key]

        if action["self_annotators"] and action["key"] not in self_actions:
            # Freeze EVERY original annotator, not just the self-annotator.
            # An independent reviewer must be a distinct fourth human.
            original = sorted(action["annotators"], key=str.casefold)
            blocked = {name.casefold() for name in original + action["proposers"]}
            self_actions[action["key"]] = {
                "original_annotators": original,
                "original_self_annotators": sorted(action["self_annotators"], key=str.casefold),
                "eligible_reviewers": [r for r in reviewers if r.casefold() not in blocked],
            }
            changed = True

        # Upgrade ledgers written by the previous implementation, which
        # incorrectly assigned self-reviews to existing annotators. At upgrade
        # time we cannot tell from the export whether an annotation is an
        # actual later review, so freeze all CURRENT annotators as the safe
        # baseline. This deliberately revokes false completion credit without
        # overwriting previously tracked 2/1 disagreement votes.
        if action["key"] in self_actions and "original_annotators" not in self_actions[action["key"]]:
            old = self_actions[action["key"]]
            old["original_annotators"] = sorted(action["annotators"], key=str.casefold)
            blocked = {name.casefold() for name in old["original_annotators"] + action["proposers"]}
            old["eligible_reviewers"] = [r for r in reviewers if r.casefold() not in blocked]
            changed = True

    tasks: list[dict[str, Any]] = []
    for key, action in actions.items():
        dispute_units = history_by_action.get(key, {})
        self_info = self_actions.get(key)
        if not dispute_units and self_info is None:
            continue
        is_self = self_info is not None
        if is_self:
            # Even if the conflict audit found just one disputed band, all five
            # are part of the self-annotation review. Eligibility is frozen to
            # original annotators; do NOT exclude a later fourth reviewer just
            # because their new judgment now appears in the export.
            bands = list(EXPECTED_HORIZONS)
            original = {name.casefold() for name in self_info["original_annotators"]}
            current_proposers = {name.casefold() for name in action["proposers"]}
            candidates = [name for name in reviewers
                          if name.casefold() not in original
                          and name.casefold() not in current_proposers]
            if self_info["eligible_reviewers"] != candidates:
                self_info["eligible_reviewers"] = candidates
                changed = True
        else:
            bands = [h for h in EXPECTED_HORIZONS if h in dispute_units]
            # Freeze the original action-wide exclusion sets to avoid treating
            # the newly-arrived fourth reviewer as an original annotator.
            blocked = {name.casefold() for unit in dispute_units.values()
                       for name in unit["ineligible_people"]}
            blocked.update(name.casefold() for name in action["proposers"])
            candidates = [name for name in reviewers if name.casefold() not in blocked]
        # Restrict both types to the configured six-person roster.
        candidates = list(dict.fromkeys(candidates))
        task = {k: action[k] for k in ("key", "row", "clip_id", "canonical_action_id", "action")}
        task.update({
            "proposers": action["proposers"],
            "annotators": action["annotators"],
            "type": "self" if is_self else "disagreement",
            "had_disagreement": bool(dispute_units),
            "self_annotators": self_info.get("original_self_annotators", []) if self_info else [],
            "original_annotators": self_info.get("original_annotators", []) if self_info else [],
            "bands": bands,
            "unit_count": len(bands),
            "eligible_reviewers": candidates,
            "baselines": dispute_units,
            "ratings": action.get("ratings", {}),
            "completions": action["completions"],
            "assigned_to": None,
        })
        tasks.append(task)
    tasks.sort(key=lambda t: (t["row"], t["canonical_action_id"]))
    return tasks, changed


def _inferred_legacy_reviewer(task: dict[str, Any], roster: tuple[str, ...]) -> str | None:
    """Preserve a previously-attributed fourth reviewer, when unambiguous."""
    roster_map = {name.casefold(): name for name in roster}
    remembered = [roster_map[str(unit.get("reviewer")).casefold()]
                  for unit in task["baselines"].values()
                  if unit.get("reviewer") and str(unit["reviewer"]).casefold() in roster_map]
    if remembered:
        counts = collections.Counter(remembered)
        for name, _count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold())):
            if name in task["eligible_reviewers"]:
                return name

    # Historical v1 baselines might record a fourth vote but not yet its name;
    # a single new person on all eligible disputed bands is stronger evidence
    # than assigning the action to an unrelated teammate.
    newcomers = collections.Counter()
    for horizon, unit in task["baselines"].items():
        original = {name.casefold() for name in unit["original_raters"]}
        current = {name.casefold(): name
                   for name in task["ratings"].get(horizon, {})}
        if not original.issubset(current):
            continue
        for folded, name in current.items():
            if folded not in original and name in task["eligible_reviewers"]:
                newcomers[name] += 1
    if newcomers:
        return sorted(newcomers.items(), key=lambda p: (-p[1], p[0].casefold()))[0][0]
    return None


def assign_review_tasks(
    tasks: list[dict[str, Any]], state: dict[str, Any], reviewers: tuple[str, ...]
) -> tuple[bool, list[dict[str, Any]]]:
    """Sticky, balanced assignment; all five (or fewer) bands stay together.

    New clips are processed largest-first. Prefer one person per clip when all
    its actions have an eligible reviewer in common and doing so doesn't create
    extreme imbalance. If a clip can't be kept together, use weighted least
    load with a small preference for whoever already owns work on that clip.
    """
    saved = state["assignments"]
    reviewer_lookup = {name.casefold(): name for name in reviewers}
    changed = False
    load: collections.Counter[str] = collections.Counter({r: 0 for r in reviewers})
    by_key = {task["key"]: task for task in tasks}
    for key, assignment in saved.items():
        name = reviewer_lookup.get(assignment.get("reviewer", "").casefold())
        if name is None:
            continue
        task = by_key.get(key)
        amount = task["unit_count"] if task else int(assignment.get("unit_count", 0))
        load[name] += amount

    for task in tasks:
        old = saved.get(task["key"])
        if not old:
            continue
        name = reviewer_lookup.get(old.get("reviewer", "").casefold())
        if name not in task["eligible_reviewers"]:
            # Never knowingly keep an invalid assignment, e.g. a newly merged
            # proposal reveals that the reviewer was an original proposer.
            if name in reviewers:
                load[name] -= task["unit_count"]
            saved.pop(task["key"])
            changed = True
            continue
        task["assigned_to"] = name
        if old.get("reviewer") != name or old.get("unit_count") != task["unit_count"]:
            saved[task["key"]] = {"reviewer": name, "unit_count": task["unit_count"]}
            changed = True

    def set_assignment(task: dict[str, Any], person: str) -> None:
        nonlocal changed
        task["assigned_to"] = person
        saved[task["key"]] = {"reviewer": person, "unit_count": task["unit_count"]}
        load[person] += task["unit_count"]
        changed = True

    # Pin completed/started legacy work before planning new assignments.
    for task in tasks:
        if task["assigned_to"] or task["type"] == "self":
            continue
        old_name = _inferred_legacy_reviewer(task, reviewers)
        if old_name:
            set_assignment(task, old_name)

    by_clip: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for task in tasks:
        if task["assigned_to"] is None and task["eligible_reviewers"]:
            by_clip[task["clip_id"]].append(task)

    all_weight = sum(task["unit_count"] for task in tasks)
    per_person_average = all_weight / max(1, len(reviewers))
    # Small exports may need a clip split to make the workload meaningful;
    # large exports can keep 20-100 unit clips together almost always.
    group_cap = max(6, int(per_person_average * 1.3))
    existing_clip: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for task in tasks:
        if task["assigned_to"]:
            existing_clip[task["clip_id"]][task["assigned_to"]] += task["unit_count"]

    ordered_clips = sorted(by_clip.items(), key=lambda pair: (
        -sum(t["unit_count"] for t in pair[1]), pair[0]))
    for clip_id, clip_tasks in ordered_clips:
        clip_tasks.sort(key=lambda t: (-t["unit_count"], t["canonical_action_id"]))
        same_candidates = set(clip_tasks[0]["eligible_reviewers"])
        for task in clip_tasks[1:]:
            same_candidates.intersection_update(task["eligible_reviewers"])
        weight = sum(task["unit_count"] for task in clip_tasks)
        if same_candidates and weight <= group_cap:
            # Strong preference for a reviewer already assigned this clip,
            # but avoid piling substantially more onto one person.
            def whole_clip_score(person: str) -> tuple[float, int, str]:
                affinity = 0 if existing_clip[clip_id][person] else min(12, weight * .40)
                return (load[person] + affinity, load[person], person.casefold())
            person = min(same_candidates, key=whole_clip_score)
            for task in clip_tasks:
                set_assignment(task, person)
            existing_clip[clip_id][person] += weight
            continue

        for task in clip_tasks:
            def action_score(person: str) -> tuple[float, int, str]:
                affinity_discount = min(8, max(2, task["unit_count"] * .5)) \
                    if existing_clip[clip_id][person] else 0
                return (load[person] - affinity_discount, load[person], person.casefold())
            person = min(task["eligible_reviewers"], key=action_score)
            set_assignment(task, person)
            existing_clip[clip_id][person] += task["unit_count"]

    unassigned = [task for task in tasks if not task["assigned_to"]]
    return changed, unassigned


def update_review_completion(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute reviewed per band from actual export evidence, not row order.

    Both kinds: assignee must be a new, independent human who supplied a
    complete judgment. Self-annotations additionally require three other
    distinct human judgments on each band before declaring a fourth review.
    """
    reviewed = collections.Counter()
    assigned = collections.Counter()
    for task in tasks:
        person = task["assigned_to"]
        status: dict[str, bool] = {}
        for horizon in task["bands"]:
            complete = False
            if person:
                complete = any(name.casefold() == person.casefold()
                               for name in task["completions"].get(horizon, set()))
                if task["type"] == "disagreement":
                    baseline = task["baselines"][horizon]
                    original = {name.casefold() for name in baseline["original_raters"]}
                    now = {name.casefold() for name in task["ratings"].get(horizon, {})}
                    complete = (complete and person.casefold() not in original
                                and original.issubset(now)
                                and person.casefold() in now)
                else:
                    # Never credit an already-existing annotator as a fourth
                    # reviewer. The original reviewer baseline survives future
                    # workflow runs in the committed review-state JSON.
                    original = {name.casefold() for name in task["original_annotators"]}
                    current_votes = {name.casefold() for name in
                                     task["ratings"].get(horizon, {})}
                    other_votes = current_votes - {person.casefold()}
                    complete = (complete
                                and person.casefold() not in original
                                and person.casefold() not in
                                {name.casefold() for name in task["proposers"]}
                                and person.casefold() in current_votes
                                and len(other_votes) >= 3)
            status[horizon] = bool(complete)
            if person:
                assigned[person] += 1
                if complete:
                    reviewed[person] += 1
        task["reviewed_by_band"] = status
        task["reviewed_count"] = sum(status.values())
    return {"assigned": dict(assigned), "reviewed": dict(reviewed)}


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_int(value: Any) -> str:
    return f"{int(value):,}"


def badge(value: str, css_class: str = "") -> str:
    return f'<span class="badge {esc(css_class)}">{esc(value)}</span>'


def stacked_badges(names: list[str], badge_class: str = "") -> str:
    return ('<div class="stacked-names">'
            + "".join(badge(name, badge_class) for name in names)
            + '</div>') if names else "—"


def render_review_progress_chart(
    tasks: list[dict[str, Any]], roster: tuple[str, ...], totals: dict[str, Any]
) -> str:
    ranked = sorted(roster, key=lambda name: (
        -totals["assigned"].get(name, 0), name.casefold()))
    bars = []
    for name in ranked:
        maximum = int(totals["assigned"].get(name, 0))
        complete = int(totals["reviewed"].get(name, 0))
        percent = (100 * complete / maximum) if maximum else 0
        bars.append(
            '<div class="progress-row">'
            f'<div class="progress-person" title="{esc(name)}">{esc(name)}</div>'
            f'<div class="progress-track" role="progressbar" aria-label="{esc(name)} reviewed units" '
            f'aria-valuemin="0" aria-valuemax="{maximum}" aria-valuenow="{complete}">'
            f'<div class="progress-fill" style="width:{percent:.2f}%"></div></div>'
            f'<div class="progress-count"><b>{fmt_int(complete)}</b>'
            f'<span> / {fmt_int(maximum)}</span></div></div>'
        )
    return (
        '<section class="panel progress-panel">'
        '<div class="panel-header"><h2>Review progress</h2>'
        f'<span class="caption-pill">{fmt_int(sum(totals["assigned"].values()))} units assigned</span></div>'
        '<p>Completed / assigned time-band units. Each bar’s limit is that person’s own assigned total. '
        'Both disagreements and self-annotations require a new, complete fourth '
        'human annotation from the assigned independent reviewer; original '
        'annotations are never counted as reviews.</p>'
        f'<div class="progress-bars">{"".join(bars)}</div></section>'
    )


def render_review_table(tasks: list[dict[str, Any]], reviewers: tuple[str, ...]) -> str:
    options = ''.join(f'<option value="{esc(name)}">{esc(name)}</option>' for name in reviewers)
    rows = []
    for task in tasks:
        if not task["assigned_to"]:
            continue
        bands = ''.join(f'<div class="band-line">{esc(HORIZON_LABELS[h])}</div>'
                        for h in task["bands"])
        # Aligned with the adjacent band rows: one status per time band.
        statuses = ''.join(
            '<div class="band-line">'
            + ('<span class="status done">● Reviewed</span>' if task["reviewed_by_band"][h]
               else '<span class="status pending">● Not reviewed</span>')
            + '</div>' for h in task["bands"]
        )
        type_label = 'Self-annotation' if task['type'] == 'self' else '2 Yes / 1 No'
        title_extra = ' · includes disputed bands' if task['type'] == 'self' and task['had_disagreement'] else ''
        rows.append(
            f'<tr data-assigned="{esc(task["assigned_to"])}" '
            f'data-units="{task["unit_count"]}" '
            f'data-reviewed="{task["reviewed_count"]}" hidden>'
            f'<td class="num">{fmt_int(task["row"])}</td>'
            f'<td class="ca">{esc(task["canonical_action_id"])}</td>'
            f'<td class="action">{esc(task["action"])}</td>'
            f'<td class="category"><span class="type-pill {task["type"]}" '
            f'title="{esc(type_label + title_extra)}">{esc(type_label)}</span></td>'
            f'<td class="band">{bands}</td>'
            f'<td class="annotators">{stacked_badges(task["annotators"])}</td>'
            f'<td class="proposers">{stacked_badges(task["proposers"], "proposer")}</td>'
            f'<td class="review-status">{statuses}</td></tr>'
        )
    return (
        '<section class="panel queue-panel">'
        '<div class="panel-header"><h2>Assigned reviews</h2>'
        f'<span class="caption-pill">{fmt_int(len(tasks))} canonical actions</span></div>'
        '<div class="filter-toolbar">'
        '<label for="assigned-reviewer-filter">Reviewer</label>'
        '<select id="assigned-reviewer-filter" aria-controls="review-table" required>'
        '<option value="">Select your name…</option>'
        f'{options}</select>'
        '<span id="filter-status" class="filter-status" aria-live="polite"></span></div>'
        '<div id="choose-person-warning" class="choice-warning" role="status">'
        'Please select your name to view your assigned review actions.</div>'
        '<div id="selected-reviewer-work" hidden>'
        '<div class="table-wrap"><table class="audit-table" id="review-table">'
        '<thead><tr><th>Row</th><th>CA</th><th>Action</th><th>Review type</th>'
        '<th>Time bands</th><th>Annotated by</th><th>Proposed by</th>'
        f'<th>Reviewed</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        '<div id="filter-empty" class="empty-message" hidden>No review actions assigned to this person.</div>'
        '</div></section>'
    )


FILTER_SCRIPT = r"""<script>
(() => {
  'use strict';
  const picker = document.getElementById('assigned-reviewer-filter');
  const message = document.getElementById('choose-person-warning');
  const work = document.getElementById('selected-reviewer-work');
  const table = document.getElementById('review-table');
  const status = document.getElementById('filter-status');
  const empty = document.getElementById('filter-empty');
  const rows = Array.from(table.querySelectorAll('tbody tr'));
  const format = value => new Intl.NumberFormat('en-AU').format(value);
  function apply() {
    const name = picker.value;
    const selected = Boolean(name);
    message.hidden = selected;
    work.hidden = !selected;
    let count = 0, units = 0, reviewed = 0;
    for (const row of rows) {
      const visible = selected && row.dataset.assigned === name;
      row.hidden = !visible;
      if (visible) {
        count++;
        units += Number(row.dataset.units || 0);
        reviewed += Number(row.dataset.reviewed || 0);
      }
    }
    status.textContent = selected
      ? `${format(count)} actions · ${format(reviewed)}/${format(units)} units reviewed`
      : '';
    empty.hidden = !selected || count > 0;
    table.hidden = selected && count === 0;
  }
  picker.addEventListener('change', apply);
  apply();
})();
</script>"""


def render_dashboard(
    tasks: list[dict[str, Any]],
    reviewers: tuple[str, ...],
    totals: dict[str, Any],
    unassigned: list[dict[str, Any]],
) -> str:
    updated_at_aest = dt.datetime.now(dt.timezone.utc).astimezone(AEST)
    unassignable_html = ''
    if unassigned:
        names = ''.join(
            f'<li>Row {task["row"]} · {esc(task["canonical_action_id"])} — '
            f'{esc(task["action"])} ({task["unit_count"]} units)</li>'
            for task in unassigned
        )
        unassignable_html = (
            '<section class="panel exceptions"><h2>Requires manual assignment</h2>'
            '<p>No one meets the eligibility rules for the following actions. '
            'They are not silently assigned to a proposer or prior annotator.</p>'
            f'<ul>{names}</ul></section>'
        )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Annotation Review Assignments</title>
<style>
:root{{--bg:#090d18;--panel:#111827;--text:#f4f7ff;--muted:#9aa8c5;--line:#28344e;--blue:#78a7ff;--green:#67d89f;--red:#ff7f8c;--orange:#ffbd73;--purple:#bb91ff}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;min-height:100vh;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at 12% -5%,rgba(83,119,255,.22),transparent 34rem),radial-gradient(circle at 92% 12%,rgba(255,127,140,.10),transparent 30rem),var(--bg)}}
.container{{max-width:1440px;margin:0 auto;padding:30px 24px 60px}}.hero{{border:1px solid var(--line);border-radius:24px;padding:27px 30px;background:linear-gradient(135deg,#1e2e50,#101829)}}h1{{margin:8px 0 15px;font-size:clamp(31px,4vw,48px);letter-spacing:-.045em}}.eyebrow{{color:#b8c8eb;text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:800}}
.mode-pill{{display:inline-block;padding:5px 10px;border:1px solid #42567e;border-radius:999px;background:#15233b;color:#dbe7ff;font-size:12px;font-weight:700}}
.panel{{margin-top:18px;border:1px solid var(--line);border-radius:20px;padding:22px;background:var(--panel)}}.panel h2{{margin:0;font-size:19px}}.panel p{{margin:7px 0 14px;color:var(--muted);line-height:1.55;font-size:12px}}.panel-header{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}.caption-pill{{border:1px solid #415578;border-radius:999px;background:#17243b;padding:6px 11px;font-weight:700;font-size:11px;color:#cbdfff}}
.progress-panel{{border-top:3px solid var(--blue)}}.progress-bars{{margin-top:18px}}.progress-row{{display:grid;grid-template-columns:minmax(145px,230px) minmax(100px,1fr) 100px;align-items:center;gap:15px;margin:14px 0}}.progress-person{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:700}}.progress-track{{height:13px;border-radius:99px;background:#253148;overflow:hidden}}.progress-fill{{height:100%;border-radius:99px;background:linear-gradient(90deg,#4dbb93,var(--green))}}.progress-count{{text-align:right;white-space:nowrap;color:#a5b5d0;font-size:12px;font-variant-numeric:tabular-nums}}.progress-count b{{color:#e9f3ff;font-size:14px}}
.queue-panel{{border-top:3px solid var(--orange)}}.filter-toolbar{{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin:17px 0;padding:13px 15px;border:1px solid var(--line);border-radius:13px;background:#0d1422}}.filter-toolbar label{{font-size:12px;font-weight:800}}.filter-toolbar select{{min-width:260px;max-width:100%;border:1px solid #526682;border-radius:9px;background:#1b2840;color:#f4f7ff;padding:9px 12px;font-size:12px;cursor:pointer}}.filter-toolbar select:focus-visible{{outline:2px solid var(--blue);outline-offset:2px}}.filter-status{{margin-left:auto;color:#b6c7e2;font-size:11px}}.choice-warning{{border:1px solid #7e6332;background:rgba(255,189,115,.09);border-radius:12px;padding:15px 18px;color:#ffdca7;font-weight:700;font-size:12px}}
.table-wrap{{width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:13px}}.audit-table{{width:100%;min-width:1090px;border-collapse:collapse;table-layout:auto}}.audit-table th,.audit-table td{{padding:10px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left;overflow-wrap:anywhere}}.audit-table th{{background:#151e31;color:#a6b9da;position:sticky;top:0;font-size:10px;letter-spacing:.045em;text-transform:uppercase}}.audit-table td{{color:#e0e9fa;font-size:11px;line-height:1.6}}.audit-table tr:last-child td{{border-bottom:none}}.audit-table tbody tr:hover td{{background:#141e31}}
.num,.ca{{white-space:nowrap;font-weight:800}}.action{{min-width:250px;max-width:430px}}.band{{min-width:142px}}.review-status{{min-width:130px}}.annotators,.proposers{{min-width:120px;max-width:155px}}.band-line{{min-height:25px;display:flex;align-items:center;margin:1px 0}}.status{{border:1px solid transparent;border-radius:7px;padding:2px 6px;display:inline-block;font-weight:850;font-size:10px;white-space:nowrap}}.status.done{{color:#aeedd1;border-color:#376753;background:rgba(103,216,159,.09)}}.status.pending{{color:#ffb4bd;border-color:#70404a;background:rgba(255,127,140,.09)}}
.stacked-names{{display:flex;flex-direction:column;align-items:flex-start;gap:4px}}.badge{{display:block;margin:0;padding:3px 7px;border-radius:99px;border:1px solid #324057;background:#0e1626;color:#d3dff4;font-size:9px;font-weight:750;max-width:100%}}.badge.proposer{{border-color:#5f4c82;color:#dbc5ff;background:rgba(187,145,255,.07)}}.type-pill{{display:inline-block;white-space:nowrap;padding:4px 7px;border-radius:8px;font-size:10px;font-weight:800;border:1px solid #385679;color:#bcd7ff;background:#18273d}}.type-pill.self{{color:#e7cbff;border-color:#62497f;background:#281e37}}.category{{min-width:111px}}.empty-message{{margin-top:12px;padding:14px 16px;border:1px dashed var(--line);border-radius:10px;color:var(--muted);font-size:12px}}
.exceptions{{border-top:3px solid var(--red)}}.exceptions li{{font-size:12px;margin:9px 0;color:#f7c0c4}}footer{{margin-top:22px;text-align:center;color:#8190af;font-size:10px}}[hidden],tr[hidden]{{display:none !important}}
@media(max-width:700px){{.container{{padding:14px 10px 35px}}.hero,.panel{{padding:16px;border-radius:16px}}.progress-row{{grid-template-columns:110px minmax(70px,1fr) 75px;gap:7px}}.progress-person,.progress-count{{font-size:10px}}.progress-count b{{font-size:11px}}.filter-status{{margin-left:0;width:100%}}}}
</style></head><body><div class="container">
<section class="hero"><div class="eyebrow">Temporal NoRA · annotation review</div><h1>Annotation Review Assignments</h1><span class="mode-pill"><b>Last Updated:</b> {esc(updated_at_aest.strftime('%d %B %Y, %I:%M %p AEST').lstrip('0'))}</span></section>
{render_review_progress_chart(tasks,reviewers,totals)}
{render_review_table(tasks,reviewers)}
{unassignable_html}
<footer>Annotation review assignments · data refreshed on scheduled runs</footer>
</div>{FILTER_SCRIPT}</body></html>'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a persistent, balanced review queue and self-contained HTML dashboard."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--annotator", default=DEFAULT_ANNOTATOR)
    parser.add_argument("--input", type=Path, help="Use local JSON/JSONL instead of downloading.")
    parser.add_argument("--export-path", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start-row", type=int, default=0)
    parser.add_argument("-n", "--num-rows", type=int, default=None)
    parser.add_argument("--ai-annotator", action="append", default=[])
    parser.add_argument("--reviewer", action="append", default=[],
                        help="Replace six-person roster; repeat for each name.")
    parser.add_argument("--progress-state", type=Path, default=DEFAULT_PROGRESS_STATE)
    parser.add_argument("--review-target", type=int, default=DEFAULT_REVIEW_TARGET,
                        help="Deprecated: bars now use actual assigned workloads.")
    parser.add_argument("--exclude-clip", action="append", default=[])
    parser.add_argument("--exclude-annotator", action="append", default=[])
    parser.add_argument("--include-default-exclusions", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()
    if args.start_row < 0 or (args.num_rows is not None and args.num_rows <= 0):
        raise ValueError("--start-row must be >= 0 and -n must be > 0")
    if args.review_target <= 0:
        raise ValueError("--review-target must be positive")
    if args.input is not None:
        export_path = args.input
        if not export_path.exists():
            raise FileNotFoundError(export_path)
    else:
        export_path = download_export(args)
    all_records = load_export_records(export_path)
    if args.start_row > len(all_records):
        raise ValueError("--start-row exceeds the export size")
    end = len(all_records) if args.num_rows is None else min(len(all_records), args.start_row + args.num_rows)
    records = all_records[args.start_row:end]
    ai_names = set(KNOWN_AI_ANNOTATORS) | set(args.ai_annotator)
    excluded_clip_ids = set(args.exclude_clip)
    excluded_annotators = set(args.exclude_annotator)
    if not args.include_default_exclusions:
        excluded_clip_ids |= DEFAULT_EXCLUDED_CLIP_IDS
        excluded_annotators |= DEFAULT_EXCLUDED_ANNOTATORS
    reviewers = (tuple(dict.fromkeys(name.strip() for name in args.reviewer if name.strip()))
                 if args.reviewer else DEFAULT_REVIEWERS)
    reviewers = tuple(name for name in reviewers
                      if not is_ai_annotator(name, ai_names)
                      and not is_excluded(name, excluded_annotators))
    if not reviewers or len({name.casefold() for name in reviewers}) != len(reviewers):
        raise ValueError("Reviewer roster is empty or contains duplicate names")

    print(f"Loaded {len(all_records):,} clips; selected rows {args.start_row + 1}–{end}.")
    state = load_review_progress(args.progress_state)
    actions, duplicate_conflicts = collect_action_observations(
        records, start_row=args.start_row,
        ai_names=ai_names, excluded_clip_ids=excluded_clip_ids,
        excluded_annotators=excluded_annotators,
    )
    tasks, discovered = discover_tasks(actions, state, reviewers)
    assigned, unassigned = assign_review_tasks(tasks, state, reviewers)
    totals = update_review_completion(tasks)
    # Writes must happen once, after analysis/assignment succeeds. Unchanged
    # state isn't rewritten/committed on every 10-minute workflow run.
    if discovered or assigned or not args.progress_state.exists():
        write_review_progress(args.progress_state, state)
    report = render_dashboard(tasks, reviewers, totals, unassigned)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    try:
        tmp.write_text(report, encoding="utf-8")
        tmp.replace(args.output)
    finally:
        tmp.unlink(missing_ok=True)

    print(f"Review actions: {len(tasks):,}, units: {sum(t['unit_count'] for t in tasks):,}")
    print(f"Unassignable actions: {len(unassigned):,}, units: {sum(t['unit_count'] for t in unassigned):,}")
    if duplicate_conflicts:
        print(f"Conflicting duplicate person/band ratings ignored: {duplicate_conflicts:,}")
    for name in reviewers:
        print(f"  {name}: {totals['reviewed'].get(name, 0):,} / {totals['assigned'].get(name, 0):,} reviewed")
    print(f"HTML: {args.output}; state: {args.progress_state}")
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
