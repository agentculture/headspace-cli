"""Tests for the artifact inventory and the atomic, digest-verified export.

Two promises are under test, one per acceptance criterion:

1. an export publishes the final path only after the digest verifies, so an
   interruption at any point leaves nothing there — and never damages an
   artifact already sitting at that path;
2. every inventory entry carries the full field set the result package renders
   (name, type, size, digest, purpose, retention status).

The interruption tests interrupt for real: a source generator that raises
mid-stream, and one that raises ``KeyboardInterrupt`` mid-stream.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from headspace.core.artifacts import (
    RETENTION_DECLARED,
    RETENTION_DISCARDED,
    RETENTION_EXPORTED,
    ArtifactInventory,
    ArtifactRecord,
    export_artifact,
    partial_path,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunks(data: bytes, size: int = 8) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _exports_dir(tmp_path: Path) -> Path:
    d = tmp_path / "exports"
    d.mkdir()
    return d


# --- criterion 1: atomic, digest-verified export --------------------------


def test_export_streams_to_partial_then_renames(tmp_path: Path) -> None:
    """Happy path: content lands whole at the final path, no sidecar survives."""
    payload = b"col_a,col_b\n" + b"1,2\n" * 200
    dest = _exports_dir(tmp_path) / "report.csv"

    record = export_artifact(
        _chunks(payload),
        dest,
        purpose="the computation's tabular result",
        content_type="text/csv",
        expected_sha256=_sha256(payload),
    )

    assert dest.read_bytes() == payload
    assert record.name == "report.csv"
    assert record.sha256 == _sha256(payload)
    assert record.size_bytes == len(payload)
    assert record.retention == RETENTION_EXPORTED
    # The .partial sidecar is consumed by the rename, not left behind.
    assert not partial_path(dest).exists()
    assert list(dest.parent.iterdir()) == [dest]


def test_export_accepts_a_binary_file_object(tmp_path: Path) -> None:
    """The provider hands over a stream, not a list — reading via .read() works."""
    payload = b"stream-me" * 50
    source_file = tmp_path / "source.bin"
    source_file.write_bytes(payload)
    dest = _exports_dir(tmp_path) / "copied.bin"

    with source_file.open("rb") as handle:
        record = export_artifact(handle, dest, purpose="raw capture", chunk_size=7)

    assert dest.read_bytes() == payload
    assert record.sha256 == _sha256(payload)
    assert record.size_bytes == len(payload)


def test_export_consumes_the_source_lazily(tmp_path: Path) -> None:
    """Streaming proof: each chunk is on disk before the next one is pulled.

    The source generator inspects the destination directory between yields. If
    the implementation slurped the whole source into memory first, the sidecar
    would still be empty at every observation; if it published early, the final
    path would exist mid-stream. Both are asserted against.
    """
    dest = _exports_dir(tmp_path) / "big.bin"
    partial = partial_path(dest)
    parts = [b"a" * 8, b"b" * 8, b"c" * 8]
    observed: list[tuple[int, bool]] = []

    def watching_source() -> Iterator[bytes]:
        for part in parts:
            observed.append((partial.stat().st_size if partial.exists() else -1, dest.exists()))
            yield part

    record = export_artifact(watching_source(), dest, purpose="streaming proof")

    # Bytes already durable when the next chunk was requested: 0, 8, 16.
    assert [size for size, _ in observed] == [0, 8, 16]
    # The final path never existed while the copy was in flight.
    assert [existed for _, existed in observed] == [False, False, False]
    assert dest.read_bytes() == b"".join(parts)
    assert record.size_bytes == 24


def test_export_refuses_to_publish_on_digest_mismatch(tmp_path: Path) -> None:
    """A digest that does not verify is a failure — nothing gets published."""
    dest = _exports_dir(tmp_path) / "report.csv"

    with pytest.raises(CliError) as exc:
        export_artifact(
            _chunks(b"payload that will not match"),
            dest,
            purpose="tabular result",
            expected_sha256="0" * 64,
        )

    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation
    assert not dest.exists()
    assert not partial_path(dest).exists()
    assert list(dest.parent.iterdir()) == []


def test_source_failing_midstream_leaves_no_file_at_final_path(tmp_path: Path) -> None:
    """Criterion 1's interruption: the source dies after the first chunk."""
    dest = _exports_dir(tmp_path) / "report.csv"

    def dying_source() -> Iterator[bytes]:
        yield b"first half of the artifact"
        raise RuntimeError("source stream died")

    with pytest.raises(CliError) as exc:
        export_artifact(dying_source(), dest, purpose="tabular result")

    assert exc.value.code == EXIT_ENV_ERROR
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert not dest.exists()
    assert not partial_path(dest).exists()
    assert list(dest.parent.iterdir()) == []


def test_keyboard_interrupt_midstream_publishes_nothing(tmp_path: Path) -> None:
    """A real interrupt propagates untouched and still cleans up the sidecar."""
    dest = _exports_dir(tmp_path) / "report.csv"

    def interrupted_source() -> Iterator[bytes]:
        yield b"first half of the artifact"
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        export_artifact(interrupted_source(), dest, purpose="tabular result")

    assert not dest.exists()
    assert not partial_path(dest).exists()
    assert list(dest.parent.iterdir()) == []


def test_failed_export_leaves_a_previous_good_artifact_untouched(tmp_path: Path) -> None:
    """A failed export must not destroy the artifact already at that path."""
    dest = _exports_dir(tmp_path) / "report.csv"
    previous = b"the previous, good, verified artifact"
    dest.write_bytes(previous)

    def dying_source() -> Iterator[bytes]:
        yield b"replacement bytes"
        raise OSError("volume went away")

    with pytest.raises(CliError):
        export_artifact(dying_source(), dest, purpose="tabular result")
    assert dest.read_bytes() == previous

    with pytest.raises(CliError):
        export_artifact(
            _chunks(b"replacement bytes"),
            dest,
            purpose="tabular result",
            expected_sha256="1" * 64,
        )
    assert dest.read_bytes() == previous
    assert not partial_path(dest).exists()


def test_export_rejects_a_missing_destination_directory(tmp_path: Path) -> None:
    dest = tmp_path / "no-such-dir" / "report.csv"

    with pytest.raises(CliError) as exc:
        export_artifact(_chunks(b"payload"), dest, purpose="tabular result")

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation
    assert not dest.exists()


def test_export_rejects_a_malformed_expected_digest(tmp_path: Path) -> None:
    dest = _exports_dir(tmp_path) / "report.csv"

    with pytest.raises(CliError) as exc:
        export_artifact(
            _chunks(b"payload"), dest, purpose="tabular result", expected_sha256="deadbeef"
        )

    assert exc.value.code == EXIT_USER_ERROR
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_export_rejects_a_non_bytes_chunk(tmp_path: Path) -> None:
    dest = _exports_dir(tmp_path) / "report.csv"

    def text_source() -> Iterator[object]:
        yield "not bytes"

    with pytest.raises(CliError) as exc:
        export_artifact(text_source(), dest, purpose="tabular result")  # type: ignore[arg-type]

    assert exc.value.code == EXIT_USER_ERROR
    assert not dest.exists()
    assert not partial_path(dest).exists()


# --- criterion 2: the inventory -------------------------------------------


def test_declared_artifact_carries_its_identity_fields(tmp_path: Path) -> None:
    inventory = ArtifactInventory()
    record = inventory.declare("report.csv", content_type="text/csv", purpose="the tabular result")

    assert record.name == "report.csv"
    assert record.content_type == "text/csv"
    assert record.purpose == "the tabular result"
    assert record.retention == RETENTION_DECLARED
    # Size and digest are honestly unknown before the export, not a fake zero.
    assert record.size_bytes is None
    assert record.sha256 is None
    # Every field of the dataclass is serialized — including fields added later.
    assert set(record.to_dict()) == {f.name for f in dataclasses.fields(ArtifactRecord)}


def test_every_field_of_an_exported_record_is_populated(tmp_path: Path) -> None:
    """Criterion 2's field guard: a future field left unset fails this test."""
    payload = b"a,b\n1,2\n"
    dest = _exports_dir(tmp_path) / "report.csv"
    inventory = ArtifactInventory()
    inventory.declare("report.csv", content_type="text/csv", purpose="the tabular result")

    exported = export_artifact(
        _chunks(payload),
        dest,
        purpose="the tabular result",
        content_type="text/csv",
    )
    record = inventory.mark_exported(
        exported.name, size_bytes=exported.size_bytes, sha256=exported.sha256
    )

    fields = dataclasses.fields(ArtifactRecord)
    assert fields, "ArtifactRecord must declare fields"
    for field in fields:
        value = getattr(record, field.name)
        assert value is not None, f"exported record leaves {field.name} unset"
        if isinstance(value, str):
            assert value.strip(), f"exported record leaves {field.name} empty"

    assert record.retention == RETENTION_EXPORTED
    assert record.sha256 == _sha256(payload)
    assert record.size_bytes == len(payload)


def test_inventory_reports_every_declared_artifact(tmp_path: Path) -> None:
    inventory = ArtifactInventory()
    inventory.declare("a.csv", content_type="text/csv", purpose="the tabular result")
    inventory.declare("b.log", content_type="text/plain", purpose="the solver trace")
    inventory.declare("c.bin", purpose="the intermediate heap dump")
    inventory.mark_exported("a.csv", size_bytes=12, sha256=_sha256(b"a,b\n1,2\n"))
    inventory.mark_discarded("c.bin")

    entries = inventory.to_list()
    assert [entry["name"] for entry in entries] == ["a.csv", "b.log", "c.bin"]
    required = {"name", "content_type", "size_bytes", "sha256", "purpose", "retention"}
    for entry in entries:
        assert required <= set(entry), f"missing fields in {entry}"
    assert [entry["retention"] for entry in entries] == [
        RETENTION_EXPORTED,
        RETENTION_DECLARED,
        RETENTION_DISCARDED,
    ]
    # The destroy guard's input: declared, nothing on disk yet.
    assert [record.name for record in inventory.unexported()] == ["b.log"]
    assert len(inventory) == 3
    assert "a.csv" in inventory
    assert inventory.get("missing.csv") is None


def test_record_round_trips_through_its_serialized_form() -> None:
    record = ArtifactRecord(
        name="report.csv",
        content_type="text/csv",
        purpose="the tabular result",
        retention=RETENTION_EXPORTED,
        size_bytes=8,
        sha256=_sha256(b"a,b\n1,2\n"),
    )

    payload = json.dumps(record.to_dict())
    assert ArtifactRecord.from_dict(json.loads(payload)) == record


def test_unknown_retention_status_is_rejected() -> None:
    with pytest.raises(CliError) as exc:
        ArtifactRecord(
            name="report.csv",
            content_type="text/csv",
            purpose="the tabular result",
            retention="probably-fine",
        )
    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation


def test_record_rejects_an_empty_name_or_purpose() -> None:
    for name, purpose in ((" ", "the tabular result"), ("report.csv", "")):
        with pytest.raises(CliError) as exc:
            ArtifactRecord(
                name=name,
                content_type="text/csv",
                purpose=purpose,
                retention=RETENTION_DECLARED,
            )
        assert exc.value.code == EXIT_USER_ERROR


def test_declaring_the_same_name_twice_is_rejected() -> None:
    inventory = ArtifactInventory()
    inventory.declare("report.csv", content_type="text/csv", purpose="the tabular result")

    with pytest.raises(CliError) as exc:
        inventory.declare("report.csv", content_type="text/csv", purpose="a second one")

    assert exc.value.code == EXIT_USER_ERROR
    assert len(inventory) == 1


def test_marking_an_undeclared_artifact_is_rejected() -> None:
    inventory = ArtifactInventory()

    with pytest.raises(CliError) as exc:
        inventory.mark_exported("ghost.csv", size_bytes=1, sha256=_sha256(b"x"))

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation
