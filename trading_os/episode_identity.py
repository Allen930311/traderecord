"""Evidence-first canonical trade-episode identity for Method Validation B1 v3.

This module consumes only the immutable B1 v2 source-record dataset and its
already-pinned provenance.  It creates a separate episode layer while retaining
every source row.  The identity decision is deliberately independent of any
aggregate result: no performance artifact, metric, ranking, or strategy score
is an input to this module.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .method_validation import _canonical_bytes
from scripts.canonical_migration.frontmatter import parse_markdown


B1_V2_SCHEMA_VERSION = "method-validation-b1.v2"
B1_V2_DATASET_SHA256 = "c49af93748864bb863192bb46fea19f314a3107c9455a4d587c2f16835714ed5"
B1_V2_MANIFEST_DIGEST = "e9efe9d145e8203d0e1b63cbcaaa303bfed73f4ee4fc7337fd96eef6ab844ffe"
B1_V2_MANIFEST_FILE_SHA256 = "fc49b2bf94a78bcf4bdc43aa8acb3b3aa965c1f46f244a4ea660a0eee3d16aa9"
B1_V2_README_SHA256 = "a869979159b3bdb87f83e84914922124866fbac5a2f45eeb5cd944f812ad0a81"
B1_V3_SCHEMA_VERSION = "method-validation-b1.v3"
B1_V3_DATASET_VERSION = "method-validation-b1.v3"
B1_V3_GENERATOR_MODE = "method_validation_b1_episode_aware_dataset"

B1_V2_DATASET_PATH = "exports/method-validation/b1/canonical-trade-dataset.jsonl"
B1_V2_MANIFEST_PATH = "exports/method-validation/b1/manifest.json"
B1_V2_README_PATH = "exports/method-validation/b1/README.md"
B3_MANIFEST_PATH = "exports/method-validation/b3/manifest.json"
STEP0_RESULTS_PATH = "exports/strategy-discovery/step-0/step-0-results.jsonl"
STEP0_CORPUS_MANIFEST_PATH = "exports/strategy-discovery/step-0/corpus-manifest.json"
REGISTRY_PATH = "registry/strategy-registry.yaml"
PER_TRADE_APPLICATION_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-per-trade-application-20260910.jsonl"
)
PER_TRADE_DECISIONS_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-per-trade-decisions-20260910.jsonl"
)
RESIDUAL_LANE_HANDOFF_PATH = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-residual-lane-handoff-20260910.jsonl"
)
FILENAME_DATE_POLICY_PATH = "canonical/adjudications/adj_filename_date_policy_20260901_v2.json"
HISTORICAL_CLEANING_PATH = "canonical/adjudications/historical-cleaning-human-revisions.jsonl"

DEFAULT_B1_V3_OUTPUT = Path("exports/method-validation/b1-v3")
DEFAULT_ADJUDICATION_OUTPUT = Path("canonical/adjudications/method-validation")
EPISODE_RESOLUTION_NAME = "episode-identity-resolution-20260911.jsonl"
EPISODE_CANDIDATE_NAME = "episode-candidate-audit-20260911.jsonl"
EPISODE_REPORT_NAME = "episode-identity-report.md"
DECISION_PACKET_NAME = "episode-identity-decision-packet.md"

KNOWN_SOURCE_COUNT = 255
EXECUTION_FIELDS = (
    "event_date",
    "instrument",
    "direction",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
)
COMPATIBILITY_FIELDS = (
    "strategy_id",
    "strategy_status",
    "family_strategy_id",
    "parent_strategy_id",
    "canonical_realized_r",
    "outcome",
    "event_date",
    "instrument",
    "direction",
    "sample_origin",
)
PROTECTED_SOURCE_FIELDS = COMPATIBILITY_FIELDS + ("source_path", "trade_ref", "step0_record_id")
PLACEHOLDER_LABELS = {"", "unknown", "unspecified", "none", "null"}

class EpisodeIdentityError(ValueError):
    """Raised when episode identity cannot be established safely."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise EpisodeIdentityError(f"unable to hash file: {path}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EpisodeIdentityError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EpisodeIdentityError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EpisodeIdentityError(f"unable to read JSONL: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EpisodeIdentityError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise EpisodeIdentityError(f"JSONL object required at {path}:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text.rstrip() + "\n")


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _distinct(values: Iterable[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _row_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("source_path", "")), str(row.get("step0_record_id", "")), str(row.get("trade_ref", "")))


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=_row_order)


def _execution_base(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in EXECUTION_FIELDS)


def _execution_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in EXECUTION_FIELDS} | {
        "canonical_realized_r": row.get("canonical_realized_r"),
        "outcome": row.get("outcome"),
    }


def _filename_profile(row: Mapping[str, Any]) -> dict[str, Any]:
    source_path = str(row.get("source_path", ""))
    stem = Path(source_path).stem.lower()
    direction_token = {"long": "l", "short": "s"}.get(str(row.get("direction")), "")
    prefix = f"{str(row.get('event_date', '')).lower()}_{str(row.get('instrument', '')).lower()}_{direction_token}"
    remainder = stem[len(prefix):].lstrip("_") if stem.startswith(prefix) else stem
    if re.fullmatch(r"\d+(?:_\d+)*", remainder or ""):
        role = "numeric_variant"
    elif remainder in PLACEHOLDER_LABELS:
        role = "placeholder_variant"
    else:
        role = "named_variant"
    labels = {
        str(row.get("legacy_strategy_label") or "").strip().lower(),
        str(row.get("frontmatter_strategy_label") or "").strip().lower(),
        remainder,
    }
    labels -= PLACEHOLDER_LABELS
    return {
        "filename_family": prefix,
        "filename_suffix": remainder,
        "filename_role": role,
        "labels": sorted(labels),
    }


def _source_evidence_profile(root: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    path = (root / str(row.get("source_path"))).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EpisodeIdentityError(f"source path escapes project root: {row.get('source_path')}") from exc
    from .legacy_source import LegacySourceError, read_legacy_bytes
    try:
        raw = read_legacy_bytes(root, path.relative_to(root))
    except LegacySourceError as exc:
        raise EpisodeIdentityError(f"source path missing: {row.get('source_path')}") from exc
    source_sha = _sha256_bytes(raw)
    if source_sha != row.get("source_sha256"):
        raise EpisodeIdentityError(f"source SHA drift: {row.get('source_path')}")
    try:
        parsed = parse_markdown(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EpisodeIdentityError(f"source body cannot be parsed: {row.get('source_path')}") from exc
    body_sha = _sha256_bytes(parsed.body.encode("utf-8"))
    if body_sha != row.get("source_body_sha256"):
        raise EpisodeIdentityError(f"source body SHA drift: {row.get('source_path')}")
    filename = _filename_profile(row)
    return {
        "source_sha256": source_sha,
        "source_body_sha256": body_sha,
        "filename_family": filename["filename_family"],
        "filename_suffix": filename["filename_suffix"],
        "filename_role": filename["filename_role"],
        "labels": filename["labels"],
        "time_entry_present": not _is_missing(row.get("time_entry")),
        "actual_exit_present": not _is_missing(row.get("exit_price")),
        "sample_origin": row.get("sample_origin"),
        "mapping": {
            "strategy_id": row.get("strategy_id"),
            "strategy_status": row.get("strategy_status"),
            "legacy_strategy_label": row.get("legacy_strategy_label"),
            "frontmatter_strategy_label": row.get("frontmatter_strategy_label"),
            "match_type": row.get("match_type"),
            "decision_id": row.get("decision_id"),
        },
        "existing_episode_adjudication": {
            "episode_id_v2": row.get("episode_id"),
            "episode_note": row.get("episode_note"),
            "related_record_refs": list(row.get("related_record_refs") or []),
            "provenance": row.get("provenance"),
        },
    }


def _representation_relationship(
    rows: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    items = [profiles[str(row["trade_ref"])] for row in rows]
    labels = [set(item.get("labels") or []) for item in items]
    label_overlap = any(left & right for index, left in enumerate(labels) for right in labels[index + 1:])
    roles = {str(item.get("filename_role")) for item in items}
    shape_pairs = {
        (bool(item.get("time_entry_present")), bool(item.get("actual_exit_present")))
        for item in items
    }
    complementary_template_shape = {(True, False), (False, True)}.issuperset(shape_pairs) and len(shape_pairs) > 1
    named_and_variant = "named_variant" in roles and bool(roles & {"numeric_variant", "placeholder_variant"})
    same_filename_family = len({item.get("filename_family") for item in items}) == 1
    relationship = bool(label_overlap or (same_filename_family and named_and_variant) or complementary_template_shape)
    reasons: list[str] = []
    if label_overlap:
        reasons.append("shared non-placeholder strategy/file label")
    if same_filename_family:
        reasons.append("same date-instrument-direction filename family")
    if named_and_variant:
        reasons.append("named strategy representation paired with numeric/placeholder variant")
    if complementary_template_shape:
        reasons.append("complementary sparse/full template shape: time_entry/actual_exit")
    return {
        "relationship_present": relationship,
        "same_filename_family": same_filename_family,
        "label_overlap": label_overlap,
        "complementary_template_shape": complementary_template_shape,
        "reasons": reasons,
    }


def _contradictions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    contradictions: list[dict[str, Any]] = []
    for field in ("time_entry", "exit_price", "sample_origin", "analysis_eligibility", "outcome", "canonical_realized_r", "strategy_id"):
        values = [row.get(field) for row in rows if not _is_missing(row.get(field))]
        if len(_distinct(values)) > 1:
            contradictions.append({"field": field, "values": _distinct(values)})
    return contradictions


def _same_execution_resolution(
    rows: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, str, dict[str, Any]]:
    source_shas = _distinct(row.get("source_sha256") for row in rows)
    contradictions = _contradictions(rows)
    relationship = _representation_relationship(rows, profiles)
    strategy_ids = _distinct(row.get("strategy_id") for row in rows)
    common_evidence = {
        "execution_identity": _execution_identity(rows[0]),
        "source_sha256_values": source_shas,
        "representation_relationship": relationship,
        "contradictions": contradictions,
        "strategy_ids": strategy_ids,
        "sample_origins": _distinct(row.get("sample_origin") for row in rows),
        "eligibility_values": _distinct(row.get("analysis_eligibility") for row in rows),
    }
    if len(source_shas) == 1 and not contradictions and len(strategy_ids) == 1:
        return True, "exact_duplicate", common_evidence
    if (
        not contradictions
        and len(strategy_ids) == 1
        and strategy_ids[0] is not None
        and relationship["relationship_present"]
    ):
        return True, "same_execution", common_evidence
    if not strategy_ids or strategy_ids == [None]:
        common_evidence["fail_closed_reason"] = "strategy mapping is absent"
    elif len(strategy_ids) > 1:
        common_evidence["fail_closed_reason"] = "strategy mapping differs across representations"
    elif not relationship["relationship_present"]:
        common_evidence["fail_closed_reason"] = "no sufficient source/template relationship"
    elif contradictions:
        common_evidence["fail_closed_reason"] = "contradictory execution or provenance facts"
    return False, "unresolved", common_evidence


def _candidate_id(kind: str, rows: Sequence[Mapping[str, Any]], key: Any) -> str:
    material = {
        "kind": kind,
        "key": key,
        "trade_refs": sorted(str(row.get("trade_ref")) for row in rows),
    }
    return f"candidate_{_sha256_bytes(_canonical_bytes(material))[:24]}"


def _episode_id(rows: Sequence[Mapping[str, Any]], relationship: str) -> str:
    material = {
        "relationship": relationship,
        "trade_refs": sorted(str(row.get("trade_ref")) for row in rows),
        "execution_identity": _execution_identity(rows[0]),
    }
    return f"episode_{_sha256_bytes(_canonical_bytes(material))[:24]}"


def _representative_score(row: Mapping[str, Any]) -> tuple[int, int, int, int, str, str]:
    richness_fields = (
        "event_date", "instrument", "direction", "entry_price", "stop_loss_price",
        "take_profit_price", "exit_price", "canonical_realized_r", "outcome",
        "sample_origin", "strategy_id", "strategy_status", "match_type", "decision_id",
    )
    richness = sum(not _is_missing(row.get(field)) for field in richness_fields)
    complete_exit = int(not _is_missing(row.get("exit_price")))
    provenance = sum(not _is_missing(row.get(field)) for field in ("source_sha256", "source_body_sha256", "step0_record_id", "decision_id"))
    mapped = int(row.get("strategy_id") is not None and row.get("match_type") is not None)
    return (richness, complete_exit, provenance, mapped, str(row.get("source_path", "")), str(row.get("step0_record_id", "")))


def _choose_representative(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    # Highest authority/completeness wins; lexical order is only the final tie-break.
    def sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, str, str]:
        richness, complete_exit, provenance, mapped, source_path, step0_record_id = _representative_score(row)
        return (-richness, -complete_exit, -provenance, -mapped, source_path, step0_record_id)

    return sorted(rows, key=sort_key)[0]


def _member_observation(row: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trade_ref": row.get("trade_ref"),
        "source_record_id": row.get("step0_record_id"),
        "source_path": row.get("source_path"),
        "source_sha256": row.get("source_sha256"),
        "source_body_sha256": row.get("source_body_sha256"),
        "event_date": row.get("event_date"),
        "instrument": row.get("instrument"),
        "direction": row.get("direction"),
        "entry_price": row.get("entry_price"),
        "stop_loss_price": row.get("stop_loss_price"),
        "take_profit_price": row.get("take_profit_price"),
        "exit_price": row.get("exit_price"),
        "canonical_realized_r": row.get("canonical_realized_r"),
        "outcome": row.get("outcome"),
        "analysis_eligibility": row.get("analysis_eligibility"),
        "ineligibility_reason": row.get("ineligibility_reason"),
        "sample_origin": row.get("sample_origin"),
        "strategy_id": row.get("strategy_id"),
        "strategy_status": row.get("strategy_status"),
        "family_strategy_id": row.get("family_strategy_id"),
        "parent_strategy_id": row.get("parent_strategy_id"),
        "legacy_strategy_label": row.get("legacy_strategy_label"),
        "frontmatter_strategy_label": row.get("frontmatter_strategy_label"),
        "filename_family": profile.get("filename_family"),
        "filename_role": profile.get("filename_role"),
        "time_entry": row.get("time_entry"),
        "related_record_refs": list(row.get("related_record_refs") or []),
        "episode_id_v2": row.get("episode_id"),
        "episode_note_v2": row.get("episode_note"),
        "mapping_authority": profile.get("mapping"),
        "existing_episode_adjudication": profile.get("existing_episode_adjudication"),
    }


def _candidate_record(
    *,
    kind: str,
    candidate_class: str,
    rows: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    key: Any,
    resolution: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    ordered = _ordered(rows)
    return {
        "schema_version": "method-validation-b1-episode-candidate.v1",
        "candidate_id": _candidate_id(kind, ordered, key),
        "candidate_kind": kind,
        "candidate_class": candidate_class,
        "member_trade_refs": [str(row.get("trade_ref")) for row in ordered],
        "member_source_paths": [str(row.get("source_path")) for row in ordered],
        "member_observations": [_member_observation(row, profiles[str(row["trade_ref"])]) for row in ordered],
        "execution_identity": _execution_identity(ordered[0]),
        "resolution": resolution,
        "evidence": dict(evidence),
        "performance_counting_policy": "count_once" if resolution == "resolved" else ("exclude_until_resolved" if resolution == "unresolved" else "count_separately"),
        "confidence": "high" if candidate_class in {"exact_duplicate", "distinct"} else ("medium" if resolution == "resolved" else "unresolved"),
        "performance_blind": True,
    }


def discover_episode_candidates(
    root: Path,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return candidate audit rows, final resolution groups, and audit counts."""

    profiles = {str(row["trade_ref"]): _source_evidence_profile(root, row) for row in rows}
    by_base: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    by_source_sha: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_coarse: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_base[_execution_base(row)].append(row)
        by_source_sha[str(row.get("source_sha256"))].append(row)
        by_coarse[tuple(row.get(field) for field in ("event_date", "instrument", "direction"))].append(row)

    candidate_rows: list[dict[str, Any]] = []
    resolution_groups: list[dict[str, Any]] = []
    strong_rows: set[str] = set()
    unresolved_rows: set[str] = set()
    exact_duplicate_groups = 0
    exact_duplicate_source_rows = 0
    strong_group_count = 0
    unresolved_group_count = 0

    for source_sha, group in sorted(by_source_sha.items()):
        if len(group) < 2:
            continue
        exact_duplicate_groups += 1
        exact_duplicate_source_rows += len(group) - 1

    for key, group in sorted(by_base.items(), key=lambda item: (str(item[0]), _row_order(_ordered(item[1])[0]))):
        ordered = _ordered(group)
        if len(ordered) < 2:
            continue
        resolved, relationship, evidence = _same_execution_resolution(ordered, profiles)
        if resolved:
            strong_group_count += 1
            strong_rows.update(str(row["trade_ref"]) for row in ordered)
            resolution_groups.append(_resolution_group(ordered, profiles, relationship, evidence))
            candidate_rows.append(_candidate_record(
                kind="execution_collision",
                candidate_class="same_execution_strong_candidate" if relationship == "same_execution" else "exact_duplicate",
                rows=ordered,
                profiles=profiles,
                key=key,
                resolution="resolved",
                evidence=evidence,
            ))
        else:
            unresolved_group_count += 1
            unresolved_rows.update(str(row["trade_ref"]) for row in ordered)
            resolution_groups.append(_resolution_group(ordered, profiles, "unresolved", evidence))
            candidate_rows.append(_candidate_record(
                kind="execution_collision",
                candidate_class="unresolved",
                rows=ordered,
                profiles=profiles,
                key=key,
                resolution="unresolved",
                evidence=evidence,
            ))

    resolution_member_refs = strong_rows | unresolved_rows
    for row in _ordered(rows):
        if str(row["trade_ref"]) in resolution_member_refs:
            continue
        resolution_groups.append(_resolution_group([row], profiles, "distinct", {
            "reason": "no full execution-identity collision detected",
            "execution_identity": _execution_identity(row),
        }))

    possible_related_groups = 0
    for key, group in sorted(by_coarse.items(), key=lambda item: (str(item[0]), _row_order(_ordered(item[1])[0]))):
        ordered = _ordered(group)
        if len(ordered) < 2:
            continue
        plan_values = _distinct(tuple(row.get(field) for field in ("entry_price", "stop_loss_price", "take_profit_price")) for row in ordered)
        if len(plan_values) < 2:
            continue
        possible_related_groups += 1
        candidate_rows.append(_candidate_record(
            kind="coarse_identity_collision",
            candidate_class="possible_related",
            rows=ordered,
            profiles=profiles,
            key=key,
            resolution="distinct",
            evidence={
                "shared_fields": ["event_date", "instrument", "direction"],
                "contradictory_execution_facts": {
                    "entry_stop_target_tuples": plan_values,
                },
                "reason": "shared coarse identity is insufficient; differing execution plan facts remain separate",
            },
        ))

    # Add exact duplicate subgroups explicitly so the audit retains the A-class
    # evidence even when a larger same-execution group contains both templates.
    for source_sha, group in sorted(by_source_sha.items()):
        if len(group) < 2:
            continue
        ordered = _ordered(group)
        candidate_rows.append(_candidate_record(
            kind="source_hash_collision",
            candidate_class="exact_duplicate",
            rows=ordered,
            profiles=profiles,
            key=source_sha,
            resolution="resolved",
            evidence={
                "source_sha256": source_sha,
                "source_bytes_identical": True,
                "canonical_source_identity": "source SHA-256 and B1 body SHA-256 match across members",
            },
        ))

    candidate_rows.sort(key=lambda row: str(row["candidate_id"]))
    resolution_groups.sort(key=lambda row: str(row["episode_id"]))
    counts = {
        "source_rows": len(rows),
        "execution_collision_groups": sum(1 for group in by_base.values() if len(group) > 1),
        "exact_duplicate_groups": exact_duplicate_groups,
        "exact_duplicate_source_rows_excluded": exact_duplicate_source_rows,
        "same_execution_resolved_groups": strong_group_count,
        "same_execution_resolved_source_rows": len(strong_rows),
        "same_execution_collapsed_source_rows": len(strong_rows) - strong_group_count,
        "unresolved_episode_groups": unresolved_group_count,
        "unresolved_source_rows": len(unresolved_rows),
        "unresolved_eligible_source_rows": sum(
            1 for row in rows if str(row["trade_ref"]) in unresolved_rows and row.get("analysis_eligibility") == "eligible"
        ),
        "possible_related_groups": possible_related_groups,
        "distinct_singleton_groups": sum(1 for group in resolution_groups if group["relationship"] == "distinct"),
        "total_episode_groups": len(resolution_groups),
        "resolved_episode_count": sum(1 for group in resolution_groups if group["relationship"] != "unresolved"),
    }
    return candidate_rows, resolution_groups, counts


def _resolution_group(
    rows: Sequence[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    relationship: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    ordered = _ordered(rows)
    representative = _choose_representative(ordered) if relationship != "unresolved" else None
    episode_id = _episode_id(ordered, relationship)
    return {
        "schema_version": "method-validation-b1-episode-resolution.v1",
        "episode_id": episode_id,
        "member_trade_refs": [str(row.get("trade_ref")) for row in ordered],
        "member_source_record_ids": [str(row.get("step0_record_id")) for row in ordered],
        "member_source_paths": [str(row.get("source_path")) for row in ordered],
        "relationship": relationship,
        "canonical_representative": representative.get("trade_ref") if representative else None,
        "counting_policy": "count_once" if relationship in {"exact_duplicate", "same_execution"} else ("exclude_until_resolved" if relationship == "unresolved" else "count_separately"),
        "resolution_source": "deterministic_exact_duplicate" if relationship == "exact_duplicate" else ("evidence_resolution" if relationship == "same_execution" else ("unresolved" if relationship == "unresolved" else "no_identity_collision")),
        "evidence": dict(evidence),
        "confidence": "high" if relationship in {"exact_duplicate", "distinct"} else ("medium" if relationship == "same_execution" else "unresolved"),
        "member_count": len(ordered),
        "member_observations": [_member_observation(row, profiles[str(row["trade_ref"])]) for row in ordered],
        "performance_counting_eligibility": bool(
            relationship != "unresolved"
            and representative is not None
            and all(row.get("analysis_eligibility") == "eligible" for row in ordered)
        ),
        "performance_blind": True,
    }


def _load_and_validate_b1_v2(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(root / B1_V2_MANIFEST_PATH)
    if manifest.get("schema_version") != B1_V2_SCHEMA_VERSION or manifest.get("dataset_version") != B1_V2_SCHEMA_VERSION:
        raise EpisodeIdentityError("B1 v2 schema/version pin mismatch")
    if manifest.get("dataset_sha256") != B1_V2_DATASET_SHA256 or manifest.get("manifest_digest") != B1_V2_MANIFEST_DIGEST:
        raise EpisodeIdentityError("B1 v2 content pin mismatch")
    if _sha256_file(root / B1_V2_DATASET_PATH) != B1_V2_DATASET_SHA256:
        raise EpisodeIdentityError("B1 v2 dataset bytes changed")
    if _sha256_file(root / B1_V2_MANIFEST_PATH) != B1_V2_MANIFEST_FILE_SHA256:
        raise EpisodeIdentityError("B1 v2 manifest bytes changed")
    if _sha256_file(root / B1_V2_README_PATH) != B1_V2_README_SHA256:
        raise EpisodeIdentityError("B1 v2 README bytes changed")
    unsigned = dict(manifest)
    supplied = unsigned.pop("manifest_digest", None)
    if supplied != _sha256_bytes(_canonical_bytes(unsigned)):
        raise EpisodeIdentityError("B1 v2 manifest digest is invalid")
    rows = _read_jsonl(root / B1_V2_DATASET_PATH)
    if len(rows) != KNOWN_SOURCE_COUNT:
        raise EpisodeIdentityError(f"B1 v2 source count changed: {len(rows)}")
    if len({row.get("trade_ref") for row in rows}) != len(rows):
        raise EpisodeIdentityError("B1 v2 contains duplicate trade_ref values")
    if len({row.get("step0_record_id") for row in rows}) != len(rows):
        raise EpisodeIdentityError("B1 v2 contains duplicate step0_record_id values")
    return manifest, rows


def _source_file_pins(root: Path, b1_manifest: Mapping[str, Any]) -> dict[str, str]:
    pinned = b1_manifest.get("pinned_source_digests")
    if not isinstance(pinned, Mapping):
        raise EpisodeIdentityError("B1 v2 source pins missing")
    result: dict[str, str] = {}
    for key, path in (
        ("step0_results_sha256", STEP0_RESULTS_PATH),
        ("step0_corpus_manifest_sha256", STEP0_CORPUS_MANIFEST_PATH),
        ("registry_file_sha256", REGISTRY_PATH),
        ("per_trade_application_sha256", PER_TRADE_APPLICATION_PATH),
        ("per_trade_decisions_sha256", PER_TRADE_DECISIONS_PATH),
        ("residual_lane_handoff_sha256", RESIDUAL_LANE_HANDOFF_PATH),
        ("filename_date_policy_sha256", FILENAME_DATE_POLICY_PATH),
    ):
        expected = pinned.get(key)
        actual = _sha256_file(root / path)
        if actual != expected:
            if path == REGISTRY_PATH:
                b3_manifest = _read_json(root / B3_MANIFEST_PATH)
                allowed_after = (((b3_manifest.get("registry") or {}).get("after") or {}).get("file_sha256"))
                if actual != allowed_after and not _registry_preserves_b3_freeze(root, b3_manifest):
                    raise EpisodeIdentityError(f"upstream pin drift: {path}")
            else:
                raise EpisodeIdentityError(f"upstream pin drift: {path}")
        result[key] = actual
    historical_path = root / HISTORICAL_CLEANING_PATH
    if historical_path.is_file():
        result["historical_cleaning_human_revisions_sha256"] = _sha256_file(historical_path)
    return result


# The frozen B3 baseline lives in trading_os.registry so that the registration packet
# pins and this guard share one definition.
from .registry import registry_as_of_b3  # noqa: E402


def _registry_preserves_b3_freeze(root: Path, b3_manifest: Mapping[str, Any]) -> bool:
    """Allow additive post-B3 registry entries while enforcing every B3 pin.

    B1 v3 operates on the immutable B1 v2 rows and frozen B3 strategy versions.
    A later active strategy may be added for operational resolution, but it must
    not replace, retire, or edit any strategy/version that B3 actually froze.
    Everything else in the registry (aliases, variants, retired and experimental
    entries) must be unchanged: with the allowed additions removed, the registry
    digest has to equal the digest the B3 manifest recorded.
    """
    try:
        from .registry import load_registry, load_version, registry_digest, validate_registry

        registry = load_registry(root)
        if validate_registry(registry, root):
            return False
        expected_digest = ((b3_manifest.get("registry") or {}).get("after") or {}).get("registry_digest")
        historical = registry_as_of_b3(registry)
        if not isinstance(expected_digest, str) or registry_digest(historical) != expected_digest:
            return False
        b3_strategy_ids = b3_manifest.get("active_strategy_ids")
        frozen_versions = b3_manifest.get("strategy_versions")
        if not isinstance(b3_strategy_ids, list) or not isinstance(frozen_versions, list):
            return False
        frozen_by_id = {
            item.get("strategy_id"): item
            for item in frozen_versions
            if isinstance(item, Mapping) and isinstance(item.get("strategy_id"), str)
        }
        if set(b3_strategy_ids) != set(frozen_by_id):
            return False
        current_by_id = {
            item.get("strategy_id"): item
            for item in registry.get("strategies", [])
            if isinstance(item, Mapping)
        }
        for strategy_id in b3_strategy_ids:
            frozen = frozen_by_id[strategy_id]
            current = current_by_id.get(strategy_id)
            if not isinstance(current, Mapping):
                return False
            if (
                current.get("status") != "active"
                or current.get("display_name") != frozen.get("display_name")
                or current.get("classification") != frozen.get("classification")
                or current.get("parent_strategy_id") != frozen.get("parent_strategy_id")
                or frozen.get("rule_version") not in current.get("rule_versions", [])
            ):
                return False
            version_id = frozen.get("rule_version")
            if not isinstance(version_id, str):
                return False
            version = load_version(root, version_id)
            version_path = root / "registry" / "versions" / f"{version_id.replace(':', '-')}.yaml"
            if (
                version.get("strategy_id") != strategy_id
                or version.get("status") != "active"
                or version.get("lifecycle_at_freeze") != "active"
                or version.get("classification") != frozen.get("classification")
                or version.get("parent_strategy_id") != frozen.get("parent_strategy_id")
                or _sha256_file(version_path) != frozen.get("version_file_sha256")
                or _sha256_bytes(_canonical_bytes(version.get("rules", []))) != frozen.get("rule_definition_sha256")
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _source_compatibility(v2: Mapping[str, Any], v3: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in COMPATIBILITY_FIELDS:
        if v2.get(field) != v3.get(field):
            errors.append(field)
    return errors


def _render_group(group: Mapping[str, Any]) -> list[str]:
    lines = [
        f"### `{group['episode_id']}` — `{group['relationship']}`",
        "",
        f"- Members ({group['member_count']}): " + ", ".join(f"`{item}`" for item in group["member_trade_refs"]),
        f"- Canonical representative: `{group['canonical_representative'] or 'none'}`",
        f"- Counting policy: `{group['counting_policy']}`; confidence: `{group['confidence']}`.",
    ]
    evidence = group.get("evidence") or {}
    if evidence.get("fail_closed_reason"):
        lines.append(f"- Fail-closed reason: {evidence['fail_closed_reason']}.")
    if evidence.get("representation_relationship"):
        relationship = evidence["representation_relationship"]
        lines.append("- Representation evidence: " + "; ".join(relationship.get("reasons") or ["none"]) + ".")
    if evidence.get("contradictions"):
        lines.append(f"- Contradictory fields: `{json.dumps(evidence['contradictions'], ensure_ascii=False, sort_keys=True)}`.")
    lines.append("")
    return lines


def _render_report(
    *,
    counts: Mapping[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    resolution_groups: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# Method Validation B1 v3 — Canonical Trade Episode Identity Audit",
        "",
        "This report is an evidence-first, fail-closed identity audit over all 255 B1 v2 source records. It preserves source provenance and does not read aggregate results or choose groups because of any result quality.",
        "",
        "## Audit summary",
        "",
        f"- Source records audited: **{counts['source_rows']}**.",
        f"- Full execution-identity collision groups: **{counts['execution_collision_groups']}**.",
        f"- Exact byte-identical SHA groups: **{counts['exact_duplicate_groups']}**; duplicate source rows excluded from counting: **{counts['exact_duplicate_source_rows_excluded']}**.",
        f"- Same-execution groups resolved with non-identical representations: **{counts['same_execution_resolved_groups']}**; source rows in those groups: **{counts['same_execution_resolved_source_rows']}**; collapsed rows: **{counts['same_execution_collapsed_source_rows']}**.",
        f"- Unresolved episode groups: **{counts['unresolved_episode_groups']}**; source rows fail-closed: **{counts['unresolved_source_rows']}**; eligible rows fail-closed: **{counts['unresolved_eligible_source_rows']}**.",
        f"- Possible-related coarse groups retained as separate execution candidates: **{counts['possible_related_groups']}**.",
        f"- Final distinct singleton groups: **{counts['distinct_singleton_groups']}**.",
        f"- Total episode groups: **{counts['total_episode_groups']}**; resolved canonical episodes: **{counts['resolved_episode_count']}**.",
        "",
        "## Candidate classes and policy",
        "",
        "- `exact_duplicate`: source SHA-256 and B1 body SHA-256 are identical; deterministic resolution is allowed.",
        "- `same_execution_strong_candidate`: all execution identity fields agree, sample origin and outcome/R agree, a source/template relationship is present, and no contradictory fact exists. The representative is selected by completeness/provenance authority, then lexical tie-break only.",
        "- `possible_related`: only coarse date/instrument/direction identity is shared while execution plan facts differ; rows remain separate.",
        "- `distinct`: no full execution-identity collision exists; the source row remains its own episode.",
        "- `unresolved`: strategy mapping is absent/different, evidence is insufficient, or facts conflict; the complete group contributes zero performance observations.",
        "",
        "## Known regression cases",
        "",
        "- Exact duplicate: `2024-01-17_BTCUSDT_S_1.md` and `2024-01-17_BTCUSDT_S_1_1.md` share source SHA and resolve to one canonical episode with one counting representative.",
        "- Different representation: `2023-10-21_BTCUSDT_L_2.md` and `2023-10-21_BTCUSDT_L_mss-vegas.md` resolve only because the full execution identity agrees and the named/numeric sparse/full template relationship is evidenced; no filename is hard-coded in production logic.",
        "- Legitimate similar trades with the same date/instrument/direction but different execution facts are retained as separate or unresolved candidates; coarse identity never auto-merges them.",
        "",
        "## Non-trivial final groups",
        "",
    ]
    for group in resolution_groups:
        if group["member_count"] > 1:
            lines.extend(_render_group(group))
    lines.extend([
        "## Performance migration notes",
        "",
        "B2 must repin the B1 v3 dataset and manifest plus the episode-resolution artifact. Its performance boundary is one `performance_counting_eligibility == true` row per unique `canonical_episode_id`; it must fail closed if an episode ID appears more than once. Previous B2 counts were invalidated pending this upstream resolution and are not reused.",
        "",
        "## Protected boundary",
        "",
        "B1 v2, trade Markdown, Step-0, registry, mapping/adjudication artifacts, strategy lifecycle/taxonomy, canonical R, outcome, and historical execution facts are inputs only. No source file is deleted, renamed, or edited.",
        "",
    ])
    return "\n".join(lines)


def _render_decision_packet(resolution_groups: Sequence[Mapping[str, Any]]) -> str:
    unresolved = [group for group in resolution_groups if group["relationship"] == "unresolved"]
    lines = [
        "# Method Validation B1 v3 — Batched Episode Identity Decision Packet",
        "",
        "This packet contains only unresolved groups. They are excluded from performance counting until an owner-authorized identity decision is available. No per-group question is required to complete deterministic work.",
        "",
        f"Unresolved groups: **{len(unresolved)}**; source rows excluded: **{sum(group['member_count'] for group in unresolved)}**.",
        "",
    ]
    for group in unresolved:
        lines.extend(_render_group(group))
        evidence = group.get("evidence") or {}
        lines.extend([
            "- Evidence packet:",
            f"  - execution identity: `{json.dumps(group['member_observations'][0] | {'canonical_realized_r': group['member_observations'][0].get('canonical_realized_r'), 'outcome': group['member_observations'][0].get('outcome')}, ensure_ascii=False, sort_keys=True)}`",
            f"  - strategy IDs: `{json.dumps(evidence.get('strategy_ids', []), ensure_ascii=False)}`",
            f"  - fail-closed reason: `{evidence.get('fail_closed_reason', 'unresolved')}`",
            "- Recommended interpretation: do not merge without additional authoritative execution/provenance evidence; retain every source row.",
            f"- Performance observation impact: **{group['member_count']} source rows -> 0 until resolved**.",
            "",
        ])
    if not unresolved:
        lines.append("No unresolved groups.")
    return "\n".join(lines)


def _render_readme(manifest: Mapping[str, Any], counts: Mapping[str, Any]) -> str:
    return "\n".join([
        "# Method Validation — B1 v3 episode-aware canonical dataset",
        "",
        "This is a new versioned downstream identity layer. B1 v2 remains byte-identical and immutable. All source rows are retained; only the performance-counting eligibility and canonical episode identity layer are added.",
        "",
        "## Pins",
        "",
        f"- `schema_version`: `{manifest['schema_version']}`",
        f"- `dataset_version`: `{manifest['dataset_version']}`",
        f"- `baseline_sha`: `{manifest['baseline_sha']}`",
        f"- `generated_at`: `{manifest['generated_at']}`",
        f"- B1 v2 dataset SHA-256: `{B1_V2_DATASET_SHA256}`",
        f"- B1 v2 manifest digest: `{B1_V2_MANIFEST_DIGEST}`",
        "",
        "## Universe and episode identity",
        "",
        f"- Source rows retained: **{counts['source_rows']}**.",
        f"- Resolved canonical episodes: **{counts['resolved_episode_count']}**; unresolved groups: **{counts['unresolved_episode_groups']}**.",
        f"- Performance-countable episodes: **{manifest['counts']['performance_countable_episode_count']}**.",
        "- The canonical representative is selected from execution/provenance completeness and stable authority fields, never by first encounter or aggregate result.",
        "- Unresolved groups are retained with `performance_counting_eligibility: false` and `unresolved_episode_grouping`; no source is physically cleaned.",
        "",
        "## Artifacts",
        "",
        f"- Candidate audit: `{manifest['artifacts']['episode_candidate_audit']['path']}`",
        f"- Canonical episode resolution: `{manifest['artifacts']['episode_resolution']['path']}`",
        f"- Human report: `{manifest['artifacts']['episode_report']['path']}`",
        f"- Batched decision packet: `{manifest['artifacts']['decision_packet']['path']}`",
        "",
        "## B2 migration contract",
        "",
        "B2 must consume B1 v3, count at most one row for each unique `canonical_episode_id`, and fail closed on duplicate performance-countable episode IDs. Previous B2 performance numbers are invalidated pending this repin.",
        "",
        "## Protected inputs",
        "",
        "B1 v2, trades, Step-0, registry, mapping/adjudication artifacts, lifecycle/taxonomy, canonical R, outcome, and historical execution facts remain unchanged. The episode algorithm reads no aggregate result artifact or metric.",
        "",
        "## Reproduction",
        "",
        f"`python -m trading_os b1-v3 --baseline-sha {manifest['baseline_sha']} --generated-at {manifest['generated_at']} --output-dir exports/method-validation/b1-v3 --adjudication-dir canonical/adjudications/method-validation`",
        "",
        "Stop at fresh external review. Do not merge this upstream PR or dependent B2 PR #34.",
    ])


def _build_manifest(
    *,
    root: Path,
    output_dir: Path,
    adjudication_dir: Path,
    baseline_sha: str,
    generated_at: str,
    b1_manifest: Mapping[str, Any],
    counts: Mapping[str, Any],
    performance_countable: int,
    artifact_paths: Mapping[str, Path],
    source_pins: Mapping[str, str],
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    for key, path in sorted(artifact_paths.items()):
        entry: dict[str, Any] = {
            "path": str(path.relative_to(root)).replace("\\", "/") if path.is_relative_to(root) else str(path).replace("\\", "/"),
        }
        # The manifest contains this artifact table and therefore cannot carry
        # a self-referential content hash.
        if key != "manifest":
            entry["sha256"] = _sha256_file(path)
        artifacts[key] = entry
    manifest: dict[str, Any] = {
        "schema_version": B1_V3_SCHEMA_VERSION,
        "dataset_version": B1_V3_DATASET_VERSION,
        "generated_at": generated_at,
        "baseline_sha": baseline_sha,
        "mode": B1_V3_GENERATOR_MODE,
        "read_only_inputs": True,
        "source_rows_retained": True,
        "performance_data_consumed": False,
        "optimization_performed": False,
        "b1_v2_pin": {
            "schema_version": B1_V2_SCHEMA_VERSION,
            "dataset_path": B1_V2_DATASET_PATH,
            "dataset_sha256": B1_V2_DATASET_SHA256,
            "manifest_path": B1_V2_MANIFEST_PATH,
            "manifest_digest": B1_V2_MANIFEST_DIGEST,
            "manifest_file_sha256": B1_V2_MANIFEST_FILE_SHA256,
            "readme_path": B1_V2_README_PATH,
            "readme_sha256": B1_V2_README_SHA256,
        },
        "source_authority_pins": {
            "b1_v2_manifest_pinned_source_digests": dict(sorted((b1_manifest.get("pinned_source_digests") or {}).items())),
            "current_verified_files": dict(sorted(source_pins.items())),
            "known_source_pin_drift": {
                key: {
                    "b1_v2_pinned_sha256": (b1_manifest.get("pinned_source_digests") or {}).get(key),
                    "current_sha256": value,
                }
                for key, value in sorted(source_pins.items())
                if key in (b1_manifest.get("pinned_source_digests") or {})
                if value != (b1_manifest.get("pinned_source_digests") or {}).get(key)
            },
            "additional_current_verified_files": {
                key: value
                for key, value in sorted(source_pins.items())
                if key not in (b1_manifest.get("pinned_source_digests") or {})
            },
        },
        "episode_identity_policy": {
            "evidence_first": True,
            "fail_closed": True,
            "performance_blind": True,
            "exact_duplicate_auto_resolution": True,
            "nonidentical_requires_full_execution_identity_and_relationship": True,
            "same_day_is_not_identity": True,
            "same_entry_is_not_identity": True,
            "same_r_is_not_identity": True,
            "canonical_representative_not_first_encountered": True,
            "unresolved_counting_policy": "exclude_until_resolved",
        },
        "counts": dict(sorted({**counts, "performance_countable_episode_count": performance_countable}.items())),
        "artifacts": artifacts,
        "compatibility_contract": {
            "unchanged_fields": list(COMPATIBILITY_FIELDS),
            "episode_fields_added_or_revised": [
                "source_record_id", "canonical_episode_id", "episode_role", "episode_resolution",
                "episode_member_count", "related_record_refs", "performance_counting_eligibility",
            ],
        },
        "negative_guarantees": {
            "b1_v2_unchanged": True,
            "trades_unchanged": True,
            "step0_unchanged": True,
            "registry_unchanged": True,
            "mapping_unchanged": True,
            "adjudications_unchanged": True,
            "strategy_lifecycle_unchanged": True,
            "strategy_taxonomy_unchanged": True,
            "canonical_r_unchanged": True,
            "outcome_unchanged": True,
            "historical_execution_facts_unchanged": True,
            "source_files_deleted": False,
            "source_files_renamed": False,
            "performance_driven_grouping": False,
            "heuristic_deduplication": False,
            "synthetic_or_future_data": False,
        },
        "manifest_digest": None,
    }
    unsigned = dict(manifest)
    unsigned.pop("manifest_digest", None)
    manifest["manifest_digest"] = _sha256_bytes(_canonical_bytes(unsigned))
    return manifest


def validate_b1_v3_manifest(
    manifest: Mapping[str, Any],
    *,
    project_root: Path | None = None,
    output_dir: Path | None = None,
    adjudication_dir: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    required = ("schema_version", "dataset_version", "generated_at", "baseline_sha", "b1_v2_pin", "counts", "artifacts", "negative_guarantees", "manifest_digest")
    errors.extend(f"missing:{key}" for key in required if key not in manifest)
    if errors:
        return errors
    if manifest.get("schema_version") != B1_V3_SCHEMA_VERSION or manifest.get("dataset_version") != B1_V3_DATASET_VERSION:
        errors.append("schema_version")
    if not isinstance(manifest.get("baseline_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("baseline_sha"))):
        errors.append("baseline_sha")
    unsigned = dict(manifest)
    supplied = unsigned.pop("manifest_digest", None)
    if supplied != _sha256_bytes(_canonical_bytes(unsigned)):
        errors.append("manifest_digest")
    b1_pin = manifest.get("b1_v2_pin")
    if not isinstance(b1_pin, Mapping) or b1_pin.get("dataset_sha256") != B1_V2_DATASET_SHA256 or b1_pin.get("manifest_digest") != B1_V2_MANIFEST_DIGEST:
        errors.append("b1_v2_pin")
    counts = manifest.get("counts")
    if not isinstance(counts, Mapping) or counts.get("source_rows") != KNOWN_SOURCE_COUNT:
        errors.append("counts.source_rows")
    guarantees = manifest.get("negative_guarantees")
    if not isinstance(guarantees, Mapping) or any(guarantees.get(key) is not True for key in ("b1_v2_unchanged", "trades_unchanged", "step0_unchanged", "registry_unchanged", "mapping_unchanged", "adjudications_unchanged", "strategy_lifecycle_unchanged", "strategy_taxonomy_unchanged", "canonical_r_unchanged", "outcome_unchanged", "historical_execution_facts_unchanged")) or any(guarantees.get(key) is not False for key in ("source_files_deleted", "source_files_renamed", "performance_driven_grouping", "heuristic_deduplication", "synthetic_or_future_data")):
        errors.append("negative_guarantees")
    if manifest.get("performance_data_consumed") is not False or manifest.get("optimization_performed") is not False:
        errors.append("performance_blind")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"dataset", "episode_candidate_audit", "episode_resolution", "episode_report", "decision_packet", "manifest", "readme"}:
        errors.append("artifacts")
    if project_root is not None:
        root = project_root.resolve()
        try:
            _, v2_rows = _load_and_validate_b1_v2(root)
            candidates, groups, audit_counts = discover_episode_candidates(root, v2_rows)
            for key, expected in audit_counts.items():
                if dict(counts or {}).get(key) != expected:
                    errors.append(f"counts.current_{key}")
            if output_dir is not None:
                dataset_rows = _read_jsonl(output_dir / "canonical-trade-dataset.jsonl")
                if len(dataset_rows) != len(v2_rows):
                    errors.append("dataset.source_preservation")
                v2_by_trade_ref = {str(row.get("trade_ref")): row for row in v2_rows}
                v3_by_trade_ref = {str(row.get("trade_ref")): row for row in dataset_rows}
                if set(v2_by_trade_ref) != set(v3_by_trade_ref):
                    errors.append("dataset.trade_ref_preservation")
                for trade_ref, v2_row in v2_by_trade_ref.items():
                    v3_row = v3_by_trade_ref.get(trade_ref)
                    if v3_row is None:
                        continue
                    if _source_compatibility(v2_row, v3_row):
                        errors.append(f"dataset.compatibility:{trade_ref}")
                    for field in PROTECTED_SOURCE_FIELDS:
                        if v2_row.get(field) != v3_row.get(field):
                            errors.append(f"dataset.protected:{trade_ref}:{field}")
                    if v2_row.get("source_sha256") != v3_row.get("source_sha256") or v2_row.get("source_body_sha256") != v3_row.get("source_body_sha256"):
                        errors.append(f"dataset.source_hash:{trade_ref}")
                if len({row.get("canonical_episode_id") for row in dataset_rows if row.get("performance_counting_eligibility")}) != counts.get("performance_countable_episode_count"):
                    errors.append("dataset.performance_count")
                if any(row.get("performance_counting_eligibility") and not row.get("canonical_episode_id") for row in dataset_rows):
                    errors.append("dataset.missing_episode_id")
                counted_ids = [row.get("canonical_episode_id") for row in dataset_rows if row.get("performance_counting_eligibility")]
                if len(counted_ids) != len(set(counted_ids)):
                    errors.append("dataset.duplicate_counted_episode")
            if adjudication_dir is not None:
                resolution_path = adjudication_dir / EPISODE_RESOLUTION_NAME
                candidate_path = adjudication_dir / EPISODE_CANDIDATE_NAME
                if len(_read_jsonl(resolution_path)) != len(groups) or len(_read_jsonl(candidate_path)) != len(candidates):
                    errors.append("adjudication.reconstruction")
        except EpisodeIdentityError as exc:
            errors.append(f"inputs:{exc}")
    artifact_locations = {
        "dataset": (output_dir, "canonical-trade-dataset.jsonl"),
        "manifest": (output_dir, "manifest.json"),
        "readme": (output_dir, "README.md"),
        "episode_report": (output_dir, EPISODE_REPORT_NAME),
        "decision_packet": (output_dir, DECISION_PACKET_NAME),
        "episode_candidate_audit": (adjudication_dir, EPISODE_CANDIDATE_NAME),
        "episode_resolution": (adjudication_dir, EPISODE_RESOLUTION_NAME),
    }
    for name, entry in (artifacts.items() if isinstance(artifacts, Mapping) else []):
        if not isinstance(entry, Mapping):
            errors.append(f"artifact_hash:{name}")
            continue
        location = artifact_locations.get(name)
        if location is not None and location[0] is not None:
            path = location[0] / location[1]
            if not path.is_file():
                errors.append(f"artifact_hash:{name}")
            elif name != "manifest" and (not isinstance(entry.get("sha256"), str) or _sha256_file(path) != entry.get("sha256")):
                errors.append(f"artifact_hash:{name}")
        elif name != "manifest" and not isinstance(entry.get("sha256"), str):
            errors.append(f"artifact_hash:{name}")
    return errors


def run_b1_v3(
    root: Path,
    *,
    baseline_sha: str,
    generated_at: str,
    output_dir: Path | None = None,
    adjudication_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", baseline_sha):
        raise EpisodeIdentityError("baseline_sha must be a full lowercase 40-character SHA")
    if not generated_at or not isinstance(generated_at, str):
        raise EpisodeIdentityError("generated_at is required")
    output_dir = (root / DEFAULT_B1_V3_OUTPUT if output_dir is None else (root / output_dir if not output_dir.is_absolute() else output_dir)).resolve()
    adjudication_dir = (root / DEFAULT_ADJUDICATION_OUTPUT if adjudication_dir is None else (root / adjudication_dir if not adjudication_dir.is_absolute() else adjudication_dir)).resolve()
    b1_manifest, v2_rows = _load_and_validate_b1_v2(root)
    source_pins = _source_file_pins(root, b1_manifest)
    candidates, groups, counts = discover_episode_candidates(root, v2_rows)

    resolution_path = adjudication_dir / EPISODE_RESOLUTION_NAME
    candidate_path = adjudication_dir / EPISODE_CANDIDATE_NAME
    _write_jsonl(resolution_path, groups)
    _write_jsonl(candidate_path, candidates)
    _write_text(output_dir / EPISODE_REPORT_NAME, _render_report(counts=counts, candidate_rows=candidates, resolution_groups=groups))
    _write_text(output_dir / DECISION_PACKET_NAME, _render_decision_packet(groups))

    by_trade_ref: dict[str, Mapping[str, Any]] = {}
    for group in groups:
        for observation in group["member_observations"]:
            by_trade_ref[str(observation["trade_ref"])] = group
    if len(by_trade_ref) != len(v2_rows):
        raise EpisodeIdentityError("episode resolution does not cover every B1 v2 source row")

    v3_rows: list[dict[str, Any]] = []
    performance_countable = 0
    for row in _ordered(v2_rows):
        group = by_trade_ref[str(row["trade_ref"])]
        representative = group.get("canonical_representative")
        is_primary = representative == row.get("trade_ref")
        original_eligibility = row.get("analysis_eligibility") == "eligible"
        all_members_eligible = all(item.get("analysis_eligibility") == "eligible" for item in group["member_observations"])
        can_count = bool(group["relationship"] != "unresolved" and is_primary and original_eligibility and all_members_eligible)
        if can_count:
            performance_countable += 1
        item = dict(row)
        item["source_record_id"] = row.get("step0_record_id")
        item["source_episode_id_v2"] = row.get("episode_id")
        item["canonical_episode_id"] = group["episode_id"]
        item["episode_id"] = group["episode_id"]
        item["episode_role"] = "unresolved" if group["relationship"] == "unresolved" else ("primary" if is_primary else "duplicate_representation")
        item["episode_resolution"] = group["relationship"]
        item["episode_member_count"] = group["member_count"]
        item["episode_member_trade_refs"] = list(group["member_trade_refs"])
        item["source_related_record_refs"] = list(row.get("related_record_refs") or [])
        item["related_record_refs"] = [ref for ref in group["member_trade_refs"] if ref != row.get("trade_ref")]
        item["canonical_representative_trade_ref"] = representative
        item["performance_counting_eligibility"] = can_count
        if can_count:
            item["performance_exclusion_reason"] = None
        elif group["relationship"] == "unresolved":
            item["performance_exclusion_reason"] = "unresolved_episode_grouping"
        elif not original_eligibility:
            item["performance_exclusion_reason"] = row.get("ineligibility_reason")
        else:
            item["performance_exclusion_reason"] = "duplicate_episode_representation"
        item["episode_resolution_source"] = group["resolution_source"]
        item["episode_confidence"] = group["confidence"]
        item["episode_evidence"] = group["evidence"]
        v3_rows.append(item)

    if len({row.get("canonical_episode_id") for row in v3_rows if row.get("performance_counting_eligibility")}) != performance_countable:
        raise EpisodeIdentityError("performance episode uniqueness invariant failed")
    dataset_path = output_dir / "canonical-trade-dataset.jsonl"
    _write_jsonl(dataset_path, v3_rows)
    counts = dict(counts)
    counts["performance_countable_episode_count"] = performance_countable
    counts["excluded_duplicate_representation_rows"] = sum(
        1 for row in v3_rows if row.get("performance_exclusion_reason") == "duplicate_episode_representation"
    )
    counts["unresolved_excluded_rows"] = sum(
        1 for row in v3_rows if row.get("episode_resolution") == "unresolved"
    )
    counts["ineligible_source_rows_retained"] = sum(1 for row in v3_rows if row.get("analysis_eligibility") != "eligible")

    artifact_paths = {
        "dataset": dataset_path,
        "episode_candidate_audit": candidate_path,
        "episode_resolution": resolution_path,
        "episode_report": output_dir / EPISODE_REPORT_NAME,
        "decision_packet": output_dir / DECISION_PACKET_NAME,
    }
    provisional = {
        "schema_version": B1_V3_SCHEMA_VERSION,
        "dataset_version": B1_V3_DATASET_VERSION,
        "baseline_sha": baseline_sha,
        "generated_at": generated_at,
        "counts": counts,
        "artifacts": {
            key: {"path": str(path.relative_to(root)).replace("\\", "/") if path.is_relative_to(root) else str(path).replace("\\", "/")}
            for key, path in artifact_paths.items()
        },
    }
    _write_text(output_dir / "README.md", _render_readme(provisional, counts))
    artifact_paths["readme"] = output_dir / "README.md"
    manifest_path = output_dir / "manifest.json"
    artifact_paths["manifest"] = manifest_path
    manifest = _build_manifest(
        root=root,
        output_dir=output_dir,
        adjudication_dir=adjudication_dir,
        baseline_sha=baseline_sha,
        generated_at=generated_at,
        b1_manifest=b1_manifest,
        counts=counts,
        performance_countable=performance_countable,
        artifact_paths=artifact_paths,
        source_pins=source_pins,
    )
    _write_json(manifest_path, manifest)
    # The manifest hash is intentionally not self-referential; its own artifact
    # entry documents the path while all content hashes are immutable inputs.
    errors = validate_b1_v3_manifest(
        manifest,
        project_root=root,
        output_dir=output_dir,
        adjudication_dir=adjudication_dir,
    )
    if errors:
        raise EpisodeIdentityError("generated B1 v3 manifest failed validation: " + "; ".join(errors))
    return manifest
