from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import trading_os.registration_test_lane as registration_test_lane
from trading_os.registration_test_lane import (
    RegistrationTestLaneError,
    begin_mutation,
    build_current_plan,
    build_plan,
    changed_statuses,
    diff_manifest,
    load_policy,
)


ROOT = Path(__file__).resolve().parents[1]

# Path-only fixture for routine registration outputs, including the refreshed
# PropW Census added to the shared projection closure.
# All files written by these tests are under pytest's temporary directory.
T27_DATA_PATHS = (
    "PERFORMANCE.md",
    "canonical/drafts/rec_82164a3a4ca947d9e07a.json",
    "canonical/evidence/artifacts/" + "0" * 64 + ".metadata.json",
    "canonical/evidence/artifacts/" + "0" * 64 + ".png",
    "canonical/evidence/artifacts/" + "1" * 64 + ".metadata.json",
    "canonical/evidence/artifacts/" + "1" * 64 + ".png",
    "canonical/evidence/artifacts/" + "2" * 64 + ".json",
    "canonical/evidence/artifacts/" + "2" * 64 + ".metadata.json",
    "canonical/lifecycle/current.json",
    "canonical/lifecycle/events.jsonl",
    "canonical/lifecycle/identity-links.jsonl",
    "canonical/records/trades/trd_01a11911-98da-70a1-9f8b-3d05aed89f91.json",
    "canonical/registration/packets/" + "3" * 64 + ".json",
    "canonical/sequences/finalizations.json",
    "canonical/sequences/numbers.json",
    "provider-ledgers/propw/registration-census.json",
    "exports/operational/analytics/current.json",
    "exports/operational/analytics/manifest.json",
    "exports/operational/handoff/current.json",
    "exports/operational/handoff/manifest.json",
    "exports/operational/health/current.json",
    "exports/operational/health/manifest.json",
    "exports/operational/trade-view/manifest.json",
    "exports/operational/trade-view/trade-view-data.json",
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _git_fixture(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "Registration Fast Path Test")
    _git(root, "config", "user.email", "registration-fast-path-test@example.invalid")
    (root / "README.md").write_text("isolated registration lane fixture\n", encoding="utf-8")
    tracked = root / "canonical/lifecycle/current.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("synthetic base lifecycle state\n", encoding="utf-8")
    _git(root, "add", "README.md", "canonical/lifecycle/current.json")
    _git(root, "commit", "-m", "fixture base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return _git(root, "rev-parse", "HEAD")


def _fixture_with_contracts(root: Path) -> tuple[Path, str]:
    base = _git_fixture(root)
    workflow_dir = root / "workflows"
    workflow_dir.mkdir()
    shutil.copy2(ROOT / "workflows/registration-execution.yaml", workflow_dir / "registration-execution.yaml")
    shutil.copy2(ROOT / "workflows/record-reconciliation.yaml", workflow_dir / "record-reconciliation.yaml")
    _git(root, "add", "workflows")
    _git(root, "commit", "-m", "fixture registration contracts")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root, _git(root, "rev-parse", "HEAD")


def test_t27_data_only_fixture_selects_focused_lane(tmp_path: Path) -> None:
    repo = tmp_path / "isolated-data-only-fixture"
    base = _git_fixture(repo)
    for relative in T27_DATA_PATHS:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture bytes; not a trade record\n", encoding="utf-8")
    statuses = changed_statuses(repo, base)
    paths = list(statuses)
    lane_policy, routine_allowlist = load_policy(ROOT)

    plan = build_plan(paths, lane_policy, routine_allowlist)

    assert set(paths) == set(T27_DATA_PATHS)
    assert plan["classification"] == "DATA_ONLY_REGISTRATION"
    assert plan["test_lane"] == "focused"
    assert plan["changed_paths"] == sorted(T27_DATA_PATHS)
    assert plan["pytest_targets"] == lane_policy["data_only_registration"]["pytest_targets"]
    assert plan["dry_run"] is True
    assert statuses["canonical/lifecycle/current.json"] == "M"
    assert all(status == "A" for path, status in statuses.items() if path not in {"README.md", "canonical/lifecycle/current.json"})

    (repo / "canonical/lifecycle/current.json").unlink()
    deleted_statuses = changed_statuses(repo, base)
    delete_plan = build_plan(
        list(deleted_statuses),
        lane_policy,
        routine_allowlist,
        unsafe_paths=[path for path, status in deleted_statuses.items() if status not in {"A", "M"}],
    )
    assert deleted_statuses["canonical/lifecycle/current.json"] == "D"
    assert delete_plan["test_lane"] == "full_suite"
    assert "canonical/lifecycle/current.json" in delete_plan["unsafe_paths"]


def test_unknown_file_types_and_paths_in_data_directories_force_full_suite() -> None:
    lane_policy, routine_allowlist = load_policy(ROOT)
    digest = "a" * 64
    rejected_paths = (
        f"canonical/evidence/artifacts/{digest}.py",
        f"canonical/evidence/artifacts/{digest}.sh",
        "exports/operational/analytics/injected.py",
        "exports/operational/analytics/injected.sh",
        "exports/operational/trade-view/app.js",
        "canonical/records/trades/trd_fixture.json",
    )
    for path in rejected_paths:
        plan = build_plan([path], lane_policy, routine_allowlist)
        assert plan["test_lane"] == "full_suite", path
        assert path in plan["excluded_paths"]


def test_symlinked_data_path_forces_full_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "isolated-symlink-fixture"
    base = _git_fixture(repo)
    lane_policy, routine_allowlist = load_policy(ROOT)
    artifact = repo / "canonical/evidence/artifacts" / ("a" * 64 + ".png")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("synthetic link target\n", encoding="utf-8")
    real_is_symlink = Path.is_symlink

    def report_fixture_symlink(path: Path) -> bool:
        return path == artifact or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_fixture_symlink)

    statuses, unsafe, _ = diff_manifest(repo, base)
    plan = build_plan(list(statuses), lane_policy, routine_allowlist, unsafe_paths=unsafe)

    assert statuses[artifact.relative_to(repo).as_posix()] == "A"
    assert artifact.relative_to(repo).as_posix() in unsafe
    assert plan["test_lane"] == "full_suite"
    assert plan["reason"] == "unsafe_changed_path_status_or_file_type"


def test_executable_data_mode_forces_full_suite(tmp_path: Path) -> None:
    repo = tmp_path / "isolated-executable-mode-fixture"
    base = _git_fixture(repo)
    lane_policy, routine_allowlist = load_policy(ROOT)
    data_file = repo / "PERFORMANCE.md"
    data_file.write_text("synthetic performance projection\n", encoding="utf-8")
    _git(repo, "add", "PERFORMANCE.md")
    _git(repo, "update-index", "--chmod=+x", "--", "PERFORMANCE.md")

    statuses, unsafe, _ = diff_manifest(repo, base)
    plan = build_plan(list(statuses), lane_policy, routine_allowlist, unsafe_paths=unsafe)

    assert statuses["PERFORMANCE.md"] == "A"
    assert "PERFORMANCE.md" in unsafe
    assert plan["test_lane"] == "full_suite"
    assert plan["reason"] == "unsafe_changed_path_status_or_file_type"


def test_mutation_base_is_captured_from_clean_origin_main_not_agent_input(tmp_path: Path) -> None:
    repo = tmp_path / "isolated-base-fixture"
    base = _git_fixture(repo)
    receipt = begin_mutation(repo)
    assert receipt["mutation_base_sha"] == base
    assert receipt["run_id"]

    code = repo / "trading_os/earlier_change.py"
    code.parent.mkdir()
    code.write_text("system change\n", encoding="utf-8")
    _git(repo, "add", "trading_os/earlier_change.py")
    _git(repo, "commit", "-m", "earlier system change")
    with pytest.raises(RegistrationTestLaneError, match="HEAD must equal freshly fetched origin/main"):
        begin_mutation(repo)


def test_mutation_base_capture_requires_clean_worktree_and_plan_rejects_base_override(tmp_path: Path) -> None:
    repo = tmp_path / "isolated-cli-fixture"
    _git_fixture(repo)
    (repo / "untracked.txt").write_text("pre-existing change\n", encoding="utf-8")
    with pytest.raises(RegistrationTestLaneError, match="worktree must be clean"):
        begin_mutation(repo)

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    registration_test_lane.add_cli(subparsers)
    with pytest.raises(SystemExit):
        parser.parse_args(["registration-test-lane", "plan", "--base", "HEAD"])


def test_tested_diff_receipt_detects_changes_before_commit_and_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = _fixture_with_contracts(tmp_path / "isolated-tested-diff-fixture")
    receipt = begin_mutation(repo)
    data = repo / "PERFORMANCE.md"
    data.write_text("version one\n", encoding="utf-8")
    args = SimpleNamespace(registration_test_action="run", run_id=receipt["run_id"])

    def mutate_during_tests(*_args, **_kwargs):
        data.write_text("version two\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    mocked_subprocess = SimpleNamespace(
        run=mutate_during_tests,
        check_output=subprocess.check_output,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
    )
    monkeypatch.setattr(registration_test_lane, "subprocess", mocked_subprocess)
    with pytest.raises(SystemExit, match="2"):
        registration_test_lane.dispatch_cli(repo, args)

    def tests_pass_without_mutation(*_args, **_kwargs):
        return SimpleNamespace(returncode=0)

    mocked_subprocess.run = tests_pass_without_mutation
    registration_test_lane.dispatch_cli(repo, args)
    verification = SimpleNamespace(registration_test_action="verify-tested-diff", run_id=receipt["run_id"])
    _git(repo, "add", "PERFORMANCE.md")
    assert registration_test_lane._validate_tested_diff(repo, verification.run_id)["verified"] is True

    tested_contents = data.read_text(encoding="utf-8")
    data.write_text("staged content not covered by tests\n", encoding="utf-8")
    _git(repo, "add", "PERFORMANCE.md")
    data.write_text(tested_contents, encoding="utf-8")
    with pytest.raises(RegistrationTestLaneError, match="staged index differs"):
        registration_test_lane._validate_tested_diff(repo, verification.run_id)

    _git(repo, "add", "PERFORMANCE.md")
    assert registration_test_lane._validate_tested_diff(repo, verification.run_id)["verified"] is True

    monkeypatch.undo()
    _git(repo, "commit", "-m", "persist tested fixture data")
    assert registration_test_lane._validate_tested_diff(repo, verification.run_id)["mutation_base_sha"] == base


def test_origin_main_movement_invalidates_captured_run(tmp_path: Path) -> None:
    repo = tmp_path / "isolated-stale-base-fixture"
    base = _git_fixture(repo)
    receipt = begin_mutation(repo)
    (repo / "PERFORMANCE.md").write_text("synthetic data\n", encoding="utf-8")
    (repo / "authority.txt").write_text("new remote base\n", encoding="utf-8")
    _git(repo, "add", "authority.txt")
    _git(repo, "commit", "-m", "advance fixture main")
    advanced = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", advanced)

    assert advanced != base
    with pytest.raises(RegistrationTestLaneError, match="origin/main moved"):
        build_current_plan(repo, receipt["run_id"])


def test_unreadable_diff_selects_full_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _ = _fixture_with_contracts(tmp_path / "isolated-unreadable-diff-fixture")
    receipt = begin_mutation(repo)

    def unreadable(*_args, **_kwargs):
        raise RegistrationTestLaneError("synthetic Git diff failure")

    monkeypatch.setattr(registration_test_lane, "diff_manifest", unreadable)
    plan, _, _ = build_current_plan(repo, receipt["run_id"])
    assert plan["classification"] == "SYSTEM_OR_UNKNOWN_CHANGE"
    assert plan["test_lane"] == "full_suite"
    assert plan["reason"] == "classifier_error_fails_closed"


def test_system_change_and_empty_diff_select_full_suite() -> None:
    lane_policy, routine_allowlist = load_policy(ROOT)
    data_path = "canonical/records/trades/trd_01a11911-98da-70a1-9f8b-3d05aed89f91.json"

    data_only = build_plan(T27_DATA_PATHS, lane_policy, routine_allowlist)
    mixed = build_plan([data_path, "trading_os/registration_test_lane.py"], lane_policy, routine_allowlist)
    deleted = build_plan([data_path], lane_policy, routine_allowlist, unsafe_paths=[data_path])
    empty = build_plan([], lane_policy, routine_allowlist)
    ci_workflow = yaml.safe_load((ROOT / ".github/workflows/trading-record-v1.yml").read_text(encoding="utf-8"))
    ci_steps = ci_workflow["jobs"]["project-tests"]["steps"]
    project_suite = next(step["run"] for step in ci_steps if step.get("name") == "Run project test suite")
    ingestion_suite = next(step["run"] for step in ci_steps if step.get("name") == "Run read-only ingestion tests")

    assert data_only["classification"] == "DATA_ONLY_REGISTRATION"
    assert data_only["test_lane"] == "focused"
    assert mixed["classification"] == "SYSTEM_OR_UNKNOWN_CHANGE"
    assert mixed["test_lane"] == "full_suite"
    assert "trading_os/registration_test_lane.py" in mixed["excluded_paths"]
    assert deleted["classification"] == "SYSTEM_OR_UNKNOWN_CHANGE"
    assert deleted["unsafe_paths"] == [data_path]
    assert empty["classification"] == "SYSTEM_OR_UNKNOWN_CHANGE"
    assert empty["reason"] == "empty_diff_fails_closed"
    assert len(mixed["pytest_commands"]) == 3
    normalized_commands = ["python " + " ".join(command[1:]) for command in mixed["pytest_commands"]]
    assert normalized_commands[0] == project_suite
    assert normalized_commands[1:] == ingestion_suite.splitlines()


def test_registration_contract_keeps_runtime_gates_and_single_projection_verify() -> None:
    execution = yaml.safe_load((ROOT / "workflows/registration-execution.yaml").read_text(encoding="utf-8"))
    lane_policy = execution["test_policy"]
    steps = {step["id"]: step for step in execution["runtime_workflow"]}

    assert set(lane_policy["data_only_registration"]["preserves"]) == {
        "provider_verification",
        "evidence_sha256_and_exact_bytes",
        "frozen_packet_integrity_and_resolver_pins",
        "t_id_allocation_census_and_idempotency",
        "lifecycle_transition_and_entry_exit_invariants",
        "projection_digest_chain_and_target_visibility",
        "remote_main_read_back",
    }
    assert execution["projection_closure"]["command"] == "python -m trading_os projections refresh --target-trade <trade_id>"
    assert execution["projection_closure"]["local_verification"]["performed_by"] == "projections.refresh"
    assert execution["projection_closure"]["local_verification"]["explicit_second_verify"] is False
    assert "Never issue a separate local projections verify" in steps["refresh_and_verify_projections"]["action"]
    assert steps["refresh_and_verify_projections"]["fallback_command"] == "python -m trading_os projections refresh --target-trade <trade_id>"
    assert steps["capture_mutation_base"]["command"] == "python -m trading_os registration-test-lane begin"
    assert steps["select_and_run_required_tests"]["command"] == "python -m trading_os registration-test-lane run --run-id <registration-run-id>"
    assert steps["verify_tested_diff_before_commit"]["command"] == (
        "python -m trading_os registration-test-lane verify-tested-diff --run-id <registration-run-id>"
    )
    assert "staged index exactly matches the tested worktree" in steps["verify_tested_diff_before_commit"]["action"]
    sequence = execution["persistence"]["routine_record"]["sequence"]
    assert sequence.index("required_tests") < sequence.index("verify_tested_diff_before_commit_and_push")
    assert sequence.index("verify_tested_diff_before_commit_and_push") < sequence.index("commit_and_push_origin_main")
    assert sequence.index("commit_and_push_origin_main") < sequence.index("fetch_origin_main_and_read_back_exact_remote_objects")
    assert [step["id"] for step in execution["workflow"]] == ["execute_registration"]
    path_rules = lane_policy["data_only_registration"]["path_rules"]
    assert not any("artifacts/**" in rule or "operational/**" in rule for rule in path_rules)
    assert lane_policy["telemetry"]["schema"] == "registration-fast-path-telemetry.v1"
    assert set(lane_policy["telemetry"]["segments"]) == {
        "registration_runtime", "projection", "focused_tests", "git_persistence"
    }


class _FakeProcess:
    """A pytest worker double: `poll` returns the scripted code (None = still running)."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def kill(self):
        self.killed = True


def _fake_subprocess(*, collected: bytes, processes=None, popen=None, run=None):
    launched: list[list[str]] = []
    queue = list(processes or [])

    def fake_popen(args, **_kwargs):
        launched.append(list(args))
        if popen is not None:
            return popen(args)
        return queue.pop(0)

    return SimpleNamespace(
        run=run or (lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=collected)),
        Popen=fake_popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
        TimeoutExpired=subprocess.TimeoutExpired,
    ), launched


_LANE_COMMAND = ["python", "-m", "pytest", "tests/test_x.py", "-q", "-p", "no:cacheprovider"]


def test_focused_tests_run_in_parallel_with_every_selected_test_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_ids = [f"tests/test_x.py::test_{index}" for index in range(10)]
    # Pytest prints other lines too; only real node ids count, and a repeated id runs once.
    collected = ("\n".join([*node_ids, node_ids[0], "warning: a::b"]) + "\n\n10 tests collected\n").encode()
    fake, launched = _fake_subprocess(collected=collected, processes=[_FakeProcess([0]) for _ in range(4)])
    monkeypatch.setattr(registration_test_lane.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(registration_test_lane, "subprocess", fake)

    assert registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND) == [0, 0, 0, 0]
    ran = [item for args in launched for item in args if "::" in item]
    assert sorted(ran) == sorted(node_ids)  # each selected test runs exactly once
    assert all(args[-3:] == ["-q", "-p", "no:cacheprovider"] for args in launched)


def test_a_failing_worker_fails_the_gate_and_stops_the_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_ids = "\n".join(f"tests/test_x.py::test_{index}" for index in range(6)).encode()
    running = _FakeProcess([None])  # would run forever
    fake, _ = _fake_subprocess(
        collected=node_ids, processes=[_FakeProcess([0]), _FakeProcess([None, 1]), running]
    )
    monkeypatch.setattr(registration_test_lane.os, "cpu_count", lambda: 3)
    monkeypatch.setattr(registration_test_lane, "subprocess", fake)

    codes = registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND)

    assert codes is not None and any(codes)
    assert running.killed  # the hung worker is stopped as soon as another one fails


def test_a_hung_worker_times_out_and_fails_instead_of_waiting_forever(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_ids = "\n".join(f"tests/test_x.py::test_{index}" for index in range(4)).encode()
    hung = [_FakeProcess([None]), _FakeProcess([None])]
    fake, _ = _fake_subprocess(collected=node_ids, processes=hung)
    monkeypatch.setattr(registration_test_lane.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(registration_test_lane, "subprocess", fake)
    monkeypatch.setattr(registration_test_lane, "PARALLEL_RUN_TIMEOUT_SECONDS", 0)

    codes = registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND)

    assert codes == [registration_test_lane.TIMED_OUT_RETURNCODE] * 2
    assert all(process.killed for process in hung)


def test_parallel_run_falls_back_to_serial_when_it_cannot_start_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_ids = "\n".join(f"tests/test_x.py::test_{index}" for index in range(6)).encode()
    monkeypatch.setattr(registration_test_lane.os, "cpu_count", lambda: 3)

    # Collection fails or times out.
    for run in (
        lambda *_a, **_k: SimpleNamespace(returncode=2, stdout=b""),
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 1)),
    ):
        fake, _ = _fake_subprocess(collected=b"", run=run)
        monkeypatch.setattr(registration_test_lane, "subprocess", fake)
        assert registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND) is None

    # A command line that would exceed the Windows limit.
    monkeypatch.setattr(registration_test_lane, "MAX_COMMAND_LINE_CHARACTERS", 50)
    fake, launched = _fake_subprocess(collected=node_ids)
    monkeypatch.setattr(registration_test_lane, "subprocess", fake)
    assert registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND) is None
    assert launched == []
    monkeypatch.undo()

    # A worker that cannot be started: the ones already started are stopped.
    started = _FakeProcess([None])
    calls = {"count": 0}

    def popen(_args):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("WinError 206")
        return started

    monkeypatch.setattr(registration_test_lane.os, "cpu_count", lambda: 3)
    fake, _ = _fake_subprocess(collected=node_ids, popen=popen)
    monkeypatch.setattr(registration_test_lane, "subprocess", fake)
    assert registration_test_lane._run_focused_in_parallel(tmp_path, _LANE_COMMAND) is None
    assert started.killed

    # The full-suite lane is never split.
    monkeypatch.setattr(
        registration_test_lane, "subprocess",
        SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0)),
    )
    plan = {"test_lane": "full_suite", "pytest_commands": [_LANE_COMMAND, _LANE_COMMAND, _LANE_COMMAND]}
    assert registration_test_lane._run_pytest_commands(tmp_path, plan) == [0, 0, 0]


def test_a_parallel_failure_is_decided_by_the_serial_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = {"test_lane": "focused", "pytest_commands": [_LANE_COMMAND]}
    serial_calls: list[list[str]] = []

    def fake_serial(command, **_kwargs):
        serial_calls.append(list(command))
        return SimpleNamespace(returncode=serial_code["value"])

    serial_code = {"value": 0}
    monkeypatch.setattr(registration_test_lane, "subprocess", SimpleNamespace(run=fake_serial))

    # Parallel passes: the serial command is not needed.
    monkeypatch.setattr(registration_test_lane, "_run_focused_in_parallel", lambda *_a: [0, 0, 0])
    assert registration_test_lane._run_pytest_commands(tmp_path, plan) == [0, 0, 0]
    assert serial_calls == []

    # Parallel fails or times out (a worker killed on the way reports 124): serial decides.
    monkeypatch.setattr(registration_test_lane, "_run_focused_in_parallel", lambda *_a: [1, 124, 124])
    assert registration_test_lane._run_pytest_commands(tmp_path, plan) == [0]  # flaky-in-parallel, passes serially
    assert serial_calls == [_LANE_COMMAND]
    serial_code["value"] = 1
    assert registration_test_lane._run_pytest_commands(tmp_path, plan) == [1]  # a real failure still fails the gate
