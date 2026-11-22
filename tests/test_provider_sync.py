"""Provider Sync pipeline: fetch -> sanitized ledger -> census -> optional commit/push (fail-closed)."""

from __future__ import annotations

import json
import sqlite3
import shutil
import subprocess
from pathlib import Path

import pytest

from trading_os.provider_sync import ProviderSyncError, run_provider_sync

TODAY = "20261007"
BRANCH = f"data/propw-ledger-{TODAY}"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "tester")
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "ingestion" / "propw" / "data").mkdir(parents=True)
    (root / "ingestion" / "propw" / "data" / "propw.sqlite3").write_bytes(b"")
    (root / "canonical" / "sequences").mkdir(parents=True)
    (root / "canonical" / "sequences" / "finalizations.json").write_text(json.dumps({"records": {}}), encoding="utf-8")
    # the real propw source is needed for the sanitizer re-scan
    real = Path(__file__).resolve().parents[1] / "ingestion" / "propw" / "src"
    shutil.copytree(real, root / "ingestion" / "propw" / "src", ignore=shutil.ignore_patterns("__pycache__"))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _lifecycle(open_id, entry_time="2025-06-04T01:49:13Z"):
    return {"open_id": open_id, "lifecycle": {"open_id": open_id, "instrument": "BTC", "direction": "long", "entry_time": entry_time, "entry_price": "100", "exit_time": "2025-06-04T02:00:00Z", "weighted_exit_price": "101", "open_size": "1", "closed_size": "1", "is_closed": 1, "is_partial": 0, "normalization_status": "ok", "provider_lifecycle_phase": "CLOSED"}}


def make_exporter(open_ids, bad_key=None):
    def exporter(root, db):
        ledger = root / "provider-ledgers" / "propw"
        (ledger / "normalized").mkdir(parents=True, exist_ok=True)
        rows = [_lifecycle(o) for o in open_ids]
        if bad_key:
            rows[0]["lifecycle"][bad_key] = "secret-value"
        (ledger / "normalized" / "trade-lifecycles.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return {"changed": True, "added": len(rows), "updated": 0, "counts": {"trade_lifecycles": len(rows)}}

    return exporter


def sync(root, exporter, **kwargs):
    return run_provider_sync(root, "propw", exporter=exporter, today=TODAY, **kwargs)


def commit_pr_projection_inputs(repo, analytics_data=None):
    analytics = repo / "exports" / "operational" / "analytics" / "current.json"
    health = repo / "exports" / "operational" / "health" / "current.json"
    analytics.parent.mkdir(parents=True, exist_ok=True)
    health.parent.mkdir(parents=True, exist_ok=True)
    analytics.write_text(json.dumps(analytics_data or {}), encoding="utf-8")
    health.write_text("{}", encoding="utf-8")
    git(repo, "add", "exports/operational/analytics/current.json", "exports/operational/health/current.json")
    git(repo, "commit", "-q", "-m", "add committed Trade View inputs")


def fake_projection_refresh(root):
    """Write deterministic generated outputs for PR-scope tests."""
    from trading_os.operational_analytics import provider_fee_index

    analytics_path = root / "exports/operational/analytics/current.json"
    try:
        analytics = json.loads(analytics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        analytics = {}
    analytics.setdefault("source_version_pins", {})["provider_fee_sources"] = provider_fee_index(root)[1]
    analytics_path.parent.mkdir(parents=True, exist_ok=True)
    analytics_path.write_text(json.dumps(analytics, sort_keys=True) + "\n", encoding="utf-8")

    generated = {
        "PERFORMANCE.md": "generated performance\n",
        "exports/operational/analytics/manifest.json": "{}\n",
        "exports/operational/health/manifest.json": "{}\n",
        "exports/operational/health/policy.json": "{}\n",
        "exports/operational/handoff/current.json": "{}\n",
        "exports/operational/handoff/manifest.json": "{}\n",
        "exports/operational/trade-view/index.html": "<main>view</main>\n",
        "exports/operational/trade-view/style.css": ".view {}\n",
        "exports/operational/trade-view/app.js": "// Census panel\n",
        "exports/operational/trade-view/trade-view-data.json": '{"registration_census":{"freshness":{"status":"fresh"}}}\n',
        "exports/operational/trade-view/manifest.json": '{"registration_census_freshness":"fresh"}\n',
    }
    for relative, content in generated.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "exports/operational/health/current.json").write_text("{}\n", encoding="utf-8")
    (root / "exports/operational/analytics/manifest.json").write_text("{}\n", encoding="utf-8")
    return {
        "status": "CURRENT",
        "refresh_order": ["analytics", "health", "registration_census", "trade_view", "handoff"],
        "verification": {"analytics_artifact_digest": "analytics-sha", "health_artifact_digest": "health-sha"},
        "trade_view": {
            "data_sha256": "data-sha",
            "registration_census_source_identity_sha256": "source-sha",
            "registration_census_freshness": "fresh",
        },
    }


def add_origin_with_main(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    pushed = git(repo, "push", "-u", "origin", "main")
    assert pushed.returncode == 0
    return remote


def test_prepare_only_writes_files_but_does_not_touch_git(repo):
    report = sync(repo, make_exporter(["1", "2"]))
    assert report["status"] == "READY_TO_COMMIT" and report["steps"]["census"]["counts"]["UNREGISTERED"] == 2
    assert report["steps"]["census"]["unregistered_candidates"] == 2
    assert report["steps"]["census"]["provider_phase_counts"]["CLOSED"] == 2
    assert report["steps"]["census"]["freshness"]["status"] == "fresh"
    assert "canonical_finalizations_sha256" in report["steps"]["census"]["source_digests"]
    assert (repo / "provider-ledgers" / "propw" / "registration-census.json").exists()
    assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"


def test_provider_recency_is_read_from_local_sqlite_without_fetch_or_write(repo):
    db_path = repo / "ingestion" / "propw" / "data" / "propw.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, finished_at TEXT, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO sync_runs (id, finished_at, status) VALUES (?, ?, ?)",
            [
                (1, "2026-10-01T12:00:00Z", "ok"),
                (2, "2026-10-09T12:00:00Z", "failed"),
            ],
        )
    before = db_path.read_bytes()

    report = sync(repo, make_exporter(["1"]))

    recency = report["steps"]["provider_sync_recency"]
    assert recency["status"] == "recorded"
    assert recency["last_successful_sync_at"] == "2026-10-01T12:00:00Z"
    assert recency["fetched_this_run"] is False
    assert recency["currentness"] == "not_asserted"
    assert db_path.read_bytes() == before
    saved = json.loads((repo / "provider-ledgers/propw/provider-sync-status.json").read_text(encoding="utf-8"))
    assert saved["status"] == "recorded"
    assert "source_digests" not in saved


def test_commit_uses_a_data_branch_and_only_the_ledger_paths(repo):
    (repo / "unrelated.txt").write_text("keep me", encoding="utf-8")  # unrelated local work must stay untouched
    report = sync(repo, make_exporter(["1", "2"]), commit=True)
    assert report["status"] == "COMMITTED" and report["steps"]["git"]["branch"] == BRANCH
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == BRANCH
    files = git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
    assert files and all(f.startswith("provider-ledgers/propw/") for f in files)
    assert (repo / "unrelated.txt").read_text(encoding="utf-8") == "keep me"
    assert "unrelated.txt" in git(repo, "status", "--porcelain").stdout  # still untracked, not committed
    assert git(repo, "log", "main", "--oneline").stdout.count("\n") == 1  # main untouched


def test_second_run_is_no_change_and_makes_no_commit(repo):
    sync(repo, make_exporter(["1", "2"]), commit=True)
    before = git(repo, "rev-parse", "HEAD").stdout
    report = sync(repo, make_exporter(["1", "2"]), commit=True)
    assert report["status"] == "NO_CHANGE" and git(repo, "rev-parse", "HEAD").stdout == before


def test_malformed_normalized_ledger_fails_before_census_replacement(repo):
    sync(repo, make_exporter(["1"]))
    census = repo / "provider-ledgers" / "propw" / "registration-census.json"
    previous_census = census.read_bytes()

    def malformed_exporter(root, db):
        ledger = root / "provider-ledgers" / "propw" / "normalized" / "trade-lifecycles.jsonl"
        ledger.write_text('{"open_id": "1", "lifecycle": ', encoding="utf-8")
        return {"changed": True, "added": 0, "updated": 1, "counts": {"trade_lifecycles": 1}}

    with pytest.raises(json.JSONDecodeError):
        sync(repo, malformed_exporter)
    assert census.read_bytes() == previous_census


def test_new_lifecycle_shows_up_in_census_delta_and_a_new_commit(repo):
    sync(repo, make_exporter(["1"]), commit=True)
    report = sync(repo, make_exporter(["1", "2"]), commit=True)
    assert report["steps"]["census"]["previous_counts"]["UNREGISTERED"] == 1 and report["steps"]["census"]["counts"]["UNREGISTERED"] == 2
    assert report["status"] == "COMMITTED"


def test_unrelated_staged_files_block_the_commit(repo):
    (repo / "other.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "other.txt")
    with pytest.raises(ProviderSyncError, match="unrelated files are already staged"):
        sync(repo, make_exporter(["1"]), commit=True)
    assert git(repo, "log", "--oneline").stdout.count("\n") == 1


@pytest.mark.parametrize("bad_key", ["Authorization", "loginToken", "session"])
def test_credential_like_ledger_content_aborts_before_any_commit(repo, bad_key):
    with pytest.raises(ProviderSyncError, match="credential-like"):
        sync(repo, make_exporter(["1"], bad_key=bad_key), commit=True)
    assert git(repo, "log", "--oneline").stdout.count("\n") == 1


def test_fetch_failure_stops_before_export(repo):
    def failing_fetch(root, db):
        raise ProviderSyncError("propw sync failed (exit 1): auth")

    exported = []

    def exporter(root, db):
        exported.append(1)
        return {}

    with pytest.raises(ProviderSyncError, match="propw sync failed"):
        run_provider_sync(repo, "propw", fetch=True, fetcher=failing_fetch, exporter=exporter, today=TODAY)
    assert exported == []


def test_push_requires_commit_and_only_pushes_the_data_branch(repo, tmp_path):
    with pytest.raises(ProviderSyncError, match="--push requires --commit"):
        sync(repo, make_exporter(["1"]), push=True)
    remote = add_origin_with_main(repo, tmp_path)
    report = sync(repo, make_exporter(["1"]), commit=True, push=True)
    assert report["steps"]["git"]["pushed"] is True
    assert set(git(remote, "branch", "--list").stdout.split()) == {BRANCH, "main"}


def test_push_refuses_local_main_ahead_of_origin_when_it_contains_out_of_scope_paths(repo, tmp_path):
    remote = add_origin_with_main(repo, tmp_path)
    (repo / "README.md").write_text("local main-only change\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "unpublished local main change")
    before = git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ProviderSyncError, match="out-of-scope paths"):
        sync(repo, make_exporter(["1"]), commit=True, push=True)

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert git(remote, "branch", "--list").stdout.split() == ["main"]


def test_missing_database_is_a_clear_error(repo):
    (repo / "ingestion" / "propw" / "data" / "propw.sqlite3").unlink()
    with pytest.raises(ProviderSyncError, match="database not found"):
        sync(repo, make_exporter(["1"]))


def test_commit_is_refused_on_any_other_feature_branch(repo):
    git(repo, "switch", "-q", "-c", "feat/foo")
    head = git(repo, "rev-parse", "HEAD").stdout
    with pytest.raises(ProviderSyncError, match="refusing to commit provider data on branch 'feat/foo'"):
        sync(repo, make_exporter(["1"]), commit=True)
    assert git(repo, "rev-parse", "HEAD").stdout == head and git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feat/foo"
    assert not (repo / "provider-ledgers").exists()  # failed closed before exporting anything
    with pytest.raises(ProviderSyncError, match="refusing to commit provider data"):
        sync(repo, make_exporter(["1"]), commit=True, branch="data/other")  # an explicit branch does not bypass it either


def test_commit_continues_when_already_on_the_exact_data_branch(repo):
    git(repo, "switch", "-q", "-c", BRANCH)
    report = sync(repo, make_exporter(["1"]), commit=True)
    assert report["status"] == "COMMITTED" and report["steps"]["git"]["branch"] == BRANCH


def test_provider_data_target_cannot_be_main(repo):
    with pytest.raises(ProviderSyncError, match="refusing provider data target"):
        sync(repo, make_exporter(["1"]), commit=True, branch="main")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert not (repo / "provider-ledgers").exists()


def test_create_pr_requires_explicit_push(repo):
    with pytest.raises(ProviderSyncError, match="--create-pr requires --push"):
        sync(repo, make_exporter(["1"]), create_pr=True)
    assert not (repo / "provider-ledgers").exists()


@pytest.mark.parametrize(
    ("dirty_path", "analytics_data", "error_match"),
    [
        ("canonical/registration/pending-strategy.jsonl", None, "clean committed read-model source paths"),
        ("registry/strategy-registry.yaml", None, "clean committed read-model source paths"),
        (
            "canonical/evidence/artifacts/untracked.png",
            {"observations": [{"evidence_artifacts": [{"locator": "artifact:untracked.png"}]}]},
            "not committed on origin/main",
        ),
    ],
)
def test_create_pr_rejects_uncommitted_read_model_sources_before_export(repo, tmp_path, dirty_path, analytics_data, error_match):
    pending = repo / dirty_path
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("uncommitted source\n", encoding="utf-8")
    commit_pr_projection_inputs(repo, analytics_data)
    add_origin_with_main(repo, tmp_path)
    exported = []

    def exporter(root, db):
        exported.append(True)
        return make_exporter(["1"])(root, db)

    with pytest.raises(ProviderSyncError, match=error_match):
        sync(repo, exporter, commit=True, push=True, create_pr=True)
    assert exported == []
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert not (repo / "provider-ledgers" / "propw").exists()


def test_unreferenced_untracked_evidence_does_not_block_pr_source_validation(repo, tmp_path):
    from trading_os.provider_sync import _ensure_pr_read_model_sources_clean

    commit_pr_projection_inputs(repo)
    add_origin_with_main(repo, tmp_path)
    artifact = repo / "canonical" / "evidence" / "artifacts" / "unreferenced.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"preserve this unrelated evidence")

    _ensure_pr_read_model_sources_clean(repo)

    assert artifact.read_bytes() == b"preserve this unrelated evidence"


def test_referenced_evidence_missing_locally_and_from_main_blocks_pr_source_validation(repo, tmp_path):
    from trading_os.provider_sync import _ensure_pr_read_model_sources_clean

    commit_pr_projection_inputs(
        repo,
        {"observations": [{"evidence_artifacts": [{"locator": "artifact:missing.png"}]}]},
    )
    add_origin_with_main(repo, tmp_path)

    with pytest.raises(ProviderSyncError, match="missing locally and from origin/main"):
        _ensure_pr_read_model_sources_clean(repo)

    assert not (repo / "canonical/evidence/artifacts/missing.png").exists()


@pytest.mark.parametrize("source_kind", ["canonical_record", "lifecycle_projection"])
@pytest.mark.parametrize("guard", ["projection", "pr"])
def test_evidence_added_to_canonical_sources_is_checked_even_when_old_analytics_omits_it(
    repo, tmp_path, source_kind, guard
):
    from trading_os.provider_sync import (
        _ensure_pr_read_model_sources_clean,
        _ensure_projection_sources_clean,
    )

    commit_pr_projection_inputs(repo, {"observations": [], "canonical_read_model": []})
    artifact_name = f"{source_kind}.png"
    artifact = repo / "canonical" / "evidence" / "artifacts" / artifact_name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"committed evidence")
    locator = f"artifact:{artifact_name}"
    if source_kind == "canonical_record":
        source_path = repo / "canonical" / "records" / "trades" / "canonical-only.json"
        source_data = {"trade_id": "trd-canonical-only", "evidence": [{"locator": locator}]}
    else:
        source_path = repo / "canonical" / "lifecycle" / "current.json"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_data = {"records": {"trd-lifecycle-only": {"evidence": [{"locator": locator}]}}}
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(json.dumps(source_data), encoding="utf-8")
    git(repo, "add", source_path.relative_to(repo).as_posix(), artifact.relative_to(repo).as_posix())
    git(repo, "commit", "-q", "-m", "add canonical evidence source")
    if guard == "pr":
        add_origin_with_main(repo, tmp_path)

    # The exact path is omitted from the current analytics read model. A local
    # byte change must still fail because the upcoming refresh reads the newer
    # canonical record/lifecycle source directly.
    artifact.write_bytes(b"uncommitted evidence change")
    check = _ensure_pr_read_model_sources_clean if guard == "pr" else _ensure_projection_sources_clean
    expected = "differs from origin/main" if guard == "pr" else "committed evidence bytes"
    with pytest.raises(ProviderSyncError, match=expected):
        check(repo)


def test_commit_refuses_dirty_projection_destinations_before_export(repo):
    commit_pr_projection_inputs(repo)
    output = repo / "PERFORMANCE.md"
    output.write_text("committed projection\n", encoding="utf-8")
    git(repo, "add", "PERFORMANCE.md")
    git(repo, "commit", "-q", "-m", "add projection destination")
    output.write_text("preserve owner edit\n", encoding="utf-8")
    before = output.read_bytes()
    exported = []

    def exporter(root, db):
        exported.append(True)
        return make_exporter(["1"])(root, db)

    with pytest.raises(ProviderSyncError, match="clean tracked generated output paths"):
        sync(repo, exporter, commit=True)

    assert exported == []
    assert output.read_bytes() == before


@pytest.mark.parametrize("guard", ["projection", "pr"])
def test_invalid_lifecycle_projection_event_evidence_is_checked_before_refresh(repo, tmp_path, guard):
    from trading_os.provider_sync import (
        _ensure_pr_read_model_sources_clean,
        _ensure_projection_sources_clean,
    )

    commit_pr_projection_inputs(repo, {"observations": [], "canonical_read_model": []})
    artifact_name = "event-only.png"
    artifact = repo / "canonical" / "evidence" / "artifacts" / artifact_name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"committed lifecycle event evidence")
    events_path = repo / "canonical" / "lifecycle" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_version": "canonical-lifecycle-event.v2",
        "event_id": "lev_test_event_evidence",
        "event_type": "evidence_attached",
        "idempotency_key": "evidence:test:event-only",
        "occurred_at": "2026-10-25T00:00:00Z",
        "payload": {"evidence": {"locator": f"artifact:{artifact_name}"}},
    }
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    current_path = repo / "canonical" / "lifecycle" / "current.json"
    current_path.write_text(json.dumps({"schema_version": "obsolete", "records": {}}), encoding="utf-8")
    git(repo, "add", events_path.relative_to(repo).as_posix(), current_path.relative_to(repo).as_posix(), artifact.relative_to(repo).as_posix())
    git(repo, "commit", "-q", "-m", "add lifecycle event evidence source")
    if guard == "pr":
        add_origin_with_main(repo, tmp_path)

    # The invalid current projection triggers LifecycleStore.current()'s event
    # reconstruction path; the old analytics and current projection omit this
    # locator, but the committed event log still supplies it to refresh.
    artifact.write_bytes(b"uncommitted lifecycle evidence change")
    check = _ensure_pr_read_model_sources_clean if guard == "pr" else _ensure_projection_sources_clean
    expected = "differs from origin/main" if guard == "pr" else "committed evidence bytes"
    with pytest.raises(ProviderSyncError, match=expected):
        check(repo)


def test_commit_refuses_ignored_untracked_projection_destination(repo):
    commit_pr_projection_inputs(repo)
    (repo / ".gitignore").write_text("PERFORMANCE.md\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore untracked projection output")
    output = repo / "PERFORMANCE.md"
    output.write_text("preserve ignored owner file\n", encoding="utf-8")
    before = output.read_bytes()
    exported = []

    def exporter(root, db):
        exported.append(True)
        return make_exporter(["1"])(root, db)

    with pytest.raises(ProviderSyncError, match="clean tracked generated output paths"):
        sync(repo, exporter, commit=True)

    assert exported == []
    assert output.read_bytes() == before


def test_prepare_then_commit_defers_projection_writes_until_publish(repo, monkeypatch):
    from trading_os import provider_sync

    commit_pr_projection_inputs(repo)
    refreshes = []
    monkeypatch.setattr(provider_sync, "refresh_projections", lambda root: (refreshes.append(root) or fake_projection_refresh(root)))

    prepared = sync(repo, make_exporter(["1"]))
    analytics = repo / "exports/operational/analytics/current.json"
    assert prepared["status"] == "READY_TO_COMMIT"
    assert prepared["steps"]["projection_refresh"]["status"] == "DEFERRED"
    assert prepared["steps"]["trade_view"]["status"] == "DEFERRED"
    assert json.loads(analytics.read_text(encoding="utf-8")) == {}
    assert refreshes == []

    committed = sync(repo, make_exporter(["1"]), commit=True)

    assert committed["status"] == "COMMITTED"
    assert committed["steps"]["projection_refresh"]["status"] == "CURRENT"
    assert len(refreshes) == 1
    changed = set(git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split())
    assert "exports/operational/analytics/current.json" in changed
    assert "exports/operational/health/current.json" in changed
    assert "exports/operational/handoff/current.json" in changed
    assert "exports/operational/trade-view/trade-view-data.json" in changed
    assert "PERFORMANCE.md" in changed


def test_create_pr_rejects_existing_data_branch_with_out_of_scope_commit(repo, tmp_path, monkeypatch):
    from trading_os import provider_sync

    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)
    git(repo, "switch", "-q", "-c", BRANCH)
    (repo / "unrelated.txt").write_text("must not appear in provider PR\n", encoding="utf-8")
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-q", "-m", "unrelated change on reused data branch")
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    real_run = provider_sync._run
    gh_calls = []

    def fake_run(args, *, cwd, env=None):
        if args[0] == "gh":
            gh_calls.append(args)
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setattr(provider_sync, "refresh_projections", fake_projection_refresh)
    monkeypatch.setattr(provider_sync, "verify_projections", lambda *_args, **_kwargs: {
        "status": "CURRENT", "registration_census_freshness": "fresh",
        "registration_census_source_identity_sha256": "source-sha", "registration_census_candidate_count": 1,
    })
    monkeypatch.setattr(provider_sync, "_run", fake_run)

    with pytest.raises(ProviderSyncError, match="out-of-scope paths"):
        sync(repo, make_exporter(["1"]), commit=True, push=True, create_pr=True)

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert git(remote, "branch", "--list").stdout.split() == ["main"]
    assert gh_calls == []


def test_owner_data_pr_contains_fresh_trade_view_without_canonical_paths(repo, tmp_path, monkeypatch):
    from trading_os import provider_sync

    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)

    def build_view(root):
        output = root / "exports" / "operational" / "trade-view"
        output.mkdir(parents=True, exist_ok=True)
        data = output / "trade-view-data.json"
        manifest = output / "manifest.json"
        (output / "index.html").write_text("<main>view</main>\n", encoding="utf-8")
        (output / "style.css").write_text(".view {}\n", encoding="utf-8")
        (output / "app.js").write_text("// Census panel\n", encoding="utf-8")
        data.write_text('{"registration_census":{"freshness":{"status":"fresh"}}}\n', encoding="utf-8")
        manifest.write_text('{"registration_census_freshness":"fresh"}\n', encoding="utf-8")
        return {"data_sha256": "data-sha", "registration_census_source_identity_sha256": "source-sha", "registration_census_freshness": "fresh"}

    monkeypatch.setattr(provider_sync, "refresh_projections", fake_projection_refresh)
    monkeypatch.setattr(provider_sync, "build_trade_view", build_view)
    monkeypatch.setattr(provider_sync, "verify_projections", lambda *_args, **_kwargs: {
        "status": "CURRENT", "registration_census_freshness": "fresh",
        "registration_census_source_identity_sha256": "source-sha", "registration_census_candidate_count": 1,
    })
    real_run = provider_sync._run
    calls = []
    pr_created = False
    allow_create = False

    def fake_run(args, *, cwd, env=None):
        nonlocal pr_created
        if args[0] == "gh":
            calls.append(args)
            if args[1:3] == ["pr", "list"]:
                existing = "https://github.com/owner/repo/pull/123\n" if pr_created else ""
                return subprocess.CompletedProcess(args, 0, existing, "")
            if not allow_create:
                return subprocess.CompletedProcess(args, 1, "", "temporary GitHub CLI interruption")
            pr_created = True
            return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo/pull/123\n", "")
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setattr(provider_sync, "_run", fake_run)
    with pytest.raises(ProviderSyncError, match="could not create provider-data PR"):
        sync(repo, make_exporter(["1"]), commit=True, push=True, create_pr=True)
    first_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == BRANCH
    assert set(git(remote, "branch", "--list").stdout.split()) == {BRANCH, "main"}

    allow_create = True
    report = sync(repo, make_exporter(["1"]), commit=True, push=True, create_pr=True)

    assert report["steps"]["trade_view"]["census_freshness"] == "fresh"
    assert report["steps"]["projection_readback"]["status"] == "CURRENT"
    assert report["steps"]["pull_request"] == {"url": "https://github.com/owner/repo/pull/123", "created": True}
    assert report["status"] == "NO_CHANGE"
    assert report["steps"]["git"]["committed"] is False
    assert report["steps"]["git"]["pushed"] is True
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == first_commit

    existing = sync(repo, make_exporter(["1"]), commit=True, push=True, create_pr=True)
    assert existing["steps"]["pull_request"] == {"url": "https://github.com/owner/repo/pull/123", "created": False}
    assert existing["steps"]["git"]["pushed"] is True

    committed = git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
    assert "PERFORMANCE.md" in committed
    assert "exports/operational/analytics/current.json" in committed
    assert "exports/operational/health/current.json" in committed
    assert "exports/operational/handoff/current.json" in committed
    assert "exports/operational/trade-view/trade-view-data.json" in committed
    assert "exports/operational/trade-view/manifest.json" in committed
    assert "exports/operational/trade-view/app.js" in committed
    assert "exports/operational/trade-view/index.html" in committed
    assert "exports/operational/trade-view/style.css" in committed
    assert all(not path.startswith("canonical/") for path in committed)
    assert all(path.startswith(("provider-ledgers/propw/", "exports/operational/")) or path == "PERFORMANCE.md" for path in committed)
    assert [call[1:3] for call in calls] == [["pr", "list"], ["pr", "create"], ["pr", "list"], ["pr", "create"], ["pr", "list"]]
    create_call = next(call for call in calls if call[1:3] == ["pr", "create"])
    assert "--base" in create_call and create_call[create_call.index("--base") + 1] == "main"


def test_provider_fee_source_change_refreshes_projection_closure_before_data_pr(tmp_path, monkeypatch):
    from test_operational_closeout import _make_fixture, FIXED_GENERATED_AT, FIXED_SOURCE_COMMIT
    from trading_os.operational_analytics import provider_fee_index
    from trading_os.projections import refresh_projections

    root = tmp_path / "projection-repo"
    root.mkdir()
    source = Path(__file__).resolve().parents[1]
    _make_fixture(source, root)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    db = root / "ingestion/propw/data/propw.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    shutil.copytree(source / "ingestion/propw/src", root / "ingestion/propw/src", ignore=shutil.ignore_patterns("__pycache__"))
    ledger = root / "provider-ledgers/propw/normalized/trade-lifecycles.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("", encoding="utf-8")
    refresh_projections(root, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)

    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "tester")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "projection fixture")
    add_origin_with_main(root, tmp_path)

    from trading_os import provider_sync

    real_run = provider_sync._run
    pr_created = False

    def fake_run(args, *, cwd, env=None):
        nonlocal pr_created
        if args[0] == "gh":
            if args[1:3] == ["pr", "list"]:
                url = "https://github.com/owner/repo/pull/123\n" if pr_created else ""
                return subprocess.CompletedProcess(args, 0, url, "")
            if args[1:3] == ["pr", "create"]:
                pr_created = True
                return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo/pull/123\n", "")
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setattr(provider_sync, "_run", fake_run)
    report = sync(root, make_exporter(["fee-source-change"]), commit=True, push=True, create_pr=True)

    assert report["steps"]["projection_refresh"]["status"] == "CURRENT"
    assert report["steps"]["projection_refresh"]["refresh_order"] == [
        "analytics", "health", "registration_census", "trade_view", "handoff"
    ]
    assert report["steps"]["projection_readback"]["status"] == "CURRENT"
    assert report["steps"]["projection_readback"]["census_freshness"] == "fresh"
    analytics = json.loads((root / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    assert analytics["source_version_pins"]["provider_fee_sources"] == provider_fee_index(root)[1]
    committed = git(root, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
    assert "exports/operational/analytics/current.json" in committed
    assert "exports/operational/health/current.json" in committed
    assert "exports/operational/handoff/current.json" in committed
    assert "exports/operational/trade-view/trade-view-data.json" in committed
    assert "PERFORMANCE.md" in committed
    assert not any(path.startswith("canonical/") for path in committed)


def _fake_child(returncode, stdout="", stderr=""):
    def fake(args, *, cwd, env=None):
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return fake


def test_child_stderr_secret_never_reaches_the_error(repo, monkeypatch):
    from trading_os import provider_sync

    monkeypatch.setenv("PROPW_TOKEN", "supersecret-token-value")
    (repo / "ingestion" / "propw" / ".env").write_text("PROPW_X_AUTHORIZATION=another-secret-value\nPROPW_DEVICE_ID=dev-12345\n", encoding="utf-8")
    stderr = "boom PROPW_TOKEN=supersecret-token-value x-authorization: another-secret-value device dev-12345 Bearer abcdef123456 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"
    monkeypatch.setattr(provider_sync, "_run", _fake_child(1, stderr=stderr))
    with pytest.raises(ProviderSyncError) as caught:
        run_provider_sync(repo, "propw", fetch=True, exporter=make_exporter(["1"]), today=TODAY)
    message = str(caught.value)
    for secret in ("supersecret-token-value", "another-secret-value", "dev-12345", "abcdef123456", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in message
    assert "[REDACTED]" in message


def test_child_stdout_secret_never_reaches_the_report(repo, monkeypatch):
    from trading_os import provider_sync

    monkeypatch.setenv("PROPW_TOKEN", "supersecret-token-value")
    stdout = "Sync complete\nusing PROPW_TOKEN=supersecret-token-value and loginToken: abc123def456\nTrade groups: 3"
    monkeypatch.setattr(provider_sync, "_run", _fake_child(0, stdout=stdout))
    report = run_provider_sync(repo, "propw", fetch=True, exporter=make_exporter(["1"]), today=TODAY)
    dumped = json.dumps(report)
    assert "supersecret-token-value" not in dumped and "abc123def456" not in dumped
    assert "Sync complete" in dumped and "Trade groups: 3" in dumped  # useful output survives


def test_custom_fetcher_errors_are_redacted_too(repo, monkeypatch):
    monkeypatch.setenv("PROPW_TOKEN", "supersecret-token-value")

    def leaky(root, db):
        raise ProviderSyncError("failed with PROPW_TOKEN=supersecret-token-value")

    with pytest.raises(ProviderSyncError) as caught:
        run_provider_sync(repo, "propw", fetch=True, fetcher=leaky, exporter=make_exporter(["1"]), today=TODAY)
    assert "supersecret-token-value" not in str(caught.value)


def test_relative_db_is_resolved_against_the_repo_root(repo, monkeypatch, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # a relative path must not be resolved against the caller's cwd
    seen = {}

    def exporter(root, db):
        seen["db"] = db
        return make_exporter(["1"])(root, db)

    run_provider_sync(repo, "propw", db=Path("ingestion/propw/data/propw.sqlite3"), exporter=exporter, today=TODAY)
    assert seen["db"] == (repo / "ingestion" / "propw" / "data" / "propw.sqlite3").resolve()


def test_secret_straddling_the_300_char_boundary_is_still_redacted(repo, monkeypatch):
    from trading_os import provider_sync

    token = "".join(chr(ord("a") + (i * 7) % 26) for i in range(200))  # 200-char secret
    monkeypatch.setenv("PROPW_TOKEN", token)
    # the last 300 chars start inside the token: 100 + 12 + 200 + 150 = 462 -> cut at 162 (50 chars into the secret)
    stderr = "A" * 100 + "PROPW_TOKEN=" + token + " " + "B" * 149
    monkeypatch.setattr(provider_sync, "_run", _fake_child(1, stderr=stderr))
    with pytest.raises(ProviderSyncError) as caught:
        run_provider_sync(repo, "propw", fetch=True, exporter=make_exporter(["1"]), today=TODAY)
    message = str(caught.value)
    assert token[60:120] not in message and token[-40:] not in message and token[:40] not in message
    assert "[REDACTED]" in message and "B" * 20 in message  # the harmless tail survives

    # the same for stdout (bounded to the last 12 lines)
    stdout = "\n".join(["line"] * 20 + ["PROPW_TOKEN=" + token])
    monkeypatch.setattr(provider_sync, "_run", _fake_child(0, stdout=stdout))
    report = run_provider_sync(repo, "propw", fetch=True, exporter=make_exporter(["1"]), today=TODAY)
    assert token[:40] not in json.dumps(report) and token[-40:] not in json.dumps(report)


def test_commit_contains_only_sync_owned_paths_not_legacy_representation(repo):
    legacy = repo / "provider-ledgers" / "propw" / "legacy-representation"
    legacy.mkdir(parents=True)
    (legacy / "candidates.md").write_text("owner review candidates", encoding="utf-8")
    (legacy / "candidates.json").write_text("{}", encoding="utf-8")
    (legacy / "decisions.jsonl").write_text('{"decision":"confirm"}\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "legacy files already tracked")
    # both concerns dirty at once: provider ledger changes + a legacy candidates edit
    (legacy / "candidates.md").write_text("owner review candidates - regenerated", encoding="utf-8")
    report = sync(repo, make_exporter(["1", "2"]), commit=True)
    assert report["status"] == "COMMITTED"
    files = git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
    assert files and all(f.startswith(("provider-ledgers/propw/normalized/", "provider-ledgers/propw/registration-census.json", "provider-ledgers/propw/provider-sync-status.json", "provider-ledgers/propw/index.json", "provider-ledgers/propw/sync-manifests/", "provider-ledgers/propw/raw/")) for f in files)
    assert not any("legacy-representation" in f for f in files)
    assert "legacy-representation/candidates.md" in git(repo, "status", "--porcelain").stdout  # still uncommitted


def test_a_dirty_legacy_file_alone_is_not_a_provider_change(repo):
    sync(repo, make_exporter(["1"]), commit=True)
    legacy = repo / "provider-ledgers" / "propw" / "legacy-representation"
    legacy.mkdir(parents=True)
    (legacy / "candidates.md").write_text("regenerated", encoding="utf-8")
    report = sync(repo, make_exporter(["1"]), commit=True)
    assert report["status"] == "NO_CHANGE" and report["changed"] is False


def test_staged_legacy_files_block_the_commit(repo):
    legacy = repo / "provider-ledgers" / "propw" / "legacy-representation"
    legacy.mkdir(parents=True)
    (legacy / "candidates.md").write_text("x", encoding="utf-8")
    git(repo, "add", "provider-ledgers/propw/legacy-representation/candidates.md")
    with pytest.raises(ProviderSyncError, match="unrelated files are already staged"):
        sync(repo, make_exporter(["1"]), commit=True)


def _stub_read_model(monkeypatch):
    from trading_os import provider_sync

    def build_view(root):
        output = root / "exports" / "operational" / "trade-view"
        output.mkdir(parents=True, exist_ok=True)
        for name in ("index.html", "style.css", "app.js"):
            (output / name).write_text(f"// {name}\n", encoding="utf-8")
        (output / "trade-view-data.json").write_text('{"fresh":true}\n', encoding="utf-8")
        (output / "manifest.json").write_text('{"registration_census_freshness":"fresh"}\n', encoding="utf-8")
        return {"data_sha256": "d", "registration_census_source_identity_sha256": "s", "registration_census_freshness": "fresh"}

    monkeypatch.setattr(provider_sync, "build_trade_view", build_view)
    monkeypatch.setattr(provider_sync, "_provider_fee_projection_is_stale", lambda _root: False)
    monkeypatch.setattr(provider_sync, "verify_projections", lambda *_a, **_k: {
        "status": "CURRENT", "registration_census_freshness": "fresh",
        "registration_census_source_identity_sha256": "s", "registration_census_candidate_count": 1,
    })


def test_publish_main_requires_commit_and_rejects_data_branch_flags(repo):
    with pytest.raises(ProviderSyncError, match="--publish-main requires --commit"):
        sync(repo, make_exporter(["1"]), publish_main=True)
    for extra in ({"push": True}, {"push": True, "create_pr": True}, {"branch": BRANCH}):
        with pytest.raises(ProviderSyncError, match="replaces --push/--create-pr/--branch"):
            sync(repo, make_exporter(["1"]), commit=True, publish_main=True, **extra)
    assert not (repo / "provider-ledgers").exists()


def test_publish_main_fast_forwards_only_provider_paths_and_reads_back(repo, tmp_path, monkeypatch):
    _stub_read_model(monkeypatch)
    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)

    report = sync(repo, make_exporter(["1"]), commit=True, publish_main=True)

    git_step = report["steps"]["git"]
    assert report["status"] == "COMMITTED" and git_step["pushed"] is True and git_step["branch"] == "main"
    assert git_step["readback"]["matches_commit"] is True
    assert git(remote, "rev-parse", "main").stdout.strip() == git_step["commit"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(remote, "branch", "--list").stdout.split() == ["main"]  # no data branch, no PR
    committed = git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
    assert committed and all(p.startswith(("provider-ledgers/propw/", "exports/operational/trade-view/")) for p in committed)

    again = sync(repo, make_exporter(["1"]), commit=True, publish_main=True)
    assert again["status"] == "NO_CHANGE" and again["steps"]["git"]["pushed"] is False


def test_publish_main_refuses_a_stale_or_ahead_checkout_before_writing(repo, tmp_path, monkeypatch):
    _stub_read_model(monkeypatch)
    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)
    (repo / "README.md").write_text("local only\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "unpublished local change")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ProviderSyncError, match="HEAD to equal the freshly fetched origin/main"):
        sync(repo, make_exporter(["1"]), commit=True, publish_main=True)

    assert not (repo / "provider-ledgers").exists()
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert git(remote, "rev-parse", "main").stdout.strip() != head


def test_publish_main_refuses_a_feature_branch(repo, tmp_path, monkeypatch):
    _stub_read_model(monkeypatch)
    commit_pr_projection_inputs(repo)
    add_origin_with_main(repo, tmp_path)
    git(repo, "switch", "-q", "-c", "feat/foo")
    with pytest.raises(ProviderSyncError, match="must run on main or a detached origin/main"):
        sync(repo, make_exporter(["1"]), commit=True, publish_main=True)
    assert not (repo / "provider-ledgers").exists()


def test_publish_main_blocks_on_a_stale_projection_without_pushing(repo, tmp_path, monkeypatch):
    from trading_os import provider_sync

    _stub_read_model(monkeypatch)
    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)
    main_before = git(remote, "rev-parse", "main").stdout.strip()

    def stale(*_a, **_k):
        raise ValueError("census stale")

    monkeypatch.setattr(provider_sync, "verify_projections", stale)
    with pytest.raises(ProviderSyncError, match="publish to main blocked"):
        sync(repo, make_exporter(["1"]), commit=True, publish_main=True)
    assert git(remote, "rev-parse", "main").stdout.strip() == main_before


def test_publish_main_never_force_pushes_when_main_moved(repo, tmp_path, monkeypatch):
    from trading_os import provider_sync

    _stub_read_model(monkeypatch)
    commit_pr_projection_inputs(repo)
    remote = add_origin_with_main(repo, tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(other))
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "other")
    (other / "other.txt").write_text("someone else\n", encoding="utf-8")
    git(other, "add", "other.txt")
    git(other, "commit", "-q", "-m", "someone else moved main")
    real_run = provider_sync._run
    moved = False

    def move_main_before_push(args, *, cwd, env=None):
        nonlocal moved
        if args[:2] == ["git", "push"] and not moved:
            moved = True
            assert git(other, "push", "-q", "origin", "main").returncode == 0
        return real_run(args, cwd=cwd, env=env)

    monkeypatch.setattr(provider_sync, "_run", move_main_before_push)
    with pytest.raises(ProviderSyncError, match="push to main failed"):
        sync(repo, make_exporter(["1"]), commit=True, publish_main=True)
    assert git(remote, "log", "-1", "--format=%s", "main").stdout.strip() == "someone else moved main"
