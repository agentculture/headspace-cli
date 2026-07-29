"""Tests for the host-side input model (headspace.core.inputs).

Each test traces to one of this task's three acceptance criteria:

1. a directory expands to a sorted, deterministic per-file list; each file is
   read exactly once while its sha256 and size are computed.
2. a payload exceeding remaining ``storage_bytes`` is refused before any
   provider call, with a taxonomy exit code and a remediation naming the
   budget.
3. the recorded digest equals an independent sha256sum of the source; the
   manifest equals the file set exactly.

A fourth block enforces the layering rule this task's brief calls out
explicitly: the module imports nothing beyond the standard library and
``headspace.cli._errors`` (the same allowance already granted to
``policy.py``, ``states.py``, and ``profiles.py``).
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_POLICY_DENIED, EXIT_USER_ERROR, CliError
from headspace.core import inputs
from headspace.core.inputs import InputEntry, InputManifest, expand_input, precheck_storage_budget


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- criterion 1: sorted, deterministic expansion; each file read once ------


def test_directory_expands_to_a_sorted_deterministic_manifest(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _write(payload / "b" / "two.txt", b"two")
    _write(payload / "a.txt", b"a")
    _write(payload / "b" / "one.txt", b"one")
    _write(payload / "c" / "d" / "nested.txt", b"nested")

    manifest = expand_input(payload, "harness")

    destinations = [entry.destination for entry in manifest]
    assert destinations == sorted(destinations)
    assert destinations == [
        "harness/a.txt",
        "harness/b/one.txt",
        "harness/b/two.txt",
        "harness/c/d/nested.txt",
    ]


def test_expanding_the_same_directory_twice_yields_equal_manifests(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _write(payload / "a.txt", b"a" * 100)
    _write(payload / "sub" / "b.txt", b"b" * 200)

    first = expand_input(payload, "harness")
    second = expand_input(payload, "harness")

    assert first == second


def test_each_file_is_opened_exactly_once_while_hashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming proof: the implementation opens each file's content exactly
    once (never re-reads it to separately measure size), and never buffers a
    whole file -- exercised here with a chunk size much smaller than either
    payload file so the read loop must iterate.
    """
    payload = tmp_path / "payload"
    data_a = os.urandom(5000)
    data_b = os.urandom(3000)
    _write(payload / "a.bin", data_a)
    _write(payload / "sub" / "b.bin", data_b)

    open_counts: dict[str, int] = {}
    real_open = os.open

    def counting_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        key = os.fspath(path) if hasattr(path, "__fspath__") or isinstance(path, (str, bytes)) else str(path)
        open_counts[key] = open_counts.get(key, 0) + 1
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(inputs.os, "open", counting_open)

    manifest = expand_input(payload, "harness", chunk_size=64)

    assert len(manifest) == 2
    for entry in manifest:
        assert open_counts.get(str(entry.source), 0) == 1, (
            f"{entry.source} opened {open_counts.get(str(entry.source), 0)} times, expected 1"
        )
    # And the streaming read still produced correct results.
    by_dest = {entry.destination: entry for entry in manifest}
    assert by_dest["harness/a.bin"].sha256 == _sha256(data_a)
    assert by_dest["harness/a.bin"].size_bytes == len(data_a)
    assert by_dest["harness/sub/b.bin"].sha256 == _sha256(data_b)
    assert by_dest["harness/sub/b.bin"].size_bytes == len(data_b)


def test_a_single_file_expands_to_a_one_entry_manifest(tmp_path: Path) -> None:
    source = tmp_path / "solo.csv"
    source.write_bytes(b"col_a,col_b\n1,2\n")

    manifest = expand_input(source, "data/solo.csv")

    assert len(manifest) == 1
    entry = manifest.entries[0]
    assert entry.source == source
    assert entry.destination == "data/solo.csv"
    assert entry.sha256 == _sha256(source.read_bytes())
    assert entry.size_bytes == source.stat().st_size


def test_an_empty_directory_yields_an_empty_manifest(tmp_path: Path) -> None:
    payload = tmp_path / "empty"
    payload.mkdir()

    manifest = expand_input(payload, "harness")

    assert len(manifest) == 0
    assert manifest.total_bytes == 0


# --- criterion 3: independent digest match; manifest equals the file set ----


def test_recorded_digest_matches_an_independent_sha256sum(tmp_path: Path) -> None:
    source = tmp_path / "report.bin"
    payload = (b"x" * 70000) + b"tail"  # larger than one chunk at a small chunk_size
    source.write_bytes(payload)

    manifest = expand_input(source, "in/report.bin", chunk_size=4096)

    independent = hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.entries[0].sha256 == independent
    assert manifest.entries[0].sha256 == _sha256(payload)


def test_manifest_equals_the_directory_file_set_exactly(tmp_path: Path) -> None:
    payload = tmp_path / "harness"
    relative_paths = {
        "run.py",
        "lib/helper.py",
        "lib/data/config.json",
        "README",
    }
    for rel in relative_paths:
        _write(payload / rel, rel.encode() * 3)

    manifest = expand_input(payload, "workspace-root")

    expected_destinations = {f"workspace-root/{rel}" for rel in relative_paths}
    actual_destinations = {entry.destination for entry in manifest}
    assert actual_destinations == expected_destinations  # nothing extra, nothing missing

    # And every entry's digest matches the source file it claims to describe.
    for entry in manifest:
        assert entry.sha256 == _sha256(entry.source.read_bytes())
        assert entry.size_bytes == entry.source.stat().st_size


# --- criterion 2: budget precheck, before any provider call -----------------


def test_a_fitting_payload_is_not_refused(tmp_path: Path) -> None:
    source = tmp_path / "small.txt"
    source.write_bytes(b"fits easily")
    manifest = expand_input(source, "small.txt")

    precheck_storage_budget(manifest, storage_bytes_remaining=manifest.total_bytes)
    precheck_storage_budget(manifest, storage_bytes_remaining=manifest.total_bytes + 1)


def test_an_oversized_payload_is_refused_with_a_taxonomy_code_and_budget_remediation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"0123456789" * 100)  # 1000 bytes
    manifest = expand_input(source, "big.bin")

    with pytest.raises(CliError) as exc:
        precheck_storage_budget(manifest, storage_bytes_remaining=manifest.total_bytes - 1)

    assert exc.value.code == EXIT_POLICY_DENIED
    assert exc.value.category == "policy_denied"
    assert "storage_bytes" in exc.value.remediation
    assert exc.value.remediation


def test_budget_precheck_never_touches_the_filesystem_or_a_provider(tmp_path: Path) -> None:
    """The precheck operates purely on a manifest already in hand -- it takes
    no path argument at all, so structurally it cannot reach an engine or
    even the filesystem. A manifest built from files that no longer exist on
    disk still gets a correct refusal."""
    entry = InputEntry(
        source=tmp_path / "gone.bin",
        destination="gone.bin",
        size_bytes=999,
        sha256=_sha256(b"whatever"),
    )
    manifest = InputManifest((entry,))

    with pytest.raises(CliError) as exc:
        precheck_storage_budget(manifest, storage_bytes_remaining=10)
    assert exc.value.code == EXIT_POLICY_DENIED


# --- symlinks and special files: refused loudly, never silently -------------


def test_a_symlink_inside_a_directory_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    real = payload / "real.txt"
    real.write_bytes(b"actual content")
    link = payload / "link.txt"
    link.symlink_to(real)

    with pytest.raises(CliError) as exc:
        expand_input(payload, "harness")

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation


def test_a_dangling_symlink_is_refused_not_silently_skipped(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    link = payload / "dangling.txt"
    link.symlink_to(payload / "does-not-exist.txt")

    with pytest.raises(CliError) as exc:
        expand_input(payload, "harness")

    assert exc.value.code == EXIT_USER_ERROR


def test_a_symlinked_directory_is_refused_not_silently_skipped(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "f.txt").write_bytes(b"data")
    payload.mkdir()
    (payload / "linked_dir").symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(CliError) as exc:
        expand_input(payload, "harness")

    assert exc.value.code == EXIT_USER_ERROR


def test_a_symlink_as_the_top_level_host_path_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_bytes(b"data")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    with pytest.raises(CliError) as exc:
        expand_input(link, "dest.txt")

    assert exc.value.code == EXIT_USER_ERROR


def test_a_special_file_inside_a_directory_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    fifo_path = payload / "pipe"
    os.mkfifo(fifo_path)

    with pytest.raises(CliError) as exc:
        expand_input(payload, "harness")

    assert exc.value.code == EXIT_USER_ERROR


# --- structural / input validation edges -------------------------------


def test_a_missing_host_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CliError) as exc:
        expand_input(tmp_path / "nope", "dest")

    assert exc.value.code == EXIT_USER_ERROR
    assert exc.value.remediation


def test_an_absolute_destination_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "f.txt"
    source.write_bytes(b"data")

    with pytest.raises(CliError) as exc:
        expand_input(source, "/etc/passwd")

    assert exc.value.code == EXIT_USER_ERROR


def test_a_destination_escaping_the_workspace_root_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "f.txt"
    source.write_bytes(b"data")

    with pytest.raises(CliError) as exc:
        expand_input(source, "../escape.txt")

    assert exc.value.code == EXIT_USER_ERROR


def test_an_empty_destination_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "f.txt"
    source.write_bytes(b"data")

    with pytest.raises(CliError) as exc:
        expand_input(source, "   ")

    assert exc.value.code == EXIT_USER_ERROR


def test_a_non_positive_chunk_size_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "f.txt"
    source.write_bytes(b"data")

    with pytest.raises(CliError) as exc:
        expand_input(source, "dest", chunk_size=0)

    assert exc.value.code == EXIT_USER_ERROR


# --- InputEntry / InputManifest field discipline -----------------------


def test_input_entry_rejects_a_negative_size() -> None:
    with pytest.raises(CliError) as exc:
        InputEntry(source=Path("x"), destination="x", size_bytes=-1, sha256=_sha256(b"x"))
    assert exc.value.code == EXIT_USER_ERROR


def test_input_entry_rejects_a_malformed_digest() -> None:
    with pytest.raises(CliError) as exc:
        InputEntry(source=Path("x"), destination="x", size_bytes=1, sha256="not-hex")
    assert exc.value.code == EXIT_USER_ERROR


def test_input_entry_rejects_an_empty_destination() -> None:
    with pytest.raises(CliError) as exc:
        InputEntry(source=Path("x"), destination=" ", size_bytes=1, sha256=_sha256(b"x"))
    assert exc.value.code == EXIT_USER_ERROR


def test_manifest_to_list_is_json_serializable_and_carries_every_field(tmp_path: Path) -> None:
    source = tmp_path / "f.txt"
    source.write_bytes(b"payload")
    manifest = expand_input(source, "f.txt")

    entries = manifest.to_list()
    assert entries == [
        {
            "source": str(source),
            "destination": "f.txt",
            "size_bytes": 7,
            "sha256": _sha256(b"payload"),
        }
    ]


# --- layering: stdlib + headspace.cli._errors only --------------------------


def test_inputs_module_imports_only_stdlib_and_the_shared_error_contract() -> None:
    """headspace/core/__init__.py's layering rule bars a provider or the
    docker SDK from any core module. This task's brief goes further: the only
    headspace import this module is permitted is ``headspace.cli._errors`` --
    the same narrow allowance already granted to policy.py, states.py, and
    profiles.py (see their module docstrings) -- enforced here at the source
    level so it cannot silently regress as the module grows."""
    source = Path(inputs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        name
        for name in imported
        if name == "docker" or name.startswith("docker.") or name.startswith("headspace.providers")
    }
    assert not forbidden

    headspace_imports = {name for name in imported if name.startswith("headspace")}
    assert headspace_imports <= {"headspace.cli._errors"}, headspace_imports
