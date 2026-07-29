"""Host-side input model: expand a host path into a digested manifest, and
refuse an over-budget payload before any provider is touched.

Headspace's copy-in path (``headspace put``, ``run --input``) gets files
INTO a network-disabled, storage-budgeted workspace -- the mirror image of
:mod:`headspace.core.artifacts`, which gets files *out*. Everything
downstream of this module -- streaming bytes into the volume, verifying them
again on the engine side, renaming them into place inside the container --
belongs to other tasks; this module is the *host* half, and it does exactly
two things, both before a provider object is ever constructed:

1. :func:`expand_input` turns one host path (a file, or a directory of
   files) into an :class:`InputManifest`: a sorted, deterministic list of
   :class:`InputEntry` records, each carrying the host source path, the
   workspace-relative destination, and the file's size and sha256 --
   measured by reading the file exactly once, streaming, the same discipline
   :func:`headspace.core.artifacts.export_artifact` uses on the way out
   (chunked reads, digest updated per chunk, artifacts.py:266-354). The
   manifest is the only thing a later task journals or records in
   provenance: path and digest, never bytes. A copied-in secret must not be
   legible in ``outcome_summary``, ``journal.jsonl``, or ``state.json`` just
   because it was legible on disk -- recording a digest instead of content is
   what makes that true structurally, not something a caller has to
   remember to redact.

2. :func:`precheck_storage_budget` refuses a manifest whose total size would
   overrun the workspace's remaining ``storage_bytes`` budget -- *before*
   anything reaches an engine. This is the same enforcement class as
   ``wall_clock``/``output_bytes``/``concurrency`` in
   :mod:`headspace.core.policy` (policy.py:301-309): headspace's own process
   enforces it, unconditionally, regardless of what a provider's storage
   accounting can or cannot cap on its own (the default Docker local volume
   driver only *measures* storage -- see ``CapabilitySnapshot.storage_enforceable``
   in policy.py). A caller that skips this precheck and lets an oversized
   payload reach the engine has already lost the closed-by-default bet the
   whole product is built on. The function takes a manifest, not a path or a
   provider handle, so it is structurally incapable of touching the engine
   itself -- there is nothing here it *could* call even by mistake.

Layering: this module is pure host-side code with **no engine, no provider,
and no docker SDK import** -- the rule ``headspace/core/__init__.py`` states
for the whole package. It goes one notch further than that rule strictly
requires: by this task's brief, it imports nothing beyond the standard
library and :mod:`headspace.cli._errors` -- the same narrow allowance
already granted to ``policy.py``, ``states.py``, and ``profiles.py`` (see
their module docstrings). ``_errors`` itself imports nothing from the rest
of the package, so this is not a back door into a cycle; it just reuses the
one error contract the whole CLI shares instead of inventing a second one.
Staying this narrow keeps the module trivially safe to import from anywhere
-- a future CLI-side flag parser included -- without pulling in the engine
or a provider's parsing/validation code as a transitive dependency.

Symlinks and special files: refused, always, loudly -- both as the
top-level host path and anywhere inside a directory being expanded, dangling
or not. A symlink is a promise about *where a file is*, not what the file
*is*; a directory walk that quietly follows one can leave a workspace
holding bytes from a path the caller never named, or -- if the link is
dangling -- holding nothing while the caller believes a copy happened. A
device node, socket, or FIFO is not a payload a copy-in can make sense of at
all: opening one can block forever, or return bytes with no relation to any
file a human would call "the file at that path". In every one of these
cases this module raises a loud, named :class:`CliError` naming the
offending path rather than silently skipping the entry or silently
dereferencing it -- a caller who really did mean to copy a symlink's target
can just say so, by pointing the input directly at that target instead of
the link.

Determinism: two calls to :func:`expand_input` over the same host directory
produce ``==`` manifests. That is not a promise that a directory listing
looks the same on every OS -- ``readdir`` order is unspecified and this
module does not rely on it -- it is a promise that entries are always
returned sorted by destination path before a caller ever sees them, so two
runs, or two hosts, produce identical manifests as long as the underlying
files do, and a diff between two manifests is a meaningful diff of the
payload rather than readdir noise.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_POLICY_DENIED, EXIT_USER_ERROR, CliError

#: Read granularity for the host-side hashing pass. Same magnitude as
#: :data:`headspace.core.artifacts.DEFAULT_CHUNK_SIZE` and for the same
#: reason (large enough to keep syscall overhead off the read, small enough
#: that a multi-gigabyte input never sits in memory) -- redefined locally
#: rather than imported because this module's import surface is
#: deliberately narrower than the rest of ``headspace.core`` (see the module
#: docstring).
DEFAULT_CHUNK_SIZE = 1 << 20

_HEX_DIGITS = frozenset("0123456789abcdef")
_DIGEST_LENGTH = 64


@dataclass(frozen=True)
class InputEntry:
    """One file bound for a workspace: where it comes from, where it lands,
    and what it is -- by size and digest, never by content.

    Unlike :class:`~headspace.core.artifacts.ArtifactRecord`, which has a
    ``declared`` state that predates an export, an ``InputEntry`` has no
    unmeasured state to represent: it is only ever constructed once
    :func:`expand_input` has already read the source exactly once to measure
    it, so ``size_bytes`` and ``sha256`` are always populated, never
    ``None``.
    """

    #: The host filesystem path this entry was read from, exactly as
    #: constructed during expansion (not forced absolute or symlink-resolved
    #: beyond what :func:`expand_input` already verified).
    source: Path
    #: Workspace-relative destination path, POSIX-style (``/`` separators),
    #: never absolute and never containing a ``..`` segment.
    destination: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not str(self.destination).strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message="input entry destination must not be empty",
                remediation="give every copied-in file a non-empty workspace-relative destination",
            )
        if self.size_bytes < 0:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(
                    f"input entry '{self.destination}' has a negative size ({self.size_bytes})"
                ),
                remediation="record the byte count the streaming read measured",
            )
        digest = self.sha256.strip().lower()
        if len(digest) != _DIGEST_LENGTH or not set(digest) <= _HEX_DIGITS:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"input entry '{self.destination}' has a malformed sha256: {self.sha256!r}",
                remediation=f"sha256 must be {_DIGEST_LENGTH} lowercase hex characters",
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain, JSON-serializable form -- path and digest, never content."""
        return {
            "source": str(self.source),
            "destination": self.destination,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class InputManifest:
    """A sorted, deterministic set of files bound for one copy-in.

    ``entries`` is sorted by destination path whenever it comes from
    :func:`expand_input`; this class does not itself re-sort or otherwise
    validate a hand-built tuple, so a caller assembling one directly (as
    tests do) is trusted to pass entries in the order it wants recorded.
    """

    entries: tuple[InputEntry, ...]

    @property
    def total_bytes(self) -> int:
        """The sum a budget precheck compares against -- not a peak, not an
        estimate: the exact sum of every entry's measured ``size_bytes``."""
        return sum(entry.size_bytes for entry in self.entries)

    def to_list(self) -> list[dict[str, Any]]:
        """The provenance-ready form: a plain list of dicts, manifest order."""
        return [entry.to_dict() for entry in self.entries]

    def __iter__(self) -> Iterator[InputEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def expand_input(
    host_path: str | os.PathLike[str],
    destination: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> InputManifest:
    """Expand ``host_path`` into a sorted, digested :class:`InputManifest`.

    ``host_path`` may be a regular file, in which case the manifest holds
    exactly one entry at ``destination``; or a directory, in which case
    every regular file it contains (recursively) becomes one entry, with
    ``destination`` treated as the workspace-relative prefix each file's
    path-within-the-directory is joined onto. Every other case -- a missing
    path, a symlink (anywhere), or a special file (device/socket/FIFO,
    anywhere) -- is a loud :class:`CliError` naming the offending path; see
    the module docstring for why silently skipping or dereferencing is not
    on the table.

    Each file's bytes are read exactly once, streamed in ``chunk_size``
    reads, to compute both ``size_bytes`` and ``sha256`` together in a
    single pass -- mirroring :func:`headspace.core.artifacts.export_artifact`
    (artifacts.py:266-354). Raises :class:`CliError` (exit 1, user error) for
    a structurally bad input (missing path, symlink, special file,
    non-workspace-relative destination, non-positive ``chunk_size``), or
    (exit 2, environment error) for an I/O failure reading a file whose type
    was already verified.
    """
    if chunk_size <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"chunk_size must be positive, got {chunk_size}",
            remediation=f"omit chunk_size to stream in {DEFAULT_CHUNK_SIZE}-byte reads",
        )
    _validate_destination(destination)
    top = Path(host_path)

    try:
        top_stat = top.lstat()
    except FileNotFoundError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"host input path does not exist: {top}",
            remediation="pass a host path that exists and is readable",
        ) from err
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot inspect host input path '{top}': {err.strerror or err}",
            remediation="check the path is readable",
        ) from err

    if stat.S_ISLNK(top_stat.st_mode):
        raise _refuse_symlink(top)

    if stat.S_ISREG(top_stat.st_mode):
        size, digest = _hash_file(top, chunk_size)
        return InputManifest(
            (InputEntry(source=top, destination=destination, size_bytes=size, sha256=digest),)
        )

    if stat.S_ISDIR(top_stat.st_mode):
        entries: list[InputEntry] = []
        for file_path in _walk_files(top):
            relative = file_path.relative_to(top).as_posix()
            size, digest = _hash_file(file_path, chunk_size)
            entries.append(
                InputEntry(
                    source=file_path,
                    destination=_join_destination(destination, relative),
                    size_bytes=size,
                    sha256=digest,
                )
            )
        entries.sort(key=lambda entry: entry.destination)
        return InputManifest(tuple(entries))

    raise _refuse_special(top)


def precheck_storage_budget(manifest: InputManifest, *, storage_bytes_remaining: int) -> None:
    """Refuse ``manifest`` before it reaches a provider if its total size
    would overrun the workspace's remaining ``storage_bytes`` budget.

    Pure comparison against a manifest already in hand -- no path, no
    provider, no I/O -- so a caller that runs this before constructing a
    provider has structurally satisfied "refused before any provider call",
    not just done so by convention. Raises :class:`CliError` with
    :data:`~headspace.cli._errors.EXIT_POLICY_DENIED` (3): a budget refusal
    is "refused by policy before running", the taxonomy's own category for
    exactly this shape of failure -- not an internal error, and not the
    ``resource_exhausted`` (8) category, which names a job killed for
    exceeding a ceiling *during* execution, not a payload refused before one
    ever starts. The remediation names the budget by its policy field name,
    ``storage_bytes``, so an agent reading the failure knows which ceiling
    to raise or which artifacts to clear.
    """
    if manifest.total_bytes <= storage_bytes_remaining:
        return
    raise CliError(
        code=EXIT_POLICY_DENIED,
        message=(
            f"copy-in of {manifest.total_bytes} byte(s) across {len(manifest)} file(s) exceeds "
            f"the workspace's remaining storage_bytes budget "
            f"({storage_bytes_remaining} byte(s) remaining)"
        ),
        remediation=(
            f"the storage_bytes budget has {storage_bytes_remaining} byte(s) remaining but this "
            f"payload is {manifest.total_bytes} byte(s) -- reduce the payload, free storage_bytes "
            "by exporting or destroying artifacts, or create the workspace with a larger "
            "storage_bytes budget"
        ),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_destination(destination: str) -> None:
    if not destination.strip():
        raise CliError(
            code=EXIT_USER_ERROR,
            message="destination workspace path must not be empty",
            remediation="pass a non-empty, workspace-relative destination path",
        )
    if destination.startswith("/"):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"destination '{destination}' must be workspace-relative, not absolute",
            remediation="pass a path relative to the workspace root, without a leading '/'",
        )
    normalised = posixpath.normpath(destination)
    if normalised == ".." or normalised.startswith("../"):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"destination '{destination}' escapes the workspace root",
            remediation="pass a destination path that stays inside the workspace",
        )


def _join_destination(root: str, relative: str) -> str:
    """Join a directory's workspace-relative prefix onto a file's
    path-within-the-directory. ``relative`` always comes from
    :meth:`Path.relative_to` on a real, walked filesystem entry, so it can
    never itself carry a ``..`` segment -- only ``root`` (the caller-supplied
    prefix) needs the traversal check in :func:`_validate_destination`.
    """
    stripped = root.rstrip("/")
    return f"{stripped}/{relative}" if stripped else relative


def _hash_file(path: Path, chunk_size: int) -> tuple[int, str]:
    """Read ``path`` once, streaming, returning its size and sha256 hex digest.

    Mirrors the chunked-read discipline :func:`headspace.core.artifacts.export_artifact`
    uses on the way out (artifacts.py:266-354): the digest is updated per
    chunk and the file is never held whole in memory, however large it is.
    Opened with ``O_NOFOLLOW`` where the platform supports it -- pure
    defense in depth against a symlink swapped in between the directory scan
    that classified this path as a regular file and this open call; refusing
    a symlink as a matter of policy is the scan's job (see the module
    docstring), this is only a backstop against a race with that check.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot open host input '{path}': {err.strerror or err}",
            remediation=(
                "check the file is readable and was not replaced by a symlink after being listed"
            ),
        ) from err

    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"failed reading host input '{path}': {err.strerror or err}",
            remediation="the file changed or became unreadable mid-read; retry the copy-in",
        ) from err
    return size, digest.hexdigest()


def _walk_files(top: Path) -> list[Path]:
    """Depth-first walk of ``top`` collecting regular files, classifying
    every entry -- file or directory -- from its own ``lstat`` before any
    decision is made about it.

    Implemented directly over :func:`os.scandir` rather than :func:`os.walk`
    on purpose: ``os.walk(..., followlinks=False)`` stops short of
    recursing into a symlinked directory, but it does so *silently* --
    the symlinked directory just contributes nothing, with no signal that
    anything was skipped. This module's whole position on symlinks (see the
    module docstring) is that silence is the wrong failure mode here, so a
    symlinked directory must be an explicit, loud refusal naming the path,
    not a quietly empty branch of the walk.
    """
    files: list[Path] = []

    def _walk(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as err:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"cannot list host directory '{directory}': {err.strerror or err}",
                remediation="check the directory is readable",
            ) from err
        for entry in children:
            child = Path(entry.path)
            if entry.is_symlink():
                raise _refuse_symlink(child)
            if entry.is_dir(follow_symlinks=False):
                _walk(child)
            elif entry.is_file(follow_symlinks=False):
                files.append(child)
            else:
                raise _refuse_special(child)

    _walk(top)
    return files


def _refuse_symlink(path: Path) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"host input contains a symlink, which copy-in refuses: {path}",
        remediation=(
            "copy-in never follows symlinks, dangling or not -- point the input directly at "
            "the real file or directory instead of a link to it"
        ),
    )


def _refuse_special(path: Path) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"host input is not a regular file or directory: {path}",
        remediation="copy-in only accepts regular files and directories of regular files",
    )
