"""Readability probes for referenced evidence assets.

The existing evidence diagnostics only ask whether an embed *resolves* to a
file on disk.  That check passed for
``assets/2026-08-24_BTCUSDCP_L_vt-long_position.jpg``, a 180x390 JPEG whose
entropy stream is truncated and which no decoder can open, while a human
attestation recorded it as ``artifact_supported``.  A file that exists is not
the same as evidence a person can re-read.

This module closes that gap with header-level probes only.  It uses the
standard library so the package keeps its single PyYAML dependency, and it
never decodes pixel data: the question is whether an asset is structurally
intact and large enough to carry a readable price axis, not what it depicts.

Every result is a *review reason*, never a decision.  Per the Phase 4B
operating model a queue item cannot create or retract a claim; a person
adjudicates what the reason means.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# A chart screenshot has to survive being read back by a person: price axis
# labels, entry/stop/target values and structure annotations all have to stay
# legible.  400px on the short edge is the floor at which the corpus's phone
# captures (591x1280) remain readable and the known-bad 180x390 renditions do
# not.  It is a review threshold, not a decision: crossing it queues the asset
# for a person, it does not retract any claim.
MIN_LEGIBLE_SHORT_EDGE = 400

RASTER_SUFFIXES = {".png", ".jpg", ".jpeg"}
VECTOR_SUFFIXES = {".svg"}

# Reason codes, ordered from most to least severe.
UNDECODABLE = "undecodable_evidence_ref"
ILLEGIBLE = "illegible_evidence_ref"
NON_RASTER = "non_raster_evidence_ref"
UNPROBEABLE = "unprobeable_evidence_ref"


@dataclass
class AssetProbe:
    """Structural facts about one evidence asset.

    ``reasons`` is empty when the asset is intact and large enough to read.
    ``detail`` carries the machine-verifiable basis for each reason so a
    reviewer does not have to re-derive it.
    """

    path: str
    fmt: str | None = None
    width: int | None = None
    height: int | None = None
    intact: bool = False
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def short_edge(self) -> int | None:
        if self.width is None or self.height is None:
            return None
        return min(self.width, self.height)


def _probe_png(data: bytes) -> tuple[int | None, int | None, bool, str]:
    """Return width, height, intact, detail for a PNG byte string."""

    if len(data) < 24 or data[12:16] != b"IHDR":
        return None, None, False, "PNG header missing or truncated IHDR"
    width, height = struct.unpack(">II", data[16:24])
    # A PNG that never reaches IEND was cut off in transit or on disk.
    if not data.rstrip(b"\x00").endswith(b"IEND\xaeB`\x82"):
        return width, height, False, "PNG stream truncated: no IEND chunk"
    return width, height, True, "PNG intact"


def _probe_jpeg(data: bytes) -> tuple[int | None, int | None, bool, str]:
    """Return width, height, intact, detail for a JPEG byte string.

    Walks the marker chain to the frame header for dimensions, then checks
    that the entropy-coded stream actually terminates.  A missing EOI marker
    is what distinguishes the corpus's unreadable capture from its readable
    ones.
    """

    index = 2
    width = height = None
    saw_frame = False
    while index < len(data) - 1:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            break
        if index + 4 > len(data):
            return width, height, False, "JPEG truncated inside a segment header"
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        # SOF0-SOF15 carry the frame size; DHT/JPG/DAC share the range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > len(data):
                return None, None, False, "JPEG truncated inside its frame header"
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            saw_frame = True
        if marker == 0xDA:
            ends_with_eoi = data.rstrip(b"\x00")[-2:] == b"\xff\xd9"
            if not ends_with_eoi:
                return width, height, False, "JPEG entropy stream truncated: no EOI marker"
            if not saw_frame:
                return width, height, False, "JPEG has no frame header"
            return width, height, True, "JPEG intact"
        index += 2 + segment_length
    return width, height, False, "JPEG has no scan segment"


def probe_asset(root: Path, relative_path: str) -> AssetProbe:
    """Probe one evidence asset for structural integrity and legible size."""

    probe = AssetProbe(path=relative_path)
    target = root / relative_path
    if not target.is_file():
        # Absence is already reported as unresolved_evidence_ref upstream;
        # this module stays silent so a missing file is not counted twice.
        return probe

    suffix = target.suffix.lower()
    if suffix in VECTOR_SUFFIXES:
        probe.fmt = "svg"
        text = target.read_text(encoding="utf-8", errors="replace")
        # A capture embedded in an SVG wrapper still carries pixels; a drawing
        # made of rects and text carries only what its author typed.
        if "<image" not in text:
            probe.reasons.append(NON_RASTER)
            probe.detail[NON_RASTER] = (
                f"vector file with no embedded raster: "
                f"{text.count('<rect')} rect / {text.count('<text')} text elements, "
                f"0 image elements"
            )
        else:
            probe.intact = True
        return probe

    if suffix not in RASTER_SUFFIXES:
        probe.reasons.append(UNPROBEABLE)
        probe.detail[UNPROBEABLE] = f"no readability probe for suffix {suffix or '(none)'}"
        return probe

    data = target.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        probe.fmt = "png"
        width, height, intact, detail = _probe_png(data)
    elif data[:2] == b"\xff\xd8":
        probe.fmt = "jpeg"
        width, height, intact, detail = _probe_jpeg(data)
    else:
        probe.reasons.append(UNDECODABLE)
        probe.detail[UNDECODABLE] = f"content is not a PNG or JPEG despite the {suffix} suffix"
        return probe

    probe.width, probe.height, probe.intact = width, height, intact
    if not intact:
        probe.reasons.append(UNDECODABLE)
        probe.detail[UNDECODABLE] = detail
        return probe

    short_edge = probe.short_edge
    if short_edge is not None and short_edge < MIN_LEGIBLE_SHORT_EDGE:
        probe.reasons.append(ILLEGIBLE)
        probe.detail[ILLEGIBLE] = (
            f"{width}x{height}: short edge {short_edge}px is below the "
            f"{MIN_LEGIBLE_SHORT_EDGE}px review threshold for a re-readable chart"
        )
    return probe


def readability_reasons(root: Path, relative_paths: list[str]) -> tuple[list[str], list[dict[str, object]]]:
    """Probe several assets and return deduplicated reasons plus their facts."""

    reasons: list[str] = []
    facts: list[dict[str, object]] = []
    for relative_path in relative_paths:
        probe = probe_asset(root, relative_path)
        if not probe.reasons:
            continue
        reasons.extend(probe.reasons)
        facts.append(
            {
                "path": probe.path,
                "format": probe.fmt,
                "pixels": None if probe.width is None else f"{probe.width}x{probe.height}",
                "reasons": probe.reasons,
                "detail": probe.detail,
            }
        )
    return list(dict.fromkeys(reasons)), facts
