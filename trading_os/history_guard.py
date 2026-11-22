"""Git-level guard that canonical history only grows.

The lifecycle store is append-only by contract and the GitHub Runner already
checks that for its own writes. Local Agents write ``main`` directly, so CI
re-checks pushes, PRs and, daily, the whole history of ``main``: an
append-only log may only gain lines at its end, and a content-addressed
evidence artifact or frozen registration packet may never change or disappear
once committed.

Every commit in a range is checked against each of its parents, so a rewrite
that a later commit restores is still caught; the range endpoints are also
compared so a force-push onto unrelated history is caught.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

APPEND_ONLY_LOGS = (
    "canonical/lifecycle/events.jsonl",
    "canonical/lifecycle/identity-links.jsonl",
    "canonical/registration/pending-strategy.jsonl",
)
IMMUTABLE_PREFIXES = (
    "canonical/evidence/artifacts/",
    "canonical/registration/packets/",
)
# EvidenceStore merges newly seen original filenames into an artifact's
# metadata when identical bytes are ingested again; the bytes never change.
MUTABLE_SUFFIXES = (".metadata.json",)
ZERO_SHA = "0" * 40


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def _git_lines(root: Path, *args: str) -> list[str]:
    done = _git(root, *args)
    if done.returncode:
        raise ValueError(f"git {args[0]} failed: " + done.stderr.decode("utf-8", "replace"))
    return done.stdout.decode("utf-8").split()


def _blob(root: Path, revision: str, path: str) -> bytes | None:
    shown = _git(root, "show", f"{revision}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def _require_commit(root: Path, revision: str) -> None:
    if _git(root, "rev-parse", "--verify", "--quiet", revision + "^{commit}").returncode:
        raise ValueError(f"unknown revision: {revision}")


def history_violations(root: Path, base: str, head: str) -> list[str]:
    """Return every way ``head``'s tree rewrites canonical history present in ``base``'s tree."""
    for revision in (base, head):
        _require_commit(root, revision)
    violations = []
    for path in APPEND_ONLY_LOGS:
        before = _blob(root, base, path)
        if before is None:
            continue
        after = _blob(root, head, path)
        if after is None:
            violations.append(f"{path}: append-only log was deleted")
        elif not after.startswith(before):
            violations.append(f"{path}: existing lines were changed or removed (only appends are allowed)")
    changed = _git(root, "diff", "--name-status", "--no-renames", base, head, "--", *IMMUTABLE_PREFIXES)
    if changed.returncode:
        raise ValueError("git diff failed: " + changed.stderr.decode("utf-8", "replace"))
    for line in changed.stdout.decode("utf-8").splitlines():
        status, _, path = line.partition("\t")
        if status != "A" and not path.endswith(MUTABLE_SUFFIXES):
            violations.append(f"{path}: immutable canonical file was {'deleted' if status == 'D' else 'modified'}")
    return violations


def range_violations(root: Path, base: str, head: str) -> list[str]:
    """Check the endpoints and every commit in ``base..head`` against each parent."""
    violations = [f"{base[:12]}..{head[:12]}: {item}" for item in history_violations(root, base, head)]
    for commit in _git_lines(root, "rev-list", f"{base}..{head}"):
        parents = _git_lines(root, "rev-list", "--parents", "-n", "1", commit)[1:]
        for parent in parents:
            violations += [f"{commit[:12]} vs parent {parent[:12]}: {item}"
                           for item in history_violations(root, parent, commit)]
    return violations


def full_history_violations(root: Path, head: str, anchor: str) -> list[str]:
    """``anchor`` must still be in ``head``'s history, and every commit since must only grow it."""
    _require_commit(root, anchor)
    _require_commit(root, head)
    if _git(root, "merge-base", "--is-ancestor", anchor, head).returncode:
        return [f"anchor {anchor[:12]} is no longer in the history of {head[:12]} (rewritten or recreated branch)"]
    return range_violations(root, anchor, head)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("revisions", nargs="+", metavar="REV", help="BASE HEAD, or HEAD with --anchor")
    parser.add_argument("--root", default=".")
    parser.add_argument("--anchor", help="check all history of HEAD since this commit")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.anchor:
        if len(args.revisions) != 1:
            parser.error("--anchor takes exactly one revision: HEAD")
        violations = full_history_violations(root, args.revisions[0], args.anchor)
    elif len(args.revisions) != 2:
        parser.error("expected BASE HEAD")
    elif not args.revisions[0] or args.revisions[0] == ZERO_SHA:
        # A recreated branch has no previous revision; never pass that silently.
        violations = ["no base revision to compare (branch created or recreated); use --anchor"]
    else:
        violations = range_violations(root, *args.revisions)
    print(json.dumps({"status": "BLOCKED" if violations else "OK", "violations": violations}, ensure_ascii=False))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
