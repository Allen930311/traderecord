"""Deterministic test-lane selection for routine trade registrations.

The lane policy is owned by ``workflows/registration-execution.yaml``. A run
starts by capturing a clean ``HEAD == origin/main`` mutation base in this
worktree's private Git directory. Agents cannot choose a base ref. The module
classifies the complete diff, runs the selected pytest targets, and records a
content/mode manifest that must still match before commit and push.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import yaml


FALLBACK_FULL_COMMANDS = (
    ("python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"),
    ("python", "-m", "pytest", "ingestion/intake/tests", "-q", "-p", "no:cacheprovider"),
    ("python", "-m", "pytest", "ingestion/propw/tests", "-q", "-p", "no:cacheprovider"),
)
RUN_RECEIPT_SCHEMA = "registration-test-lane-run.v1"


class RegistrationTestLaneError(ValueError):
    """Raised when the test-lane policy or Git diff cannot be trusted."""


def _git(root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.check_output(["git", *arguments], cwd=root, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        details = exc.stderr.decode("utf-8", errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise RegistrationTestLaneError(f"unable to inspect registration diff: {details}") from exc


def _git_sha(root: Path, ref: str) -> str:
    value = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RegistrationTestLaneError(f"{ref} did not resolve to a full Git commit SHA")
    return value


def changed_statuses(root: Path, base_sha: str) -> dict[str, str]:
    """Return tracked/untracked path statuses since a trusted commit SHA."""

    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise RegistrationTestLaneError("mutation base must be a full commit SHA")
    resolved_base = _git_sha(root, base_sha)
    tracked = _git(root, "diff", "--name-status", "--no-renames", "-z", resolved_base, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    tokens = [value for value in tracked.split(b"\x00") if value]
    if len(tokens) % 2:
        raise RegistrationTestLaneError("Git returned an incomplete changed-path status record")
    values: dict[str, str] = {}
    for index in range(0, len(tokens), 2):
        status = tokens[index].decode("ascii", errors="strict")
        path = tokens[index + 1].decode("utf-8", errors="strict")
        values[path] = status
    for value in untracked.split(b"\x00"):
        if value:
            path = value.decode("utf-8", errors="strict")
            values[path] = "A"
    return dict(sorted(values.items()))


def _run_directory(root: Path) -> Path:
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").decode("utf-8").strip())
    return git_dir / "registration-test-lane"


def _receipt_path(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise RegistrationTestLaneError("run-id must be the opaque id returned by registration-test-lane begin")
    return _run_directory(root) / f"{run_id}.json"


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_receipt(root: Path, run_id: str) -> dict[str, Any]:
    path = _receipt_path(root, run_id)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationTestLaneError("mutation-base receipt is missing or invalid; start a new registration run") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != RUN_RECEIPT_SCHEMA
        or receipt.get("run_id") != run_id
        or receipt.get("worktree") != str(root.resolve())
        or not isinstance(receipt.get("mutation_base_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", receipt["mutation_base_sha"])
    ):
        raise RegistrationTestLaneError("mutation-base receipt does not match this run and worktree")
    return receipt


def begin_mutation(root: Path) -> dict[str, Any]:
    """Capture the runtime base only from a clean worktree exactly at origin/main."""

    root = root.resolve()
    base_sha = _git_sha(root, "origin/main")
    head_sha = _git_sha(root, "HEAD")
    if head_sha != base_sha:
        raise RegistrationTestLaneError("HEAD must equal freshly fetched origin/main before registration mutation")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status.strip():
        raise RegistrationTestLaneError("worktree must be clean before capturing the registration mutation base")
    run_id = uuid.uuid4().hex
    receipt: dict[str, Any] = {
        "schema_version": RUN_RECEIPT_SCHEMA,
        "run_id": run_id,
        "worktree": str(root),
        "mutation_base_sha": base_sha,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "test_status": "not_run",
        "tested_diff_sha256": None,
    }
    _write_receipt(_receipt_path(root, run_id), receipt)
    return receipt


def _validate_run_base(root: Path, receipt: Mapping[str, Any], *, allow_committed: bool) -> str:
    base_sha = str(receipt["mutation_base_sha"])
    if _git_sha(root, "origin/main") != base_sha:
        raise RegistrationTestLaneError("origin/main moved from the captured mutation base; refresh authority and restart the run")
    head_sha = _git_sha(root, "HEAD")
    if allow_committed:
        try:
            subprocess.run(["git", "merge-base", "--is-ancestor", base_sha, head_sha], cwd=root, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RegistrationTestLaneError("HEAD no longer descends from the captured mutation base") from exc
    elif head_sha != base_sha:
        raise RegistrationTestLaneError("HEAD changed after mutation-base capture; restart classification from fresh origin/main")
    return base_sha


def _path_component_facts(root: Path, path: str) -> tuple[bool, list[dict[str, Any]]]:
    """Describe each path component while rejecting traversal and symlinks."""

    pure = PurePosixPath(path)
    if "\\" in path or pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        return False, [{"kind": "invalid_path"}]
    current = root
    facts: list[dict[str, Any]] = []
    try:
        for index, part in enumerate(pure.parts):
            current = current / part
            info = current.lstat()
            fact: dict[str, Any] = {"path": "/".join(pure.parts[: index + 1]), "mode": info.st_mode & 0o777}
            if current.is_symlink():
                fact["kind"] = "symlink"
                fact["target"] = os.readlink(current)
                facts.append(fact)
                return False, facts
            if index < len(pure.parts) - 1 and not current.is_dir():
                fact["kind"] = "non_directory_parent"
                facts.append(fact)
                return False, facts
            facts.append(fact)
        if current.is_file():
            facts[-1]["kind"] = "regular_file"
            return True, facts
        facts[-1]["kind"] = "non_regular_file"
        return False, facts
    except OSError:
        return False, facts + [{"kind": "missing_or_unreadable_component"}]


def _executable_mode_paths(root: Path, base_sha: str) -> set[str]:
    """Return changed paths whose Git worktree or index mode is executable."""

    executable: set[str] = set()
    for cached in (False, True):
        arguments = ["diff", "--raw", "-z", "--no-renames", "--no-abbrev"]
        if cached:
            arguments.append("--cached")
        raw = _git(root, *arguments, base_sha, "--")
        tokens = [value for value in raw.split(b"\x00") if value]
        if len(tokens) % 2:
            raise RegistrationTestLaneError("Git returned an incomplete raw mode diff record")
        for index in range(0, len(tokens), 2):
            header = tokens[index].decode("ascii", errors="strict").split()
            path = tokens[index + 1].decode("utf-8", errors="strict")
            if len(header) != 5 or not header[0].startswith(":"):
                raise RegistrationTestLaneError("Git returned an invalid raw mode diff record")
            new_mode = header[1]
            try:
                has_execute_bit = bool(int(new_mode, 8) & 0o111)
            except ValueError as exc:
                raise RegistrationTestLaneError("Git returned an invalid file mode") from exc
            if has_execute_bit:
                executable.add(path)
    return executable


def diff_manifest(root: Path, base_sha: str) -> tuple[dict[str, str], list[str], str]:
    """Return statuses, unsafe paths, and a stable hash of the complete diff."""

    root = root.resolve()
    statuses = changed_statuses(root, base_sha)
    executable_paths = _executable_mode_paths(root, base_sha)
    unsafe: list[str] = []
    entries: list[dict[str, Any]] = []
    for path, status in statuses.items():
        entry: dict[str, Any] = {"path": path, "status": status}
        if status in {"A", "M"}:
            safe_file, component_facts = _path_component_facts(root, path)
            if not safe_file:
                unsafe.append(path)
                entry["components"] = component_facts
            else:
                target = root.joinpath(*PurePosixPath(path).parts)
                info = target.lstat()
                mode = info.st_mode & 0o777
                entry.update(
                    {
                        "kind": "regular_file",
                        "mode": mode,
                        "size": info.st_size,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                )
                if mode & 0o111 or path in executable_paths:
                    unsafe.append(path)
                    entry["kind"] = "executable_file"
        else:
            unsafe.append(path)
            entry["kind"] = "status_not_added_or_modified"
        entries.append(entry)
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return statuses, sorted(set(unsafe)), hashlib.sha256(encoded).hexdigest()


def load_policy(root: Path) -> tuple[Mapping[str, Any], Sequence[str]]:
    """Load the registration lane and routine-write allowlist from project contracts."""

    execution_path = root / "workflows" / "registration-execution.yaml"
    reconciliation_path = root / "workflows" / "record-reconciliation.yaml"
    try:
        execution = yaml.safe_load(execution_path.read_text(encoding="utf-8"))
        reconciliation = yaml.safe_load(reconciliation_path.read_text(encoding="utf-8"))
        lane_policy = execution["test_policy"]
        routine_allowlist = reconciliation["persistence_policy"]["routine_artifact_allowlist"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise RegistrationTestLaneError("registration test-lane contract is missing or invalid") from exc
    if not isinstance(lane_policy, Mapping) or not isinstance(routine_allowlist, list):
        raise RegistrationTestLaneError("registration test-lane contract has invalid shape")
    data_policy = lane_policy.get("data_only_registration")
    full_policy = lane_policy.get("full_suite")
    rules = data_policy.get("path_rules") if isinstance(data_policy, Mapping) else None
    targets = data_policy.get("pytest_targets") if isinstance(data_policy, Mapping) else None
    commands = full_policy.get("commands") if isinstance(full_policy, Mapping) else None
    safe_target = re.compile(r"tests/test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)*\Z")
    if not isinstance(rules, list) or not rules or any(not isinstance(rule, str) for rule in rules):
        raise RegistrationTestLaneError("data-only registration path rules are invalid")
    try:
        for rule in rules:
            re.compile(rule)
    except re.error as exc:
        raise RegistrationTestLaneError("data-only registration path rule is invalid") from exc
    if any(not isinstance(item, str) for item in routine_allowlist):
        raise RegistrationTestLaneError("routine artifact allowlist is invalid")
    if not isinstance(targets, list) or not targets or any(not isinstance(target, str) or not safe_target.fullmatch(target) for target in targets):
        raise RegistrationTestLaneError("focused test targets must be explicit project test files or test node ids")
    expected_commands = [list(command) for command in FALLBACK_FULL_COMMANDS]
    if commands != expected_commands:
        raise RegistrationTestLaneError("full-suite commands must match the fixed project pytest gate")
    return lane_policy, tuple(str(item) for item in routine_allowlist)


def _matches_registration(path: str, rules: Sequence[str]) -> bool:
    return any(re.fullmatch(rule, path) is not None for rule in rules)


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def build_plan(
    paths: Sequence[str],
    lane_policy: Mapping[str, Any],
    routine_allowlist: Sequence[str],
    *,
    unsafe_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Choose a focused lane only when every diff path is an explicitly allowed data file."""

    normalized = sorted({str(path).removeprefix("./") for path in paths if str(path)})
    data_policy = lane_policy.get("data_only_registration", {})
    rules = data_policy.get("path_rules", []) if isinstance(data_policy, Mapping) else []
    targets = data_policy.get("pytest_targets", []) if isinstance(data_policy, Mapping) else []
    full_policy = lane_policy.get("full_suite", {})
    full_commands = full_policy.get("commands", []) if isinstance(full_policy, Mapping) else []
    if (
        not isinstance(rules, list)
        or not isinstance(targets, list)
        or not targets
        or not isinstance(full_commands, list)
        or not full_commands
        or any(not isinstance(command, list) or not command for command in full_commands)
        or [list(command) for command in full_commands] != [list(command) for command in FALLBACK_FULL_COMMANDS]
    ):
        raise RegistrationTestLaneError("data-only registration test policy is invalid")

    routine_paths = [path for path in normalized if _matches(path, routine_allowlist)]
    registration_paths = [path for path in normalized if _matches_registration(path, rules)]
    unsafe = sorted({str(path) for path in unsafe_paths})
    is_data_only = bool(normalized) and not unsafe and len(routine_paths) == len(normalized) and len(registration_paths) == len(normalized)

    if is_data_only:
        command = [sys.executable, "-m", "pytest", *[str(item) for item in targets], "-q", "-p", "no:cacheprovider"]
        return {
            "classification": "DATA_ONLY_REGISTRATION",
            "test_lane": "focused",
            "changed_paths": normalized,
            "unsafe_paths": [],
            "excluded_paths": [],
            "pytest_targets": [str(item) for item in targets],
            "pytest_commands": [command],
            "dry_run": True,
        }

    excluded = sorted((set(normalized) - (set(routine_paths) & set(registration_paths))) | set(unsafe))
    if unsafe:
        reason = "unsafe_changed_path_status_or_file_type"
    elif not normalized:
        reason = "empty_diff_fails_closed"
    else:
        reason = "path_outside_data_only_registration_allowlist"
    commands = [
        [sys.executable if index == 0 and str(part) == "python" else str(part) for index, part in enumerate(command)]
        for command in full_commands
    ]
    return {
        "classification": "SYSTEM_OR_UNKNOWN_CHANGE",
        "test_lane": "full_suite",
        "reason": reason,
        "changed_paths": normalized,
        "unsafe_paths": unsafe,
        "excluded_paths": excluded,
        "pytest_targets": [str(command[3]) for command in full_commands if len(command) > 3],
        "pytest_commands": commands,
        "dry_run": True,
    }


def _plan_from_statuses(
    statuses: Mapping[str, str],
    unsafe_paths: Sequence[str],
    lane_policy: Mapping[str, Any],
    routine_allowlist: Sequence[str],
) -> dict[str, Any]:
    plan = build_plan(list(statuses), lane_policy, routine_allowlist, unsafe_paths=unsafe_paths)
    if "<classifier-error>" in statuses:
        plan["reason"] = "classifier_error_fails_closed"
    return plan


def build_current_plan(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Build a plan only from the base captured by ``begin``."""

    root = root.resolve()
    receipt = _read_receipt(root, run_id)
    base_sha = _validate_run_base(root, receipt, allow_committed=False)
    try:
        statuses, unsafe_paths, diff_sha = diff_manifest(root, base_sha)
    except RegistrationTestLaneError:
        statuses = {"<classifier-error>": "X"}
        unsafe_paths = ["<classifier-error>"]
        diff_sha = ""
    try:
        lane_policy, routine_allowlist = load_policy(root)
        plan = _plan_from_statuses(statuses, unsafe_paths, lane_policy, routine_allowlist)
    except RegistrationTestLaneError:
        commands = [[sys.executable if index == 0 and part == "python" else part for index, part in enumerate(command)] for command in FALLBACK_FULL_COMMANDS]
        plan = {
            "classification": "SYSTEM_OR_UNKNOWN_CHANGE",
            "test_lane": "full_suite",
            "reason": "invalid_lane_contract_fails_closed",
            "changed_paths": list(statuses),
            "unsafe_paths": list(unsafe_paths),
            "excluded_paths": list(statuses),
            "pytest_targets": ["tests", "ingestion/intake/tests", "ingestion/propw/tests"],
            "pytest_commands": commands,
            "dry_run": True,
        }
    plan["mutation_base_sha"] = base_sha
    return plan, receipt, diff_sha


def _validate_tested_diff(root: Path, run_id: str) -> dict[str, Any]:
    root = root.resolve()
    receipt = _read_receipt(root, run_id)
    base_sha = _validate_run_base(root, receipt, allow_committed=True)
    if receipt.get("test_status") != "passed" or not isinstance(receipt.get("tested_diff_sha256"), str):
        raise RegistrationTestLaneError("no successful test receipt exists for this diff; run the lane before commit or push")
    _, _, actual_sha = diff_manifest(root, base_sha)
    if actual_sha != receipt["tested_diff_sha256"]:
        raise RegistrationTestLaneError(
            "diff changed after tests; reclassify and rerun registration-test-lane run before commit or push"
        )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked:
        raise RegistrationTestLaneError("stage the complete tested diff before commit or push; untracked files remain")
    working_patch = _git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", base_sha, "--")
    staged_patch = _git(root, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", base_sha, "--")
    if staged_patch != working_patch:
        raise RegistrationTestLaneError("staged index differs from the tested worktree; stage only the exact tested diff")
    return {
        "run_id": run_id,
        "mutation_base_sha": base_sha,
        "tested_diff_sha256": actual_sha,
        "index_matches_worktree": True,
        "verified": True,
    }


MAX_PARALLEL_WORKERS = 6
PYTEST_FOCUSED_SUFFIX = ["-q", "-p", "no:cacheprovider"]
# Windows allows 32,767 characters on a command line; stay well below it so a large selection
# falls back to the serial command instead of failing to start.
MAX_COMMAND_LINE_CHARACTERS = 24000
COLLECT_TIMEOUT_SECONDS = 180
PARALLEL_RUN_TIMEOUT_SECONDS = 1800
TIMED_OUT_RETURNCODE = 124
_TimeoutExpired = subprocess.TimeoutExpired  # captured so a test double for `subprocess` keeps working
_NODE_ID = re.compile(r"^[^\s:][^\s]*\.py::\S.*$")


def _collect_node_ids(root: Path, command: Sequence[str]) -> list[str] | None:
    """Return the exact pytest node ids a focused command selects, or None when they cannot be listed."""

    targets = list(command[3:-len(PYTEST_FOCUSED_SUFFIX)])
    if list(command[1:3]) != ["-m", "pytest"] or list(command[-len(PYTEST_FOCUSED_SUFFIX):]) != PYTEST_FOCUSED_SUFFIX or not targets:
        return None
    collect = [command[0], "-m", "pytest", *targets, "--collect-only", "-q", "-p", "no:cacheprovider"]
    try:
        completed = subprocess.run(
            collect, cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=COLLECT_TIMEOUT_SECONDS,
        )
        output = getattr(completed, "stdout", None)
        if completed.returncode != 0 or not isinstance(output, bytes):
            return None
        lines = [line.strip() for line in output.decode("utf-8", "replace").splitlines()]
    except (OSError, AttributeError, TypeError, _TimeoutExpired):
        return None
    # Only real pytest node ids (path.py::name), each once and in pytest's own order.
    node_ids = list(dict.fromkeys(line for line in lines if _NODE_ID.match(line)))
    return node_ids or None


def _run_focused_in_parallel(root: Path, command: Sequence[str]) -> list[int] | None:
    """Run the same focused tests split across several pytest processes; None means run serially.

    Every selected test still runs exactly once and every worker must pass, so this is the same
    gate as the serial run, only spread over the available cores. A failing worker stops the rest
    at once, and a run that exceeds its time limit fails the gate; it never waits indefinitely.
    """

    node_ids = _collect_node_ids(root, command)
    workers = min(os.cpu_count() or 1, MAX_PARALLEL_WORKERS, len(node_ids or []))
    if not node_ids or workers < 2:
        return None
    commands = [
        [command[0], "-m", "pytest", *node_ids[index::workers], *PYTEST_FOCUSED_SUFFIX]
        for index in range(workers)
    ]
    if any(sum(len(part) + 1 for part in worker_command) > MAX_COMMAND_LINE_CHARACTERS for worker_command in commands):
        return None
    processes: list[Any] = []
    outputs: list[Any] = []
    try:
        try:
            for worker_command in commands:
                log = tempfile.TemporaryFile()
                outputs.append(log)
                processes.append(
                    subprocess.Popen(worker_command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
                )
        except OSError:
            # A worker could not start: stop the ones that did and run the serial command instead.
            return None
        returncodes: list[int | None] = [None] * len(processes)
        deadline = time.monotonic() + PARALLEL_RUN_TIMEOUT_SECONDS
        while True:
            for index, process in enumerate(processes):
                if returncodes[index] is None:
                    returncodes[index] = process.poll()
            if any(code not in (None, 0) for code in returncodes) or all(code is not None for code in returncodes):
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.2)
        unfinished = [index for index, code in enumerate(returncodes) if code is None]
        for index in unfinished:
            processes[index].kill()
        final = [TIMED_OUT_RETURNCODE if code is None else int(code) for code in returncodes]
        for index, (log, code) in enumerate(zip(outputs, final)):
            log.seek(0)
            text = log.read().decode("utf-8", "replace").strip()
            if index in unfinished:
                print(f"pytest worker {index + 1}/{len(processes)} did not finish and was stopped", file=sys.stderr)
            elif code:
                print(text, file=sys.stderr)
            elif text:
                print(text.splitlines()[-1])
        return final
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for log in outputs:
            log.close()


def _run_pytest_commands(root: Path, plan: Mapping[str, Any]) -> list[int]:
    commands = plan["pytest_commands"]
    if plan.get("test_lane") == "focused" and len(commands) == 1:
        parallel = _run_focused_in_parallel(root, commands[0])
        if parallel is not None and not any(parallel):
            return parallel
        # The parallel run only accelerates the gate. When any worker failed (a real failure, a
        # timeout, or a test that does not like running beside another), the serial command decides,
        # so the reported result is the serial run's and never a worker killed on the way.
    returncodes: list[int] = []
    for command in commands:
        completed = subprocess.run(command, cwd=root, check=False)
        returncodes.append(completed.returncode)
        if completed.returncode:
            break
    return returncodes


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "registration-test-lane",
        help="capture a registration mutation base, select required tests, and verify the tested diff",
    )
    actions = parser.add_subparsers(dest="registration_test_action", required=True)
    actions.add_parser("begin", help="capture clean HEAD == origin/main before registration mutations")
    for action, help_text in (
        ("plan", "show the deterministic test plan for a captured registration run"),
        ("run", "run the selected tests and freeze the tested diff manifest"),
        ("verify-tested-diff", "verify no diff changes occurred since the successful test run"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("--run-id", required=True, help="opaque registration-test-lane begin receipt id")


def dispatch_cli(root: Path, args: Any) -> None:
    root = root.resolve()
    if args.registration_test_action == "begin":
        print(json.dumps(begin_mutation(root), ensure_ascii=False, sort_keys=True, indent=2))
        return
    if args.registration_test_action == "verify-tested-diff":
        print(json.dumps(_validate_tested_diff(root, args.run_id), ensure_ascii=False, sort_keys=True, indent=2))
        return

    plan, receipt, before_sha = build_current_plan(root, args.run_id)
    if args.registration_test_action == "plan":
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        return

    receipt["test_status"] = "running"
    receipt["tested_diff_sha256"] = None
    receipt["test_classification"] = plan["classification"]
    receipt["test_lane"] = plan["test_lane"]
    receipt["pytest_targets"] = plan["pytest_targets"]
    receipt_path = _receipt_path(root, args.run_id)
    _write_receipt(receipt_path, receipt)
    started = time.perf_counter()
    returncodes = _run_pytest_commands(root, plan)
    elapsed = round(time.perf_counter() - started, 6)
    returncode = next((code for code in returncodes if code), 0)
    diff_changed_during_tests = False
    after_sha: str | None = None
    if returncode == 0:
        try:
            _, _, after_sha = diff_manifest(root, str(receipt["mutation_base_sha"]))
        except RegistrationTestLaneError:
            after_sha = None
        diff_changed_during_tests = before_sha == "" or after_sha is None or after_sha != before_sha
        if not diff_changed_during_tests:
            receipt["test_status"] = "passed"
            receipt["tested_diff_sha256"] = after_sha
            receipt["tests_completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _write_receipt(receipt_path, receipt)

    test_segment = "focused_tests_seconds" if plan["test_lane"] == "focused" else "full_tests_seconds"
    report = {
        **plan,
        "dry_run": False,
        "returncode": returncode or (2 if diff_changed_during_tests else 0),
        "command_returncodes": returncodes,
        "diff_changed_during_tests": diff_changed_during_tests,
        "tested_diff_sha256": after_sha if not diff_changed_during_tests and returncode == 0 else None,
        "telemetry": {test_segment: elapsed},
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if report["returncode"]:
        raise SystemExit(report["returncode"])
