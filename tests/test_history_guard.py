"""Canonical history may only grow between revisions (CI guard for direct main writes)."""
import subprocess
from pathlib import Path

import pytest

from trading_os.history_guard import history_violations, main

EVENTS = "canonical/lifecycle/events.jsonl"
ARTIFACT = "canonical/evidence/artifacts/" + "a" * 64 + ".png"
METADATA = "canonical/evidence/artifacts/" + "a" * 64 + ".metadata.json"
PACKET = "canonical/registration/packets/" + "b" * 64 + ".json"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(root, files, message):
    for path, content in files.items():
        target = root / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "--initial-branch=main")
    _git(tmp_path, "config", "user.name", "fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    base = _commit(tmp_path, {
        EVENTS: b'{"event":1}\n',
        ARTIFACT: b"\x89PNG original",
        METADATA: b'{"original_filenames":["a.png"]}\n',
        PACKET: b'{"frozen":true}\n',
    }, "base")
    return tmp_path, base


def test_appending_events_and_adding_files_is_allowed(repo):
    root, base = repo
    head = _commit(root, {
        EVENTS: b'{"event":1}\n{"event":2}\n',
        "canonical/evidence/artifacts/" + "c" * 64 + ".png": b"new",
        METADATA: b'{"original_filenames":["a.png","b.png"]}\n',
    }, "append")
    assert history_violations(root, base, head) == []
    assert main(["--root", str(root), base, head]) == 0


@pytest.mark.parametrize("files, expected", [
    ({EVENTS: b'{"event":"rewritten"}\n{"event":2}\n'}, "only appends"),
    ({EVENTS: None}, "deleted"),
    ({ARTIFACT: b"\x89PNG edited"}, "modified"),
    ({ARTIFACT: None}, "deleted"),
    ({PACKET: b'{"frozen":false}\n'}, "modified"),
])
def test_rewriting_canonical_history_is_blocked(repo, files, expected):
    root, base = repo
    head = _commit(root, files, "rewrite")
    violations = history_violations(root, base, head)
    assert len(violations) == 1 and expected in violations[0]
    assert main(["--root", str(root), base, head]) == 1


def test_unknown_revision_fails_instead_of_passing(repo):
    root, base = repo
    with pytest.raises(ValueError, match="unknown revision"):
        history_violations(root, base, "0" * 40)


def test_rewrite_restored_by_a_later_commit_is_still_caught(repo):
    """Codex: endpoint-only comparison missed rewrite-then-restore inside a range."""
    from trading_os.history_guard import range_violations

    root, base = repo
    _commit(root, {EVENTS: b'{"event":"rewritten"}\n'}, "rewrite")
    head = _commit(root, {EVENTS: b'{"event":1}\n{"event":2}\n'}, "restore and append")
    assert history_violations(root, base, head) == []
    violations = range_violations(root, base, head)
    assert violations and all("only appends" in item for item in violations)
    assert main(["--root", str(root), base, head]) == 1


def test_rewrite_on_a_merged_side_branch_is_caught(repo):
    from trading_os.history_guard import range_violations

    root, base = repo
    _git(root, "checkout", "-qb", "side")
    _commit(root, {ARTIFACT: b"\x89PNG tampered"}, "side rewrite")
    _commit(root, {ARTIFACT: b"\x89PNG original"}, "side restore")
    _git(root, "checkout", "-q", "main")
    _commit(root, {EVENTS: b'{"event":1}\n{"event":2}\n'}, "main append")
    _git(root, "merge", "-q", "--no-edit", "side")
    head = _git(root, "rev-parse", "HEAD")
    assert any("modified" in item for item in range_violations(root, base, head))


def test_zero_base_fails_closed(repo):
    """Codex: a recreated branch (all-zero before) must not pass silently."""
    root, base = repo
    assert main(["--root", str(root), "0" * 40, base]) == 1


def test_full_history_requires_the_anchor_to_remain(repo):
    from trading_os.history_guard import full_history_violations

    root, anchor = repo
    head = _commit(root, {EVENTS: b'{"event":1}\n{"event":2}\n'}, "append")
    assert full_history_violations(root, head, anchor) == []
    assert main(["--root", str(root), "--anchor", anchor, head]) == 0
    _git(root, "checkout", "-q", "--orphan", "recreated")
    _git(root, "rm", "-rq", "--cached", ".")
    recreated = _commit(root, {EVENTS: b'{"event":"forged"}\n'}, "recreated main")
    violations = full_history_violations(root, recreated, anchor)
    assert len(violations) == 1 and "no longer in the history" in violations[0]


def test_pull_request_job_keeps_credentials_to_fetch_the_pr_commits():
    """The PR job runs `git fetch` of the PR ref in a private repo; without the read-only token it fails (seen on PR 173)."""
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/canonical-history-guard.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["history-only-grows"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout"))
    assert "pull_request_target" in str(checkout["with"]["persist-credentials"])
    assert workflow["permissions"] == {"contents": "read"}
    fetching = [step for step in steps if "git fetch" in str(step.get("run", ""))]
    assert fetching and all("pull_request_target" in str(step.get("if", "")) for step in fetching)
    # Nothing from the PR is executed: the checker always runs from the trusted base checkout.
    assert checkout["with"]["ref"].startswith("${{ github.event_name == 'pull_request_target' && github.event.pull_request.base.sha")
