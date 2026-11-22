"""Small, dependency-light frontmatter and evidence helpers.

The legacy diary contains intentionally loose YAML (including template
placeholders and values such as ``84.62 (Actual) / 84.495 (Diary)``).  The
pilot therefore parses conservatively and always returns the original body and
frontmatter separately.  A parse warning is safer than silently coercing an
ambiguous value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import re
from typing import Any

try:  # PyYAML is available in the development environment, but is optional.
    import yaml  # type: ignore
except Exception:  # pragma: no cover - exercised only in minimal environments
    yaml = None


@dataclass(frozen=True)
class ParsedMarkdown:
    frontmatter: dict[str, Any]
    frontmatter_text: str
    body: str
    warnings: tuple[str, ...]


def json_safe(value: Any) -> Any:
    """Convert YAML-native values to deterministic JSON-compatible values."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fallback_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return None
    # Keep template strings and ambiguous composite values as strings.
    if value[0:1] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)", value):
        try:
            return float(value)
        except ValueError:
            pass
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value.replace("'", '"'))
        except Exception:
            return [part.strip() for part in value[1:-1].split(",") if part.strip()]
    return value


def _fallback_parse(text: str) -> dict[str, Any]:
    """Parse the simple key/value subset used by the legacy diary."""

    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        list_match = re.match(r"^-\s*(.*)$", stripped)
        if list_match and current_list_key:
            if not isinstance(result.get(current_list_key), list):
                result[current_list_key] = []
            result[current_list_key].append(_fallback_scalar(list_match.group(1)))
            continue
        match = re.match(r"^([^:#][^:]*):(?:\s*(.*))?$", line)
        if not match:
            current_list_key = None
            continue
        key = match.group(1).strip()
        raw_value = match.group(2) or ""
        # Remove an inline comment only when it is separated by whitespace.
        raw_value = re.sub(r"\s+#.*$", "", raw_value).strip()
        value = _fallback_scalar(raw_value)
        result[key] = value
        current_list_key = key if value is None else None
    return result


def parse_markdown(text: str) -> ParsedMarkdown:
    """Split Markdown frontmatter from body without mutating either."""

    warnings: list[str] = []
    # A single leading UTF-8 BOM (U+FEFF) is a byte-order artifact, not content.
    # Strip exactly one for frontmatter recognition; the caller keeps the
    # original bytes and source hash untouched.  Only one code point is removed
    # and only when it is the very first character, so no other leading
    # whitespace is affected and missing/malformed frontmatter still fails
    # closed below.
    if text and ord(text[0]) == 0xFEFF:  # strip one leading U+FEFF byte-order mark
        text = text[1:]
    if not text.startswith("---"):
        return ParsedMarkdown({}, "", text, ("missing_frontmatter",))

    lines = text.splitlines(keepends=True)
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return ParsedMarkdown({}, "", text, ("frontmatter_unclosed",))

    frontmatter_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    parsed: Any
    if yaml is not None:
        try:
            parsed = yaml.safe_load(frontmatter_text) or {}
        except Exception:
            warnings.append("frontmatter_parse_error")
            parsed = _fallback_parse(frontmatter_text)
    else:  # pragma: no cover - fallback is kept for portability.
        parsed = _fallback_parse(frontmatter_text)
    if not isinstance(parsed, dict):
        warnings.append("frontmatter_not_mapping")
        parsed = {}
    # PyYAML's SafeLoader interprets values such as ``14:32`` as YAML
    # sexagesimal integers (872).  That would destroy a legacy timestamp in
    # the normalized record.  Overlay simple top-level scalars with the
    # conservative textual parser while retaining SafeLoader's support for
    # nested maps/lists and preserving the exact frontmatter text above.
    for line in frontmatter_text.splitlines():
        scalar_match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not scalar_match or not scalar_match.group(2).strip():
            continue
        key, raw_value = scalar_match.groups()
        raw_value = re.sub(r"\s+#.*$", "", raw_value).strip()
        parsed[key] = _fallback_scalar(raw_value)
    return ParsedMarkdown(json_safe(parsed), frontmatter_text, body, tuple(warnings))


def extract_embeds(body: str) -> list[dict[str, str]]:
    """Return Obsidian image embeds with a short textual context window."""

    embeds: list[dict[str, str]] = []
    pattern = re.compile(r"!\[\[([^\]]+)\]\]")
    for match in pattern.finditer(body):
        context = body[max(0, match.start() - 240) : match.start()]
        embeds.append({"target": match.group(1).strip(), "context": context})
    return embeds
