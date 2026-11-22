"""Safety checks for the single GitHub-only Agent bridge (no production writes)."""
import json

import pytest

from trading_os import github_agent_runner as runner


def make_request(tmp_path, value):
    path = tmp_path / "agent-request.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_existing_registration_schema_still_accepted(tmp_path):
    req = {
        "schema_version": runner.SCHEMA,
        "operation": "preview",
        "registration_intake": {},
        "approval": None,
    }
    assert runner._read_request(make_request(tmp_path, req)) == req


@pytest.mark.parametrize("population", ["live", "paper", "backtest", "all"])
def test_analyze_explicit_population_and_read_only_schema(tmp_path, population):
    req = {
        "schema_version": runner.ANALYZE_SCHEMA, "operation": "analyze",
        "analysis_mode": "performance", "population": population, "strategy_id": None,
    }
    assert runner._read_request(make_request(tmp_path, req)) == req


@pytest.mark.parametrize("mutation", [
    {"population": None},
    {"population": "live_and_backtest"},
    {"operation": "commit"},
    {"approval": {"confirmed_in_chat": True}},
])
def test_analysis_refuses_implicit_population_or_write_fields(tmp_path, mutation):
    req = {
        "schema_version": runner.ANALYZE_SCHEMA, "operation": "analyze",
        "analysis_mode": "performance", "population": "live", "strategy_id": None,
    }
    req.update(mutation)
    with pytest.raises(ValueError):
        runner._read_request(make_request(tmp_path, req))


def test_review_preview_never_accepts_inband_approval(tmp_path):
    req = {
        "schema_version": runner.REVIEW_SCHEMA, "operation": "review_preview",
        "trade_id": "trd_1234", "review": {}, "approval": {"confirmed_in_chat": True},
    }
    with pytest.raises(ValueError):
        runner._read_request(make_request(tmp_path, req))


def test_review_commit_requires_owner_confirmation_before_staging():
    with pytest.raises(ValueError, match="authorization"):
        runner._review_commit({
            "trade_id": "trd_1234", "review": {}, "approval": None,
        })


def test_single_request_rejects_extra_changed_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_ACTOR", runner.EXPECTED_ACTOR)
    monkeypatch.setenv("GITHUB_REF_NAME", runner.INTAKE_REF)
    monkeypatch.setenv("EVENT_BEFORE", "a" * 40)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)

    def fake_git(_root, *args):
        if "--diff-filter=A" in args:
            return "agent-requests/test.json"
        return "agent-requests/test.json\ncanonical/lifecycle/events.jsonl"

    monkeypatch.setattr(runner, "git", fake_git)
    with pytest.raises(ValueError, match="no other changed files"):
        runner._single_new_request()


def test_synthetic_review_writer_first_commit_and_identical_retry(tmp_path, monkeypatch):
    """A disposable fixture tests the real Review Writer, never Owner's trade."""
    import copy
    import subprocess
    import sys
    from pathlib import Path
    from test_operational_closeout import _make_fixture
    from review_test_support import review_candidate
    from trading_os.review_session import preview_review
    from trading_os.operational_lifecycle import LifecycleStore
    import hashlib

    root = tmp_path / "synthetic-git"
    source = Path(__file__).resolve().parents[1]
    store, _, trade_id = _make_fixture(source, root, write_review=False, include_partial=False)
    review = review_candidate(store, trade_id, {
        "what_went_well": "synthetic case only",
        "what_went_wrong": "none observed",
        "rule_compliance": "compliant",
        "execution_deviation": "none observed",
        "thesis_validity": "synthetic setup",
        "entry_quality": "synthetic entry",
        "exit_quality": "synthetic exit",
        "evidence_completeness": "synthetic fixture",
        "owner_notes": "synthetic delegated approval ONLY; not a real Owner decision",
        "diagnostic_class": "METHOD",
    })

    def command(*args):
        return subprocess.run(["git", "-C", str(root), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    command("init", "--initial-branch=main")
    command("config", "user.name", "synthetic-fixture")
    command("config", "user.email", "synthetic@example.invalid")
    # Like the real repository, projections are tracked before any Review, so
    # the Writer may only update existing projection paths, never create them.
    from trading_os.projections import refresh_projections
    refresh_projections(root)
    command("add", "--all")
    command("commit", "-m", "isolated synthetic closed trade")
    trade_no = store.current()["records"][trade_id]["trade_no"]
    image_bytes = b"\x89PNG\r\n\x1a\nsynthetic new review original"
    # Original is committed before the Agent preview, not created by the Writer.
    image_rel = f"review-intake/{trade_no}/additional.png"
    image_src = root / image_rel
    image_src.parent.mkdir(parents=True)
    image_src.write_bytes(image_bytes)
    command("add", "--all")
    command("commit", "-m", "synthetic owner original image")
    image = {
        "path": image_rel,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "type": "chart_snapshot",
    }
    preview = runner._review_preview(
        {"trade_id": trade_id, "review": review, "images": [image]}, root=root
    )
    saved = {
        "status": "READY_FOR_OWNER_REVIEW_CONFIRMATION",
        "trade_id": trade_id,
        "trade_no": preview["trade_no"],
        "review_sha256": runner.digest(review),
        "preview_digest": preview["preview_digest"],
    }
    folder = root / "agent-receipts"
    folder.mkdir(exist_ok=True)
    (folder / "synthetic-preview.json").write_text(json.dumps(saved), encoding="utf-8")
    command("add", "--all")
    command("commit", "-m", "synthetic preview receipt")
    monkeypatch.setattr(runner, "ROOT", root)

    request = {
        "schema_version": runner.REVIEW_SCHEMA,
        "operation": "review_commit",
        "trade_id": trade_id,
        "review": review,
        "images": [image],
        "approval": {
            "preview_request": "synthetic-preview",
            "preview_digest": preview["preview_digest"],
            "confirmed_in_chat": True,
            "confirmed_at": "2026-10-11T04:00:00+08:00",
            "agent_id": "synthetic-agent-fixture",
            "owner_statement": "SYNTHETIC FIXTURE ONLY; NOT OWNER APPROVAL",
        },
    }
    real_run = subprocess.run

    def no_nested_pytest(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == [sys.executable, "-m", "pytest"]:
            return subprocess.CompletedProcess(cmd, 0, "synthetic fixture: separately CI tested", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", no_nested_pytest)
    first = runner._review_commit(copy.deepcopy(request))
    try:
        tmp, bare, work, receipt = first
        assert receipt["status"] == "REVIEW_WRITER_VERIFIED"
        assert receipt["appended_review_events"] == 1
        actual = LifecycleStore(work).current()["records"][trade_id]
        assert actual["state"] == "REVIEWED"
        assert (work / "canonical/evidence/artifacts" / (image["sha256"] + ".png")).is_file()
        assert any(item.get("sha256") == image["sha256"] for item in actual["evidence"])
        command("fetch", str(bare), "main")
        command("merge", "--ff-only", "FETCH_HEAD")
        events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
        numbers = (root / "canonical/sequences/numbers.json").read_bytes()
    finally:
        first[0].cleanup()
    second = runner._review_commit(copy.deepcopy(request))
    try:
        assert second[3]["status"] == "ALREADY_REVIEWED_VERIFIED"
        assert second[3]["appended_review_events"] == 0
        assert (second[2] / "canonical/lifecycle/events.jsonl").read_bytes() == events
        assert (second[2] / "canonical/sequences/numbers.json").read_bytes() == numbers
        assert LifecycleStore(second[2]).current()["records"][trade_id]["state"] == "REVIEWED"
    finally:
        second[0].cleanup()
    changed = copy.deepcopy(request)
    changed["review"]["owner_notes"] = "synthetic tampering after confirmation"
    with pytest.raises(ValueError, match="content or digest differs"):
        runner._review_commit(changed)
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes() == events
    assert (root / "canonical/sequences/numbers.json").read_bytes() == numbers


@pytest.mark.parametrize("path", [
    "../other.png", "review-intake/T999999/image.png",
    "review-intake/T000001/../../another.png",
    "review-intake/T000001/unapproved.txt",
])
def test_review_images_must_match_trade_and_git_original_path(tmp_path, path, monkeypatch):
    class FixtureStore:
        def __init__(self, root):
            self.root = root

        def current(self):
            return {"records": {"trd_fixture": {"trade_no": "T000001"}}}

    monkeypatch.setattr("trading_os.operational_lifecycle.LifecycleStore", FixtureStore)
    with pytest.raises(ValueError, match="GitHub-original"):
        runner._review_intake_manifest(
            {"trade_id": "trd_fixture", "images": [{
                "path": path, "sha256": "a" * 64, "type": "chart_snapshot",
            }]},
            tmp_path, tmp_path,
        )


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _intake_fixture(tmp_path):
    """origin (bare) with main and image-intake = main + an uploaded original."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "init", "-q", "--initial-branch=main", str(seed))
    for key, value in (("user.name", "owner"), ("user.email", "owner@example.invalid")):
        _git(seed, "config", key, value)
    (seed / "canonical").mkdir()
    (seed / "canonical/state.txt").write_text("v1\n", encoding="utf-8")
    _git(seed, "add", "--all")
    _git(seed, "commit", "-qm", "main v1")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "checkout", "-qb", "image-intake")
    (seed / "review-intake/T000001").mkdir(parents=True)
    (seed / "review-intake/T000001/chart.png").write_bytes(b"\x89PNG\r\n\x1a\nowner original")
    _git(seed, "add", "--all")
    _git(seed, "commit", "-qm", "owner upload")
    _git(seed, "push", "-q", "origin", "image-intake")
    # Main moves on independently (e.g. a local Agent's routine write).
    _git(seed, "checkout", "-q", "main")
    (seed / "canonical/state.txt").write_text("v2\n", encoding="utf-8")
    _git(seed, "commit", "-qam", "main v2")
    _git(seed, "push", "-q", "origin", "main")
    runner_root = tmp_path / "runner"
    _git(tmp_path, "clone", "-q", "--branch", "image-intake", str(origin), str(runner_root))
    return origin, runner_root


def test_intake_run_merges_main_and_publishes_write_to_main_and_intake(tmp_path, monkeypatch):
    origin, root = _intake_fixture(tmp_path)
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setenv("GITHUB_SHA", _git(root, "rev-parse", "HEAD"))
    runner._sync_intake_with_main()
    assert (root / "canonical/state.txt").read_text(encoding="utf-8") == "v2\n"
    assert (root / "review-intake/T000001/chart.png").is_file()

    tmp, bare, work = runner._staging_repository()
    try:
        for key, value in (("user.name", "writer"), ("user.email", "writer@example.invalid")):
            _git(work, "config", key, value)
        (work / "canonical/state.txt").write_text("v3 reviewed\n", encoding="utf-8")
        _git(work, "commit", "-qam", "Writer result")
        _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        request = root / "agent-requests/synthetic.json"
        runner._publish_receipt(request, {"status": "REVIEW_WRITER_VERIFIED"}, working=work, bare=bare)
    finally:
        tmp.cleanup()
    main_head = _git(origin, "rev-parse", "refs/heads/main")
    assert main_head == _git(origin, "rev-parse", "refs/heads/image-intake")
    assert _git(origin, "show", f"{main_head}:canonical/state.txt") == "v3 reviewed"
    assert "synthetic.json" in _git(origin, "ls-tree", "-r", "--name-only", main_head, "agent-receipts")


def test_intake_publish_refuses_when_main_moved_during_run(tmp_path, monkeypatch):
    origin, root = _intake_fixture(tmp_path)
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setenv("GITHUB_SHA", _git(root, "rev-parse", "HEAD"))
    runner._sync_intake_with_main()
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    for key, value in (("user.name", "local"), ("user.email", "local@example.invalid")):
        _git(other, "config", key, value)
    (other / "canonical/state.txt").write_text("concurrent\n", encoding="utf-8")
    _git(other, "commit", "-qam", "concurrent main write")
    _git(other, "push", "-q", "origin", "main")
    before = _git(origin, "rev-parse", "refs/heads/main")
    with pytest.raises(ValueError, match="main changed"):
        runner._publish_receipt(root / "agent-requests/x.json", {"status": "READY"},
                                working=root, bare=None)
    assert _git(origin, "rev-parse", "refs/heads/main") == before


@pytest.mark.parametrize("smuggled", ["canonical/lifecycle/events.jsonl", "trading_os/github_agent_runner.py"])
def test_sync_refuses_image_intake_changes_outside_intake_paths(tmp_path, monkeypatch, smuggled):
    """A direct edit committed on image-intake must never ride into main with a Writer publish."""
    origin, root = _intake_fixture(tmp_path)
    for key, value in (("user.name", "agent"), ("user.email", "agent@example.invalid")):
        _git(root, "config", key, value)
    target = root / smuggled
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("edited outside the Writer\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "direct edit on image-intake")
    _git(root, "push", "-q", "origin", "image-intake")
    monkeypatch.setattr(runner, "ROOT", root)
    with pytest.raises(ValueError, match="outside intake-only paths"):
        runner._sync_intake_with_main()


@pytest.mark.parametrize("operation", ["rename_into_intake", "delete"])
def test_sync_refuses_renames_or_deletions_of_main_files(tmp_path, monkeypatch, operation):
    """Moving a main file into an intake path must still be seen as removing it from main."""
    origin, root = _intake_fixture(tmp_path)
    for key, value in (("user.name", "agent"), ("user.email", "agent@example.invalid")):
        _git(root, "config", key, value)
    _git(root, "fetch", "-q", "origin", "main")
    _git(root, "merge", "-q", "--no-edit", "origin/main")
    if operation == "rename_into_intake":
        (root / "agent-receipts").mkdir(exist_ok=True)
        _git(root, "mv", "canonical/state.txt", "agent-receipts/state.json")
    else:
        _git(root, "rm", "-q", "canonical/state.txt")
    _git(root, "commit", "-qm", operation)
    _git(root, "push", "-q", "origin", "image-intake")
    monkeypatch.setattr(runner, "ROOT", root)
    with pytest.raises(ValueError, match="canonical/state.txt"):
        runner._sync_intake_with_main()


def test_runner_only_accepts_requests_on_image_intake(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTOR", runner.EXPECTED_ACTOR)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    with pytest.raises(ValueError, match="image-intake"):
        runner._single_new_request()


def test_synthetic_backtest_preview_commit_and_identical_retry(tmp_path, monkeypatch):
    """GitHub-only BACKTEST uses the shared service; never Owner's real data."""
    import copy
    import subprocess
    from test_backtest_registration import _fixture, APPROVAL
    from trading_os.projections import refresh_projections
    from trading_os.operational_lifecycle import LifecycleStore

    root, intake = _fixture(tmp_path)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    for key, value in (("user.name", "synthetic"), ("user.email", "synthetic@example.invalid")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    refresh_projections(root)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "synthetic base with committed originals"], check=True)
    monkeypatch.setattr(runner, "ROOT", root)

    req = {"schema_version": runner.BACKTEST_SCHEMA, "operation": "backtest_preview",
           "backtest_intake": intake}
    assert runner._read_request(make_request(tmp_path, req)) == req
    preview = runner._backtest_preview(req)
    assert preview["status"] == "READY_FOR_OWNER_BACKTEST_CONFIRMATION"
    assert preview["expected_trade_no"] == "BT000001"
    (root / "agent-receipts").mkdir()
    (root / "agent-receipts/bt-preview.json").write_text(json.dumps(preview), encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "preview receipt"], check=True)

    commit = {"schema_version": runner.BACKTEST_SCHEMA, "operation": "backtest_commit",
              "backtest_intake": intake,
              "approval": {"preview_request": "bt-preview", "preview_digest": preview["preview_digest"],
                           **{k: APPROVAL[k] for k in ("confirmed_in_chat", "confirmed_at", "agent_id", "owner_statement")}}}
    tmp, bare, work, receipt = runner._backtest_commit(copy.deepcopy(commit))
    try:
        assert receipt["status"] == "BACKTEST_REGISTERED_VERIFIED"
        assert receipt["trade_no"] == "BT000001"
        record = LifecycleStore(work).current()["records"][receipt["trade_id"]]
        assert record["trade_type"] == "backtest" and record.get("provider_pnl") is None
        subprocess.run(["git", "-C", str(root), "fetch", "-q", str(bare), "main"], check=True)
        subprocess.run(["git", "-C", str(root), "merge", "-q", "--ff-only", "FETCH_HEAD"], check=True)
    finally:
        tmp.cleanup()
    events = (root / "canonical/lifecycle/events.jsonl").read_bytes()
    tmp, bare, work, again = runner._backtest_commit(copy.deepcopy(commit))
    try:
        assert again["status"] == "ALREADY_REGISTERED_VERIFIED" and again["trade_no"] == "BT000001"
        assert (work / "canonical/lifecycle/events.jsonl").read_bytes() == events
    finally:
        tmp.cleanup()
    # Same original images, other prices: still one sample, and the receipt says the plan differs.
    reread = copy.deepcopy(intake)
    reread["prices"]["stop"]["value"] = 96.0
    same = runner._backtest_preview({"backtest_intake": reread})
    assert same["status"] == "ALREADY_REGISTERED" and same["existing_plan_matches"] is False
    assert runner._backtest_preview({"backtest_intake": intake})["existing_plan_matches"] is True
    tampered = copy.deepcopy(commit)
    tampered["backtest_intake"]["owner_verbatim"] = "changed after Owner approval"
    with pytest.raises(ValueError, match="differs from Owner preview"):
        runner._backtest_commit(tampered)
    no_approval = copy.deepcopy(commit)
    no_approval["approval"]["confirmed_in_chat"] = False
    with pytest.raises(ValueError, match="Owner BACKTEST decision"):
        runner._backtest_commit(no_approval)


def test_registration_preview_receipt_retains_sync_warning_outside_owner_digest():
    data = {
        "status": "READY_FOR_OWNER_CONFIRMATION",
        "preview_digest": "a" * 64,
        "strategy_resolution": {"status": "MATCHED"},
        "registration_state": {"status": "UNREGISTERED"},
        "provider_facts": {"provider": "propw"},
        "image_evidence": [],
        "canonical_capture": {},
        "provider_sync_recency": {
            "last_successful_sync_at": None,
            "currentness": "not_asserted",
        },
        "provider_sync_guidance": {
            "live_sync_performed": False,
            "sync_capability": "credentialed_local_agent_only",
        },
    }
    intake = {"provider": "propw", "provider_lifecycle_id": "fixture"}
    receipt = runner._registration_preview_receipt(data, intake)
    assert receipt["preview_digest"] == "a" * 64
    assert receipt["intake_sha256"] == runner.digest(intake)
    assert receipt["provider_sync_recency"]["currentness"] == "not_asserted"
    assert receipt["provider_sync_guidance"]["live_sync_performed"] is False


def test_missing_original_sl_preview_writes_actionable_receipt_without_registration(monkeypatch, tmp_path):
    from trading_os.registration_packet import OwnerInputRequired, ORIGINAL_SL_QUESTION

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    request_path = tmp_path / "agent-requests/needs-original-sl.json"
    request_path.parent.mkdir(parents=True)
    request = {
        "operation": "preview", "approval": None,
        "registration_intake": {"provider": "propw", "provider_lifecycle_id": "fixture"},
    }
    monkeypatch.setattr(runner, "_single_new_request", lambda: request_path)
    monkeypatch.setattr(runner, "_read_request", lambda _: request)
    monkeypatch.setattr(runner, "_sync_intake_with_main", lambda: None)

    def ask_sl(*_args, **_kwargs):
        raise OwnerInputRequired(ORIGINAL_SL_QUESTION)

    monkeypatch.setattr(runner, "_preview", ask_sl)
    published = []
    monkeypatch.setattr(
        runner, "_publish_receipt",
        lambda path, receipt, **_kw: published.append((path, receipt)),
    )
    runner.main()
    assert len(published) == 1
    assert published[0][0] == request_path
    receipt = published[0][1]
    assert receipt["status"] == "OWNER_INPUT_REQUIRED"
    assert "原始停損（SL）" in receipt["owner_question"]
    assert receipt["preview_digest"] is None
    assert receipt["owner_confirmation_allowed"] is False
    assert receipt["provider_sync_recency"]["currentness"] == "not_asserted"


@pytest.mark.parametrize("path, expected", [
    ("canonical/lifecycle/events.lock", True),
    ("canonical/sequences/numbers.lock", True),
    ("canonical/sequences/finalizations.lock", True),
    ("canonical/registration/backtest-commit.lock", True),
    ("canonical/lifecycle/events.jsonl", False),          # content, never excluded
    ("canonical/evidence/artifacts/" + "a" * 64 + ".png", False),
    ("Research/notes.lock", False),                          # only the canonical write locks are exempt
    ("agent-receipts/x.lock", False),
    # Review finding: a real content file that merely ends in .lock must still be seen.
    ("canonical/evidence/artifacts/" + "a" * 64 + ".lock", False),
    ("canonical/lifecycle/other.lock", False),
    ("canonical/lifecycle/events.lock.bak", False),
])
def test_only_the_canonical_write_lock_files_are_exempt_from_the_created_files_check(path, expected):
    assert runner.is_write_lock_file(path) is expected
