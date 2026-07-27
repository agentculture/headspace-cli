"""The ``~/.headspace`` state store: schema-versioned, crash-atomic, lock-serialised.

headspace is CLI-owned — there is no daemon — so this store *is* the workspace
registry between invocations, and several CLI processes can reach it at once.
Three properties are therefore built in here rather than left to callers:

* **Fail-closed schema versioning.** Every file on disk is stamped with
  :data:`SCHEMA_VERSION`, and that stamp is validated before any other field is
  interpreted. A file written by a future headspace raises :class:`CliError`
  with a migration hint — never reinterpreted, never overwritten (claim c27).
* **Atomic writes.** State is serialised into a temporary file in the *same*
  directory, fsynced, then :func:`os.replace`d into position. A process killed
  mid-write leaves either the whole old file or the whole new one, never a
  partial one — which is why readers here need no lock.
* **A per-workspace advisory lock.** Every mutating operation takes
  :func:`fcntl.flock` on the workspace's lock file, so interleaved verbs from
  parallel CLI invocations cannot corrupt state or double-start a job (c24).

This module owns *all* filesystem access under the store root. No other module
may read or write there: centralising it is what makes the schema check
unskippable.

On-disk layout::

    <root>/                     $HEADSPACE_HOME, else ~/.headspace
      workspaces/
        <workspace-id>/
          state.json            one schema-stamped envelope (see StateRecord)
          journal.jsonl         append-only intent journal, one envelope a line
          .lock                 flock target; never read, never parsed

Scope note: the lock serialises *processes*. ``flock`` is held per open file
description, so nested :meth:`Store.lock` calls on one :class:`Store` are
treated as re-entrant (a mutating call inside ``with store.lock(...)`` must not
deadlock against itself); two threads sharing one :class:`Store` therefore share
its lock rather than queueing behind it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from headspace.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

# Bump only alongside a documented migration. Every file this module writes
# carries this number; every file it reads is checked against it first.
SCHEMA_VERSION = 1

HOME_ENV_VAR = "HEADSPACE_HOME"
DEFAULT_STORE_DIRNAME = ".headspace"

_WORKSPACES_DIRNAME = "workspaces"
_STATE_FILENAME = "state.json"
_JOURNAL_FILENAME = "journal.jsonl"
_LOCK_FILENAME = ".lock"

# Workspace ids become directory names, so they are constrained rather than
# escaped: no separators, no leading dot, no traversal.
_WORKSPACE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_UPGRADE_HINT = (
    "Upgrade headspace-cli, or point HEADSPACE_HOME at a different store root. "
    "headspace never rewrites state it cannot read."
)


def store_root() -> Path:
    """Resolve the store root: ``$HEADSPACE_HOME`` when set, else ``~/.headspace``.

    Pure resolution — it never creates the directory, so importing or
    instantiating the store has no side effect on the operator's home.
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_STORE_DIRNAME


@dataclass(frozen=True)
class StateRecord:
    """One workspace's persisted state, unwrapped from its schema envelope."""

    workspace_id: str
    updated_at: str
    state: dict[str, Any]


@dataclass(frozen=True)
class JournalEntry:
    """One intent-journal line, unwrapped from its schema envelope."""

    recorded_at: str
    entry: dict[str, Any]


@dataclass(eq=False)
class Store:
    """Filesystem-backed workspace registry rooted at :func:`store_root`.

    Identity-compared on purpose: an instance owns open lock file descriptors,
    so two Stores over one root are not interchangeable.
    """

    root: Path = field(default_factory=store_root)
    _held: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _bookkeeping: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    # --- paths ------------------------------------------------------------

    def workspace_dir(self, workspace_id: str) -> Path:
        """Directory holding one workspace's state, journal, and lock."""
        return self.root / _WORKSPACES_DIRNAME / _validate_workspace_id(workspace_id)

    # --- locking ----------------------------------------------------------

    @contextmanager
    def lock(self, workspace_id: str, *, blocking: bool = True) -> Iterator[None]:
        """Hold this workspace's advisory lock for the duration of the block.

        Every mutating method takes it. With ``blocking=False`` a workspace
        another process is already working on raises :class:`CliError` instead
        of waiting, so a CLI verb can report a busy workspace rather than hang.
        """
        workspace = _validate_workspace_id(workspace_id)
        with self._bookkeeping:
            reentrant = workspace in self._held
        if reentrant:
            yield
            return

        # Acquired outside the bookkeeping mutex: a thread waiting on flock must
        # never block the thread that has to release it.
        fd = self._acquire_flock(workspace, blocking=blocking)
        with self._bookkeeping:
            self._held[workspace] = fd
        try:
            yield
        finally:
            with self._bookkeeping:
                held_fd = self._held.pop(workspace, fd)
            try:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
            finally:
                os.close(held_fd)

    def _acquire_flock(self, workspace: str, *, blocking: bool) -> int:
        path = self._ensure_workspace_dir(workspace) / _LOCK_FILENAME
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as err:
            raise CliError(
                EXIT_ENV_ERROR,
                f"cannot open the lock file for workspace {workspace}: {err}",
                f"Check permissions on {path.parent}.",
            ) from err
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            os.close(fd)
            raise CliError(
                EXIT_ENV_ERROR,
                f"workspace {workspace} is locked by another headspace process",
                "Wait for the other invocation to finish, then retry.",
            ) from err
        except OSError as err:  # pragma: no cover - platform/filesystem specific
            os.close(fd)
            raise CliError(
                EXIT_ENV_ERROR,
                f"cannot lock workspace {workspace}: {err}",
                f"Check that {path} lives on a filesystem supporting flock.",
            ) from err
        return fd

    # --- reads ------------------------------------------------------------

    def exists(self, workspace_id: str) -> bool:
        """True when this workspace has a state file on disk."""
        return (self.workspace_dir(workspace_id) / _STATE_FILENAME).is_file()

    def list_workspaces(self) -> list[str]:
        """Every workspace directory under the root, sorted; nothing is parsed."""
        workspaces = self.root / _WORKSPACES_DIRNAME
        if not workspaces.is_dir():
            return []
        return sorted(
            child.name
            for child in workspaces.iterdir()
            if child.is_dir() and _WORKSPACE_ID_RE.match(child.name)
        )

    def read_state(self, workspace_id: str) -> StateRecord:
        """Load one workspace's state, failing closed on an unreadable schema."""
        return self._read_state(_validate_workspace_id(workspace_id))

    def read_all_states(self) -> dict[str, StateRecord]:
        """Load every workspace that has a state file — one bad schema fails the lot."""
        records: dict[str, StateRecord] = {}
        for workspace in self.list_workspaces():
            if (self.workspace_dir(workspace) / _STATE_FILENAME).is_file():
                records[workspace] = self._read_state(workspace)
        return records

    def read_journal(self, workspace_id: str) -> list[JournalEntry]:
        """Load a workspace's intent journal in append order; empty when absent."""
        workspace = _validate_workspace_id(workspace_id)
        path = self.workspace_dir(workspace) / _JOURNAL_FILENAME
        if not path.is_file():
            return []
        entries: list[JournalEntry] = []
        for number, line in enumerate(_read_text(path).splitlines(), start=1):
            if not line.strip():
                continue
            document = _decode_json(line, path, line_number=number)
            _check_schema(document, path, line_number=number)
            entries.append(
                JournalEntry(
                    recorded_at=str(document.get("recorded_at", "")),
                    entry=_require_mapping(document.get("entry"), path, "entry"),
                )
            )
        return entries

    # --- writes -----------------------------------------------------------

    def write_state(self, workspace_id: str, state: Mapping[str, Any]) -> StateRecord:
        """Replace a workspace's state atomically, stamping the schema version."""
        workspace = _validate_workspace_id(workspace_id)
        with self.lock(workspace):
            return self._write_state(workspace, state)

    def update_state(
        self, workspace_id: str, mutate: Callable[[dict[str, Any]], Mapping[str, Any]]
    ) -> StateRecord:
        """Read-modify-write under a single lock hold, so no update can be lost."""
        workspace = _validate_workspace_id(workspace_id)
        with self.lock(workspace):
            current = self._read_state(workspace)
            updated = mutate(dict(current.state))
            if not isinstance(updated, Mapping):
                raise CliError(
                    EXIT_USER_ERROR,
                    f"the state mutator for workspace {workspace} returned "
                    f"{type(updated).__name__}, not a mapping",
                    "Return the new state mapping from the mutator.",
                )
            return self._write_state(workspace, updated)

    def append_journal(self, workspace_id: str, entry: Mapping[str, Any]) -> JournalEntry:
        """Append one intent record before an engine mutation (crash-consistency hook).

        The line is serialised in full before the file is opened and written in
        a single append, so a crash leaves whole lines or nothing.
        """
        workspace = _validate_workspace_id(workspace_id)
        recorded_at = _now()
        document = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace,
            "recorded_at": recorded_at,
            "entry": dict(entry),
        }
        line = _encode_json(document, f"journal entry for workspace {workspace}") + "\n"
        with self.lock(workspace):
            path = self._ensure_workspace_dir(workspace) / _JOURNAL_FILENAME
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as err:
                raise CliError(
                    EXIT_ENV_ERROR,
                    f"cannot append to {path}: {err}",
                    "Check permissions and free space on the store filesystem.",
                ) from err
        return JournalEntry(recorded_at=recorded_at, entry=dict(entry))

    def delete_workspace(self, workspace_id: str) -> None:
        """Remove a workspace directory and everything under it."""
        workspace = _validate_workspace_id(workspace_id)
        directory = self.workspace_dir(workspace)
        if not directory.is_dir():
            raise CliError(
                EXIT_USER_ERROR,
                f"unknown workspace {workspace}",
                "Run `headspace-cli` against a workspace id that exists in the store.",
            )
        with self.lock(workspace):
            try:
                shutil.rmtree(directory)
            except OSError as err:
                raise CliError(
                    EXIT_ENV_ERROR,
                    f"cannot remove {directory}: {err}",
                    "Check permissions on the store root.",
                ) from err

    # --- internals --------------------------------------------------------

    def _read_state(self, workspace: str) -> StateRecord:
        path = self.workspace_dir(workspace) / _STATE_FILENAME
        if not path.is_file():
            raise CliError(
                EXIT_USER_ERROR,
                f"unknown workspace {workspace}",
                "Run `headspace-cli` against a workspace id that exists in the store.",
            )
        document = _decode_json(_read_text(path), path)
        # Version first: nothing else in the document is trusted until it passes.
        _check_schema(document, path)
        return StateRecord(
            workspace_id=str(document.get("workspace_id", workspace)),
            updated_at=str(document.get("updated_at", "")),
            state=_require_mapping(document.get("state"), path, "state"),
        )

    def _write_state(self, workspace: str, state: Mapping[str, Any]) -> StateRecord:
        updated_at = _now()
        document = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace,
            "updated_at": updated_at,
            "state": dict(state),
        }
        path = self._ensure_workspace_dir(workspace) / _STATE_FILENAME
        _write_json_atomically(path, document, what=f"state for workspace {workspace}")
        return StateRecord(workspace_id=workspace, updated_at=updated_at, state=dict(state))

    def _ensure_workspace_dir(self, workspace: str) -> Path:
        directory = self.root / _WORKSPACES_DIRNAME / workspace
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as err:
            raise CliError(
                EXIT_ENV_ERROR,
                f"cannot use the headspace store at {self.root}: {err}",
                f"Ensure {self.root} is a writable directory, or set " f"{HOME_ENV_VAR} to one.",
            ) from err
        return directory


# --- module-level helpers -------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_RE.match(workspace_id):
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid workspace id {workspace_id!r}",
            "Workspace ids are 1-64 characters of letters, digits, '.', '-', or "
            "'_', and must not start with '.' — they name a directory in the store.",
        )
    return workspace_id


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot read {path}: {err}",
            "Check permissions on the headspace store, or set "
            f"{HOME_ENV_VAR} to a readable store root.",
        ) from err


def _decode_json(raw: str, path: Path, *, line_number: int | None = None) -> dict[str, Any]:
    where = f"{path}" if line_number is None else f"{path} line {line_number}"
    try:
        document = json.loads(raw)
    except ValueError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{where} is not valid JSON: {err}",
            "The store file is corrupt. Move it aside and let headspace "
            "recreate the workspace; headspace will not overwrite it for you.",
        ) from err
    if not isinstance(document, dict):
        raise CliError(
            EXIT_ENV_ERROR,
            f"{where} is not a JSON object",
            "The store file is corrupt. Move it aside and let headspace "
            "recreate the workspace; headspace will not overwrite it for you.",
        )
    return document


def _check_schema(document: dict[str, Any], path: Path, *, line_number: int | None = None) -> None:
    """Validate the schema stamp before any other field is interpreted.

    Anything other than an exact match fails closed: a future version could mean
    fields this build would silently drop on the next write, and an unknown
    older version has no migration to run.
    """
    where = f"{path}" if line_number is None else f"{path} line {line_number}"
    found = document.get("schema_version")
    if not isinstance(found, int) or isinstance(found, bool):
        raise CliError(
            EXIT_ENV_ERROR,
            f"{where} carries no usable schema_version; this build writes v{SCHEMA_VERSION}",
            "The file was not written by headspace, or predates schema "
            f"stamping. {_UPGRADE_HINT}",
        )
    if found > SCHEMA_VERSION:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{where} was written by a newer headspace (schema v{found}); "
            f"this build understands v{SCHEMA_VERSION}",
            _UPGRADE_HINT,
        )
    if found < SCHEMA_VERSION:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{where} carries an unrecognised schema v{found}; "
            f"this build understands v{SCHEMA_VERSION}",
            f"No migration from v{found} exists. {_UPGRADE_HINT}",
        )


def _require_mapping(value: Any, path: Path, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CliError(
            EXIT_ENV_ERROR,
            f"{path} has a {field_name!r} field that is not an object",
            "The store file is corrupt. Move it aside and let headspace "
            "recreate the workspace; headspace will not overwrite it for you.",
        )
    return value


def _encode_json(document: Mapping[str, Any], what: str) -> str:
    try:
        return json.dumps(document, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as err:
        raise CliError(
            EXIT_USER_ERROR,
            f"the {what} is not JSON-serialisable: {err}",
            "Store only JSON types (objects, arrays, strings, numbers, "
            "booleans, null) in workspace state.",
        ) from err


def _write_json_atomically(path: Path, document: Mapping[str, Any], *, what: str) -> None:
    """Serialise to a sibling temp file, fsync, then rename over the target.

    The temp file shares the target's directory so the rename never crosses a
    filesystem, and it is removed on any failure — a partial serialisation is
    only ever visible at the temp path, never at ``path``.
    """
    payload = _encode_json(document, what) + "\n"
    directory = path.parent
    try:
        handle_fd, temp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as err:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot create a temporary file in {directory}: {err}",
            "Check permissions and free space on the store filesystem.",
        ) from err
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(directory)
    except OSError as err:
        temp_path.unlink(missing_ok=True)
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot write {path}: {err}",
            "Check permissions and free space on the store filesystem.",
        ) from err
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Durably record the rename itself, not just the file contents."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
