from __future__ import annotations

import argparse
import re
import shlex
import sys
import types
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "workflows" / "blocked-conditions-index.yaml"
PROPW_SRC = ROOT / "ingestion" / "propw" / "src"

EXPECTED_PHASES = [
    "evidence_handling",
    "packet_validation",
    "authority_pins",
    "provider_preflight",
    "execution_gates",
    "review_prerequisites",
]
EXPECTED_CONDITION_COUNT = 47
REQUIRED_CONDITION_FIELDS = (
    "id",
    "phase",
    "stop_state",
    "source_file",
    "source_section",
    "trigger",
    "description",
    "recovery",
)


class _CapturedParser(Exception):
    """Sentinel raised right after ArgumentParser construction in main()."""


def _load_yaml(rel_path: str | Path) -> dict[str, Any]:
    path = ROOT / rel_path
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{rel_path} must parse as a YAML mapping"
    return loaded


def _resolve_yaml_section(doc: Any, path_expr: str) -> Any:
    current = doc
    for segment in path_expr.split("."):
        if isinstance(current, dict):
            if segment not in current:
                raise KeyError(f"missing mapping key {segment!r} in path {path_expr!r}")
            current = current[segment]
        elif isinstance(current, list):
            match = next(
                (item for item in current if isinstance(item, dict) and item.get("id") == segment),
                None,
            )
            if match is None:
                raise KeyError(f"missing list item with id={segment!r} in path {path_expr!r}")
            current = match
        else:
            raise KeyError(f"cannot traverse non-container {type(current).__name__} at {segment!r}")
    return current


def _get_trading_os_parser() -> argparse.ArgumentParser:
    import trading_os.__main__ as trading_os_main

    captured: list[argparse.ArgumentParser] = []
    original_parse_args = argparse.ArgumentParser.parse_args

    def capture_parse_args(self: argparse.ArgumentParser, args=None, namespace=None):
        captured.append(self)
        raise _CapturedParser()

    argparse.ArgumentParser.parse_args = capture_parse_args  # type: ignore[method-assign]
    try:
        try:
            trading_os_main.main([])
        except _CapturedParser:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original_parse_args  # type: ignore[method-assign]

    assert len(captured) == 1, "expected trading_os.__main__.main() to build root ArgumentParser"
    return captured[0]


def _get_propw_parser() -> argparse.ArgumentParser:
    added_path = str(PROPW_SRC) not in sys.path
    if added_path:
        sys.path.insert(0, str(PROPW_SRC))

    pre_propw = {name for name in sys.modules if name == "propw" or name.startswith("propw.")}

    def _clear_new_propw() -> None:
        for name in list(sys.modules):
            if (name == "propw" or name.startswith("propw.")) and name not in pre_propw:
                sys.modules.pop(name, None)

    try:
        try:
            from propw.cli import build_parser as build_propw_parser

            return build_propw_parser()
        except ModuleNotFoundError as exc:
            if exc.name != "httpx":
                raise
            # CI's `tests` lane installs only pytest + PyYAML before propw's requirements.txt.
            # Inject a minimal httpx module shim strictly while building propw's ArgumentParser,
            # then restore sys.modules so neither the shim nor partially bound propw.* leaks.
            _clear_new_propw()
            had_httpx = "httpx" in sys.modules
            prev_httpx = sys.modules.get("httpx")
            sys.modules["httpx"] = types.ModuleType("httpx")
            try:
                from propw.cli import build_parser as build_propw_parser

                return build_propw_parser()
            finally:
                _clear_new_propw()
                if had_httpx:
                    sys.modules["httpx"] = prev_httpx
                else:
                    sys.modules.pop("httpx", None)
    finally:
        if added_path and str(PROPW_SRC) in sys.path:
            sys.path.remove(str(PROPW_SRC))


def _parser_spec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    options: dict[str, argparse.Action] = {}
    positionals: list[argparse.Action] = []
    subcommands: dict[str, dict[str, Any]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                subcommands[name] = _parser_spec(subparser)
        elif action.option_strings:
            for opt in action.option_strings:
                options[opt] = action
        else:
            positionals.append(action)
    return {
        "options": options,
        "positionals": positionals,
        "subcommands": subcommands,
    }


def _tokenize_cli(span: str) -> list[str]:
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"__PH_{len(placeholders) - 1}__"

    masked = re.sub(r"<[^>]+>", _stash, span).replace("[", " ").replace("]", " ")
    raw_tokens = shlex.split(masked)
    return [
        re.sub(r"__PH_(\d+)__", lambda m: placeholders[int(m.group(1))], token)
        for token in raw_tokens
    ]


def _validate_tokens_against_spec(
    tokens: list[str],
    root_spec: dict[str, Any],
    label: str,
) -> None:
    idx = 0
    if len(tokens) >= 3 and tokens[0] == "python" and tokens[1] == "-m" and tokens[2] in {"trading_os", "propw"}:
        idx = 3

    cur = root_spec
    path = [label]
    while idx < len(tokens):
        tok = tokens[idx]
        if tok.startswith("-"):
            break
        if cur["subcommands"]:
            if "|" in tok:
                alternatives = tok.split("|")
                missing = [alt for alt in alternatives if alt not in cur["subcommands"]]
                assert not missing, (
                    f"{' '.join(path)}: unknown subcommand alternative(s) {missing} "
                    f"(valid: {sorted(cur['subcommands'])})"
                )
                remaining = tokens[idx + 1 :]
                for alt in alternatives:
                    _validate_tokens_against_spec(remaining, cur["subcommands"][alt], " ".join(path + [alt]))
                return
            assert tok in cur["subcommands"], (
                f"{' '.join(path)}: unknown subcommand {tok!r} (valid: {sorted(cur['subcommands'])})"
            )
            cur = cur["subcommands"][tok]
            path.append(tok)
            idx += 1
        else:
            # Positional argument on leaf command
            if not (tok.startswith("<") and tok.endswith(">")):
                assert cur["positionals"], f"{' '.join(path)}: unexpected positional argument {tok!r}"
                # If positional has explicit choices (e.g., backtest-registration preview|commit), check them
                first_pos = cur["positionals"][0]
                if first_pos.choices is not None:
                    assert tok in first_pos.choices, (
                        f"{' '.join(path)}: invalid positional {tok!r} not in {sorted(first_pos.choices)}"
                    )
            idx += 1

    while idx < len(tokens):
        tok = tokens[idx]
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            assert flag in cur["options"], (
                f"{' '.join(path)}: unknown flag {flag!r} (valid: {sorted(cur['options'])})"
            )
            action = cur["options"][flag]
            takes_value = not isinstance(
                action,
                (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction),
            )
            if takes_value and idx + 1 < len(tokens) and not tokens[idx + 1].startswith("-"):
                val = tokens[idx + 1]
                if action.choices is not None and not (val.startswith("<") and val.endswith(">")):
                    assert val in action.choices, (
                        f"{' '.join(path)} {flag}: invalid choice {val!r} not in {sorted(action.choices)}"
                    )
                idx += 2
                continue
        idx += 1


def _extract_cli_invocations(text: str, known_commands: set[str]) -> tuple[list[tuple[str, str]], list[str]]:
    full_invocations: list[tuple[str, str]] = []
    shorthands: list[str] = []

    for match in re.finditer(r"`([^`]+)`", text):
        span = match.group(1).strip()
        if span.startswith("python -m trading_os "):
            full_invocations.append(("trading_os", span))
            continue
        if span.startswith("python -m propw "):
            full_invocations.append(("propw", span))
            continue
        # Shorthand CLI forms such as `registration-executor run` or `capture verify-exit`
        # Exclude schema identifiers (*.v1, *.v2) and bracketed/field paths like `evidence[]`
        head = span.split()[0] if span else ""
        if head in known_commands and "." not in head and "[]" not in span and "[" not in head:
            if " " in span or span.startswith(("registration-", "provider-sync")):
                shorthands.append(span)

    # Also catch any un-backticked `python -m trading_os ...` or `python -m propw ...`
    outside_backticks = re.sub(r"`[^`]+`", " ", text)
    for match in re.finditer(r"python -m (trading_os|propw)\s+[^\n;,)]+", outside_backticks):
        tool = match.group(1)
        cmd = match.group(0).strip().rstrip(".")
        full_invocations.append((tool, cmd))

    return full_invocations, shorthands


def test_every_workflow_invariant_is_plain_text_and_index_parses_cleanly() -> None:
    for path in sorted((ROOT / "workflows").glob("*.yaml")):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), f"{path.name}: workflow must parse as a YAML mapping"
        invariants = parsed.get("invariants")
        if isinstance(invariants, list):
            bad = [i for i, item in enumerate(invariants) if not isinstance(item, str)]
            assert not bad, f"{path.name}: invariants {bad} parsed as mappings (unquoted ': ' in text)"

    index = _load_yaml("workflows/blocked-conditions-index.yaml")
    assert index["schema_version"] == "blocked-conditions-index.v1"
    assert index["total_conditions"] == EXPECTED_CONDITION_COUNT
    for i, condition in enumerate(index["conditions"], 1):
        for field in REQUIRED_CONDITION_FIELDS:
            val = condition.get(field)
            assert isinstance(val, str) and val.strip(), (
                f"condition #{i} ({condition.get('id')}) field {field!r} must be a non-empty string"
            )


def test_blocked_conditions_exact_count_phases_and_bijection() -> None:
    index = _load_yaml("workflows/blocked-conditions-index.yaml")
    phases = index["phases"]
    conditions = index["conditions"]

    assert list(phases.keys()) == EXPECTED_PHASES
    assert len(conditions) == EXPECTED_CONDITION_COUNT
    assert index["total_conditions"] == EXPECTED_CONDITION_COUNT

    condition_ids = [c["id"] for c in conditions]
    assert len(set(condition_ids)) == EXPECTED_CONDITION_COUNT, "duplicate condition ids in conditions[]"

    phase_listed_ids: list[str] = []
    expected_phase_by_id: dict[str, str] = {}
    for phase_name in EXPECTED_PHASES:
        ids_in_phase = phases[phase_name]["condition_ids"]
        assert isinstance(ids_in_phase, list) and ids_in_phase
        for cid in ids_in_phase:
            assert cid not in expected_phase_by_id, f"duplicate condition_id {cid!r} across phases"
            expected_phase_by_id[cid] = phase_name
            phase_listed_ids.append(cid)

    assert len(phase_listed_ids) == EXPECTED_CONDITION_COUNT
    assert set(phase_listed_ids) == set(condition_ids), (
        f"bijection mismatch: phase_only={set(phase_listed_ids) - set(condition_ids)} "
        f"conditions_only={set(condition_ids) - set(phase_listed_ids)}"
    )
    assert condition_ids == phase_listed_ids, "conditions[] ordering must match phases.*.condition_ids"

    for c in conditions:
        assert c["phase"] == expected_phase_by_id[c["id"]], (
            f"{c['id']}: phase={c['phase']!r} != listed phase {expected_phase_by_id[c['id']]!r}"
        )


def test_all_47_condition_sources_and_inline_workflow_refs_resolve() -> None:
    index = _load_yaml("workflows/blocked-conditions-index.yaml")
    scope = index["scope"]
    allowed_files = set(scope["primary_workflows"] + scope["supporting_delegated_workflows"])

    yaml_cache: dict[str, dict[str, Any]] = {}
    for rel in allowed_files:
        assert (ROOT / rel).is_file(), f"scoped workflow missing: {rel}"
        yaml_cache[rel] = _load_yaml(rel)

    for c in index["conditions"]:
        sf = c["source_file"]
        ss = c["source_section"]
        assert sf in allowed_files, f"{c['id']}: source_file {sf!r} not declared in scope"
        resolved = _resolve_yaml_section(yaml_cache[sf], ss)
        assert resolved is not None, f"{c['id']}: {sf}#{ss} resolved to None"

        for field in ("description", "recovery"):
            for match in re.finditer(r"`([^`]+\.yaml#[^`]+)`", c[field]):
                ref = match.group(1)
                wf_path, frag = ref.split("#", 1)
                if not wf_path.startswith("workflows/"):
                    wf_path = f"workflows/{wf_path}"
                if ":" in frag:
                    frag = frag.split(":", 1)[0].strip()
                wf_doc = yaml_cache.setdefault(wf_path, _load_yaml(wf_path))
                _resolve_yaml_section(wf_doc, frag)


def test_no_evidence_ingest_or_unregistered_capture_bypass_in_recovery() -> None:
    index = _load_yaml("workflows/blocked-conditions-index.yaml")

    banned_registration_capture = re.compile(
        r"\bcapture\s+(?:draft|evidence|entry-pending|identity-link|verify-entry|finalize|live|retrospective-live|backtest|partial-close)\b"
    )
    by_id = {c["id"]: c for c in index["conditions"]}

    for c in index["conditions"]:
        cid = c["id"]
        recovery = c["recovery"]
        assert not re.search(r"\bevidence\s+ingest\b", recovery, re.IGNORECASE), (
            f"{cid}: forbidden 'evidence ingest' bypass instruction found in recovery"
        )

        # Ensure banned direct `capture <cmd>` commands never appear except in an explicit negative prohibition
        for m in banned_registration_capture.finditer(recovery):
            prefix = recovery[max(0, m.start() - 32) : m.start()].lower()
            assert "never" in prefix or "do not" in prefix or "halt" in prefix, (
                f"{cid}: unauthorized direct capture recommendation {m.group(0)!r} in recovery"
            )

        # Review capture & lifecycle close/verify commands belong strictly post-registration
        if c["phase"] != "review_prerequisites" and cid != "ALREADY_REGISTERED_BINDING_OR_SEMANTIC_MISMATCH":
            assert not re.search(r"\bcapture\s+(?:review|review-preview|close|verify-exit|strategy-attribution)\b", recovery), (
                f"{cid}: post-registration/review capture subcommand recommended outside valid phase"
            )

    # Specifically verify #07 routes strictly through registration-executor
    c07 = by_id["SAME_AGENT_IMAGE_MISSING_FROM_EVIDENCE_STORE"]["recovery"]
    assert "registration-executor run" in c07
    assert "registration-executor-input.v2" in c07
    assert "registration-executor-input.v1" in c07
    assert "image_files" in c07
    assert "registration-packet validate" not in c07

    # Specifically verify #42 and #43 restrict capture close / capture verify-exit to registered canonical trades only
    c42 = by_id["REVIEW_TRADE_NOT_CLOSED"]["recovery"]
    assert "already-registered canonical `trade_id`" in c42
    assert "never use `capture close` or `capture verify-exit` on an unregistered trade" in c42.lower()

    c43 = by_id["REVIEW_EXIT_VERIFICATION_NOT_CONFIRMED"]["recovery"]
    assert "already-registered canonical `trade_id`" in c43
    assert "never unregistered trades" in c43.lower()


def _all_spec_options(spec: dict[str, Any]) -> set[str]:
    flags = set(spec["options"].keys())
    for sub in spec["subcommands"].values():
        flags.update(_all_spec_options(sub))
    return flags


def test_all_47_recovery_cli_commands_and_flags_exist_in_argparse() -> None:
    index = _load_yaml("workflows/blocked-conditions-index.yaml")
    trading_os_spec = _parser_spec(_get_trading_os_parser())
    propw_spec = _parser_spec(_get_propw_parser())
    known_t_cmds = set(trading_os_spec["subcommands"].keys())
    all_valid_flags = _all_spec_options(trading_os_spec) | _all_spec_options(propw_spec)

    trading_os_checked = 0
    propw_checked = 0
    shorthand_checked = 0
    standalone_flags_checked = 0

    for c in index["conditions"]:
        for field in ("recovery", "description"):
            full_cmds, shorthands = _extract_cli_invocations(c[field], known_t_cmds)
            for tool, cmd in full_cmds:
                tokens = _tokenize_cli(cmd)
                if tool == "trading_os":
                    _validate_tokens_against_spec(tokens, trading_os_spec, f"{c['id']}.{field}")
                    trading_os_checked += 1
                else:
                    _validate_tokens_against_spec(tokens, propw_spec, f"{c['id']}.{field}")
                    propw_checked += 1
            for cmd in shorthands:
                tokens = _tokenize_cli(cmd)
                # Ignore bare single-token references unless multi-token or top-level command
                if len(toks := tokens) >= 2:
                    _validate_tokens_against_spec(toks, trading_os_spec, f"{c['id']}.{field}(shorthand)")
                    shorthand_checked += 1

            for match in re.finditer(r"`(--[a-z0-9-]+)(?:[\s=][^`]*)?`", c[field]):
                flag = match.group(1)
                assert flag in all_valid_flags, (
                    f"{c['id']}.{field}: standalone flag {flag!r} not found in trading_os/propw argparse"
                )
                standalone_flags_checked += 1

    assert trading_os_checked >= 30, f"expected >= 30 trading_os CLI checks, got {trading_os_checked}"
    assert propw_checked >= 2, f"expected >= 2 propw CLI checks, got {propw_checked}"
    assert shorthand_checked >= 5, f"expected >= 5 shorthand CLI checks, got {shorthand_checked}"
    assert standalone_flags_checked >= 5, f"expected >= 5 standalone flag checks, got {standalone_flags_checked}"
