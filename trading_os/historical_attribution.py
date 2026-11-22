"""Governed contracts used by retrospective strategy attribution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml


REGISTRY_VERSION = "retrospective-attribution-registry.v1"


class HistoricalAttributionRegistryError(ValueError):
    """Raised when retrospective expression/context authority is malformed."""


def load_historical_attribution_registry(root: Path) -> dict[str, Any]:
    path = Path(root).resolve() / "registry" / "retrospective-attribution.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HistoricalAttributionRegistryError(
            f"cannot load retrospective attribution registry: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("registry_version") != REGISTRY_VERSION:
        raise HistoricalAttributionRegistryError(
            f"retrospective attribution registry must use {REGISTRY_VERSION}"
        )

    expressions = payload.get("entry_expressions")
    if not isinstance(expressions, list):
        raise HistoricalAttributionRegistryError("entry_expressions must be a list")
    seen_expressions: set[tuple[str, str, str]] = set()
    for index, item in enumerate(expressions):
        if not isinstance(item, Mapping):
            raise HistoricalAttributionRegistryError(f"entry_expressions[{index}] must be an object")
        strategy_id = item.get("strategy_id")
        rule_version = item.get("rule_version")
        expression_id = item.get("expression_id")
        required = item.get("required_components")
        if not all(isinstance(value, str) and value.strip() for value in (strategy_id, rule_version, expression_id)):
            raise HistoricalAttributionRegistryError(
                f"entry_expressions[{index}] requires strategy_id, rule_version, and expression_id"
            )
        if not isinstance(required, list) or not required or any(
            not isinstance(value, str) or not value.strip() for value in required
        ) or len(set(required)) != len(required):
            raise HistoricalAttributionRegistryError(
                f"entry_expressions[{index}].required_components must be unique non-empty strings"
            )
        key = (strategy_id, rule_version, expression_id)
        if key in seen_expressions:
            raise HistoricalAttributionRegistryError(f"duplicate entry expression contract: {key}")
        seen_expressions.add(key)

    contexts = payload.get("confluence_contexts")
    if not isinstance(contexts, list):
        raise HistoricalAttributionRegistryError("confluence_contexts must be a list")
    seen_contexts: set[str] = set()
    for index, item in enumerate(contexts):
        if not isinstance(item, Mapping):
            raise HistoricalAttributionRegistryError(f"confluence_contexts[{index}] must be an object")
        context_id = item.get("context_id")
        if not isinstance(context_id, str) or not context_id.strip():
            raise HistoricalAttributionRegistryError(f"confluence_contexts[{index}].context_id is required")
        if context_id in seen_contexts:
            raise HistoricalAttributionRegistryError(f"duplicate confluence context: {context_id}")
        seen_contexts.add(context_id)
        if item.get("status") not in {"active", "retired"}:
            raise HistoricalAttributionRegistryError(f"confluence context {context_id} has invalid status")
        if not isinstance(item.get("display_name"), str) or not item["display_name"].strip():
            raise HistoricalAttributionRegistryError(f"confluence context {context_id} requires display_name")
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            raise HistoricalAttributionRegistryError(f"confluence context {context_id} aliases must be strings")
    return payload


def historical_attribution_registry_digest(root: Path) -> str:
    payload = load_historical_attribution_registry(root)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expression_contract(
    root: Path,
    strategy_id: str,
    rule_version: str,
    expression_id: str,
) -> Mapping[str, Any] | None:
    registry = load_historical_attribution_registry(root)
    for item in registry["entry_expressions"]:
        if (
            item.get("strategy_id") == strategy_id
            and item.get("rule_version") == rule_version
            and item.get("expression_id") == expression_id
        ):
            return dict(item)
    return None


def active_contexts(root: Path) -> dict[str, dict[str, Any]]:
    registry = load_historical_attribution_registry(root)
    return {
        str(item["context_id"]): dict(item)
        for item in registry["confluence_contexts"]
        if item.get("status") == "active"
    }


def normalize_context_id(root: Path, value: str) -> str | None:
    """Resolve a governed context ID, display name, or registry alias."""

    candidate = value.strip().casefold()
    for context_id, item in active_contexts(root).items():
        names = [context_id, str(item.get("display_name", "")), *item.get("aliases", [])]
        if any(candidate == name.strip().casefold() for name in names if isinstance(name, str)):
            return context_id
    return None
