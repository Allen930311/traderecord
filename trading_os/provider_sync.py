"""Provider Sync pipeline: fetch -> sanitized ledger -> projections -> commit/push.

Provider truth only: this never creates T-IDs, strategies or reviews. Each step is
explicit and fail-closed:

* fetch is opt-in (``--fetch``): it needs the owner's own logged-in session and runs
  ``python -m propw sync`` locally; this module never reads or prints credentials.
* the ledger export aborts (nothing written) on any credential-looking field, and the
  written ledger is re-scanned before it can be committed.
* commit/push are separate opt-ins; commits stage provider-owned ledger/Census
  paths and generated read models. If provider fee inputs changed, the existing
  projection closure refreshes analytics, health, Census, Trade View and handoff
  before those outputs can be published. The flow refuses unrelated staged files,
  never resets/stashes/cleans. By default it never targets main (it uses
  ``data/<provider>-ledger-YYYYMMDD``); ``--publish-main`` below is the exception.
* ``--create-pr`` requires commit and push, then opens/reports an Owner-reviewed
  main PR; it never merges. A main push runs the read-only exact-SHA projection
  check. An unchanged ledger, Census and read model produce NO CHANGE.
* ``--publish-main`` (with ``--commit``) is the Owner-authorized routine path: from a
  checkout whose HEAD equals a freshly fetched ``origin/main`` it commits only the
  provider-owned paths and fast-forward pushes them to main (never force), then reads
  origin/main back. It runs the same fresh-Census/projection checks as ``--create-pr``
  and refuses to start otherwise. It is for provider data only, never for code.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .operational_lifecycle import PROJECTION_SCHEMA as LIFECYCLE_PROJECTION_SCHEMA, canonical_provider
from .operational_analytics import provider_fee_index
from .projections import refresh_projections, verify_projections
from .provider_sync_status import SCHEMA_VERSION as SYNC_STATUS_SCHEMA, SOURCE as SYNC_STATUS_SOURCE, STATUS_PATH, read_provider_sync_recency
from .registration_census import build_census, select_candidates, write_census
from .trade_view import build_trade_view

DEFAULT_DB = Path("ingestion") / "propw" / "data" / "propw.sqlite3"
PROPW_SRC = Path("ingestion") / "propw" / "src"
TRADE_VIEW_OUTPUT_PATHS = [
    "exports/operational/trade-view/index.html",
    "exports/operational/trade-view/style.css",
    "exports/operational/trade-view/app.js",
    "exports/operational/trade-view/trade-view-data.json",
    "exports/operational/trade-view/manifest.json",
]
PROJECTION_OUTPUT_PATHS = [
    "PERFORMANCE.md",
    "exports/operational/analytics/current.json",
    "exports/operational/analytics/manifest.json",
    "exports/operational/health/current.json",
    "exports/operational/health/manifest.json",
    "exports/operational/health/policy.json",
    "exports/operational/handoff/current.json",
    "exports/operational/handoff/manifest.json",
    *TRADE_VIEW_OUTPUT_PATHS,
]
PR_READ_MODEL_SOURCE_PATHS = [
    "canonical/records/trades",
    "canonical/sequences/finalizations.json",
    "canonical/lifecycle/identity-links.jsonl",
    "canonical/lifecycle/current.json",
    "canonical/lifecycle/events.jsonl",
    "canonical/registration/pending-strategy.jsonl",
    "canonical/registration/packets",
    "exports/operational/analytics/current.json",
    "exports/operational/health/current.json",
    "exports/method-validation/b1-v3",
    "exports/method-validation/b2",
    "exports/method-validation/b3",
    "exports/trader-dna/v1",
    "registry/strategy-registry.yaml",
    "registry/versions",
]


def owned_paths(provider: str) -> list[str]:
    """Paths this pipeline owns (provider truth + its census). Everything else under provider-ledgers/<p>/ --
    e.g. legacy-representation/ candidates and Owner decisions -- is a different concern and is never staged."""

    base = f"provider-ledgers/{provider}"
    return [
        f"{base}/raw",
        f"{base}/normalized",
        f"{base}/index.json",
        f"{base}/sync-manifests",
        f"{base}/registration-census.json",
        f"{base}/provider-sync-status.json",
    ]


def _record_provider_sync_recency(root: Path, db_path: Path, provider: str) -> dict[str, Any]:
    """Record the last successful sync from the existing local SQLite history."""

    timestamp: str | None = None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_runs'"
            ).fetchone()
            if table:
                row = connection.execute(
                    "SELECT finished_at FROM sync_runs "
                    "WHERE status = 'ok' AND finished_at IS NOT NULL "
                    "ORDER BY finished_at DESC, id DESC LIMIT 1"
                ).fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    timestamp = row[0].strip()
    except (OSError, sqlite3.Error, ValueError):
        # Recency is optional metadata. Unknown is safer than blocking a valid
        # local ledger export or implying the provider is current.
        timestamp = None

    payload = {
        "schema_version": SYNC_STATUS_SCHEMA,
        "provider": provider,
        "status": "recorded" if timestamp else "unknown",
        "last_successful_sync_at": timestamp,
        "source": SYNC_STATUS_SOURCE,
    }
    path = root / STATUS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return read_provider_sync_recency(root, provider)


def _existing(root: Path, paths: list[str]) -> list[str]:
    return [p for p in paths if (root / p).exists()]


def _is_owned(path: str, owned: list[str]) -> bool:
    path = path.replace("\\", "/")
    return any(path == o or path.startswith(o + "/") for o in owned)


class ProviderSyncError(RuntimeError):
    """A pipeline step failed; nothing after it was executed."""


def _run(args: list[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env={**os.environ, **(env or {})})


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=root)


_SECRET_KEY = re.compile(r"(token|secret|passw(or)?d|cookie|session|auth|api[_-]?key|credential|device)", re.IGNORECASE)
_KV_PAIR = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:token|secret|passw(?:or)?d|cookie|session|authorization|api[_-]?key|device[_-]?id)[A-Za-z0-9_-]*)(\s*[:=]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
REDACTED = "[REDACTED]"


def _env_file_values(path: Path) -> set[str]:
    values: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if len(value) >= 4:
                    values.add(value)
    return values


def secret_values(root: Path) -> list[str]:
    """Known secret values: credential-looking environment variables plus every value in the local propw .env files."""

    values = {v for k, v in os.environ.items() if _SECRET_KEY.search(k) and len(v) >= 4}
    values |= _env_file_values(Path(root) / "ingestion" / "propw" / ".env") | _env_file_values(Path(root) / ".env")
    return sorted(values, key=len, reverse=True)


def redact(text: str, root: Path) -> str:
    """Remove known secret values and credential-looking key=value / Bearer / JWT patterns from child output."""

    for value in secret_values(root):
        text = text.replace(value, REDACTED)
    text = _KV_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    return _JWT.sub(REDACTED, text)


def _redacted_tail(text: str, root: Path, *, chars: int | None = None, lines: int | None = None) -> str:
    """Redact the complete text first, THEN bound it (truncating first could cut a secret in half and defeat redaction)."""

    cleaned = redact((text or "").strip(), root)
    if lines is not None:
        cleaned = "\n".join(cleaned.splitlines()[-lines:])
    return cleaned[-chars:] if chars is not None else cleaned


def _propw_env(root: Path) -> dict[str, str]:
    existing = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": os.pathsep.join(filter(None, [str(root / PROPW_SRC), existing])), "PYTHONIOENCODING": "utf-8"}


def default_fetcher(root: Path, db: Path) -> dict[str, Any]:
    """Run the owner-local read-only PropW sync. Credentials stay in the owner's environment/.env."""

    done = _run([sys.executable, "-m", "propw", "--db", str(db), "sync"], cwd=root / "ingestion" / "propw", env=_propw_env(root))
    # Child output is untrusted for secrets: redact known values and credential-like patterns, then bound it.
    tail = _redacted_tail(done.stdout, root, lines=12)
    if done.returncode != 0:
        raise ProviderSyncError(f"propw sync failed (exit {done.returncode}): {_redacted_tail(done.stderr, root, chars=300)}")
    return {"ran": True, "summary": tail}


def default_exporter(root: Path, db: Path) -> dict[str, Any]:
    done = _run([sys.executable, "-m", "propw", "--db", str(db), "ledger-export"], cwd=root, env=_propw_env(root))
    if done.returncode != 0:
        raise ProviderSyncError(f"ledger export failed (exit {done.returncode}; nothing written): {_redacted_tail(done.stderr, root, chars=300)}")
    return json.loads((done.stdout or "").strip().splitlines()[-1])


def verify_ledger_clean(ledger_dir: Path, root: Path) -> int:
    """Re-scan every written ledger file for credential-looking keys/values. Returns the number of lines scanned."""

    sys.path.insert(0, str(root / PROPW_SRC))
    try:
        from propw.ledger import LedgerSanitizationError, assert_clean
    finally:
        sys.path.pop(0)
    scanned = 0
    for path in sorted(ledger_dir.rglob("*.json*")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines() if path.suffix == ".jsonl" else [text]
        for line in lines:
            if not line.strip():
                continue
            try:
                assert_clean(json.loads(line), str(path.relative_to(ledger_dir)))
            except LedgerSanitizationError as exc:
                raise ProviderSyncError(f"ledger re-scan found a credential-like field: {exc}") from exc
            scanned += 1
    return scanned


def _target_branch(root: Path, provider: str, branch: str | None, today: str) -> tuple[str, str]:
    """(current, target). Provider truth is committed only on main->data branch or already on that exact data branch."""

    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    target = branch or f"data/{provider}-ledger-{today}"
    if current != target and current not in {"main", "master"}:
        raise ProviderSyncError(
            f"refusing to commit provider data on branch '{current}': run from main (a data branch is created) "
            f"or from the data branch '{target}' itself; provider truth must not be mixed into another concern's branch"
        )
    if not re.fullmatch(rf"data/{re.escape(provider)}-ledger-\d{{8}}", target):
        raise ProviderSyncError(f"refusing provider data target outside data/{provider}-ledger-YYYYMMDD: {target}")
    return current, target


def _projection_evidence_locators(root: Path) -> set[str]:
    """Collect artifact locators from every input used to build analytics.

    Analytics can contain a stale projection that predates evidence attached
    directly to a canonical trade or lifecycle record. The refresh reads those
    canonical sources too, so their artifact locators must be guarded even when
    the old analytics read model does not yet list them.
    """

    locators: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            locator = value.get("locator")
            if isinstance(locator, str) and locator.startswith("artifact:"):
                locators.add(locator)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    analytics_path = root / "exports/operational/analytics/current.json"
    try:
        analytics = json.loads(analytics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderSyncError("could not inspect analytics evidence locators for projection refresh") from exc
    if not isinstance(analytics, Mapping):
        raise ProviderSyncError("analytics read model is not an object while inspecting evidence locators")
    for collection in ("observations", "canonical_read_model"):
        collect(analytics.get(collection, []))

    records_dir = root / "canonical/records/trades"
    if records_dir.is_dir():
        try:
            for path in sorted(records_dir.glob("*.json")):
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, Mapping):
                    collect(record)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderSyncError("could not inspect canonical trade evidence locators for projection refresh") from exc

    lifecycle_path = root / "canonical/lifecycle/current.json"
    if lifecycle_path.is_file():
        try:
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderSyncError("could not inspect lifecycle evidence locators for projection refresh") from exc
        if not isinstance(lifecycle, Mapping):
            raise ProviderSyncError("lifecycle projection is not an object while inspecting evidence locators")
        records = lifecycle.get("records", {}) if isinstance(lifecycle, Mapping) else {}
        collect(records)
    else:
        lifecycle = {}

    # LifecycleStore.current() rebuilds from the append-only event log when
    # current.json is absent or does not use the current projection schema.
    # Those events can contain evidence locators that the stale projection
    # never exposed, so guard them before refresh can read them.
    if lifecycle.get("schema_version") != LIFECYCLE_PROJECTION_SCHEMA:
        events_path = root / "canonical/lifecycle/events.jsonl"
        if events_path.is_file():
            try:
                for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if isinstance(event, Mapping):
                        collect(event.get("payload", {}))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProviderSyncError("could not inspect lifecycle event evidence locators for projection refresh") from exc

    return locators


def _verify_projection_evidence_sources(root: Path, *, baseline: str, pr_mode: bool) -> None:
    evidence_root = (root / "canonical/evidence/artifacts").resolve()
    for locator in sorted(_projection_evidence_locators(root)):
        name = locator.removeprefix("artifact:")
        artifact_path = (evidence_root / name).resolve()
        if not name or not artifact_path.is_relative_to(evidence_root):
            raise ProviderSyncError("projection source contains an invalid evidence locator")
        relative_path = artifact_path.relative_to(root).as_posix()
        base_blob = _git(root, "rev-parse", "--verify", f"{baseline}:{relative_path}")
        local_path = root / relative_path
        if not local_path.is_file():
            if base_blob.returncode == 0:
                raise ProviderSyncError(f"referenced evidence artifact is missing from the working tree: {relative_path}")
            if pr_mode:
                raise ProviderSyncError(f"referenced evidence artifact is missing locally and from origin/main: {relative_path}")
            raise ProviderSyncError(f"provider projection refresh requires committed evidence bytes: {relative_path}")
        if base_blob.returncode != 0:
            if pr_mode:
                raise ProviderSyncError(f"referenced evidence artifact is not committed on origin/main: {relative_path}")
            raise ProviderSyncError(f"provider projection refresh requires committed evidence bytes: {relative_path}")
        local_blob = _git(root, "hash-object", "--", relative_path)
        if local_blob.returncode != 0 or local_blob.stdout.strip() != base_blob.stdout.strip():
            if pr_mode:
                raise ProviderSyncError(f"referenced evidence artifact differs from origin/main: {relative_path}")
            raise ProviderSyncError(f"provider projection refresh requires committed evidence bytes: {relative_path}")


def _ensure_pr_read_model_sources_clean(root: Path, flag: str = "--create-pr") -> None:
    """Prevent a published projection from incorporating source state absent from committed state."""

    changed = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *PR_READ_MODEL_SOURCE_PATHS)
    if changed.returncode != 0:
        raise ProviderSyncError("could not verify that Trade View and Census sources match committed state")
    paths = [line[3:] for line in changed.stdout.splitlines() if line]
    if paths:
        raise ProviderSyncError(
            f"{flag} requires clean committed read-model source paths; changed paths include "
            + ", ".join(paths[:3])
        )
    fetched = _git(root, "fetch", "origin", "main")
    if fetched.returncode != 0:
        raise ProviderSyncError("could not fetch origin/main to verify Trade View evidence sources")
    _verify_projection_evidence_sources(root, baseline="origin/main", pr_mode=True)


def _ensure_projection_sources_clean(root: Path) -> None:
    """Do not publish projections built from uncommitted canonical or registry inputs."""

    changed = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *PR_READ_MODEL_SOURCE_PATHS)
    if changed.returncode != 0:
        raise ProviderSyncError("could not verify that projection source paths are clean")
    paths = [line[3:] for line in changed.stdout.splitlines() if line]
    if paths:
        raise ProviderSyncError(
            "provider projection refresh requires clean committed source paths; changed paths include "
            + ", ".join(paths[:3])
        )
    _verify_projection_evidence_sources(root, baseline="HEAD", pr_mode=False)


def _ensure_projection_outputs_clean(root: Path) -> None:
    """Never replace locally changed generated artifacts during a committed sync."""

    changed = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *PROJECTION_OUTPUT_PATHS)
    if changed.returncode != 0:
        raise ProviderSyncError("could not verify that provider projection output paths are clean")
    paths = [line[3:] for line in changed.stdout.splitlines() if line]
    for relative in PROJECTION_OUTPUT_PATHS:
        output = root / relative
        if not output.exists() and not output.is_symlink():
            continue
        if output.is_symlink() or not output.is_file():
            paths.append(relative)
            continue
        tracked = _git(root, "ls-files", "--error-unmatch", "--", relative)
        if tracked.returncode != 0:
            # git status hides ignored files. An existing ignored/untracked
            # destination is still local work that a projection writer could
            # replace, so it must fail closed too.
            paths.append(relative)
    if paths:
        raise ProviderSyncError(
            "provider projection refresh requires clean tracked generated output paths; changed or untracked paths include "
            + ", ".join(paths[:3])
        )


def _provider_fee_projection_is_stale(root: Path) -> bool:
    analytics_path = root / "exports/operational/analytics/current.json"
    try:
        analytics = json.loads(analytics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderSyncError("could not inspect analytics provider-fee source pin") from exc
    stored_pin = analytics.get("source_version_pins", {}).get("provider_fee_sources")
    return stored_pin != provider_fee_index(root)[1]


def _ensure_data_branch_scope(root: Path, branch: str, owned: list[str]) -> None:
    """Fail closed if a provider-data PR would include paths outside the pipeline allowlist."""

    fetched = _git(root, "fetch", "origin", "main")
    if fetched.returncode != 0:
        raise ProviderSyncError("could not fetch origin/main to verify provider data branch scope")
    base = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main")
    merge_base = _git(root, "merge-base", "origin/main", branch)
    if base.returncode != 0 or merge_base.returncode != 0 or merge_base.stdout.strip() != base.stdout.strip():
        raise ProviderSyncError("provider data branch must be based on the current origin/main before publishing")
    compared = _git(root, "diff", "--name-only", f"origin/main...{branch}")
    if compared.returncode != 0:
        raise ProviderSyncError("could not verify provider data branch scope against main")
    changed = [path for path in compared.stdout.splitlines() if path]
    foreign = [path for path in changed if not _is_owned(path, owned)]
    if foreign:
        raise ProviderSyncError(
            "refusing to publish provider data branch with out-of-scope paths "
            f"({len(foreign)}), e.g. {foreign[0]}"
        )


def _git_step(root: Path, owned: list[str], provider: str, *, commit: bool, push: bool, branch: str | None, today: str, summary: str, allowed_paths: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"committed": False, "pushed": False}
    current, target = _target_branch(root, provider, branch, today)
    staged = [p for p in _git(root, "diff", "--cached", "--name-only").stdout.splitlines() if p]
    foreign = [p for p in staged if not _is_owned(p, owned)]
    if foreign:
        raise ProviderSyncError(f"refusing to commit: unrelated files are already staged ({len(foreign)}), e.g. {foreign[0]}")
    if current != target:
        exists = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}").returncode == 0
        switched = _git(root, "switch", target) if exists else _git(root, "switch", "-c", target)
        if switched.returncode != 0:
            raise ProviderSyncError(f"could not switch to branch {target}: {switched.stderr.strip()[-200:]}")
    if push:
        _ensure_data_branch_scope(root, target, allowed_paths or owned)
    result["branch"] = target
    pathspec = _existing(root, owned)
    add = _git(root, "add", "--", *pathspec)
    if add.returncode != 0:
        raise ProviderSyncError(f"git add failed: {add.stderr.strip()[-200:]}")
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        result["note"] = "nothing to commit"
        return result
    message = f"data: {provider} provider ledger + registration census ({today})\n\n{summary}"
    done = _git(root, "commit", "-m", message, "--", *pathspec)
    if done.returncode != 0:
        raise ProviderSyncError(f"git commit failed: {_redacted_tail(done.stderr or done.stdout, root, chars=300)}")
    result["committed"] = True
    result["commit"] = _git(root, "rev-parse", "HEAD").stdout.strip()
    if push:
        _ensure_data_branch_scope(root, target, allowed_paths or owned)
        pushed = _git(root, "push", "-u", "origin", result["branch"])
        if pushed.returncode != 0:
            raise ProviderSyncError(f"git push failed: {pushed.stderr.strip()[-300:]}")
        result["pushed"] = True
    return result


def _ensure_head_is_origin_main(root: Path) -> str:
    """Fetch and require that HEAD is exactly origin/main, so the publish can only fast-forward."""

    fetched = _git(root, "fetch", "--no-tags", "origin", "main")
    if fetched.returncode != 0:
        raise ProviderSyncError(f"could not fetch origin/main for --publish-main: {_redacted_tail(fetched.stderr, root, chars=300)}")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    base = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main").stdout.strip()
    if not head or head != base:
        raise ProviderSyncError(
            "--publish-main requires HEAD to equal the freshly fetched origin/main "
            "(run from a clean checkout of current main; local commits or a stale main are never published)"
        )
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch not in {"main", "HEAD"}:
        raise ProviderSyncError(f"--publish-main must run on main or a detached origin/main, not '{branch}'")
    return head


def _publish_main_step(root: Path, owned: list[str], provider: str, *, today: str, summary: str, allowed_paths: list[str] | None = None) -> dict[str, Any]:
    """Commit only provider-owned paths and fast-forward push them to main, then read origin/main back."""

    result: dict[str, Any] = {"committed": False, "pushed": False, "branch": "main"}
    staged = [p for p in _git(root, "diff", "--cached", "--name-only").stdout.splitlines() if p]
    foreign = [p for p in staged if not _is_owned(p, owned)]
    if foreign:
        raise ProviderSyncError(f"refusing to commit: unrelated files are already staged ({len(foreign)}), e.g. {foreign[0]}")
    base = _ensure_head_is_origin_main(root)
    pathspec = _existing(root, owned)
    add = _git(root, "add", "--", *pathspec)
    if add.returncode != 0:
        raise ProviderSyncError(f"git add failed: {add.stderr.strip()[-200:]}")
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        result["note"] = "nothing to commit"
        return result
    message = f"data: {provider} provider ledger + registration census ({today})\n\n{summary}"
    done = _git(root, "commit", "-m", message, "--", *pathspec)
    if done.returncode != 0:
        raise ProviderSyncError(f"git commit failed: {_redacted_tail(done.stderr or done.stdout, root, chars=300)}")
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    result["committed"] = True
    result["commit"] = commit
    changed = [p for p in _git(root, "diff", "--name-only", base, commit).stdout.splitlines() if p]
    out_of_scope = [p for p in changed if not _is_owned(p, allowed_paths or owned)]
    if out_of_scope:
        raise ProviderSyncError(
            f"refusing to publish provider data to main with out-of-scope paths ({len(out_of_scope)}), e.g. {out_of_scope[0]}"
        )
    pushed = _git(root, "push", "origin", f"{commit}:refs/heads/main")  # no --force: a moved main is rejected
    if pushed.returncode != 0:
        raise ProviderSyncError(f"git push to main failed (main moved or push was rejected): {pushed.stderr.strip()[-300:]}")
    result["pushed"] = True
    fetched = _git(root, "fetch", "--no-tags", "origin", "main")
    remote = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main").stdout.strip()
    if fetched.returncode != 0 or remote != commit:
        raise ProviderSyncError(f"main read-back failed: origin/main is {remote or 'unknown'}, expected {commit}")
    result["readback"] = {"remote_ref": "origin/main", "sha": remote, "matches_commit": True}
    return result


def _redact_value(value: Any, root: Path) -> Any:
    """Redact every string inside a JSON-like structure (never the serialized form, whose escapes would confuse patterns)."""

    if isinstance(value, str):
        return redact(value, root)
    if isinstance(value, dict):
        return {k: _redact_value(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, root) for v in value]
    return value


def _create_or_find_data_pr(root: Path, branch: str, provider: str) -> dict[str, Any]:
    """Open or return the existing Owner-reviewed provider-data PR; never merge it."""

    listed = _run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url // empty"],
        cwd=root,
    )
    if listed.returncode != 0:
        raise ProviderSyncError(f"could not inspect open provider-data PRs: {_redacted_tail(listed.stderr, root, chars=300)}")
    existing = (listed.stdout or "").strip()
    if existing:
        if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", existing):
            raise ProviderSyncError("GitHub CLI returned an unrecognized provider-data PR URL")
        return {"url": existing, "created": False}

    body = (
        f"Provider-owned {provider} ledger and fresh Registration Census, with read-only generated projections.\n\n"
        "Review this data branch before merging. Merge to main is an Owner action; the main read-back workflow then verifies "
        "the exact merged commit and requires a fresh Census/projection digest chain. When provider fee inputs change, the "
        "branch includes the full analytics, health, Trade View and handoff refresh. No T-ID or canonical record is created by sync."
    )
    created = _run(
        [
            "gh", "pr", "create", "--base", "main", "--head", branch,
            "--title", f"data: {provider} ledger and projection refresh",
            "--body", body,
        ],
        cwd=root,
    )
    if created.returncode != 0:
        raise ProviderSyncError(f"could not create provider-data PR: {_redacted_tail(created.stderr or created.stdout, root, chars=300)}")
    url = (created.stdout or "").strip().splitlines()[-1] if (created.stdout or "").strip() else ""
    if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", url):
        raise ProviderSyncError("provider-data PR was created but GitHub CLI did not return a recognizable PR URL")
    return {"url": url, "created": True}


def run_provider_sync(root: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run the pipeline; nothing credential-like can leave through the report or an error message."""

    root = Path(root).resolve()
    try:
        report = _run_provider_sync(root, *args, **kwargs)
    except ProviderSyncError as exc:
        raise ProviderSyncError(redact(str(exc), root)) from None
    return _redact_value(report, root)


def _run_provider_sync(
    root: Path,
    provider: str = "propw",
    *,
    fetch: bool = False,
    commit: bool = False,
    push: bool = False,
    create_pr: bool = False,
    publish_main: bool = False,
    branch: str | None = None,
    db: Path | None = None,
    fetcher: Callable[[Path, Path], dict[str, Any]] | None = None,
    exporter: Callable[[Path, Path], dict[str, Any]] | None = None,
    today: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    provider = canonical_provider(provider)
    if push and not commit:
        raise ProviderSyncError("--push requires --commit")
    if create_pr and not push:
        raise ProviderSyncError("--create-pr requires --push (and --commit)")
    if publish_main and not commit:
        raise ProviderSyncError("--publish-main requires --commit")
    if publish_main and (push or create_pr or branch):
        raise ProviderSyncError("--publish-main replaces --push/--create-pr/--branch; do not combine them")
    if provider != "propw":
        raise ProviderSyncError(f"no sync pipeline for provider: {provider}")
    db_path = (root / db if not Path(db).is_absolute() else Path(db)) if db else root / DEFAULT_DB
    today = today or datetime.now(timezone.utc).strftime("%Y%m%d")
    current_branch = target_branch = None
    if commit and not publish_main:
        # Keep the preflight identity for the no-change PR-retry path below.
        current_branch, target_branch = _target_branch(root, provider, branch, today)  # fail closed BEFORE fetching or writing anything
    view_inputs = (
        root / "exports/operational/analytics/current.json",
        root / "exports/operational/health/current.json",
    )
    if (create_pr or publish_main) and not all(path.is_file() for path in view_inputs):
        flag = "--publish-main" if publish_main else "--create-pr"
        raise ProviderSyncError(f"{flag} requires current analytics and health projections for read-back")
    if publish_main:
        _ensure_head_is_origin_main(root)  # fail closed BEFORE writing anything
        _ensure_pr_read_model_sources_clean(root, "--publish-main")
    elif create_pr:
        _ensure_pr_read_model_sources_clean(root)
    elif commit and all(path.is_file() for path in view_inputs):
        # A full projection refresh reads canonical and registry sources as well as
        # provider data. Refuse to generate publishable read models from uncommitted
        # source edits; unrelated local work remains untouched.
        _ensure_projection_sources_clean(root)
    if commit and all(path.is_file() for path in view_inputs):
        # Projection refresh overwrites every path in this exact allowlist.
        # Check before invoking the exporter, which itself writes provider data.
        _ensure_projection_outputs_clean(root)
    ledger_dir = root / "provider-ledgers" / provider
    owned = owned_paths(provider)
    report: dict[str, Any] = {"provider": provider, "steps": {}}

    previous_counts = None
    census_path = ledger_dir / "registration-census.json"
    if census_path.exists():
        previous_counts = json.loads(census_path.read_text(encoding="utf-8")).get("counts")

    if fetch:
        report["steps"]["fetch"] = (fetcher or default_fetcher)(root, db_path)
    else:
        report["steps"]["fetch"] = {"ran": False, "note": "skipped (use --fetch to pull fresh PropW data with your local session)"}
    if not db_path.exists():
        raise ProviderSyncError(f"local PropW database not found: {db_path} (run with --fetch first)")

    export = (exporter or default_exporter)(root, db_path)
    report["steps"]["ledger"] = export
    report["steps"]["sanitizer_rescan"] = {"lines_scanned": verify_ledger_clean(ledger_dir, root)}
    report["steps"]["provider_sync_recency"] = {
        **_record_provider_sync_recency(root, db_path, provider),
        "fetched_this_run": bool(fetch),
    }

    write_census(root, provider)
    census = build_census(root, provider)
    projection_refresh = None
    projection_stale = all(path.is_file() for path in view_inputs) and _provider_fee_projection_is_stale(root)
    if projection_stale and commit:
        try:
            projection_refresh = refresh_projections(root)
        except (OSError, ValueError, KeyError) as exc:
            raise ProviderSyncError(f"provider projection refresh failed after provider-fee source change ({exc})") from exc
        census = build_census(root, provider)
        owned.extend(PROJECTION_OUTPUT_PATHS)
        report["steps"]["projection_refresh"] = {
            "status": projection_refresh["status"],
            "refresh_order": projection_refresh["refresh_order"],
            "analytics_artifact_digest": projection_refresh["verification"].get("analytics_artifact_digest"),
            "health_artifact_digest": projection_refresh["verification"].get("health_artifact_digest"),
        }
    elif projection_stale:
        # Prepare-only syncs may change provider inputs. Leave generated
        # projections untouched so a later --commit can refresh and publish
        # them in one retry-safe invocation.
        report["steps"]["projection_refresh"] = {
            "status": "DEFERRED",
            "note": "provider-fee inputs changed; rerun with --commit to refresh and publish projections",
        }
    report["steps"]["census"] = {
        "total": census["total"],
        "counts": census["counts"],
        "unregistered_closed": census["unregistered_closed"],
        "unregistered_candidates": len(select_candidates(census, phase="ALL")),
        "provider_phase_counts": {
            phase: sum(row.get("provider_phase") == phase for row in census["lifecycles"])
            for phase in ("OPEN", "PARTIALLY_CLOSED", "CLOSED", "UNKNOWN")
        },
        "freshness": census["freshness"],
        "source_digests": census["source_digests"],
        "previous_counts": previous_counts,
    }

    if all(path.is_file() for path in view_inputs) and commit:
        if projection_refresh is not None:
            view_manifest = projection_refresh["trade_view"]
        else:
            view_manifest = build_trade_view(root)
            owned.extend(TRADE_VIEW_OUTPUT_PATHS)
        report["steps"]["trade_view"] = {
            "status": "BUILT",
            "data_sha256": view_manifest["data_sha256"],
            "census_source_identity_sha256": view_manifest["registration_census_source_identity_sha256"],
            "census_freshness": view_manifest["registration_census_freshness"],
            "candidate_count": len(select_candidates(census, phase="ALL")),
        }
    elif all(path.is_file() for path in view_inputs):
        report["steps"]["trade_view"] = {
            "status": "DEFERRED",
            "note": "run with --commit to refresh and publish provider-dependent read models",
        }
    else:
        report["steps"]["trade_view"] = {"status": "SKIPPED", "note": "analytics/health read models are unavailable"}

    if create_pr or publish_main:
        try:
            verification = verify_projections(root, require_census_fresh=True)
        except (OSError, ValueError) as exc:
            target = "publish to main" if publish_main else "provider-data PR"
            raise ProviderSyncError(f"{target} blocked: Trade View read model or Census is stale ({exc})") from exc
        report["steps"]["projection_readback"] = {
            "status": verification["status"],
            "census_freshness": verification["registration_census_freshness"],
            "census_source_identity_sha256": verification["registration_census_source_identity_sha256"],
            "candidate_count": verification["registration_census_candidate_count"],
        }

    changed = _git(root, "status", "--porcelain", "--", *owned).stdout.strip() != ""
    report["changed"] = changed
    summary = (
        f"ledger: {export.get('counts')} (added {export.get('added')}, updated {export.get('updated')}); "
        f"census: {census['counts']}"
    )
    allowed_paths = [*owned_paths(provider), *PROJECTION_OUTPUT_PATHS]
    if commit and changed and publish_main:
        report["steps"]["git"] = _publish_main_step(
            root, owned, provider, today=today, summary=summary, allowed_paths=allowed_paths
        )
    elif commit and changed:
        report["steps"]["git"] = _git_step(
            root,
            owned,
            provider,
            commit=commit,
            push=push,
            branch=branch,
            today=today,
            summary=summary,
            allowed_paths=allowed_paths,
        )
        if create_pr and report["steps"]["git"].get("pushed"):
            report["steps"]["pull_request"] = _create_or_find_data_pr(root, report["steps"]["git"]["branch"], provider)
    elif commit:
        report["steps"]["git"] = {"committed": False, "pushed": False, "note": "NO CHANGE: ledger and census are already up to date"}
        if create_pr:
            if current_branch != target_branch:
                raise ProviderSyncError(
                    "--create-pr retry with no local changes requires the current branch to be its provider data branch"
                )
            _ensure_data_branch_scope(root, target_branch, [*owned_paths(provider), *PROJECTION_OUTPUT_PATHS])
            ahead = _git(root, "rev-list", "--count", f"main..{target_branch}")
            if ahead.returncode != 0:
                raise ProviderSyncError("could not verify provider data branch commits against main")
            if int((ahead.stdout or "0").strip() or "0") <= 0:
                raise ProviderSyncError("--create-pr retry found no provider data commit ahead of main")
            pushed = _git(root, "push", "-u", "origin", target_branch)
            if pushed.returncode != 0:
                raise ProviderSyncError(f"git push failed: {pushed.stderr.strip()[-300:]}")
            report["steps"]["git"].update({
                "branch": target_branch,
                "commit": _git(root, "rev-parse", "HEAD").stdout.strip(),
                "pushed": True,
                "note": "NO CHANGE: republished existing provider data branch to resume PR creation",
            })
            report["steps"]["pull_request"] = _create_or_find_data_pr(root, target_branch, provider)
    else:
        report["steps"]["git"] = {"committed": False, "pushed": False, "note": "not requested (use --commit; --push to publish)" if changed else "NO CHANGE"}
    report["status"] = "NO_CHANGE" if not changed else ("COMMITTED" if report["steps"]["git"].get("committed") else "READY_TO_COMMIT")
    return report
