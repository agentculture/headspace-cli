"""Artifact inventory and atomic, digest-verified export (durability boundary).

Everything inside a headspace is disposable — the container, the volume, the
transcript. Artifacts are the exception: they are the only thing that outlives
the workspace. That makes an export a *durability promise*, and the one outcome
that must be impossible is a truncated file sitting at the destination path
looking like a finished artifact. A caller that trusts a partial artifact is
worse off than one that got a loud failure.

So an export never writes the final path directly. Content streams to a
``.partial`` sidecar in the destination directory; the sha256 is computed
*during* the copy (one pass, never a re-read, never the whole artifact in
memory); the file is fsynced; the digest is verified against the expected value
when the caller supplied one; and only then does :func:`os.replace` publish the
result. The rename is the commit point — before it there is nothing at the final
path, after it there is a complete, verified artifact, and ``os.replace`` gives
that transition atomically within a filesystem. Any failure at all — a source
that dies mid-stream, a digest that does not match, a ``KeyboardInterrupt`` —
unlinks the ``.partial`` and leaves the final path exactly as it was, including
a previous good artifact that a failed re-export must not destroy.

Ordering note: the file is fsynced *before* the rename so the bytes are durable
when the name appears, and the directory is fsynced *after* it — a directory
fsync records the entries as they stand, so doing it before the rename would
attest to nothing.

:class:`ArtifactRecord` is the inventory entry the result package renders. It is
a plain, JSON-serializable structure carrying the four facts a caller needs in
order to decide whether an artifact is worth fetching (name, content type, size,
digest), plus why it was kept (``purpose``) and what became of it
(``retention``). ``size_bytes`` and ``sha256`` are ``None`` until an export
measures them: a declared-but-unexported artifact has no digest, and reporting a
fabricated zero would be a lie the destroy guard would then act on.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
from collections.abc import Buffer, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, Protocol, cast

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

#: Sidecar suffix for in-flight exports. Lives in the destination directory so
#: the publishing ``os.replace`` stays within one filesystem (a cross-device
#: rename is not atomic — it is a copy).
PARTIAL_SUFFIX = ".partial"

#: Read granularity. Large enough to keep syscall overhead off the copy, small
#: enough that a multi-gigabyte artifact never sits in memory.
DEFAULT_CHUNK_SIZE = 1 << 20

DEFAULT_CONTENT_TYPE = "application/octet-stream"
DIGEST_ALGORITHM = "sha256"

# Retention status — what became of a declared artifact. ``declared`` means the
# workspace promised it but nothing is on disk yet, which is exactly the set the
# destroy guard refuses to discard without an explicit force flag.
RETENTION_DECLARED = "declared"
RETENTION_EXPORTED = "exported"
RETENTION_DISCARDED = "discarded"
RETENTION_STATUSES = (RETENTION_DECLARED, RETENTION_EXPORTED, RETENTION_DISCARDED)

_HEX_DIGITS = frozenset("0123456789abcdef")
_DIGEST_LENGTH = 64


class _Readable(Protocol):
    """Minimal reader shape — what a provider hands over for a volume stream."""

    def read(self, size: int, /) -> bytes: ...  # pragma: no cover - typing only


#: Either a stream to ``.read()`` from or an iterable of byte chunks. Providers
#: yield chunks; local exports pass an open binary file. Both stay streaming.
ByteSource = Iterable[bytes] | _Readable


@dataclass(frozen=True)
class ArtifactRecord:
    """One inventory entry: a declared output and what became of it.

    Frozen because an entry describes something that already happened —
    transitions produce a new record via :func:`dataclasses.replace` rather than
    mutating history under a caller that already read it.
    """

    name: str
    content_type: str
    purpose: str
    retention: str
    size_bytes: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message="artifact name must not be empty",
                remediation="declare the artifact with the name the job writes",
            )
        if not self.purpose.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"artifact '{self.name}' has no purpose",
                remediation="state why the artifact is kept; the result package renders it",
            )
        if not self.content_type.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"artifact '{self.name}' has no content type",
                remediation=f"pass a media type, or accept the default {DEFAULT_CONTENT_TYPE}",
            )
        if self.retention not in RETENTION_STATUSES:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown retention status '{self.retention}' for artifact '{self.name}'",
                remediation=f"use one of: {', '.join(RETENTION_STATUSES)}",
            )
        if self.size_bytes is not None and self.size_bytes < 0:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"artifact '{self.name}' has a negative size ({self.size_bytes})",
                remediation="record the byte count the export measured",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize every field — a field added later is carried automatically."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArtifactRecord:
        """Rebuild a record from stored state, refusing anything it cannot model."""
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown artifact fields: {', '.join(unknown)}",
                remediation=f"artifact records carry: {', '.join(sorted(known))}",
            )
        try:
            return cls(**dict(data))
        except TypeError as err:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"malformed artifact record: {err}",
                remediation=f"required fields: {', '.join(sorted(known))}",
            ) from err


class ArtifactInventory:
    """The declared-output ledger for one workspace, in declaration order.

    A list of records would do for rendering, but two callers need to ask
    questions of it: the result package renders every entry, and destroy has to
    know whether anything was declared and never exported before it discards a
    volume. Keying by name also makes a duplicate declaration — two jobs
    claiming one output name — a loud error rather than a silent overwrite.
    """

    def __init__(self, records: Iterable[ArtifactRecord] = ()) -> None:
        self._records: dict[str, ArtifactRecord] = {}
        for record in records:
            self._insert(record)

    def _insert(self, record: ArtifactRecord) -> ArtifactRecord:
        if record.name in self._records:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"artifact '{record.name}' is already declared",
                remediation="declare each artifact name once per workspace",
            )
        self._records[record.name] = record
        return record

    def _require(self, name: str) -> ArtifactRecord:
        record = self._records.get(name)
        if record is None:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"artifact '{name}' was never declared",
                remediation="declare the artifact before recording what became of it",
            )
        return record

    def declare(
        self,
        name: str,
        *,
        purpose: str,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> ArtifactRecord:
        """Register an output the workspace promises, before it exists."""
        return self._insert(
            ArtifactRecord(
                name=name,
                content_type=content_type,
                purpose=purpose,
                retention=RETENTION_DECLARED,
            )
        )

    def mark_exported(self, name: str, *, size_bytes: int, sha256: str) -> ArtifactRecord:
        """Record a verified export — size and digest come from :func:`export_artifact`."""
        # `replace()` returns the same dataclass type it was handed, but some
        # type stubs still declare it as a bare dataclass instance, so the cast
        # states what the call already guarantees. Constructing an
        # `ArtifactRecord` field-by-field here instead would type-check without
        # a cast and be worse: it would silently drop any field added later,
        # where `replace` is total over them by construction.
        record = cast(
            ArtifactRecord,
            replace(
                self._require(name),
                retention=RETENTION_EXPORTED,
                size_bytes=size_bytes,
                sha256=sha256,
            ),
        )
        self._records[name] = record
        return record

    def mark_discarded(self, name: str) -> ArtifactRecord:
        """Record that a declared artifact went away unexported (a forced destroy)."""
        # Cast for the same reason as `mark_exported` above.
        record = cast(ArtifactRecord, replace(self._require(name), retention=RETENTION_DISCARDED))
        self._records[name] = record
        return record

    def get(self, name: str) -> ArtifactRecord | None:
        return self._records.get(name)

    def records(self) -> list[ArtifactRecord]:
        return list(self._records.values())

    def unexported(self) -> list[ArtifactRecord]:
        """Declared, nothing on disk yet — the destroy guard's input."""
        return [r for r in self._records.values() if r.retention == RETENTION_DECLARED]

    def to_list(self) -> list[dict[str, Any]]:
        """The result package's artifact section: plain dicts, declaration order."""
        return [record.to_dict() for record in self._records.values()]

    def __iter__(self) -> Iterator[ArtifactRecord]:
        return iter(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, name: object) -> bool:
        return name in self._records


def partial_path(destination: str | os.PathLike[str]) -> Path:
    """The in-flight sidecar for ``destination``, in the same directory."""
    dest = Path(destination)
    return dest.with_name(dest.name + PARTIAL_SUFFIX)


def export_artifact(
    source: ByteSource,
    destination: str | os.PathLike[str],
    *,
    purpose: str,
    name: str | None = None,
    content_type: str = DEFAULT_CONTENT_TYPE,
    expected_sha256: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ArtifactRecord:
    """Stream ``source`` to ``destination`` atomically, verifying its sha256.

    Returns the exported :class:`ArtifactRecord`. Raises :class:`CliError` and
    publishes nothing if the source fails, the digest does not match, or the
    destination is unusable; a ``KeyboardInterrupt`` propagates unchanged, also
    with nothing published.
    """
    dest = Path(destination)
    directory = dest.parent
    label = name if name is not None else dest.name

    if not directory.is_dir():
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"export destination directory does not exist: {directory}",
            remediation="create the destination directory, or export into an existing one",
        )
    if chunk_size <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"chunk_size must be positive, got {chunk_size}",
            remediation=f"omit chunk_size to stream in {DEFAULT_CHUNK_SIZE}-byte reads",
        )
    expected = _normalise_digest(expected_sha256)

    partial = partial_path(dest)
    digest = hashlib.sha256()
    size = 0
    try:
        # Opened before the first chunk is pulled: the sidecar exists for the
        # whole copy, so an interrupted export is always identifiable on disk.
        with open(partial, "wb", buffering=0) as handle:
            for chunk in _iter_chunks(source, chunk_size):
                _reject_non_bytes(chunk, label)
                digest.update(chunk)
                size += _write_through(handle, chunk, label)
            os.fsync(handle.fileno())

        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"artifact '{label}' failed digest verification: "
                    f"expected {expected}, computed {actual}"
                ),
                remediation=(
                    "nothing was published; re-run the export — the source stream was "
                    "truncated or the expected digest is stale"
                ),
            )
        # The commit point. Everything above this line is reversible.
        os.replace(partial, dest)
    except CliError:
        _discard(partial)
        raise
    except Exception as err:
        _discard(partial)
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"export of artifact '{label}' failed: {err.__class__.__name__}: {err}",
            remediation=f"nothing was published to {dest}; retry the export",
        ) from err
    except BaseException:
        # KeyboardInterrupt / SystemExit / GeneratorExit — a real interruption.
        # Clean up the sidecar, then let it travel: swallowing it would be worse
        # than the partial file this whole module exists to prevent.
        _discard(partial)
        raise

    _fsync_dir(directory)
    return ArtifactRecord(
        name=label,
        content_type=content_type,
        purpose=purpose,
        retention=RETENTION_EXPORTED,
        size_bytes=size,
        sha256=actual,
    )


def _iter_chunks(source: ByteSource, chunk_size: int) -> Iterator[bytes]:
    """Yield ``source`` in chunks, lazily — a file object reads, anything else iterates.

    The ``.read()`` branch comes first on purpose: iterating a binary file
    yields *lines*, which would split on newlines that mean nothing in a binary
    artifact and buffer arbitrarily much when there are none.
    """
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(chunk_size)
            if not chunk:
                return
            yield chunk
    else:
        yield from source  # type: ignore[misc]


def _write_through(handle: IO[bytes], chunk: Buffer, label: str) -> int:
    """Write one chunk all the way to the file descriptor; return its byte count.

    The sidecar is opened unbuffered on purpose. Chunks are already large, so a
    userspace buffer only adds a copy — and it would let bytes linger in memory
    after the write meant to put them on disk, which is precisely the property
    this module promises it does not do. A raw write may return short, so loop
    until the chunk is drained.
    """
    view = memoryview(chunk).cast("B")
    total = 0
    while view:
        written = handle.write(view) or 0
        if written <= 0:
            raise OSError(f"write to the export sidecar for '{label}' made no progress")
        view = view[written:]
        total += written
    return total


def _reject_non_bytes(chunk: object, label: str) -> None:
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(f"artifact '{label}' source yielded {type(chunk).__name__}, expected bytes"),
            remediation="stream the artifact as bytes (open the source in binary mode)",
        )


def _normalise_digest(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().lower()
    if len(candidate) != _DIGEST_LENGTH or not set(candidate) <= _HEX_DIGITS:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"expected_sha256 must be {_DIGEST_LENGTH} hex characters, got {value!r}",
            remediation="pass the sha256 hex digest, or omit it to record the computed one",
        )
    return candidate


def _discard(partial: Path) -> None:
    """Remove the sidecar. Never raises — it runs on the failure path."""
    with contextlib.suppress(OSError):
        partial.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    """fsync the directory so the *name* is durable, not just the bytes.

    Fsyncing the file guarantees the content survives a crash; only fsyncing the
    containing directory guarantees the rename that published it does. Not every
    filesystem lets a directory be opened for fsync, and a refusal is not fatal:
    the artifact is already complete and verified on disk.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
