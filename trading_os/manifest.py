from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from .contracts import validate_evidence_manifest

IMAGE_RE = re.compile(r"!\[\[([^\]|#]+)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: Path) -> dict:
    root = root.resolve()
    entries = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix().lower()):
        if not path.is_file() or ".git" in path.parts or "canonical" in path.parts or "exports" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        media = "text/markdown" if path.suffix.lower() == ".md" else "image" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".webp"} else "other"
        content_hash = sha256(path)
        # Filesystem mtime is optional observational provenance only. It is
        # absent from this deterministic payload and never affects source
        # identity, manifest digest, event date, or strategy version.
        item = {"source_record_id":"src_" + hashlib.sha256((rel + content_hash).encode()).hexdigest()[:24], "relative_path": rel, "media_type": media, "size_bytes": path.stat().st_size, "sha256": content_hash, "parser_status": "parsed" if media == "text/markdown" else "unsupported", "image_references": [], "errors": [], "warnings": []}
        if media == "text/markdown":
            text = path.read_text(encoding="utf-8", errors="replace")
            for raw in sorted(set(IMAGE_RE.findall(text)), key=str.lower):
                if raw.startswith(("http://", "https://")):
                    item["image_references"].append({"raw_ref": raw, "resolved_relative_path": None, "status": "external"})
                    continue
                candidates = [c for c in (root / "assets" / raw, path.parent / raw, root / raw) if c.exists()]
                statuses = "missing" if not candidates else "ambiguous" if len({c.resolve() for c in candidates}) > 1 else "found"
                item["image_references"].append({"raw_ref": raw, "resolved_relative_path": candidates[0].relative_to(root).as_posix() if len(candidates)==1 else None, "status": statuses})
        if validate_evidence_manifest({**item, "manifest_version":"evidence-manifest.v1"}): item["parser_status"]="failed"
        entries.append(item)
    payload = {"manifest_version": "evidence-manifest.v1", "root": root.name, "entries": entries}
    payload["entry_count"] = len(entries)
    payload["manifest_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload


def write_manifest(root: Path, output: Path) -> dict:
    before = {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "canonical" not in p.parts and "exports" not in p.parts}
    payload = build_manifest(root)
    after = {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts and "canonical" not in p.parts and "exports" not in p.parts}
    if before != after: raise RuntimeError("source mutation detected during manifest generation")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload
