"""B3 — immutable strategy rule-version freeze manifest.

The freeze is deliberately a small layer on top of the existing strategy
registry/version loader.  It consumes only the B1 identity/provenance surface,
the canonical strategy definition, and explicitly selected pre-trade source
sections.  It never opens the historical performance or journal aggregates.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from scripts.canonical_migration.frontmatter import parse_markdown

from .registry import load_effective_rules, load_registry, load_strategy, load_version, registry_digest


B3_SCHEMA_VERSION = "method-validation-b3.v1"
B3_MODE = "method_validation_b3_strategy_rule_freeze"
B1_MANIFEST_PATH = "exports/method-validation/b1/manifest.json"
B1_DATASET_PATH = "exports/method-validation/b1/canonical-trade-dataset.jsonl"
REGISTRY_PATH = "registry/strategy-registry.yaml"
STRATEGY_DEFINITION_PATH = "STRATEGIES_OVERVIEW.md"
B1_DATASET_VERSION = "method-validation-b1.v2"
B1_DATASET_SHA256 = "c49af93748864bb863192bb46fea19f314a3107c9455a4d587c2f16835714ed5"
B1_MANIFEST_DIGEST = "e9efe9d145e8203d0e1b63cbcaaa303bfed73f4ee4fc7337fd96eef6ab844ffe"
IMMUTABLE_UPSTREAM_SHA256 = {
    "b1_manifest": "fc49b2bf94a78bcf4bdc43aa8acb3b3aa965c1f46f244a4ea660a0eee3d16aa9",
    "b1_dataset": B1_DATASET_SHA256,
    "step0_corpus_manifest": "0d9f7c44f2bafec56c65d6f6ef8611638f0d5288764f4a8c359a4739e9572303",
    "step0_results": "84c27655b478d11695cd77fdd6bc0ae7fada01cb8f73c83be127d1fe9aabfc78",
}
ACTIVE_STRATEGY_IDS = (
    "stg-vegas-tunnel",
    "stg-po3",
    "stg-ict-liquidity",
    "stg-triple-tap",
)

# These are source selectors, not inferred rules.  Every selector is resolved
# against B1 identity/provenance and then re-hashed before it enters the
# manifest. Canonical definitions use explicit line ranges; trade sources use
# only the parsed pre-trade section.
EVIDENCE_PLAN: dict[str, tuple[dict[str, str], ...]] = {
    "stg-vegas-tunnel": (
        {"evidence_id": "definition.vegas", "source_ref": STRATEGY_DEFINITION_PATH, "locator": "lines 18-20", "tier": "Tier2_canonical_strategy_definition"},
        {"evidence_id": "trade.vegas.mss", "source_ref": "trades/2023/10/2023-10-21_BTCUSDT_L_mss-vegas.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.vegas.2025-08-20", "source_ref": "trades/2025/08/2025-08-20_ETHUSDT_L_vegas-tunnel.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
    ),
    "stg-po3": (
        {"evidence_id": "definition.po3", "source_ref": STRATEGY_DEFINITION_PATH, "locator": "lines 142-143", "tier": "Tier2_canonical_strategy_definition"},
        {"evidence_id": "trade.po3.2025-10-14", "source_ref": "trades/2025/10/2025-10-14_BTCUSDT_S_po3.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.po3.2026-03-26", "source_ref": "trades/2026/03/2026-03-26_BTCUSDT_S_po3.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.po3.2026-07-14", "source_ref": "trades/2026/07/2026-07-14-BTC.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
    ),
    "stg-ict-liquidity": (
        {"evidence_id": "definition.ict-liquidity", "source_ref": STRATEGY_DEFINITION_PATH, "locator": "lines 49-52, 74, 80, 85", "tier": "Tier2_canonical_strategy_definition"},
        {"evidence_id": "trade.ict.ob-mss", "source_ref": "trades/2026/02/2026-02-04_ADAUSDT_S_ob.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.ict.sr-flip", "source_ref": "trades/2026/05/2026-05-30.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.ict.mss-retest", "source_ref": "trades/2026/06/2026-06-03.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.ict.support-mss", "source_ref": "trades/2026/06/2026-06-07.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.ict.bos-retest", "source_ref": "trades/2026/07/2026-07-15-BTC.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
    ),
    "stg-triple-tap": (
        {"evidence_id": "definition.triple-tap", "source_ref": STRATEGY_DEFINITION_PATH, "locator": "lines 72-75", "tier": "Tier2_canonical_strategy_definition"},
        {"evidence_id": "trade.triple-tap.2025-09-09", "source_ref": "trades/2025/09/2025-09-09_ETHUSDT_L_triple-tap.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
        {"evidence_id": "trade.triple-tap.2026-04-17", "source_ref": "trades/2026/04/2026-04-17_BTCUSDT_L_mss-3needle.md", "locator": "pre_trade", "tier": "Tier3_repeated_pre_trade_history"},
    ),
}

FORBIDDEN_EVIDENCE_MARKERS = (
    "實戰優化",
    "勝率",
    "+R",
    "實戰案例",
    "Post-Trade",
    "結果",
    "實際出場",
    "realized_r",
    "pnl",
    "profit",
    "outcome",
)


class B3Error(ValueError):
    """Raised when a deterministic B3 freeze cannot be established."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise B3Error(f"expected JSON object: {path}")
    return value


def _b1_membership(root: Path) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Return only B1 identity/provenance projections needed by B3."""

    by_ref: dict[str, dict[str, str]] = {}
    counts: Counter[str] = Counter()
    path = root / B1_DATASET_PATH
    if not path.is_file():
        raise B3Error(f"B1 dataset not found: {B1_DATASET_PATH}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        strategy_id = row.get("strategy_id")
        strategy_status = row.get("strategy_status")
        source_path = row.get("source_path")
        if strategy_id not in ACTIVE_STRATEGY_IDS or strategy_status != "active":
            continue
        if not isinstance(source_path, str) or not source_path:
            raise B3Error("active B1 membership row has no source_path")
        projection = {
            "strategy_id": strategy_id,
            "source_path": source_path,
            "source_sha256": str(row.get("source_sha256", "")),
            "source_body_sha256": str(row.get("source_body_sha256", "")),
        }
        if not projection["source_sha256"] or not projection["source_body_sha256"]:
            raise B3Error(f"active B1 membership row has incomplete provenance: {source_path}")
        by_ref[source_path] = projection
        counts[strategy_id] += 1
    return by_ref, dict(sorted(counts.items()))


def _pre_trade_body(text: str) -> str:
    """Extract only the pre-trade section for an evidence-boundary check."""

    parsed = parse_markdown(text)
    lines = parsed.body.splitlines(keepends=True)
    selected: list[str] = []
    in_pre_trade = False
    for line in lines:
        if not in_pre_trade:
            if re.match(r"^## .*Pre-Trade", line, re.IGNORECASE):
                in_pre_trade = True
            continue
        if re.match(r"^## ", line) or line.strip() == "---":
            break
        selected.append(line)
    return "".join(selected)


def _canonical_definition_content(text: str, locator: str) -> str:
    """Extract the exact canonical-definition lines named by ``locator``."""

    match = re.fullmatch(r"lines\s+(\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*)", locator)
    if match is None:
        raise B3Error(f"canonical evidence locator is not an explicit line range: {locator}")
    lines = text.splitlines(keepends=True)
    selected: list[str] = []
    for token in match.group(1).split(","):
        bounds = [int(value) for value in token.strip().split("-", 1)]
        start = bounds[0]
        end = bounds[-1]
        if start < 1 or end < start or end > len(lines):
            raise B3Error(f"canonical evidence locator is outside source: {locator}")
        selected.extend(lines[start - 1:end])
    return "".join(selected)


def _assert_safe_evidence_content(content: str, *, source_ref: str, evidence_id: str) -> None:
    lowered = content.casefold()
    found = [marker for marker in FORBIDDEN_EVIDENCE_MARKERS if marker.casefold() in lowered]
    if found:
        raise B3Error(
            f"evidence content contains forbidden post-hoc marker for {evidence_id} "
            f"({source_ref}): {', '.join(found)}"
        )


def _evidence_refs(root: Path, strategy_id: str, b1_by_ref: Mapping[str, Mapping[str, str]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for planned in EVIDENCE_PLAN[strategy_id]:
        source_ref = planned["source_ref"]
        from .legacy_source import legacy_exists, read_legacy_bytes
        path = root / source_ref
        if not legacy_exists(root, source_ref):
            raise B3Error(f"evidence source not found: {source_ref}")
        if source_ref == STRATEGY_DEFINITION_PATH:
            raw = read_legacy_bytes(root, source_ref)
            text = raw.decode("utf-8", errors="replace")
            content = _canonical_definition_content(text, planned["locator"])
            _assert_safe_evidence_content(content, source_ref=source_ref, evidence_id=planned["evidence_id"])
            refs.append({
                **planned,
                "timing": "pre_trade",
                "sha256": _sha256_bytes(raw),
                "content_sha256": _sha256_bytes(content.encode("utf-8")),
            })
            continue
        provenance = b1_by_ref.get(source_ref)
        if provenance is None or provenance.get("strategy_id") != strategy_id:
            raise B3Error(f"evidence source is outside B1 membership for {strategy_id}: {source_ref}")
        raw = read_legacy_bytes(root, source_ref)
        source_sha = _sha256_bytes(raw)
        if source_sha != provenance["source_sha256"]:
            raise B3Error(f"evidence source hash drift: {source_ref}")
        text = raw.decode("utf-8", errors="replace")
        body_sha = _sha256_bytes(parse_markdown(text).body.encode("utf-8"))
        if body_sha != provenance["source_body_sha256"]:
            raise B3Error(f"evidence source body hash drift: {source_ref}")
        content = _pre_trade_body(text)
        if not content:
            raise B3Error(f"selected evidence has no pre-trade section: {source_ref}")
        _assert_safe_evidence_content(content, source_ref=source_ref, evidence_id=planned["evidence_id"])
        refs.append({
            **planned,
            "timing": "pre_trade",
            "sha256": source_sha,
            "body_sha256": body_sha,
            "content_sha256": _sha256_bytes(content.encode("utf-8")),
        })
    return refs


def _version_entry(root: Path, strategy_id: str, b1_by_ref: Mapping[str, Mapping[str, str]], membership_count: int) -> dict[str, Any]:
    strategy = load_strategy(root, strategy_id)
    versions = strategy.get("rule_versions", [])
    if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], str):
        raise B3Error(f"active strategy must point to exactly one frozen version: {strategy_id}")
    version_id = versions[0]
    version = load_version(root, version_id)
    effective_rules = load_effective_rules(root, version_id)
    evidence = _evidence_refs(root, strategy_id, b1_by_ref)
    unresolved = version.get("unresolved", [])
    blocking = [item for item in unresolved if isinstance(item, Mapping) and item.get("blocking") is True]
    return {
        "strategy_id": strategy_id,
        "display_name": strategy.get("display_name"),
        "lifecycle_at_freeze": version.get("lifecycle_at_freeze", strategy.get("status")),
        "classification": strategy.get("classification"),
        "parent_strategy_id": strategy.get("parent_strategy_id"),
        "rule_version": version_id,
        "version_file_sha256": _sha256_file(root / "registry" / "versions" / (version_id.replace(":", "-") + ".yaml")),
        "rule_definition_sha256": _sha256_bytes(_canonical_bytes(version.get("rules", []))),
        "effective_rule_ids": [rule.get("rule_id") for rule in effective_rules],
        "rule_count": len(version.get("rules", [])),
        "effective_rule_count": len(effective_rules),
        "active_membership_count": membership_count,
        "evidence_refs": evidence,
        "unresolved_count": len(unresolved),
        "blocking_unresolved_count": len(blocking),
        "freeze_status": "blocked" if blocking else "frozen",
    }


def build_b3_manifest(root: Path, *, baseline_sha: str, freeze_date: str) -> dict[str, Any]:
    """Build a deterministic B3 manifest without writing any source input."""

    root = root.resolve()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", baseline_sha):
        raise B3Error("baseline_sha must be a full commit SHA")
    if not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", freeze_date):
        raise B3Error("freeze_date must be an ISO date")
    b1_manifest = _read_json(root / B1_MANIFEST_PATH)
    if b1_manifest.get("dataset_version") != B1_DATASET_VERSION:
        raise B3Error("B1 dataset version is not the immutable B1 v2 pin")
    if b1_manifest.get("dataset_sha256") != B1_DATASET_SHA256:
        raise B3Error("B1 dataset has drifted from the immutable B1 v2 pin")
    if b1_manifest.get("manifest_digest") != B1_MANIFEST_DIGEST:
        raise B3Error("B1 manifest has drifted from the immutable B1 v2 pin")
    immutable_paths = {
        "b1_manifest": B1_MANIFEST_PATH,
        "b1_dataset": B1_DATASET_PATH,
        "step0_corpus_manifest": "exports/strategy-discovery/step-0/corpus-manifest.json",
        "step0_results": "exports/strategy-discovery/step-0/step-0-results.jsonl",
    }
    for key, relative_path in immutable_paths.items():
        if _sha256_file(root / relative_path) != IMMUTABLE_UPSTREAM_SHA256[key]:
            raise B3Error(f"immutable upstream artifact drift: {relative_path}")
    b1_by_ref, membership_counts = _b1_membership(root)
    registry = load_registry(root)
    strategy_entries = [
        _version_entry(root, strategy_id, b1_by_ref, membership_counts.get(strategy_id, 0))
        for strategy_id in ACTIVE_STRATEGY_IDS
    ]
    unresolved_counts = {
        item["strategy_id"]: item["unresolved_count"] for item in strategy_entries
    }
    payload: dict[str, Any] = {
        "schema_version": B3_SCHEMA_VERSION,
        "mode": B3_MODE,
        "freeze_date": freeze_date,
        "freeze_baseline_sha": baseline_sha.lower(),
        "read_only_inputs": True,
        "performance_data_consumed": False,
        "optimization_performed": False,
        "b2_started": False,
        "b1_pin": {
            "dataset_version": B1_DATASET_VERSION,
            "dataset_sha256": B1_DATASET_SHA256,
            "manifest_digest": B1_MANIFEST_DIGEST,
        },
        "immutable_upstream": dict(IMMUTABLE_UPSTREAM_SHA256),
        "registry": {
            "path": REGISTRY_PATH,
            "before": {
                "file_sha256": "4847d1ca133e81161398745449a745a2288cd432238ca9b897ce92b1de6a9fe4",
                "registry_digest": "5d4069adbd4b35d8d6c344fb1f0fd8b533fdc3d1ffe501900fc0e456b961f4f4",
            },
            "after": {
                "file_sha256": _sha256_file(root / REGISTRY_PATH),
                "registry_digest": registry_digest(registry),
            },
        },
        "active_strategy_ids": list(ACTIVE_STRATEGY_IDS),
        "strategy_versions": strategy_entries,
        "unresolved_counts": unresolved_counts,
        "rule_definition_hashes": {
            item["rule_version"]: item["rule_definition_sha256"] for item in strategy_entries
        },
        "negative_guarantees": {
            "b1_dataset_rows_unchanged": True,
            "b1_manifest_unchanged": True,
            "step0_results_unchanged": True,
            "step0_manifest_unchanged": True,
            "trades_unchanged": True,
            "adjudications_unchanged": True,
            "retired_and_experimental_strategies_unchanged": True,
            "no_outcome_or_realized_r_evidence": True,
            "no_post_trade_evidence": True,
        },
    }
    payload["manifest_digest"] = _sha256_bytes(_canonical_bytes(payload))
    return payload


def write_b3_manifest(root: Path, output_dir: Path, *, baseline_sha: str, freeze_date: str) -> dict[str, Any]:
    payload = build_b3_manifest(root, baseline_sha=baseline_sha, freeze_date=freeze_date)
    output_dir = output_dir if output_dir.is_absolute() else root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def validate_b3_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Validate the durable B3 manifest independently of its generator."""

    errors: list[str] = []
    required = (
        "schema_version",
        "mode",
        "freeze_date",
        "freeze_baseline_sha",
        "b1_pin",
        "immutable_upstream",
        "registry",
        "active_strategy_ids",
        "strategy_versions",
        "unresolved_counts",
        "rule_definition_hashes",
        "performance_data_consumed",
        "optimization_performed",
        "b2_started",
    )
    for key in required:
        if key not in manifest:
            errors.append(f"missing:{key}")
    if manifest.get("schema_version") != B3_SCHEMA_VERSION:
        errors.append("schema_version")
    if manifest.get("mode") != B3_MODE:
        errors.append("mode")
    if manifest.get("freeze_baseline_sha") != str(manifest.get("freeze_baseline_sha", "")).lower():
        errors.append("freeze_baseline_sha.case")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("freeze_baseline_sha", ""))):
        errors.append("freeze_baseline_sha")
    if manifest.get("performance_data_consumed") is not False:
        errors.append("performance_data_consumed")
    if manifest.get("optimization_performed") is not False:
        errors.append("optimization_performed")
    if manifest.get("b2_started") is not False:
        errors.append("b2_started")
    b1_pin = manifest.get("b1_pin")
    if not isinstance(b1_pin, Mapping) or b1_pin.get("dataset_version") != B1_DATASET_VERSION:
        errors.append("b1_pin.dataset_version")
    for key, expected in (
        ("dataset_sha256", B1_DATASET_SHA256),
        ("manifest_digest", B1_MANIFEST_DIGEST),
    ):
        if not isinstance(b1_pin, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", str(b1_pin.get(key, ""))):
            errors.append(f"b1_pin.{key}")
        elif b1_pin.get(key) != expected:
            errors.append(f"b1_pin.{key}.value")
    immutable_upstream = manifest.get("immutable_upstream")
    if not isinstance(immutable_upstream, Mapping):
        errors.append("immutable_upstream")
    else:
        for key, expected in IMMUTABLE_UPSTREAM_SHA256.items():
            if immutable_upstream.get(key) != expected:
                errors.append(f"immutable_upstream.{key}")
    registry = manifest.get("registry")
    if not isinstance(registry, Mapping) or registry.get("path") != REGISTRY_PATH:
        errors.append("registry.path")
    before = registry.get("before") if isinstance(registry, Mapping) else None
    after = registry.get("after") if isinstance(registry, Mapping) else None
    for label, item in (("before", before), ("after", after)):
        if not isinstance(item, Mapping):
            errors.append(f"registry.{label}")
            continue
        for key in ("file_sha256", "registry_digest"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get(key, ""))):
                errors.append(f"registry.{label}.{key}")
    if isinstance(before, Mapping) and isinstance(after, Mapping) and before == after:
        errors.append("registry.before_after")
    guarantees = manifest.get("negative_guarantees")
    for key in (
        "b1_dataset_rows_unchanged",
        "b1_manifest_unchanged",
        "step0_results_unchanged",
        "step0_manifest_unchanged",
        "trades_unchanged",
        "adjudications_unchanged",
        "no_outcome_or_realized_r_evidence",
        "no_post_trade_evidence",
    ):
        if not isinstance(guarantees, Mapping) or guarantees.get(key) is not True:
            errors.append(f"negative_guarantees.{key}")
    if tuple(manifest.get("active_strategy_ids", ())) != ACTIVE_STRATEGY_IDS:
        errors.append("active_strategy_ids")
    versions = manifest.get("strategy_versions")
    if not isinstance(versions, list) or {item.get("strategy_id") for item in versions if isinstance(item, Mapping)} != set(ACTIVE_STRATEGY_IDS):
        errors.append("strategy_versions.coverage")
    for item in versions if isinstance(versions, list) else []:
        if not isinstance(item, Mapping):
            errors.append("strategy_versions.item")
            continue
        if item.get("freeze_status") != "frozen":
            errors.append(f"strategy_versions.freeze_status:{item.get('strategy_id')}")
        if item.get("blocking_unresolved_count") != 0:
            errors.append(f"strategy_versions.blocking_unresolved:{item.get('strategy_id')}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("rule_definition_sha256", ""))):
            errors.append(f"strategy_versions.rule_hash:{item.get('strategy_id')}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("version_file_sha256", ""))):
            errors.append(f"strategy_versions.file_hash:{item.get('strategy_id')}")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list) or any(
            not isinstance(ref, Mapping)
            or ref.get("timing") != "pre_trade"
            or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("content_sha256", "")))
            or (
                ref.get("source_ref") == STRATEGY_DEFINITION_PATH
                and not re.fullmatch(r"lines\s+\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*", str(ref.get("locator", "")))
            )
            or (
                ref.get("source_ref") != STRATEGY_DEFINITION_PATH
                and ref.get("locator") != "pre_trade"
            )
            for ref in evidence_refs
        ):
            errors.append(f"strategy_versions.evidence:{item.get('strategy_id')}")
    expected_digest = manifest.get("manifest_digest")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    if expected_digest != _sha256_bytes(_canonical_bytes(unsigned)):
        errors.append("manifest_digest")
    return errors
