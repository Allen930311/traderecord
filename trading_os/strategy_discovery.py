"""Deterministic Step-0 strategy-status triage for historical review.

Step-0 is a read-only gate.  It derives the current ``trades/`` corpus with the
existing canonical-migration source selector, maps only exact registry IDs or
aliases, and writes rebuildable review projections.  It never infers strategy
meaning, opens a human worksheet, resolves identity, or changes source files.
"""

from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .identifiers import normalized_record_identifier
from .registry import load_registry, registry_digest


STEP0_SCHEMA_VERSION = "strategy-discovery-step0.v1"
CORPUS_SELECTOR_VERSION = "issue-5-full-history.v1"
CORPUS_SELECTOR_AUTHORITY = "scripts.canonical_migration.full_history.discover_sources"
REGISTRY_PATH = "registry/strategy-registry.yaml"
HISTORICAL_CAMPAIGN_LABEL = "256 corpus"
DEFAULT_OUTPUT_RELATIVE = Path("exports/strategy-discovery/step-0")
ARTIFACT_NAMES = (
    "step-0-results.jsonl",
    "corpus-manifest.json",
    "step-0-report.md",
    "blocker-backlog.jsonl",
)
IDENTITY_LOG_PATHS = (
    "canonical/adjudications/historical-cleaning-human-revisions.jsonl",
    "canonical/adjudications/phase4d-human-revisions.jsonl",
)
IDENTITY_DECISION_DOMAINS = frozenset(
    {
        "duplicate_lifecycle_identity",
        "date_instrument_direction_identity",
        "identity",
    }
)
REGISTRY_STATUSES = ("active", "retired", "experimental", "proposed")
GATE_OUTCOMES = (
    "ACTIVE",
    "RETIRED",
    "EXPERIMENTAL",
    "BLOCKED_STRATEGY_STATUS",
    "BLOCKED_STRATEGY_MAPPING",
)
BLOCKED_OUTCOMES = frozenset({"BLOCKED_STRATEGY_STATUS", "BLOCKED_STRATEGY_MAPPING"})
TERMINAL_RECORD_CLASSES = {
    "retired": "retired_strategy_trade",
    "experimental": "experiment",
}
ALLOWED_TERMINAL_REVIEW_FIELDS = {
    "review_logic_status",
    "review_feature_status",
    "review_strategy_status",
    "review_record_class",
    "review_recommend_disposition",
}
FORBIDDEN_IDENTITY_FIELDS = {
    "canonical_trade_id",
    "canonical_lifecycle_id",
    "lifecycle_id",
    "provider_group_id",
    "provider_lifecycle_id",
    "duplicate_of",
}

# --- Per-trade decision consumption ------------------------------------------
# The merged, authoritative owner per-trade application manifest (PR #27).  It
# reconciles the 67 owner per-trade strategy-mapping decisions against the
# registry: 28 rows already route via the `ob` legacy_alias, 26 rows carry an
# explicit owner route the Step-0 resolver must consume by key, and 13 rows are a
# hard exclusion (keep_blocked / historical-cleaning / position-management
# hand-off) that must stay BLOCKED_STRATEGY_MAPPING.  Step-0 never re-judges these
# rows; it performs a keyed lookup of an already-decided human route.
PER_TRADE_DECISION_LOG_PATHS = (
    "canonical/adjudications/strategy-mapping-review/"
    "strategy-mapping-per-trade-application-20260910.jsonl",
)
# Only this reconciliation mechanism is consumed as a resolver override.  The
# `applied_via_registry_alias:ob` rows are intentionally left to the registry
# alias so a trade is never double-routed.
PER_TRADE_CONSUMED_MECHANISM = "human_route_recorded_pending_resolver_consumption"
# Hard exclusion: an owner disposition of keep-blocked or a review-lane hand-off
# never produces a route, regardless of any other field on the row.
PER_TRADE_HARD_EXCLUDED_DISPOSITIONS = frozenset(
    {"keep_blocked", "historical_cleaning_handoff", "position_management_handoff"}
)
HUMAN_PER_TRADE_MATCH_TYPE = "human_per_trade_decision"
KNOWN_MATCH_TYPES = frozenset(
    {"exact_strategy_id", "exact_legacy_alias", HUMAN_PER_TRADE_MATCH_TYPE}
)


class Step0Error(ValueError):
    """Raised when a Step-0 invariant cannot be established safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _input_equals_output(authoritative_input_count: int, emitted_output_count: int) -> bool:
    """Return whether the independently counted input and output records agree."""

    return authoritative_input_count == emitted_output_count


def _calculate_deleted_source_resurrection(
    historical_reference_paths: Iterable[str],
    baseline_main_paths: Iterable[str] | None,
    current_selected_paths: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Calculate historical deletion and resurrection as explicit set operations.

    ``historically_deleted`` is the historical-reference set minus the
    baseline-main set.  A resurrection is only a member of that deleted set
    which is present in the current selected corpus; arbitrary current additions
    are tracked separately as baseline drift and are not resurrection events.
    """

    if baseline_main_paths is None:
        return [], []
    historically_deleted = sorted(set(historical_reference_paths) - set(baseline_main_paths))
    resurrected = sorted(set(historically_deleted) & set(current_selected_paths))
    return historically_deleted, resurrected


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(value), encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
        newline="\n",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise Step0Error(f"blank line in JSONL artifact: {path}:{line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Step0Error(f"invalid JSONL artifact: {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise Step0Error(f"JSONL row is not an object: {path}:{line_number}")
        rows.append(value)
    return rows


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative_path(path: str | Path) -> str:
    return Path(path).as_posix()


def _is_source_candidate(path: str) -> bool:
    name = Path(path).name.lower()
    return Path(path).suffix.lower() == ".md" and name not in {"_index.md", "readme.md"}


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise Step0Error(f"git command failed ({' '.join(args)}): {detail}")
    return completed.stdout.strip()


def _resolve_commit(root: Path, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Step0Error("a non-empty commit SHA/ref is required")
    return _run_git(root, "rev-parse", "--verify", f"{value}^{{commit}}")


def _git_source_paths(root: Path, commit: str, legacy_root: str = "trades") -> list[str]:
    output = _run_git(root, "ls-tree", "-r", "--name-only", commit, "--", legacy_root)
    paths = [_relative_path(line) for line in output.splitlines() if line.strip()]
    paths = [path for path in paths if _is_source_candidate(path)]
    if len(paths) != len(set(paths)):
        raise Step0Error(f"duplicate source paths in git tree {commit}")
    return sorted(paths)


def _assert_worktree_sources_match_baseline(root: Path, baseline_sha: str) -> None:
    status = _run_git(root, "status", "--short", "--untracked-files=all", "--", "trades")
    if status:
        raise Step0Error(f"trades/ is modified or has untracked files:\n{status}")
    changed = _run_git(root, "diff", "--name-only", baseline_sha, "--", "trades")
    if changed:
        raise Step0Error(f"trades/ differs from baseline {baseline_sha}:\n{changed}")


def build_strategy_lookup(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a collision-checked, exact-match registry lookup.

    The returned value contains one copy per exact key.  A key repeated for two
    different strategies is a registry fault and blocks the run instead of
    relying on ordering or silently choosing a winner.
    """

    strategies = registry.get("strategies")
    if not isinstance(strategies, list):
        raise Step0Error("strategy registry strategies must be a list")
    lookup: dict[str, dict[str, Any]] = {}

    def add_key(key: Any, strategy: Mapping[str, Any], match_type: str) -> None:
        if not isinstance(key, str):
            raise Step0Error("strategy IDs and legacy aliases must be strings")
        strategy_id = strategy.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            raise Step0Error("every registry strategy needs a string strategy_id")
        existing = lookup.get(key)
        if existing is not None:
            if existing["strategy"]["strategy_id"] != strategy_id:
                raise Step0Error(
                    f"exact registry lookup collision for {key!r}: "
                    f"{existing['strategy']['strategy_id']} vs {strategy_id}"
                )
            # A strategy_id match is the stronger exact form when an alias has
            # the same spelling.  This is deterministic and not fuzzy matching.
            if existing["match_type"] == "exact_strategy_id":
                return
            if match_type == "exact_strategy_id":
                lookup[key] = {"match_type": match_type, "strategy": dict(strategy)}
            return
        lookup[key] = {"match_type": match_type, "strategy": dict(strategy)}

    for strategy in strategies:
        if not isinstance(strategy, Mapping):
            raise Step0Error("every registry strategy must be an object")
        add_key(strategy.get("strategy_id"), strategy, "exact_strategy_id")
    for strategy in strategies:
        if not isinstance(strategy, Mapping):
            raise Step0Error("every registry strategy must be an object")
        aliases = strategy.get("legacy_aliases", [])
        if aliases is None:
            aliases = []
        if not isinstance(aliases, list):
            raise Step0Error(f"legacy_aliases is not a list for {strategy.get('strategy_id')}")
        for alias in aliases:
            add_key(alias, strategy, "exact_legacy_alias")
    return lookup


def classify_strategy_label(raw_label: Any, lookup: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Classify one raw label using exact string equality only."""

    if not isinstance(raw_label, str):
        return {
            "matched": False,
            "match_type": None,
            "lookup_value": raw_label,
            "strategy": None,
        }
    match = lookup.get(raw_label)
    if match is None:
        return {
            "matched": False,
            "match_type": None,
            "lookup_value": raw_label,
            "strategy": None,
        }
    return {
        "matched": True,
        "match_type": match["match_type"],
        "lookup_value": raw_label,
        "strategy": copy.deepcopy(match["strategy"]),
    }


def _route_for_match(match: Mapping[str, Any]) -> tuple[str, str]:
    if not match.get("matched"):
        return "BLOCKED_STRATEGY_MAPPING", "blocked"
    strategy = match.get("strategy")
    status = strategy.get("status") if isinstance(strategy, Mapping) else None
    if status == "active":
        return "ACTIVE", "eligible_active_review_pool"
    if status == "retired":
        return "RETIRED", "terminal_excluded"
    if status == "experimental":
        return "EXPERIMENTAL", "terminal_excluded"
    if status == "proposed":
        return "BLOCKED_STRATEGY_STATUS", "blocked"
    raise Step0Error(f"registry strategy has invalid status: {status!r}")


def _selected_record_ids(row: Mapping[str, Any]) -> set[str]:
    selected: set[str] = set()
    for value in row.get("record_ids", []) if isinstance(row.get("record_ids", []), list) else []:
        if isinstance(value, str):
            selected.add(value)
    after = row.get("after")
    if isinstance(after, Mapping):
        payload = after.get("approved_payload")
        if isinstance(payload, Mapping):
            partition = payload.get("partition")
            if isinstance(partition, Mapping):
                groups = partition.get("groups")
                if isinstance(groups, list):
                    for group in groups:
                        if not isinstance(group, Mapping):
                            continue
                        ids = group.get("record_ids")
                        if isinstance(ids, list):
                            selected.update(value for value in ids if isinstance(value, str))
    return selected


def _identity_source_paths(row: Mapping[str, Any]) -> list[str]:
    """Return only source paths selected by the durable row when determinable.

    Wave-2 has candidate bindings for a row containing both T10-A and T10-B;
    those bindings are used to avoid projecting the approval onto T10-A.  Older
    rows have one source path per selected record ID, so their exact evidence
    refs are safe to mirror.  Ambiguous log shapes are ignored fail-closed.
    """

    selected_ids = _selected_record_ids(row)
    before = row.get("before")
    bindings = before.get("candidate_bindings") if isinstance(before, Mapping) else None
    if isinstance(bindings, list):
        paths = {
            binding.get("source_path")
            for binding in bindings
            if isinstance(binding, Mapping)
            and binding.get("record_id") in selected_ids
            and isinstance(binding.get("source_path"), str)
        }
        return sorted(path for path in paths if path.startswith("trades/") and _is_source_candidate(path))

    evidence_paths = {
        value
        for value in row.get("evidence_refs", []) if isinstance(row.get("evidence_refs", []), list)
        if isinstance(value, str) and value.startswith("trades/") and _is_source_candidate(value)
    }
    if selected_ids and len(evidence_paths) == len(selected_ids):
        return sorted(evidence_paths)
    return []


def load_identity_status_refs(
    project_root: Path,
    log_paths: Sequence[str] = IDENTITY_LOG_PATHS,
) -> dict[str, list[dict[str, str]]]:
    """Read existing resolved identity rows as a non-writing provenance index."""

    by_path: dict[str, list[dict[str, str]]] = {}
    for log_path in log_paths:
        path = project_root / log_path
        if not path.is_file():
            raise Step0Error(f"required identity log is missing: {log_path}")
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                raise Step0Error(f"blank line in identity log: {log_path}:{line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise Step0Error(f"invalid identity log JSON: {log_path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise Step0Error(f"identity log row is not an object: {log_path}:{line_number}")
            if row.get("status") != "resolved" or row.get("decision_domain") not in IDENTITY_DECISION_DOMAINS:
                continue
            revision_id = row.get("revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                raise Step0Error(f"resolved identity row has no revision_id: {log_path}:{line_number}")
            for source_path in _identity_source_paths(row):
                ref = {"log_path": _relative_path(log_path), "revision_id": revision_id}
                if ref not in by_path.setdefault(source_path, []):
                    by_path[source_path].append(ref)
    for refs in by_path.values():
        refs.sort(key=lambda value: (value["log_path"], value["revision_id"]))
    return by_path


def load_per_trade_strategy_overrides(
    project_root: Path,
    lookup: Mapping[str, Mapping[str, Any]],
    log_paths: Sequence[str] = PER_TRADE_DECISION_LOG_PATHS,
) -> dict[str, dict[str, str]]:
    """Read the merged owner per-trade application manifest as a keyed override index.

    The return value maps ``trade_ref`` -> ``{"strategy_id", "decision_id",
    "log_path"}`` for exactly the rows whose owner disposition routes a single
    trade to a registry strategy.  ``keep_blocked`` and the historical-cleaning /
    position-management hand-off dispositions are a hard exclusion and never
    appear.  ``applied_via_registry_alias:ob`` rows are skipped so the registry
    alias remains the single routing mechanism for those trades.  This is a keyed
    lookup of an explicit, already-merged human decision; it performs no
    inference.
    """

    overrides: dict[str, dict[str, str]] = {}
    for log_path in log_paths:
        path = project_root / log_path
        if not path.is_file():
            raise Step0Error(f"required per-trade decision log is missing: {log_path}")
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                raise Step0Error(f"blank line in per-trade decision log: {log_path}:{line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise Step0Error(
                    f"invalid per-trade decision log JSON: {log_path}:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise Step0Error(f"per-trade decision row is not an object: {log_path}:{line_number}")
            # Hard exclusion, evaluated first and independently of the mechanism.
            if (
                row.get("application_class") == "excluded"
                or row.get("excluded_disposition") in PER_TRADE_HARD_EXCLUDED_DISPOSITIONS
                or row.get("handoff_lane") is not None
            ):
                continue
            if row.get("resolution_mechanism") != PER_TRADE_CONSUMED_MECHANISM:
                continue
            trade_ref = row.get("trade_ref")
            strategy_id = row.get("target_strategy_id")
            decision_id = row.get("decision_id")
            if not isinstance(trade_ref, str) or not trade_ref:
                raise Step0Error(f"per-trade override row lacks trade_ref: {log_path}:{line_number}")
            if not isinstance(strategy_id, str) or not strategy_id:
                raise Step0Error(
                    f"per-trade override row lacks target_strategy_id: {log_path}:{line_number}"
                )
            if not isinstance(decision_id, str) or not decision_id:
                raise Step0Error(
                    f"per-trade override row lacks decision_id: {log_path}:{line_number}"
                )
            registry_match = lookup.get(strategy_id)
            if registry_match is None or registry_match.get("match_type") != "exact_strategy_id":
                raise Step0Error(
                    f"per-trade override target is not a registry strategy_id: "
                    f"{strategy_id!r} ({log_path}:{line_number})"
                )
            candidate = {
                "strategy_id": strategy_id,
                "decision_id": decision_id,
                "log_path": _relative_path(log_path),
            }
            existing = overrides.get(trade_ref)
            if existing is not None and existing != candidate:
                raise Step0Error(f"conflicting per-trade overrides for {trade_ref!r}")
            overrides[trade_ref] = candidate
    return overrides


def _match_for_override(
    override: Mapping[str, str],
    lookup: Mapping[str, Mapping[str, Any]],
    raw_label: Any,
) -> dict[str, Any]:
    registry_match = lookup.get(override["strategy_id"])
    if registry_match is None:
        raise Step0Error(
            f"per-trade override target missing from registry: {override['strategy_id']!r}"
        )
    return {
        "matched": True,
        "match_type": HUMAN_PER_TRADE_MATCH_TYPE,
        "lookup_value": raw_label,
        "strategy": copy.deepcopy(registry_match["strategy"]),
        "decision_id": override["decision_id"],
    }


def _strategy_mapping_payload(match: Mapping[str, Any]) -> dict[str, Any]:
    strategy = match.get("strategy")
    if not match.get("matched") or not isinstance(strategy, Mapping):
        return {
            "matched": False,
            "match_type": None,
            "lookup_value": match.get("lookup_value"),
            "strategy_id": None,
            "parent_strategy_id": None,
            "registry_status": None,
            "decision_id": None,
        }
    return {
        "matched": True,
        "match_type": match.get("match_type"),
        "lookup_value": match.get("lookup_value"),
        "strategy_id": strategy.get("strategy_id"),
        "parent_strategy_id": strategy.get("parent_strategy_id"),
        "registry_status": strategy.get("status"),
        "decision_id": match.get("decision_id"),
    }


def _result_for_source(
    entry: Mapping[str, Any],
    lookup: Mapping[str, Mapping[str, Any]],
    identity_refs: Mapping[str, Sequence[Mapping[str, str]]],
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    source_path = _relative_path(entry["path"])
    source_sha = entry.get("sha256")
    body_sha = entry.get("raw_body_sha256")
    if not isinstance(source_sha, str) or not isinstance(body_sha, str):
        raise Step0Error(f"source discovery entry lacks hashes: {source_path}")
    record_id = normalized_record_identifier("tradingdiary", source_path, source_sha)
    raw_fields = entry.get("raw_fields")
    if not isinstance(raw_fields, Mapping):
        raise Step0Error(f"source discovery entry lacks raw fields: {source_path}")
    raw_label = raw_fields.get("strategy")
    override = (overrides or {}).get(source_path)
    if override is not None:
        # Precedence: an authoritative per-trade human route outranks registry
        # label matching.
        match = _match_for_override(override, lookup, raw_label)
    else:
        match = classify_strategy_label(raw_label, lookup)
    outcome, eligibility = _route_for_match(match)
    path_refs = [dict(value) for value in identity_refs.get(source_path, [])]
    result: dict[str, Any] = {
        "trade_ref": source_path,
        "record_id": record_id,
        "source_path": source_path,
        "source_sha256": source_sha,
        "source_body_sha256": body_sha,
        "legacy_strategy_label": raw_label,
        "strategy_mapping": _strategy_mapping_payload(match),
        "review_strategy_status": (
            match["strategy"].get("status")
            if match.get("matched") and isinstance(match.get("strategy"), Mapping)
            else None
        ),
        "step0_gate_outcome": outcome,
        "review_eligibility": eligibility,
        "identity_status": "resolved" if path_refs else None,
        "identity_status_source": (
            "existing durable identity-adjudication row"
            if path_refs
            else "no committed identity decision for this source in the current durable logs"
        ),
        "identity_authority_refs": path_refs,
        "provenance_refs": [source_path, REGISTRY_PATH]
        + ([override["log_path"]] if override is not None else [])
        + sorted({value["log_path"] for value in path_refs}),
    }
    if outcome in {"RETIRED", "EXPERIMENTAL"}:
        status = result["review_strategy_status"]
        result.update(
            {
                "review_logic_status": "excluded",
                "review_feature_status": "n/a",
                "review_record_class": TERMINAL_RECORD_CLASSES[status],
                "review_recommend_disposition": "keep-and-mark",
            }
        )
    return result


def _blocker_for_result(result: Mapping[str, Any]) -> dict[str, Any]:
    outcome = result["step0_gate_outcome"]
    mapping = result["strategy_mapping"]
    if outcome == "BLOCKED_STRATEGY_STATUS":
        reason = "exact registry match has status proposed; Step-0 cannot open a worksheet"
        destination = "registry review: resolve proposed status before Step-0 rerun"
    elif outcome == "BLOCKED_STRATEGY_MAPPING":
        reason = "no exact strategy_id or legacy_alias match; Step-0 cannot open a worksheet"
        destination = "registry intake: add an exact strategy_id or legacy_alias before Step-0 rerun"
    else:
        raise Step0Error(f"cannot create a blocker for route {outcome}")
    return {
        "trade_ref": result["trade_ref"],
        "record_id": result["record_id"],
        "source_path": result["source_path"],
        "legacy_strategy_label": result["legacy_strategy_label"],
        "gate_outcome": outcome,
        "reason": reason,
        "registry_lookup": mapping,
        "recommended_destination": destination,
        "provenance_refs": result["provenance_refs"],
    }


def _label_key(value: Any) -> str:
    if value is None:
        return "<null>"
    if isinstance(value, str):
        return value
    return f"<{type(value).__name__}>:{json.dumps(value, ensure_ascii=False, sort_keys=True)}"


def _route_counts(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(
        result["review_strategy_status"] or "unmapped" for result in results
    )
    outcomes = Counter(result["step0_gate_outcome"] for result in results)
    eligibility = Counter(result["review_eligibility"] for result in results)
    status_counts = {key: statuses.get(key, 0) for key in (*REGISTRY_STATUSES, "unmapped")}
    outcome_counts = {key: outcomes.get(key, 0) for key in GATE_OUTCOMES}
    return {
        "strategy_status": status_counts,
        "gate_outcome": outcome_counts,
        "eligible_active_review_pool": eligibility.get("eligible_active_review_pool", 0),
        "terminal_excluded": eligibility.get("terminal_excluded", 0),
        "blocked_total": eligibility.get("blocked", 0),
        "other_contract_terminal": 0,
        "unclassified_remainder": len(results)
        - (
            eligibility.get("eligible_active_review_pool", 0)
            + eligibility.get("terminal_excluded", 0)
            + eligibility.get("blocked", 0)
        ),
    }


def _ordered_corpus_material(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "trade_ref": result["trade_ref"],
            "source_path": result["source_path"],
            "record_id": result["record_id"],
            "source_sha256": result["source_sha256"],
            "source_body_sha256": result["source_body_sha256"],
        }
        for result in results
    ]


def _report(
    *,
    baseline_sha: str,
    historical_reference_commit: str,
    historical_paths: Sequence[str],
    fresh_paths: Sequence[str],
    authoritative_input_count: int,
    emitted_output_count: int,
    historically_deleted_paths: Sequence[str],
    resurrected_paths: Sequence[str],
    results: Sequence[Mapping[str, Any]],
    manifest_fields: Mapping[str, Any],
) -> str:
    routes = _route_counts(results)
    labels = Counter(_label_key(result["legacy_strategy_label"]) for result in results)
    match_types = Counter(
        result["strategy_mapping"].get("match_type") or "unmatched" for result in results
    )
    per_trade = manifest_fields.get("per_trade_consumption", {})
    historical_only = sorted(set(historical_paths) - set(fresh_paths))
    fresh_only = sorted(set(fresh_paths) - set(historical_paths))
    lines = [
        "# Strategy Discovery Step-0 — Historical Review Campaign",
        "",
        f"Historical campaign label: `{HISTORICAL_CAMPAIGN_LABEL}`",
        f"Fresh authoritative corpus count: **{len(results)}**",
        f"Fresh baseline/main: `{baseline_sha}`",
        f"Historical reference: `{historical_reference_commit}`",
        "",
        "## Corpus and selector",
        "",
        f"Selector authority: `{CORPUS_SELECTOR_AUTHORITY}`",
        f"Selector version: `{CORPUS_SELECTOR_VERSION}`",
        "Include all repository-relative Markdown files below `trades/`; exclude only leaf `_index.md` and `readme.md`.",
        "Ordering is repository-relative POSIX path lexicographic order.",
        f"Input records: `{authoritative_input_count}` · Output records: `{emitted_output_count}` · unique `trade_ref`: `{len({result['trade_ref'] for result in results})}`",
        f"Historical-only paths: `{len(historical_only)}` · fresh-only paths: `{len(fresh_only)}`",
        f"Historical-only set: `{', '.join(historical_only) if historical_only else 'none'}`",
        f"Fresh-only set: `{', '.join(fresh_only) if fresh_only else 'none'}`",
        f"Historically deleted paths: `{len(historically_deleted_paths)}` · resurrected paths: `{len(resurrected_paths)}`",
        f"Historically deleted set: `{', '.join(historically_deleted_paths) if historically_deleted_paths else 'none'}`",
        f"Resurrected set: `{', '.join(resurrected_paths) if resurrected_paths else 'none'}`",
        "",
        "The historical `256 corpus` label is a stale/reference cardinality here: the fresh selector produces the current corpus. No historically deleted source is resurrected.",
        "",
        "## Step-0 route summary",
        "",
        "| route | count |",
        "|---|---:|",
    ]
    for route in GATE_OUTCOMES:
        lines.append(f"| `{route}` | {routes['gate_outcome'][route]} |")
    lines.extend(
        [
            f"| **eligible active review pool** | **{routes['eligible_active_review_pool']}** |",
            f"| **terminal excluded** | **{routes['terminal_excluded']}** |",
            f"| **blocked** | **{routes['blocked_total']}** |",
            f"| **other contract terminals** | **{routes['other_contract_terminal']}** |",
            f"| **unclassified remainder** | **{routes['unclassified_remainder']}** |",
            "",
            "## Per-trade decision consumption",
            "",
            "Precedence: an authoritative per-trade owner route "
            "(`strategy-mapping-per-trade-application-20260910.jsonl`, merged in PR #27) "
            "outranks registry label matching. Only "
            f"`{per_trade.get('consumed_mechanism')}` rows are consumed as a keyed "
            "override; `applied_via_registry_alias:ob` routes are left to the registry "
            "alias and the "
            f"`{', '.join(per_trade.get('hard_excluded_dispositions') or [])}` rows are a "
            "hard exclusion that stays `BLOCKED_STRATEGY_MAPPING`.",
            "",
            f"Per-trade override index: `{per_trade.get('override_index_count')}` · "
            f"applied to the corpus: `{per_trade.get('applied_override_count')}`.",
            "",
            "| match type | count |",
            "|---|---:|",
        ]
    )
    for match_type in ("exact_strategy_id", "exact_legacy_alias", HUMAN_PER_TRADE_MATCH_TYPE, "unmatched"):
        lines.append(f"| `{match_type}` | {match_types.get(match_type, 0)} |")
    lines.extend(
        [
            "",
            "No fuzzy or inferred match type appears in `step-0-results.jsonl`; every "
            "`human_per_trade_decision` row carries the owner `decision_id`.",
            "",
            "## Registry-status summary",
            "",
            "| exact registry status | count |",
            "|---|---:|",
        ]
    )
    for status in (*REGISTRY_STATUSES, "unmapped"):
        lines.append(f"| `{status}` | {routes['strategy_status'][status]} |")
    lines.extend(
        [
            "",
            "## Eligible active pool",
            "",
            "Only exact registry matches with `status: active` are eligible for a future human worksheet. Human review has not started.",
            "",
        ]
    )
    for result in results:
        if result["review_eligibility"] == "eligible_active_review_pool":
            lines.append(
                f"- `{result['trade_ref']}` — `{result['strategy_mapping']['strategy_id']}`"
            )
    if not any(result["review_eligibility"] == "eligible_active_review_pool" for result in results):
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Blocker backlog",
            "",
            f"Blocked records: **{routes['blocked_total']}**. See `blocker-backlog.jsonl`; no blocker is silently admitted to the active pool.",
            "",
            "## Raw label distribution",
            "",
            "| legacy strategy label | count |",
            "|---|---:|",
        ]
    )
    for label, count in sorted(labels.items()):
        lines.append(f"| `{label}` | {count} |")
    lines.extend(
        [
            "",
            "## Identity and evidence boundary",
            "",
            "`identity_status` is mirrored only when an existing resolved durable identity row explicitly covers the source path. Step-0 writes no identity row, canonical trade/lifecycle ID, provider mapping, or cleanup decision.",
            "",
            "No chart/image review, provider live lookup, LLM/fuzzy matching, or human judgment was performed. Active rows contain no human review fields; retired/experimental rows contain only the contract's minimal mechanical triage fields.",
            "",
            "`trades/` is read-only and unchanged. CLEAN-03 remains absent from the fresh corpus; CLEAN-09 and CLEAN-11 are untouched.",
            "",
            f"Corpus source-list digest: `{manifest_fields['corpus']['source_list_digest']}`",
            f"Ordered corpus digest: `{manifest_fields['corpus']['ordered_corpus_digest']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_output_location(project_root: Path, output_dir: Path) -> None:
    output = output_dir.resolve()
    source_root = (project_root / "trades").resolve()
    canonical_root = (project_root / "canonical").resolve()
    registry_root = (project_root / "registry").resolve()
    if _is_within(output, source_root) or _is_within(output, canonical_root) or _is_within(output, registry_root):
        raise Step0Error("Step-0 output cannot overlap trades/, canonical/, or registry/")
    if _is_within(output, project_root.resolve()) and not _is_within(
        output, (project_root / DEFAULT_OUTPUT_RELATIVE).resolve()
    ):
        raise Step0Error("repository-local Step-0 output must be under exports/strategy-discovery/step-0")
    if output.exists() and not output.is_dir():
        raise Step0Error(f"Step-0 output path is not a directory: {output}")
    if output.is_dir():
        unexpected = sorted(path.name for path in output.iterdir() if path.name not in ARTIFACT_NAMES)
        if unexpected:
            raise Step0Error(f"unexpected files in Step-0 output directory: {unexpected}")


def _validate_route_arithmetic(results: Sequence[Mapping[str, Any]], routes: Mapping[str, Any]) -> None:
    input_count = len(results)
    status_total = sum(routes["strategy_status"].values())
    outcome_total = sum(routes["gate_outcome"].values())
    eligibility_total = (
        routes["eligible_active_review_pool"]
        + routes["terminal_excluded"]
        + routes["blocked_total"]
        + routes["other_contract_terminal"]
        + routes["unclassified_remainder"]
    )
    if status_total != input_count or outcome_total != input_count or eligibility_total != input_count:
        raise Step0Error("Step-0 route arithmetic has a silent remainder")
    if routes["unclassified_remainder"] != 0:
        raise Step0Error("Step-0 produced an unclassified remainder")


def validate_step0_artifacts(
    project_root: Path,
    output_dir: Path,
    *,
    expected_baseline_sha: str | None = None,
    per_trade_decision_log_paths: Sequence[str] = PER_TRADE_DECISION_LOG_PATHS,
    allow_historical_registry_provenance: bool = False,
) -> dict[str, Any]:
    """Validate a materialized Step-0 output against the current source tree.

    ``allow_historical_registry_provenance`` is reserved for validating an
    immutable, previously committed Step-0 projection after a later governed
    stage has advanced the registry.  It does not relax any row, source, or
    route validation; it only preserves the registry digest recorded by that
    historical projection instead of requiring the current registry digest.
    """

    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    _validate_output_location(project_root, output_dir)
    paths = {name: output_dir / name for name in ARTIFACT_NAMES}
    if any(not path.is_file() for path in paths.values()):
        missing = [name for name, path in paths.items() if not path.is_file()]
        raise Step0Error(f"missing Step-0 artifacts: {missing}")
    try:
        manifest = json.loads(paths["corpus-manifest.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Step0Error("invalid corpus-manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != STEP0_SCHEMA_VERSION:
        raise Step0Error("invalid Step-0 manifest schema")
    manifest_digest = manifest.get("manifest_digest")
    unsigned_manifest = copy.deepcopy(manifest)
    unsigned_manifest.pop("manifest_digest", None)
    if manifest_digest != _sha256(_canonical_bytes(unsigned_manifest)):
        raise Step0Error("manifest_digest does not match the canonical manifest payload")
    if expected_baseline_sha is not None and manifest.get("baseline_main_sha") != expected_baseline_sha:
        raise Step0Error("manifest baseline_main_sha does not match the expected baseline")
    results = _read_jsonl(paths["step-0-results.jsonl"])
    blockers = _read_jsonl(paths["blocker-backlog.jsonl"])
    discovery = __import__(
        "scripts.canonical_migration.full_history",
        fromlist=["discover_sources"],
    ).discover_sources(project_root, "trades")
    current_entries = [
        entry for entry in discovery.get("entries", []) if entry.get("discovery_status") == "eligible"
    ]
    current_paths = sorted(_relative_path(entry["path"]) for entry in current_entries)
    authoritative_input_count = len(current_paths)
    emitted_output_count = len(results)
    corpus = manifest.get("corpus")
    if not isinstance(corpus, dict):
        raise Step0Error("manifest corpus section is missing")
    if (
        corpus.get("input_count") != authoritative_input_count
        or corpus.get("output_count") != emitted_output_count
    ):
        raise Step0Error("manifest input/output count mismatch with selected corpus")
    checks = manifest.get("checks")
    if not isinstance(checks, Mapping):
        raise Step0Error("manifest checks section is missing")
    expected_input_equals_output = _input_equals_output(
        authoritative_input_count,
        emitted_output_count,
    )
    if checks.get("input_equals_output") != expected_input_equals_output:
        raise Step0Error("input_equals_output check does not match independent counts")
    if not expected_input_equals_output:
        raise Step0Error("Step-0 authoritative input and emitted output counts differ")
    result_paths = [result.get("source_path") for result in results]
    if result_paths != sorted(result_paths):
        raise Step0Error("Step-0 results are not in stable source-path order")
    if len(result_paths) != len(set(result_paths)):
        raise Step0Error("Step-0 results contain duplicate source paths/trade_refs")
    if result_paths != current_paths:
        raise Step0Error("Step-0 results do not cover exactly the current selected corpus")
    try:
        current_registry = load_registry(project_root)
    except (OSError, ValueError) as exc:
        raise Step0Error(str(exc)) from exc
    registry_section = manifest.get("registry")
    if not isinstance(registry_section, Mapping) or not isinstance(registry_section.get("digest"), str):
        raise Step0Error("Step-0 manifest registry provenance is missing or invalid")
    if not allow_historical_registry_provenance and registry_section.get("digest") != registry_digest(current_registry):
        raise Step0Error("Step-0 manifest registry digest does not match the current registry")
    current_lookup = build_strategy_lookup(current_registry)
    current_overrides = load_per_trade_strategy_overrides(
        project_root, current_lookup, per_trade_decision_log_paths
    )
    entry_by_path = {_relative_path(entry["path"]): entry for entry in current_entries}
    for result in results:
        source_path = result["source_path"]
        entry = entry_by_path[source_path]
        if result.get("trade_ref") != source_path:
            raise Step0Error(f"trade_ref/source_path mismatch: {source_path}")
        if result.get("source_sha256") != entry.get("sha256") or result.get("source_body_sha256") != entry.get("raw_body_sha256"):
            raise Step0Error(f"source hash mismatch: {source_path}")
        expected_record_id = normalized_record_identifier("tradingdiary", source_path, entry["sha256"])
        if result.get("record_id") != expected_record_id:
            raise Step0Error(f"record_id mismatch: {source_path}")
        outcome = result.get("step0_gate_outcome")
        if outcome not in GATE_OUTCOMES:
            raise Step0Error(f"invalid Step-0 outcome: {source_path}")
        strategy_mapping = result.get("strategy_mapping")
        if not isinstance(strategy_mapping, dict):
            raise Step0Error(f"missing strategy mapping: {source_path}")
        raw_fields = entry.get("raw_fields")
        if not isinstance(raw_fields, Mapping):
            raise Step0Error(f"source discovery entry lacks raw fields: {source_path}")
        override = current_overrides.get(source_path)
        if override is not None:
            expected_match = _match_for_override(override, current_lookup, raw_fields.get("strategy"))
        else:
            expected_match = classify_strategy_label(raw_fields.get("strategy"), current_lookup)
        expected_outcome, expected_eligibility = _route_for_match(expected_match)
        if strategy_mapping != _strategy_mapping_payload(expected_match):
            raise Step0Error(f"strategy mapping is not an exact current-registry result: {source_path}")
        match_type = strategy_mapping.get("match_type")
        if match_type is not None and match_type not in KNOWN_MATCH_TYPES:
            raise Step0Error(f"unexpected strategy match_type in Step-0 result: {source_path}")
        if (match_type == HUMAN_PER_TRADE_MATCH_TYPE) != (override is not None):
            raise Step0Error(f"per-trade override marker mismatch: {source_path}")
        if override is not None and strategy_mapping.get("decision_id") != override["decision_id"]:
            raise Step0Error(f"per-trade override decision_id mismatch: {source_path}")
        if result.get("review_strategy_status") != (
            expected_match["strategy"].get("status")
            if expected_match.get("matched") and isinstance(expected_match.get("strategy"), Mapping)
            else None
        ):
            raise Step0Error(f"strategy status mirror mismatch: {source_path}")
        if result.get("step0_gate_outcome") != expected_outcome or result.get("review_eligibility") != expected_eligibility:
            raise Step0Error(f"Step-0 route mismatch: {source_path}")
        if any(key in result for key in FORBIDDEN_IDENTITY_FIELDS):
            raise Step0Error(f"forbidden identity field in Step-0 result: {source_path}")
        if any(key.startswith("plan_") for key in result):
            raise Step0Error(f"Step-0 result contains plan fields: {source_path}")
        if outcome in {"RETIRED", "EXPERIMENTAL"}:
            review_fields = {key for key in result if key.startswith("review_")}
            if not review_fields.issubset(ALLOWED_TERMINAL_REVIEW_FIELDS | {"review_eligibility"}):
                raise Step0Error(f"terminal Step-0 row contains non-minimal review fields: {source_path}")
            if result.get("review_logic_status") != "excluded" or result.get("review_feature_status") != "n/a":
                raise Step0Error(f"terminal Step-0 row has invalid minimal fields: {source_path}")
            if result.get("review_recommend_disposition") != "keep-and-mark":
                raise Step0Error(f"terminal Step-0 row has invalid disposition: {source_path}")
        elif any(key.startswith("review_") for key in result if key not in {"review_strategy_status", "review_eligibility"}):
            raise Step0Error(f"active/blocked Step-0 row contains human review fields: {source_path}")
    applied_override_refs = {
        result["trade_ref"]
        for result in results
        if result["strategy_mapping"].get("match_type") == HUMAN_PER_TRADE_MATCH_TYPE
    }
    if applied_override_refs != set(current_overrides):
        raise Step0Error("Step-0 per-trade override coverage does not match the consumption manifest")
    per_trade_section = manifest.get("per_trade_consumption")
    if not isinstance(per_trade_section, Mapping):
        raise Step0Error("manifest per_trade_consumption section is missing")
    if per_trade_section.get("consumed_mechanism") != PER_TRADE_CONSUMED_MECHANISM:
        raise Step0Error("manifest per_trade_consumption mechanism mismatch")
    if per_trade_section.get("applied_override_count") != len(applied_override_refs):
        raise Step0Error("manifest per_trade_consumption applied count mismatch")
    if per_trade_section.get("override_index_count") != len(current_overrides):
        raise Step0Error("manifest per_trade_consumption override index count mismatch")
    if sorted(per_trade_section.get("hard_excluded_dispositions") or []) != sorted(
        PER_TRADE_HARD_EXCLUDED_DISPOSITIONS
    ):
        raise Step0Error("manifest per_trade_consumption hard-exclusion set mismatch")
    expected_routes = _route_counts(results)
    if manifest.get("routes") != expected_routes:
        raise Step0Error("manifest route counts do not match Step-0 results")
    _validate_route_arithmetic(results, expected_routes)
    blocked_refs = {row.get("trade_ref") for row in blockers}
    expected_blocked_refs = {
        result["trade_ref"] for result in results if result["step0_gate_outcome"] in BLOCKED_OUTCOMES
    }
    if blocked_refs != expected_blocked_refs or len(blockers) != len(blocked_refs):
        raise Step0Error("blocker backlog does not match blocked Step-0 results")
    if corpus.get("unique_selected_trade_ref_count") != emitted_output_count:
        raise Step0Error("manifest unique trade_ref count mismatch")
    if (
        corpus.get("duplicate_trade_ref_count") != 0
        or corpus.get("duplicate_source_path_count") != 0
        or corpus.get("missing_trade_ref_count") != 0
    ):
        raise Step0Error("manifest reports duplicate or missing trade refs")
    historically_deleted_paths = corpus.get("historically_deleted_paths")
    resurrected_paths = corpus.get("resurrected_paths")
    if not isinstance(historically_deleted_paths, list) or not isinstance(resurrected_paths, list):
        raise Step0Error("manifest historical deletion/resurrection paths are missing")
    if any(not isinstance(path, str) for path in historically_deleted_paths + resurrected_paths):
        raise Step0Error("manifest historical deletion/resurrection paths are invalid")
    expected_resurrected_paths = sorted(set(historically_deleted_paths) & set(current_paths))
    if resurrected_paths != expected_resurrected_paths:
        raise Step0Error("manifest resurrection paths do not match the historical deletion intersection")
    if corpus.get("deleted_source_resurrection_count") != len(resurrected_paths):
        raise Step0Error("manifest deleted-source resurrection count mismatch")
    if checks.get("deleted_source_resurrected") != bool(resurrected_paths):
        raise Step0Error("deleted_source_resurrected check does not match resurrection paths")
    if "deleted_source_resurrection" in checks:
        raise Step0Error("deprecated deleted_source_resurrection check is not allowed")
    if resurrected_paths:
        raise Step0Error("a historically deleted source is present in the current corpus")
    results_bytes = paths["step-0-results.jsonl"].read_bytes()
    blockers_bytes = paths["blocker-backlog.jsonl"].read_bytes()
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise Step0Error("manifest outputs section is missing")
    if outputs.get("results", {}).get("sha256") != _sha256(results_bytes):
        raise Step0Error("results artifact digest mismatch")
    if outputs.get("blockers", {}).get("sha256") != _sha256(blockers_bytes):
        raise Step0Error("blocker artifact digest mismatch")
    return manifest


def run_step0(
    project_root: Path,
    *,
    baseline_sha: str,
    historical_reference_commit: str,
    output_dir: Path | None = None,
    historical_reference_paths: Iterable[str] | None = None,
    identity_log_paths: Sequence[str] = IDENTITY_LOG_PATHS,
    per_trade_decision_log_paths: Sequence[str] = PER_TRADE_DECISION_LOG_PATHS,
    verify_git: bool = True,
) -> dict[str, Any]:
    """Run the full deterministic Step-0 derivation and write four artifacts."""

    project_root = project_root.resolve()
    output_dir = (project_root / DEFAULT_OUTPUT_RELATIVE if output_dir is None else output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    output_dir = output_dir.resolve()
    _validate_output_location(project_root, output_dir)

    if verify_git:
        baseline_sha = _resolve_commit(project_root, baseline_sha)
        historical_reference_commit = _resolve_commit(project_root, historical_reference_commit)
        _assert_worktree_sources_match_baseline(project_root, baseline_sha)
        historical_paths = _git_source_paths(project_root, historical_reference_commit)
        baseline_paths = _git_source_paths(project_root, baseline_sha)
    else:
        historical_paths = sorted(
            _relative_path(path) for path in (historical_reference_paths or []) if _is_source_candidate(_relative_path(path))
        )
        if len(historical_paths) != len(set(historical_paths)):
            raise Step0Error("duplicate historical reference paths")
        baseline_paths = None

    from scripts.canonical_migration.full_history import discover_sources

    discovery = discover_sources(project_root, "trades")
    current_entries = [
        entry for entry in discovery.get("entries", []) if entry.get("discovery_status") == "eligible"
    ]
    current_entries.sort(key=lambda entry: _relative_path(entry["path"]))
    fresh_paths = [_relative_path(entry["path"]) for entry in current_entries]
    if len(fresh_paths) != len(set(fresh_paths)):
        raise Step0Error("current selected corpus contains duplicate source paths")
    if verify_git and fresh_paths != baseline_paths:
        raise Step0Error("current selected corpus differs from the fresh baseline source tree")
    try:
        registry = load_registry(project_root)
    except (OSError, ValueError) as exc:
        raise Step0Error(str(exc)) from exc
    lookup = build_strategy_lookup(registry)
    identity_refs = load_identity_status_refs(project_root, identity_log_paths)
    per_trade_overrides = load_per_trade_strategy_overrides(
        project_root, lookup, per_trade_decision_log_paths
    )
    results = [
        _result_for_source(entry, lookup, identity_refs, per_trade_overrides)
        for entry in current_entries
    ]
    if [result["trade_ref"] for result in results] != fresh_paths:
        raise Step0Error("Step-0 result order is not the selected source order")
    applied_override_refs = {
        result["trade_ref"]
        for result in results
        if result["strategy_mapping"].get("match_type") == HUMAN_PER_TRADE_MATCH_TYPE
    }
    if applied_override_refs != set(per_trade_overrides):
        missing = sorted(set(per_trade_overrides) - applied_override_refs)
        raise Step0Error(
            f"per-trade overrides reference trades absent from the selected corpus: {missing}"
        )
    blockers = [
        _blocker_for_result(result)
        for result in results
        if result["step0_gate_outcome"] in BLOCKED_OUTCOMES
    ]
    routes = _route_counts(results)
    _validate_route_arithmetic(results, routes)
    source_list_digest = _sha256(_canonical_bytes(fresh_paths))
    ordered_corpus_digest = _sha256(_canonical_bytes(_ordered_corpus_material(results)))
    historical_only = sorted(set(historical_paths) - set(fresh_paths))
    fresh_only = sorted(set(fresh_paths) - set(historical_paths))
    historically_deleted_paths, resurrected_paths = _calculate_deleted_source_resurrection(
        historical_paths,
        baseline_paths,
        fresh_paths,
    )
    baseline_only = sorted(set(baseline_paths) - set(fresh_paths)) if baseline_paths is not None else []
    baseline_fresh_only = sorted(set(fresh_paths) - set(baseline_paths)) if baseline_paths is not None else []
    if verify_git and (baseline_only or baseline_fresh_only):
        raise Step0Error("fresh source tree changed while deriving Step-0")
    authoritative_input_count = len(fresh_paths)
    emitted_output_count = len(results)

    manifest: dict[str, Any] = {
        "schema_version": STEP0_SCHEMA_VERSION,
        "mode": "historical_review_campaign_step0",
        "read_only": True,
        "baseline_main_sha": baseline_sha,
        "historical_campaign_label": HISTORICAL_CAMPAIGN_LABEL,
        "historical_reference_commit": historical_reference_commit,
        "corpus_selector": {
            "authority": CORPUS_SELECTOR_AUTHORITY,
            "version": CORPUS_SELECTOR_VERSION,
            "legacy_root": "trades",
            "include": "all repository-relative Markdown files recursively",
            "exclude_leaf_names": ["_index.md", "readme.md"],
            "ordering": "repository-relative POSIX path lexicographic",
        },
        "corpus": {
            "input_count": authoritative_input_count,
            "output_count": emitted_output_count,
            "unique_selected_trade_ref_count": len({result["trade_ref"] for result in results}),
            "source_list_digest": source_list_digest,
            "ordered_corpus_digest": ordered_corpus_digest,
            "duplicate_trade_ref_count": len(results) - len({result["trade_ref"] for result in results}),
            "duplicate_source_path_count": len(fresh_paths) - len(set(fresh_paths)),
            "missing_trade_ref_count": 0,
            "deleted_source_resurrection_count": len(resurrected_paths),
            "historical_reference_count": len(historical_paths),
            "historical_only_paths": historical_only,
            "fresh_only_paths": fresh_only,
            "historically_deleted_paths": historically_deleted_paths,
            "resurrected_paths": resurrected_paths,
            "baseline_only_paths": baseline_only,
            "baseline_fresh_only_paths": baseline_fresh_only,
        },
        "corpus_fingerprint": {
            "baseline_main_sha": baseline_sha,
            "selection_rule": CORPUS_SELECTOR_VERSION,
            "source_list_digest": source_list_digest,
            "ordered_corpus_digest": ordered_corpus_digest,
            "input_count": authoritative_input_count,
        },
        "registry": {
            "path": REGISTRY_PATH,
            "registry_version": registry.get("registry_version"),
            "digest": registry_digest(registry),
            "strategy_count": len(registry.get("strategies", [])),
        },
        "routes": routes,
        "identity": {
            "resolved_by_existing_durable_row": sum(result["identity_status"] == "resolved" for result in results),
            "not_materialized": sum(result["identity_status"] is None for result in results),
            "logs_read": list(identity_log_paths),
            "authority_written": False,
        },
        "per_trade_consumption": {
            "logs_read": list(per_trade_decision_log_paths),
            "consumed_mechanism": PER_TRADE_CONSUMED_MECHANISM,
            "hard_excluded_dispositions": sorted(PER_TRADE_HARD_EXCLUDED_DISPOSITIONS),
            "override_index_count": len(per_trade_overrides),
            "applied_override_count": len(applied_override_refs),
            "authority": (
                "canonical/adjudications/strategy-mapping-review/"
                "strategy-mapping-per-trade-application-20260910.jsonl"
            ),
            "input_authority_note": (
                "PR #27 merged the owner per-trade decisions and the reconciliation "
                "manifest. This resolver consumes the recorded_pending_resolver_consumption "
                "routes by trade_ref key (precedence over registry label matching). The "
                "applied_via_registry_alias:ob routes remain the registry alias's "
                "responsibility; the keep_blocked / historical-cleaning / "
                "position-management rows stay BLOCKED_STRATEGY_MAPPING."
            ),
            "authority_written": False,
        },
        "outputs": {
            "results": {"path": "step-0-results.jsonl", "record_count": emitted_output_count},
            "manifest": {"path": "corpus-manifest.json"},
            "report": {"path": "step-0-report.md"},
            "blockers": {"path": "blocker-backlog.jsonl", "record_count": len(blockers)},
        },
        "checks": {
            "input_equals_output": _input_equals_output(authoritative_input_count, emitted_output_count),
            "trade_ref_unique": len(results) == len({result["trade_ref"] for result in results}),
            "source_hashes_present": all(result["source_sha256"] and result["source_body_sha256"] for result in results),
            "deleted_source_resurrected": bool(resurrected_paths),
            "human_fields_fabricated": False,
            "charts_reviewed": False,
            "network_or_provider_lookup": False,
            "trades_modified": False,
            "identity_authority_modified": False,
            "per_trade_authority_modified": False,
            "per_trade_overrides_all_consumed": applied_override_refs == set(per_trade_overrides),
            "per_trade_match_type_is_keyed_not_inferred": True,
            "route_arithmetic_ok": routes["unclassified_remainder"] == 0,
        },
    }
    report = _report(
        baseline_sha=baseline_sha,
        historical_reference_commit=historical_reference_commit,
        historical_paths=historical_paths,
        fresh_paths=fresh_paths,
        authoritative_input_count=authoritative_input_count,
        emitted_output_count=emitted_output_count,
        historically_deleted_paths=historically_deleted_paths,
        resurrected_paths=resurrected_paths,
        results=results,
        manifest_fields=manifest,
    )
    _write_jsonl(output_dir / "step-0-results.jsonl", results)
    _write_jsonl(output_dir / "blocker-backlog.jsonl", blockers)
    (output_dir / "step-0-report.md").parent.mkdir(parents=True, exist_ok=True)
    (output_dir / "step-0-report.md").write_text(report, encoding="utf-8", newline="\n")
    manifest["outputs"]["results"]["sha256"] = _sha256((output_dir / "step-0-results.jsonl").read_bytes())
    manifest["outputs"]["blockers"]["sha256"] = _sha256((output_dir / "blocker-backlog.jsonl").read_bytes())
    manifest["outputs"]["report"]["sha256"] = _sha256((output_dir / "step-0-report.md").read_bytes())
    manifest["manifest_digest"] = _sha256(_canonical_bytes(manifest))
    _write_json(output_dir / "corpus-manifest.json", manifest)
    validate_step0_artifacts(
        project_root,
        output_dir,
        expected_baseline_sha=baseline_sha,
        per_trade_decision_log_paths=per_trade_decision_log_paths,
    )
    return manifest
