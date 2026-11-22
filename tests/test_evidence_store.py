from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trading_os.evidence_store import EvidenceStore, EvidenceStoreError


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x01" * 32
# Minimal valid WebP container: RIFF header + size + "WEBP" fourcc, followed
# by a VP8 chunk header and a little padding -- enough for the store's
# magic-byte sniff (RIFF....WEBP), which is all it needs to identify.
WEBP_BYTES = b"RIFF" + (24).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 16


def test_ingest_png_is_content_addressed(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    expected_sha = hashlib.sha256(PNG_BYTES).hexdigest()
    assert result["sha256"] == expected_sha
    assert result["media_type"] == "image/png"
    assert result["byte_size"] == len(PNG_BYTES)
    assert result["original_filename"] == "chart.png"
    assert result["deduped"] is False
    assert result["locator"] == f"artifact:{expected_sha}.png"
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{expected_sha}.png"
    assert artifact_path.read_bytes() == PNG_BYTES


def test_ingest_json_keeps_artifact_bytes_separate_from_metadata(tmp_path: Path) -> None:
    payload = b'{"open_id":"2506050933259130"}\n'
    source = tmp_path / "provider_truth.json"
    source.write_bytes(payload)
    store = EvidenceStore(tmp_path)

    first = store.ingest(source)
    expected_sha = hashlib.sha256(payload).hexdigest()
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{expected_sha}.json"
    metadata_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{expected_sha}.metadata.json"

    assert first["sha256"] == expected_sha
    assert first["media_type"] == "application/json"
    assert first["locator"] == f"artifact:{expected_sha}.json"
    assert artifact_path.read_bytes() == payload
    assert metadata_path.exists()
    assert store.metadata(expected_sha)["sha256"] == expected_sha

    second = store.ingest(source)
    assert second["deduped"] is True
    assert second["locator"] == first["locator"]
    assert artifact_path.read_bytes() == payload


def test_ingest_jpeg_detects_media_type_from_magic_bytes(tmp_path: Path) -> None:
    source = tmp_path / "entry.jpg"
    source.write_bytes(JPEG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    assert result["media_type"] == "image/jpeg"
    assert result["locator"].endswith(".jpg")


def test_ingest_webp_detects_media_type_from_magic_bytes(tmp_path: Path) -> None:
    source = tmp_path / "chart.webp"
    source.write_bytes(WEBP_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    expected_sha = hashlib.sha256(WEBP_BYTES).hexdigest()
    assert result["media_type"] == "image/webp"
    assert result["locator"] == f"artifact:{expected_sha}.webp"
    assert result["deduped"] is False
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{expected_sha}.webp"
    assert artifact_path.read_bytes() == WEBP_BYTES

    # Same dedupe guarantees as PNG/JPEG: re-ingesting identical WebP bytes
    # resolves to the same locator without writing a second artifact.
    second = store.ingest(source)
    assert second["deduped"] is True
    assert second["locator"] == result["locator"]


def test_ingest_same_file_twice_dedupes_without_duplicating_bytes(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    first = store.ingest(source)
    second = store.ingest(source)
    assert first["deduped"] is False
    assert second["deduped"] is True
    assert first["locator"] == second["locator"]
    assert first["sha256"] == second["sha256"]
    artifacts = list((tmp_path / "canonical" / "evidence" / "artifacts").glob("*.png"))
    assert len(artifacts) == 1


def test_ingest_same_bytes_different_filename_still_dedupes_and_records_both_names(tmp_path: Path) -> None:
    first_source = tmp_path / "a" / "chart.png"
    first_source.parent.mkdir(parents=True)
    first_source.write_bytes(PNG_BYTES)
    second_source = tmp_path / "b" / "renamed-chart.png"
    second_source.parent.mkdir(parents=True)
    second_source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    first = store.ingest(first_source)
    second = store.ingest(second_source)
    assert first["locator"] == second["locator"]
    assert second["deduped"] is True
    metadata = store.metadata(first["sha256"])
    assert set(metadata["original_filenames"]) == {"chart.png", "renamed-chart.png"}


def test_ingest_preserves_validated_original_filename_identity(tmp_path: Path) -> None:
    source = tmp_path / "runtime-controlled.png"
    source.write_bytes(PNG_BYTES)

    result = EvidenceStore(tmp_path).ingest(source, original_filename="Owner screenshot 01.png")

    metadata = EvidenceStore(tmp_path).metadata(result["sha256"])
    assert result["original_filename"] == "Owner screenshot 01.png"
    assert metadata["original_filenames"] == ["Owner screenshot 01.png"]


@pytest.mark.parametrize(
    "unsafe_name",
    ["../escape.png", r"C:\\outside.png", r"\\\\server\\share\\x.png", "NUL.png", "COM¹.png", "LPT³.png"],
)
def test_ingest_rejects_unsafe_original_filename_identity(tmp_path: Path, unsafe_name: str) -> None:
    source = tmp_path / "runtime-controlled.png"
    source.write_bytes(PNG_BYTES)

    with pytest.raises(EvidenceStoreError, match="original_filename"):
        EvidenceStore(tmp_path).ingest(source, original_filename=unsafe_name)

    assert not (tmp_path / "canonical" / "evidence" / "artifacts").exists()


def test_ingest_migrates_legacy_metadata_without_overwriting_artifact(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    sha256 = hashlib.sha256(PNG_BYTES).hexdigest()
    artifacts = tmp_path / "canonical" / "evidence" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / f"{sha256}.png").write_bytes(PNG_BYTES)
    (artifacts / f"{sha256}.json").write_text(
        json.dumps({
            "schema_version": "evidence-artifact.v1",
            "sha256": sha256,
            "locator": f"artifact:{sha256}.png",
            "media_type": "image/png",
            "byte_size": len(PNG_BYTES),
            "original_filename": "chart.png",
            "original_filenames": ["chart.png"],
            "ingested_at": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    store = EvidenceStore(tmp_path)
    result = store.ingest(source)

    assert result["deduped"] is True
    assert (artifacts / f"{sha256}.png").read_bytes() == PNG_BYTES
    assert store.metadata(sha256)["original_filenames"] == ["chart.png"]
    assert (artifacts / f"{sha256}.metadata.json").exists()


def test_ingest_rejects_missing_file(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    with pytest.raises(EvidenceStoreError, match="does not exist"):
        store.ingest(tmp_path / "missing.png")


def test_ingest_rejects_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    store = EvidenceStore(tmp_path)
    with pytest.raises(EvidenceStoreError, match="empty"):
        store.ingest(empty)


def test_ingest_does_not_let_a_traversal_shaped_path_escape_the_store(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    real_file = tmp_path / "sub" / "evil.png"
    real_file.write_bytes(PNG_BYTES)
    traversal_path = tmp_path / "sub" / ".." / "sub" / "evil.png"
    store = EvidenceStore(tmp_path)
    result = store.ingest(traversal_path)
    artifacts_dir = (tmp_path / "canonical" / "evidence" / "artifacts").resolve()
    stored = artifacts_dir / f"{result['sha256']}.png"
    assert stored.exists()
    assert stored.resolve().parent == artifacts_dir
    # The only sanctioned identity of the input is its basename + content
    # hash; the traversal-shaped path never leaks into the stored metadata.
    assert result["original_filename"] == "evil.png"


def test_stored_bytes_survive_rehash_and_a_hash_mismatch_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{result['sha256']}.png"
    # Re-reading and re-hashing the persisted bytes must match the name.
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == result["sha256"]
    # Corrupt the stored artifact out from under the store; the next ingest
    # of the same logical content must refuse to silently overwrite it.
    artifact_path.write_bytes(b"corrupted")
    with pytest.raises(EvidenceStoreError, match="content hash"):
        store.ingest(source)


def test_one_artifact_can_be_referenced_from_multiple_evidence_categories(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    pre_trade = store.ingest(source)
    review = store.ingest(source)
    assert pre_trade["locator"] == review["locator"]
    # Both "evidence entries" (simulated here as plain dicts referencing the
    # locator) resolve to the exact same single stored file.
    entries = [
        {"category": "pre_trade_context", "source_ref": pre_trade["locator"], "sha256": pre_trade["sha256"]},
        {"category": "review", "source_ref": review["locator"], "sha256": review["sha256"]},
    ]
    assert len({entry["source_ref"] for entry in entries}) == 1
    artifacts = list((tmp_path / "canonical" / "evidence" / "artifacts").glob("*.png"))
    assert len(artifacts) == 1



def test_read_requires_correct_content_address_and_returns_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)

    assert store.read(result["locator"], expected_sha256=result["sha256"]) == PNG_BYTES
    with pytest.raises(EvidenceStoreError, match="does not match expected sha256"):
        store.read(result["locator"], expected_sha256="0" * 64)


def test_read_rejects_missing_or_corrupted_artifact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    artifact_path = tmp_path / "canonical" / "evidence" / "artifacts" / result["locator"].removeprefix("artifact:")

    artifact_path.unlink()
    with pytest.raises(EvidenceStoreError, match="missing"):
        store.read(result["locator"], expected_sha256=result["sha256"])

    artifact_path.write_bytes(b"tampered")
    with pytest.raises(EvidenceStoreError, match="do not match their content address"):
        store.read(result["locator"], expected_sha256=result["sha256"])


@pytest.mark.parametrize(
    "locator",
    ["artifact:full", "artifact:../outside.png", "artifact:" + "a" * 64 + ".png/../../outside"],
)
def test_read_rejects_legacy_or_unsafe_locators(tmp_path: Path, locator: str) -> None:
    with pytest.raises(EvidenceStoreError, match="canonical content address"):
        EvidenceStore(tmp_path).read(locator)

def test_read_rejects_corrupt_metadata_when_present(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(PNG_BYTES)
    store = EvidenceStore(tmp_path)
    result = store.ingest(source)
    metadata_path = tmp_path / "canonical" / "evidence" / "artifacts" / f"{result['sha256']}.metadata.json"
    metadata_path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(EvidenceStoreError, match="metadata is unreadable"):
        store.read(result["locator"], expected_sha256=result["sha256"])
