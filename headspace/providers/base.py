"""The provider seam: what "backend" means to headspace, and nothing more.

WHY this module exists
----------------------
headspace sells one promise: a caller gets identical lifecycle and result
semantics no matter what executes the work. Docker is only the first backend
(FR-25/FR-26). A promise like that is either structural or it is marketing —
and it becomes structural exactly here, because this is the one place where a
backend concept could enter the system. If a container id, an image layer, a
``HostConfig`` fragment, or a docker SDK object crosses this line, every layer
above inherits it: the orchestration layer starts branching on it, the result
package renders it, and "swap the backend" quietly becomes "rewrite headspace".
So the rule this module enforces is narrow and absolute: **every value crossing
this seam is a plain, JSON-serialisable, backend-neutral structure.** The
conformance suite (``tests/conformance.py``) checks that mechanically, against
every backend, forever.

Six verbs, chosen from what the orchestration layer needs and nothing else:

===============  ==========================================================
verb             what it answers
===============  ==========================================================
``capabilities`` what can this host actually enforce?
``create``       give me a workspace under this policy
``run``          execute this command in that workspace
``inspect``      what does the backend say about that workspace now?
``read``         hand me the bytes of one file the workspace holds
``remove``       tear it down and tell me exactly what went away
===============  ==========================================================

Why ``read`` is a verb and not a convenience
--------------------------------------------
The first five verbs can put work *into* a workspace and describe it, but none
of them can get a result *out*. That left ``export`` demanding its bytes from
whoever called it — which the CLI cannot supply, because the artifacts sit
inside a volume no process outside the engine can open. A product whose whole
promise is "the answer outlives the scratch space" therefore had no way to keep
it (issue #3).

It is a *streaming* verb (:class:`ByteStream`) rather than one returning
``bytes``, because an artifact may be larger than the host's memory, and a seam
that hands back a single object makes that unfixable above it. It is
*context-managed* because a backend generally holds something open — a socket,
an archive, a connection — and the consumer routinely stops early: a digest
that fails to verify abandons the stream mid-flight, and that path must release
as reliably as the happy one.

Only a regular file's bytes cross. A directory, a symlink or a device is
refused as a user error, and the path itself is normalised and bounded by
:func:`require_workspace_path` here rather than in each backend — see its
docstring for why the engine underneath cannot be trusted to do it.

A :class:`typing.Protocol`, not an abstract base class. A backend is anything
that *behaves* correctly; nothing about it should have to inherit from
headspace, and headspace should not be able to hand a backend any shared
implementation to lean on. Structural typing keeps that honest, and
``@runtime_checkable`` lets the conformance suite assert the shape it was
handed. (``isinstance`` only; ``issubclass`` is unavailable on protocols with
non-method members, which ``name`` is.)

Why workspace ids, not handles
------------------------------
Every verb after ``create`` is keyed by the ``workspace_id`` headspace itself
chose. That is not an accident of convenience:

* headspace is daemonless. The provider object serving ``run`` is a *different*
  Python object, in a different process, from the one that served ``create``.
  An operation keyed on an in-memory handle would need the handle rehydrated
  before it could be used, and the rehydration would have to survive a crash.
* Crash reconciliation depends on it. A CLI killed between the engine call and
  the state write never got a handle back, but it *did* journal the workspace
  id first — so ``inspect(workspace_id)`` is the only lookup that still works
  in the exact case the recovery path exists for.
* It makes headspace's own id the single handle in the system. A backend that
  needs its own identifier finds it from the workspace id (the Docker provider
  by label, the fake by dict key), which is why no returned structure has to
  carry one.

:class:`OpaqueRef` remains for the genuine round-trip case, and is the only
sanctioned exception (see its docstring).

Why infrastructure failure is *raised*, not returned
----------------------------------------------------
NFR-07: "infrastructure failure never masquerades as computational failure."
Making that a convention ("please set status correctly") would leave it one
careless assignment away from being false. Instead it is enforced by types:
:data:`JOB_STATUSES` — the vocabulary a :class:`JobOutcome` may carry — is the
result vocabulary *minus* ``infrastructure_failure`` and ``policy_denied``. A
broken engine cannot be described by a :class:`JobOutcome` at all; it must be
raised as :class:`ProviderError` (exit code 7), just as an unsatisfiable policy
is raised as :class:`~headspace.core.policy.PolicyError` (exit code 3). What
comes back from ``run`` is therefore always a statement about the *job*.

Why the *descriptor* never sits in ``running``
----------------------------------------------
A workspace hosts *several* jobs by design ("run multiple jobs sharing state",
doc section 11). :mod:`headspace.core.states` supports that with exactly one
cycle, ``ready -> running -> ready``, so the headspace's own lifecycle state
does move to ``running`` for the duration of a job and back again — see
deviation ``d4`` (issue #2), which added that edge precisely so a state the
spec names is one a workspace genuinely occupies.

What stays at ``ready`` is the *provider's* descriptor. The two are separate
fields on the stored record on purpose: :mod:`headspace.core.workspace` owns
the headspace lifecycle, while a descriptor reports the engine's view, and
persisting the latter must never clobber the former — otherwise a concurrent
destroy would stop seeing the job it has to refuse to interrupt. Providers
therefore report in-flight work as :attr:`WorkspaceDescriptor.active_jobs` and
leave lifecycle transitions to the layer above.

The destroy guard refuses in the table's own words either way:
:func:`guard_removable` asks ``validate_transition(running, destroyed)``, which
is precisely the edge ``states.py`` deliberately omits — and now asks it about
a state the workspace is actually in.

Why ``remove`` walks a path
---------------------------
``ready`` has no direct edge to ``destroyed`` either — a workspace created and
never used still has to be destroyable. :data:`REMOVAL_PATHS` spells out, per
state, the sequence of edges that ``states.py`` *already* allows, and every hop
is validated against the live table rather than trusted. It is an explicit
table, not a shortest-path search, because a search would happily route
``running -> cancelled -> destroyed`` and end a caller's job behind its back.
``running`` and ``destroyed`` are absent on purpose; a test asserts they stay
absent.

Reused rather than re-invented
------------------------------
:class:`~headspace.core.result.ResourceUsage` and the status vocabulary come
straight from the result contract, so the orchestration layer can move a
:class:`JobOutcome` into a result package without a translation step — and a
translation step is where two vocabularies drift apart.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Protocol, runtime_checkable

from headspace.cli._errors import EXIT_INFRASTRUCTURE_FAILURE, EXIT_USER_ERROR, CliError
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.core.result import (
    STATUS_CANCELLED,
    STATUS_FAILURE,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    ResourceUsage,
)
from headspace.core.states import State, validate_transition

# --- vocabularies -----------------------------------------------------------

#: The statuses a :class:`JobOutcome` may carry: the result contract's
#: vocabulary minus the two that describe something other than the job.
#: ``infrastructure_failure`` and ``policy_denied`` are raised, never returned
#: (see the module docstring) — omitting them here is what makes that true.
JOB_STATUSES: tuple[str, ...] = (
    STATUS_SUCCESS,
    STATUS_PARTIAL_SUCCESS,
    STATUS_FAILURE,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
)

#: Statuses that mean the command never produced an exit status of its own.
_NO_EXIT_STATUSES: frozenset[str] = frozenset({STATUS_CANCELLED, STATUS_TIMEOUT})

#: The execution environment for a workspace (Docker: the container).
RESOURCE_RUNTIME = "runtime"
#: The workspace's own persistent storage (Docker: the volume).
RESOURCE_STORAGE = "storage"

#: The closed vocabulary a removal disposition may name. Deliberately two
#: neutral nouns rather than "container" and "volume": a disposition report is
#: read by agents and humans who must not need docker's glossary. Extending
#: this tuple is a contract change, not an implementation detail.
REMOVABLE_RESOURCES: tuple[str, ...] = (RESOURCE_RUNTIME, RESOURCE_STORAGE)

#: The key an :class:`OpaqueRef` serialises under. The conformance scanner
#: keys off this name to know which subtree it must NOT interpret.
OPAQUE_KEY = "opaque"

#: Default read granularity, matching :data:`headspace.core.artifacts.DEFAULT_CHUNK_SIZE`
#: so an artifact travels from the engine to the destination file in one size of
#: chunk rather than being re-cut at the boundary. It is only a default: the
#: caller passes ``chunk_size`` when it knows better, and a backend must honour
#: it — bounded chunks are the property that makes an artifact larger than
#: memory readable at all.
DEFAULT_READ_CHUNK_BYTES = 1 << 20

#: Path segments that carry no location. Dropped during normalisation so
#: ``./out.bin``, ``out.bin/`` and ``results//out.csv`` are one path each rather
#: than four distinct ones a backend would have to canonicalise itself.
_EMPTY_SEGMENTS: frozenset[str] = frozenset({"", "."})
#: The segment that leaves. Refused, never resolved — see
#: :func:`require_workspace_path`.
_PARENT_SEGMENT = ".."

#: An algorithm-prefixed content address, e.g. ``sha256:<64 hex>``. The
#: algorithm is not hardcoded: OCI already allows others, and a future backend
#: may address environments differently — but it must still be a *content*
#: address, not an engine-local object id.
ENVIRONMENT_DIGEST_RE = re.compile(r"\A[a-z0-9]+:[0-9a-f]{32,128}\Z")

#: Per state, the sequence of legal edges ``remove`` walks to reach
#: ``destroyed``. ``running`` and ``destroyed`` are absent on purpose — see the
#: module docstring; do not add them.
REMOVAL_PATHS: dict[State, tuple[State, ...]] = {
    State.REQUESTED: (State.CANCELLED, State.DESTROYED),
    State.PROVISIONING: (State.CANCELLED, State.DESTROYED),
    State.READY: (State.CANCELLED, State.DESTROYED),
    State.COMPLETED: (State.DESTROYED,),
    State.FAILED: (State.DESTROYED,),
    State.CANCELLED: (State.DESTROYED,),
    State.EXPIRED: (State.DESTROYED,),
}


# --- errors -----------------------------------------------------------------


class ProviderError(CliError):
    """The engine or its environment broke — never the caller's computation.

    A **subclass** of :class:`~headspace.cli._errors.CliError` for the same
    reason :class:`~headspace.core.policy.PolicyError` is: ``main()`` catches
    ``CliError`` by name and wraps anything else as an internal defect, so a
    look-alike type would report a legitimate infrastructure failure as a bug
    in headspace and route it through the wrong exit code.

    ``code`` defaults to :data:`~headspace.cli._errors.EXIT_INFRASTRUCTURE_FAILURE`
    (7), the taxonomy's own slot. Reporting it as a computational failure (6)
    would erase exactly the distinction NFR-07 exists to preserve: the caller
    would retry a different algorithm when it should restart a daemon.
    """

    def __init__(
        self,
        message: str,
        remediation: str = "",
        code: int = EXIT_INFRASTRUCTURE_FAILURE,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            remediation=remediation
            or (
                "the execution engine failed, not the job — check the engine is "
                "running and reachable, then retry"
            ),
        )


# --- helpers shared by every backend ---------------------------------------


def utc_now() -> str:
    """One timestamp format across every backend and the state store."""
    return datetime.now(timezone.utc).isoformat()


def requested_limit(policy: EffectivePolicy, name: str) -> Any:
    """The value a resolved policy requested for ``name``.

    :class:`~headspace.core.policy.EffectivePolicy` exposes each limit's
    *status* but not its value; backends need the value (a wall-clock budget to
    time out against, a byte cap to truncate at). Raising on an unknown name
    rather than returning a default keeps a typo from silently becoming an
    unbounded job.
    """
    for limit in policy.limits:
        if limit.name == name:
            return limit.requested
    known = ", ".join(sorted(limit.name for limit in policy.limits))
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"the effective policy carries no limit named {name!r}",
        remediation=f"resolved limits are: {known}",
    )


def environment_digest(environment: str) -> str:
    """The content address of a runtime environment, from its reference.

    A digest-pinned reference (what :func:`headspace.core.profiles.resolve`
    returns) already carries one after ``@`` — that suffix is used verbatim, so
    provenance records the digest the profile actually pinned. Anything else is
    hashed into a deterministic stand-in, because the descriptor's contract is
    "a stable content address for this environment" and a backend that cannot
    supply one must not be allowed to report an empty field that later reads as
    "unknown provenance".

    Only the digest travels — never the registry host, repository, or tag. A
    caller needs to know *which* environment ran, not where it was fetched from.
    """
    _, separator, digest = environment.rpartition("@")
    if separator and ENVIRONMENT_DIGEST_RE.match(digest):
        return digest
    return "sha256:" + hashlib.sha256(environment.encode("utf-8")).hexdigest()


def removal_path(state: State) -> tuple[State, ...]:
    """The legal edges ``remove`` must walk from ``state`` to ``destroyed``.

    Raises :class:`CliError` for a state with no removal path, and raises it by
    asking :func:`~headspace.core.states.validate_transition` — so the message,
    the remediation, and the rule itself all come from the lifecycle table
    rather than being restated (and eventually contradicted) here.
    """
    path = REMOVAL_PATHS.get(state)
    if path is not None:
        return path
    validate_transition(state, State.DESTROYED)
    raise CliError(  # pragma: no cover - unreachable while the table lacks the edge
        code=EXIT_USER_ERROR,
        message=f"a workspace in state '{state.value}' cannot be removed",
        remediation="let the workspace reach a settled state first",
    )


def guard_removable(state: State, active_jobs: int) -> tuple[State, ...]:
    """Refuse a removal that would end work in flight; else return the path.

    In-flight work is reported as a count rather than a ``running`` workspace
    state (see the module docstring), so the refusal is produced by asking the
    table about the edge it deliberately omits. The caller gets the canonical
    ``'running' -> 'destroyed'`` message either way.
    """
    if active_jobs > 0:
        validate_transition(State.RUNNING, State.DESTROYED)
    return removal_path(state)


def require_workspace_id(workspace_id: str) -> str:
    """Reject an unusable workspace id before it reaches an engine."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid workspace id {workspace_id!r}",
            remediation="pass the non-empty workspace id the state store assigned",
        )
    return workspace_id


def require_command(command: Sequence[str]) -> tuple[str, ...]:
    """Normalise an argv into a tuple, refusing anything that cannot be executed."""
    if isinstance(command, str) or not isinstance(command, Sequence):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"a job command must be a sequence of arguments, got {type(command).__name__}",
            remediation="pass argv as a list or tuple, e.g. ['python', '-c', 'print(1)']",
        )
    argv = tuple(command)
    if not argv or not all(isinstance(part, str) for part in argv):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="a job command must be a non-empty sequence of strings",
            remediation="pass argv as a list or tuple, e.g. ['python', '-c', 'print(1)']",
        )
    return argv


def unknown_workspace(workspace_id: str) -> CliError:
    """The one refusal shape every backend uses for an id it does not hold."""
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"unknown workspace {workspace_id}",
        remediation="create the workspace first, or pass an id that still exists",
    )


def _refuse_path(path: Any, why: str) -> CliError:
    """One refusal shape for every unusable path, so the advice is stated once."""
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"unusable workspace path {path!r}: {why}",
        remediation=(
            "pass a path relative to the workspace root, such as 'out.csv' or "
            "'results/final.csv' — a workspace read never leaves the workspace"
        ),
    )


def require_workspace_path(path: str) -> str:
    """Normalise a workspace-relative path, refusing anything that could leave it.

    The workspace is a boundary, and this is where the boundary is drawn — once,
    at the seam, rather than once per backend. That placement is the whole point
    rather than tidiness: the engines underneath resolve ``..`` perfectly
    happily. Docker's archive endpoint, asked for ``/workspace/../etc/hostname``,
    returns the container's own file with a straight face. A rule that lived in
    each backend would be a rule each backend could forget, and forgetting it
    would look exactly like a working read.

    The grammar is deliberately small: a relative path, ``/``-separated, with no
    root and no ``..`` anywhere. Empty and ``.`` segments carry no location and
    are dropped, which is what makes ``./out.bin``, ``out.bin/`` and
    ``results//out.csv`` one path each instead of four a backend has to
    canonicalise itself.

    ``..`` is **refused, not resolved**. Textual resolution would produce a path
    that looks contained and then be handed to an engine that resolves symlinks
    on its own terms, so the two would disagree exactly where it matters. A
    caller that meant a sibling directory can name it directly.
    """
    if not isinstance(path, str):
        raise _refuse_path(path, f"a workspace path must be a string, not {type(path).__name__}")
    if not path.strip():
        raise _refuse_path(path, "a workspace path must name something")
    if "\x00" in path:
        raise _refuse_path(path, "a path may not contain a null byte")
    if path.startswith("/"):
        raise _refuse_path(path, "an absolute path presumes a layout the seam has none of")
    segments = [segment for segment in path.split("/") if segment not in _EMPTY_SEGMENTS]
    if _PARENT_SEGMENT in segments:
        raise _refuse_path(path, f"'{_PARENT_SEGMENT}' would leave the workspace")
    if not segments:
        raise _refuse_path(path, "a workspace path must name something")
    return "/".join(segments)


def require_chunk_size(chunk_size: int) -> int:
    """Reject a read granularity no backend could honour.

    Checked at the seam for the same reason the path is: a non-positive chunk
    size means something different in every transport — an unbounded read here,
    an empty slice there — and "bounded chunks" is the one property ``read``
    exists to promise.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"chunk_size must be a positive number of bytes, got {chunk_size!r}",
            remediation=f"omit chunk_size to read in {DEFAULT_READ_CHUNK_BYTES}-byte chunks",
        )
    return chunk_size


def missing_workspace_path(workspace_id: str, path: str) -> CliError:
    """The one refusal shape every backend uses for a path a workspace lacks.

    A :class:`CliError` and deliberately **not** a :class:`ProviderError`. The
    engine answered the question correctly and the answer was "there is no such
    file": that is the caller's mistake, not a broken engine. Reporting it as
    exit 7 would tell an agent to restart a daemon and retry, when what it needs
    is to look at what its job actually wrote.
    """
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"workspace {workspace_id} holds no readable file at '{path}'",
        remediation=(
            "check what the job wrote, relative to the workspace root; only a regular "
            "file can be read back — a directory or a link is not an artifact"
        ),
    )


def unreadable_workspace_path(workspace_id: str, path: str, kind: str) -> CliError:
    """Refuse a path that exists but is not one file's bytes.

    Separate from :func:`missing_workspace_path` because the remedy differs: a
    missing artifact means the job did not write it, while a directory or a
    symlink means the caller named the wrong *kind* of thing. The symlink case
    is a boundary check rather than a nicety — a job can plant a link pointing
    outside the workspace volume, and an engine that resolves it would quietly
    export a file the workspace never produced.
    """
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"workspace {workspace_id} holds a {kind} at '{path}', not a file",
        remediation=(
            "an artifact is one regular file's bytes; name the file itself — a "
            "directory has to be archived by the job first, and a link is not "
            "followed because its target may lie outside the workspace"
        ),
    )


# --- structures that cross the seam ----------------------------------------


@dataclass(frozen=True, repr=False)
class OpaqueRef:
    """A backend-private identifier, round-tripped and never interpreted.

    The sanctioned exception to the neutrality rule, and deliberately the only
    one. Some backends genuinely need their own handle back on the next
    invocation, and forcing them to re-derive it every time would be slower and
    more fragile than simply carrying it. What makes that safe is that nothing
    above this seam may read it: the field name says so, the conformance
    scanner refuses to walk inside it, and a companion assertion fails if the
    token also appears anywhere a caller *would* read.

    :meth:`__repr__` and :meth:`__str__` hide the token. Not obfuscation —
    these structures are rendered into logs and error messages, and a handle
    that shows up there is a handle someone eventually parses.
    """

    token: str = ""

    def to_dict(self) -> dict[str, str]:
        return {OPAQUE_KEY: self.token}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OpaqueRef:
        return cls(token=str(data.get(OPAQUE_KEY, "")))

    def __repr__(self) -> str:
        return "OpaqueRef(<opaque>)"

    def __str__(self) -> str:
        return "<opaque>"


@dataclass(frozen=True)
class WorkspaceDescriptor:
    """Provider-side facts about one workspace, normalized.

    Returned by both ``create`` and ``inspect`` on purpose: "what you just
    made" and "what is there now" are the same question, and one structure
    means one thing for the store to persist and one thing for the result
    package to draw provenance from.

    Fields, and why each earns its place:

    ``workspace_id``
        headspace's own id — the only handle any verb takes.
    ``provider``
        which backend holds it, for provenance and for routing reconciliation.
        Naming a backend is not leaking one.
    ``state``
        the normalized lifecycle state. Stays ``ready`` across jobs; see the
        module docstring.
    ``environment_digest``
        the content address of the runtime that will execute jobs. Feeds
        :attr:`headspace.core.result.Provenance.image_digest` unchanged.
    ``created_at``
        ISO-8601 UTC, for provenance and expiry.
    ``network_enabled``
        what the backend *actually* configured, not what was asked for — the
        closed-by-default posture is worth verifying, not assuming.
    ``storage_bytes``
        measured workspace storage. Measured, not capped, on the default local
        volume driver — which is why the policy view labels it that way.
    ``active_jobs``
        jobs in flight right now; the destroy guard's input.
    ``ref``
        the opaque backend handle. Never interpreted above this seam.
    """

    workspace_id: str
    provider: str
    state: State
    environment_digest: str
    created_at: str
    network_enabled: bool = False
    storage_bytes: int = 0
    active_jobs: int = 0
    ref: OpaqueRef = field(default_factory=OpaqueRef)

    def __post_init__(self) -> None:
        if self.environment_digest and not ENVIRONMENT_DIGEST_RE.match(self.environment_digest):
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(
                    f"environment_digest {self.environment_digest!r} is not an "
                    "algorithm-prefixed content address"
                ),
                remediation="record a content digest such as sha256:<64 hex>, never an engine id",
            )

    def to_dict(self) -> dict[str, Any]:
        """The persisted form. JSON primitives only, all the way down."""
        return {
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "state": self.state.value,
            "environment_digest": self.environment_digest,
            "created_at": self.created_at,
            "network_enabled": self.network_enabled,
            "storage_bytes": self.storage_bytes,
            "active_jobs": self.active_jobs,
            "ref": self.ref.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspaceDescriptor:
        """Rebuild from stored state, refusing anything this build cannot model."""
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown workspace descriptor fields: {', '.join(unknown)}",
                remediation=f"descriptors carry: {', '.join(sorted(known))}",
            )
        try:
            return cls(
                workspace_id=str(data["workspace_id"]),
                provider=str(data["provider"]),
                state=State(data["state"]),
                environment_digest=str(data["environment_digest"]),
                created_at=str(data["created_at"]),
                network_enabled=bool(data.get("network_enabled", False)),
                storage_bytes=int(data.get("storage_bytes", 0)),
                active_jobs=int(data.get("active_jobs", 0)),
                ref=OpaqueRef.from_dict(data.get("ref") or {}),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"malformed workspace descriptor: {err}",
                remediation=f"descriptors carry: {', '.join(sorted(known))}",
            ) from err


@dataclass(frozen=True)
class JobOutcome:
    """What one command did, normalized — never what the engine did to run it.

    ``output`` is stdout and stderr interleaved in emission order, already
    truncated to the policy's ``output_bytes`` budget. Two deliberate choices:

    * **Bounded at capture, not at render.** A job writing a gigabyte must not
      be allowed to fill the host or the state store before something downstream
      decides to shorten it (FR-16).
    * **Not split into two streams.** The result contract never renders a
      transcript — captured text enters only as bounded evidence excerpts — and
      the streams' *relative order* is what makes a bounded excerpt readable.
      No boundary-crossing structure can promise faithful separation *and*
      faithful ordering across every backend, so this one promises ordering.
      Backends that separate streams internally are free to; they merge here.

    ``truncated`` says whether anything was dropped, and
    ``usage.output_bytes`` is the volume the job *really* produced, so the
    compression is always visible — the same honesty clause the result package
    keeps with its truncation markers.

    ``exit_status`` is the command's, never headspace's process exit code, and
    is ``None`` exactly when the command never produced one (timeout, cancel).
    """

    job_id: str
    workspace_id: str
    status: str
    exit_status: int | None
    output: str
    truncated: bool
    started_at: str
    finished_at: str
    usage: ResourceUsage = field(default_factory=ResourceUsage)

    def __post_init__(self) -> None:
        if self.status not in JOB_STATUSES:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"a job outcome cannot carry status {self.status!r}",
                remediation=(
                    "job statuses are: "
                    + ", ".join(JOB_STATUSES)
                    + " — infrastructure_failure and policy_denied are raised as "
                    "ProviderError and PolicyError, never returned as an outcome"
                ),
            )
        no_exit = self.status in _NO_EXIT_STATUSES
        if no_exit and self.exit_status is not None:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"a {self.status} job cannot report exit status {self.exit_status}",
                remediation="a job that was stopped never produced one; report None",
            )
        if not no_exit and self.exit_status is None:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"a {self.status} job must report the exit status it produced",
                remediation="report the command's exit status, not headspace's",
            )
        if self.status == STATUS_SUCCESS and self.exit_status != 0:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"a successful job cannot exit {self.exit_status}",
                remediation=f"report status {STATUS_FAILURE!r} for a non-zero exit status",
            )
        if self.status == STATUS_FAILURE and self.exit_status == 0:
            raise CliError(
                code=EXIT_USER_ERROR,
                message="a failed job cannot exit 0",
                remediation=f"report status {STATUS_SUCCESS!r} for a zero exit status",
            )
        captured = len(self.output.encode("utf-8"))
        if self.usage.output_bytes < captured:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(
                    f"the job reports {self.usage.output_bytes} bytes produced but "
                    f"{captured} bytes were captured"
                ),
                remediation="usage.output_bytes is the volume produced, never the volume kept",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "status": self.status,
            "exit_status": self.exit_status,
            "output": self.output,
            "truncated": self.truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "usage": self.usage.to_dict(),
        }


@dataclass(frozen=True)
class RemovalDisposition:
    """What a removal actually did — the destruction report's factual half.

    The doc's destruction criterion asks for what was removed, what was
    retained, and what could not be verified. Those three are a *partition* of
    the resources a workspace holds, not three free lists: a resource in two
    buckets, or in none, is a report a caller cannot act on. That is enforced
    here rather than in a renderer, so no future report can be inconsistent.

    The vocabulary is :data:`REMOVABLE_RESOURCES` — neutral nouns, closed set.
    A backend that removed "a container and a volume" reports "runtime and
    storage"; a reader never needs docker's glossary to understand a teardown.
    """

    workspace_id: str
    removed: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        named = self.removed + self.retained + self.unverified
        unknown = sorted(set(named) - set(REMOVABLE_RESOURCES))
        if unknown:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"a removal disposition names unknown resources: {', '.join(unknown)}",
                remediation=f"removable resources are: {', '.join(REMOVABLE_RESOURCES)}",
            )
        if len(named) != len(set(named)):
            raise CliError(
                code=EXIT_USER_ERROR,
                message="a removal disposition counts a resource more than once",
                remediation=(
                    "removed, retained, and unverified partition the workspace's "
                    "resources — name each at most once"
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "removed": list(self.removed),
            "retained": list(self.retained),
            "unverified": list(self.unverified),
        }


# --- the bytes themselves ---------------------------------------------------


class ByteStream:
    """An artifact's bytes, in bounded chunks, with a release that always runs.

    The one thing crossing this seam that is not a serialisable record, and it
    earns the exception: an artifact can be larger than the host's memory, so
    ``read`` has to hand back something lazy. What it must *not* hand back is a
    bare generator, for two reasons that are really one:

    * **A backend holds something open.** A socket, an HTTP response, an archive
      reader. Whatever it is, it is the backend's business and no caller should
      have to learn its shape in order to let go of it. One ``close`` is the
      whole vocabulary.
    * **The consumer routinely stops early.** The export path abandons a stream
      whenever a digest fails to verify or a destination fills up, and an engine
      can die mid-artifact. "Read it all or leak" would therefore not be a
      theoretical failure mode; it would be the common one. So the release runs
      on exhaustion, on :meth:`close`, on ``with``-block exit, and on an
      exception raised by the source — every way out, once.

    Deliberately **not** a file object: there is no ``read(n)``. The backend is
    the side that knows what a cheap read looks like on its own transport, so
    its chunk boundaries travel unchanged all the way to disk.
    :func:`headspace.core.artifacts.export_artifact` re-chunks anything with a
    ``.read()`` and iterates anything else, so the absence is what keeps the
    backend's granularity intact rather than silently re-cut.

    Idempotent by construction: :attr:`_release` is dropped as it fires, so a
    consumer that closes twice — or closes a stream that already ran dry —
    releases once and then yields nothing more.
    """

    def __init__(
        self, chunks: Iterable[bytes], *, release: Callable[[], None] | None = None
    ) -> None:
        self._chunks: Iterator[bytes] = iter(chunks)
        self._release = release
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        """Return *self*, not a fresh iterator.

        A stream is a position in someone else's transport, not a re-readable
        collection. Two iterators over one stream would silently interleave, so
        ``iter()`` twice continues from where the last chunk left off — which is
        also what lets a caller pull one chunk with ``next(iter(stream))`` and
        then walk away.
        """
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._chunks)
        except StopIteration:
            # Exhausted is one of the ways out, and the most common: releasing
            # only on close would leak for every consumer that read to the end.
            self.close()
            raise
        except BaseException:
            # The failure path matters most. A broken engine must not also
            # strand whatever it was holding open on the way out.
            self.close()
            raise

    def close(self) -> None:
        """Release the backend's resources. Safe to call any number of times."""
        if self._closed:
            return
        self._closed = True
        chunks, self._chunks = self._chunks, iter(())
        release, self._release = self._release, None
        try:
            # Finalise a generator source so its own ``finally`` blocks run at a
            # moment the caller chose, rather than whenever the collector gets
            # to it.
            closer = getattr(chunks, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()
        finally:
            if release is not None:
                release()

    def __enter__(self) -> ByteStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False


# --- the seam ---------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """Everything headspace needs from an execution backend.

    The expected call order, which is also the reason ``capabilities`` is a
    separate verb: probe the host, resolve the caller's declared policy against
    that snapshot (:func:`headspace.core.policy.resolve`, which raises
    :class:`~headspace.core.policy.PolicyError` if the host cannot back it),
    and only then create. A policy the host cannot enforce is refused *before*
    anything runs, which is what "fails closed rather than silently degrading"
    means in practice.
    """

    @property
    def name(self) -> str:
        """Short, stable backend identifier, stamped into every descriptor."""

    def capabilities(self) -> CapabilitySnapshot:
        """Probe what this host can actually enforce.

        Returns plain data. Raises :class:`ProviderError` when the engine
        cannot be reached at all — an unreachable engine is an infrastructure
        failure, not an engine that enforces nothing.
        """

    def create(
        self, workspace_id: str, environment: str, policy: EffectivePolicy
    ) -> WorkspaceDescriptor:
        """Provision a workspace under an already-resolved policy.

        ``environment`` is whatever :func:`headspace.core.profiles.resolve`
        returned — an opaque environment reference. The Docker provider reads
        it as a digest-pinned image and pulls it (resolution never pulls; the
        provider does); another backend may read it another way.

        Raises :class:`CliError` (exit 1) for a duplicate or unusable id, and
        :class:`ProviderError` (exit 7) when the engine cannot provision.
        """

    def run(
        self,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        *,
        job_id: str,
    ) -> JobOutcome:
        """Execute one command in an existing workspace and normalize the result.

        The policy is passed again rather than remembered from ``create``
        because there is no daemon to remember it in, and because the wall-clock
        and output-byte budgets are enforced by headspace's own process — the
        provider needs the numbers in hand at each call.

        Blocking, synchronous, batch. Returns a :class:`JobOutcome` for any
        outcome *of the job*, including failure and timeout. Raises
        :class:`ProviderError` when the engine broke instead.
        """

    def inspect(self, workspace_id: str) -> WorkspaceDescriptor:
        """Report the backend's current facts about a workspace.

        Raises :class:`CliError` (exit 1) for an id this backend does not hold.
        """

    def read(
        self, workspace_id: str, path: str, *, chunk_size: int = DEFAULT_READ_CHUNK_BYTES
    ) -> ByteStream:
        """Stream one file's bytes out of a workspace.

        ``path`` is relative to the workspace root and is normalised and bounded
        by :func:`require_workspace_path`; ``chunk_size`` is the largest chunk
        the returned stream may yield, and honouring it is what makes an
        artifact larger than memory readable.

        Must work whether or not the workspace's runtime is still alive. That is
        the case the verb exists for: a container has exited but the storage —
        and the work in it — has not, and a read that needed a live runtime
        would lose exactly the results a caller most needs back.

        Raises :class:`CliError` (exit 1) for an unknown workspace, a path the
        workspace does not hold, a path that is not a regular file, and a path
        that would leave the workspace. Raises :class:`ProviderError` (exit 7)
        when the engine broke — including part-way through the stream, where it
        surfaces from the iteration rather than from this call.
        """

    def remove(self, workspace_id: str) -> RemovalDisposition:
        """Tear the workspace down and report exactly what went away.

        Refuses, via the lifecycle table, while a job is in flight — see
        :func:`guard_removable`. Removing a workspace that is already gone
        raises :class:`CliError` (exit 1); removal is not idempotent, because a
        caller that "removed" something twice usually removed the wrong thing.

        Artifact retention is *not* checked here. Whether unexported artifacts
        should block a teardown is a question about headspace's inventory, not
        about the backend, and it is answered a layer up before this is called.
        """
