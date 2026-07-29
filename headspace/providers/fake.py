"""An in-memory backend that is a real implementation, not a stub.

WHY this module exists
----------------------
Two reasons, and the second is the one that matters.

The obvious reason: CI has no Docker daemon guaranteed, the coverage gate must
not depend on one, and a developer should be able to run the whole suite on a
laptop with nothing installed. Every unit test above this seam — orchestration
flows, CLI verbs, result rendering — runs against this provider, so the entire
suite is runnable with no engine, no network, and no filesystem.

The reason that matters: **a conformance suite is only worth something if two
genuinely different implementations pass it.** If this were a stub returning
canned structures, the suite would degenerate into a test of its own fixtures,
and the Docker provider would be the first implementation to discover what the
seam actually requires — which is exactly when a leaky abstraction is most
expensive to fix. So this simulates the semantics rather than the transport: it
walks the real lifecycle table, enforces the real wall-clock and output budgets
from the resolved policy, refuses removal while work is in flight, and raises
the same errors in the same taxonomy slots. It differs from the Docker provider
in *what executes the command*, and in nothing else.

Programmable, because failure paths need testing too
----------------------------------------------------
A real engine cannot be asked to fail on cue, so the honest failure paths —
a job that exits non-zero, a job that outruns its budget, a job that floods its
output, an engine that breaks mid-run, a command the image cannot execute, a
job killed for exceeding its memory ceiling — would otherwise be untested
until production. This provider is therefore a small programmable execution
engine: :meth:`FakeProvider.script_command` maps an argv to a :class:`JobPlan`
describing what running it does, and :meth:`FakeProvider.break_next` arms an
infrastructure failure on the next call to a named verb. An unscripted command
succeeds silently, so a test scripts only the behaviour it asserts.

Deliberately clock-free and I/O-free
------------------------------------
Timings come from the plan, not from a wall clock, and a "30 second" job takes
no time at all — timeouts are decided by comparing the plan's declared duration
against the policy's budget. A test that had to sleep to observe a timeout would
be slow *and* flaky. Nothing here opens a file, a socket, or a subprocess; a
test asserts that mechanically by inspecting this module's imports.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core.artifacts import ByteSource
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.core.result import (
    STATUS_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    ResourceUsage,
)
from headspace.core.states import State, validate_transition
from headspace.providers.base import (
    DEFAULT_READ_CHUNK_BYTES,
    REMOVABLE_RESOURCES,
    ByteStream,
    JobOutcome,
    OpaqueRef,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    environment_digest,
    guard_removable,
    missing_workspace_path,
    requested_limit,
    require_chunk_size,
    require_command,
    require_workspace_id,
    require_workspace_path,
    unknown_workspace,
    utc_now,
)

#: A duration no realistic budget allows — how :meth:`JobPlan.timing_out`
#: expresses "runs forever" without knowing the budget it will be measured
#: against.
FOREVER_SECONDS = 10**9

#: The POSIX exit statuses a command the image cannot execute reports.
#: Identical in name and value to :data:`headspace.providers.docker.EXIT_COMMAND_NOT_EXECUTABLE`
#: / :data:`~headspace.providers.docker.EXIT_COMMAND_NOT_FOUND` — duplicated here
#: rather than imported, because importing anything from that module would pull
#: the Docker SDK into the one provider that must build and run without it.
#: ``NOT_FOUND`` (127) is nothing exists under that name; ``NOT_EXECUTABLE``
#: (126) is it exists but cannot be run. Two numbers, never collapsed to one,
#: because the fix differs: 126 never sends anyone off to re-spell a command
#: that was sitting right where it asked.
EXIT_COMMAND_NOT_FOUND = 127
EXIT_COMMAND_NOT_EXECUTABLE = 126

#: The exit status a real OOM kill reports on Linux: SIGKILL (9) folded into
#: the shell's ``128 + signal`` convention, and the value :meth:`JobPlan.oom_killed`
#: defaults to for realism. Unlike Docker — which has to tell a genuine kernel
#: kill apart from a process that deliberately chose the same number by reading
#: ``State.OOMKilled`` alongside it — a scripted plan states its
#: :attr:`~JobPlan.status` directly, so no such disambiguation is needed here.
EXIT_OOM_KILLED = 137

#: The verbs :meth:`FakeProvider.break_next` can break. All six, because NFR-07
#: applies to all six: the path that carries artifacts out has to report a dead
#: engine as a dead engine just as loudly as the path that runs jobs.
BREAKABLE_OPERATIONS: tuple[str, ...] = (
    "capabilities",
    "create",
    "run",
    "inspect",
    "read",
    "remove",
)

#: What this fake claims a host can enforce. Mirrors a healthy real host,
#: storage included: ``storage_enforceable=False`` is the canonical
#: measured-only case on the default local volume driver, and a fake that
#: claimed otherwise would let a policy resolve here that fails on Docker.
DEFAULT_CAPABILITIES = CapabilitySnapshot(
    engine="fake",
    api_version="1",
    network_disable_supported=True,
    filesystem_scope_enforceable=True,
    memory_enforceable=True,
    cpu_enforceable=True,
    pids_enforceable=True,
    storage_enforceable=False,
)

#: The default for ``run(..., env=...)``: an empty mapping, never a mutable
#: ``{}`` literal. A dataclass or keyword default is created exactly once, at
#: import time, and shared by every call that omits the argument — a mutable
#: default would let one caller's incidental mutation of "no env" leak into
#: every other caller's "no env" for the rest of the process. Wrapping the
#: empty dict in :class:`~types.MappingProxyType` makes that structurally
#: impossible rather than merely a convention nobody violates yet, which is
#: the same closed-by-default posture the rest of this seam takes with
#: network and filesystem access.
EMPTY_ENV: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class JobPlan:
    """What running one scripted command does. The failure-mode switchboard.

    ``wall_time_seconds`` is the duration the job *claims*; nothing sleeps for
    it. If it exceeds the policy's wall-clock budget the job times out, so a
    plan is budget-independent and the same plan behaves correctly under any
    budget a test chooses.

    ``produced_bytes`` is the output volume the job claims to have produced,
    which may exceed ``output`` — that is how a flood is simulated without
    allocating a gigabyte. Left ``None`` it is the real size of ``output``,
    which is the case that exercises the truncation path for real.

    ``infrastructure_failure``, when non-empty, makes the run *raise*
    :class:`ProviderError` instead of returning an outcome — the taxonomy's
    engine-broke slot, which a :class:`JobOutcome` structurally cannot express.

    ``during`` is called while the job is in flight, with the workspace's
    ``active_jobs`` already incremented. It exists so a test can observe the
    one state a synchronous, single-process MVP otherwise cannot reach from
    outside: a workspace with a job actually running.

    ``writes`` is what the job leaves behind in the workspace, keyed by
    workspace-relative path. It exists so read-back can be exercised against
    something a *job* produced rather than against content a test placed behind
    the provider's back — an artifact nobody produced would prove nothing about
    artifacts leaving. The bytes count towards the workspace's measured storage
    exactly as a real job's output would.
    """

    status: str = STATUS_SUCCESS
    exit_status: int | None = 0
    output: str = ""
    produced_bytes: int | None = None
    wall_time_seconds: float = 0.01
    cpu_seconds: float = 0.0
    max_memory_bytes: int = 0
    storage_bytes: int = 0
    infrastructure_failure: str = ""
    #: Whether this scripted job models an environment that refused to exec
    #: ``argv[0]``. Kept a separate field rather than inferred from
    #: ``exit_status``, exactly as the real provider keeps it separate: a
    #: command that ran can return 126 or 127 on its own account.
    command_refused: bool = False
    during: Callable[[], None] | None = None
    writes: Mapping[str, bytes] = field(default_factory=dict)
    #: When True, the job is considered stoppable: ``stop()`` can end it.
    #: The flag exists so a test can script a job that is in flight and
    #: therefore stoppable, without having to rely on a real engine's
    #: asynchronous lifecycle.
    stoppable: bool = False

    @classmethod
    def succeeding(cls, output: str = "", **overrides: Any) -> JobPlan:
        return cls(status=STATUS_SUCCESS, exit_status=0, output=output, **overrides)

    @classmethod
    def failing(cls, exit_status: int = 1, output: str = "", **overrides: Any) -> JobPlan:
        return cls(status=STATUS_FAILURE, exit_status=exit_status, output=output, **overrides)

    @classmethod
    def not_executable(
        cls, exit_status: int = EXIT_COMMAND_NOT_FOUND, output: str = "", **overrides: Any
    ) -> JobPlan:
        """A command the image cannot execute — the caller's mistake, not a broken engine.

        Mirrors what :mod:`headspace.providers.docker` reports for the identical
        case: ``status`` stays :data:`~headspace.core.result.STATUS_FAILURE` —
        never ``infrastructure_failure`` — with ``exit_status`` one of
        :data:`EXIT_COMMAND_NOT_FOUND` or :data:`EXIT_COMMAND_NOT_EXECUTABLE`. A
        test picks which by passing the constant it means to assert; defaulting
        to "not found" keeps the common case a bare call.
        """
        overrides.setdefault("command_refused", True)
        return cls.failing(exit_status=exit_status, output=output, **overrides)

    @classmethod
    def oom_killed(
        cls, exit_status: int = EXIT_OOM_KILLED, output: str = "", **overrides: Any
    ) -> JobPlan:
        """A job stopped for exceeding its memory ceiling — ``resource_exhausted``.

        Composes with ``wall_time_seconds`` exactly as every other plan does:
        :meth:`FakeProvider.run` decides ``timeout`` before it looks at
        ``status`` at all, so a plan scripted both ways still reports
        :data:`~headspace.core.result.STATUS_TIMEOUT` — the same precedence
        :mod:`headspace.providers.docker` keeps between its own wall-clock kill
        and the kernel's memory kill, because headspace stopping a job on
        purpose outranks the kernel stopping it for a different reason.
        """
        return cls(
            status=STATUS_RESOURCE_EXHAUSTED, exit_status=exit_status, output=output, **overrides
        )

    @classmethod
    def timing_out(cls, **overrides: Any) -> JobPlan:
        """A job that outruns any budget a caller could reasonably declare."""
        return cls(wall_time_seconds=FOREVER_SECONDS, **overrides)

    @classmethod
    def flooding(cls, output: str, **overrides: Any) -> JobPlan:
        """A job whose output is meant to exceed the caller's byte budget."""
        return cls(status=STATUS_SUCCESS, exit_status=0, output=output, **overrides)

    @classmethod
    def breaking(cls, message: str) -> JobPlan:
        """A job whose engine dies under it — raised, never returned."""
        return cls(infrastructure_failure=message)


@dataclass
class _Workspace:
    """The fake's private record. Never crosses the seam; descriptors do.

    ``files`` is the workspace's storage: a dict keyed by normalised
    workspace-relative path. It is the fake's stand-in for a volume, and it is
    what makes ``read`` a real read — the bytes came from somewhere, they
    outlive the job that wrote them, and they die with the workspace.

    ``storage_bytes`` counts only what *jobs claimed* to occupy; the bytes
    actually sitting in ``files`` are added on top when the workspace is
    described. Keeping the two apart means a scripted storage figure and a real
    file cannot silently double-count each other.
    """

    workspace_id: str
    environment_digest: str
    created_at: str
    network_enabled: bool
    token: str
    state: State = State.READY
    storage_bytes: int = 0
    active_jobs: int = 0
    files: dict[str, bytes] = field(default_factory=dict)
    #: The env mapping the last job ran with. Recorded so a test can assert
    #: what the job would have seen, without the fake leaking env into any
    #: other recorded field.
    last_job_env: Mapping[str, str] = field(default_factory=dict)
    #: Whether a stoppable job is currently in flight for this workspace.
    #: Set by ``run()`` when the plan is stoppable, cleared by ``stop()``.
    #: Deliberately *not* leading-underscore: a dataclass field name becomes
    #: the matching ``__init__`` keyword, and an underscore-prefixed keyword
    #: is an awkward, easy-to-typo constructor argument for no real payoff —
    #: ``_Workspace`` is already private at the class level, so the fields
    #: inside it do not need to re-assert that individually.
    job_in_flight: bool = False


class FakeProvider:
    """A complete, in-memory :class:`~headspace.providers.base.Provider`."""

    def __init__(
        self,
        *,
        name: str = "fake",
        capabilities: CapabilitySnapshot | None = None,
        script: Mapping[Sequence[str], JobPlan] | None = None,
        default_plan: JobPlan | None = None,
    ) -> None:
        self._name = name
        self._capabilities = capabilities if capabilities is not None else DEFAULT_CAPABILITIES
        self._script: dict[tuple[str, ...], JobPlan] = {
            tuple(command): plan for command, plan in (script or {}).items()
        }
        self._default_plan = default_plan if default_plan is not None else JobPlan()
        self._workspaces: dict[str, _Workspace] = {}
        self._broken: dict[str, str] = {}

    # --- simulation controls ---------------------------------------------

    def script_command(self, command: Sequence[str], plan: JobPlan) -> None:
        """Make ``command`` behave as ``plan`` describes, from now on."""
        self._script[tuple(command)] = plan

    def write_file(self, workspace_id: str, path: str, content: bytes) -> None:
        """Put bytes in a workspace with no job to run — the seeding shortcut.

        For tests whose subject is the *read* rather than the write. The
        conformance suite deliberately does not use this: it scripts a job with
        :attr:`JobPlan.writes` instead, because read-back has to be proven
        against something a job produced.

        Refuses an unknown workspace rather than conjuring one, so seeding
        cannot quietly create the very thing a test is about to assert exists.
        """
        record = self._require(require_workspace_id(workspace_id))
        record.files[require_workspace_path(path)] = bytes(content)

    def break_next(self, operation: str = "run", message: str = "") -> None:
        """Arm an infrastructure failure on the next call to ``operation``.

        One-shot: the arming is consumed when it fires, so a test can prove the
        provider recovers rather than staying permanently broken.
        """
        if operation not in BREAKABLE_OPERATIONS:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"cannot break unknown operation {operation!r}",
                remediation=f"breakable operations are: {', '.join(BREAKABLE_OPERATIONS)}",
            )
        self._broken[operation] = message or f"simulated engine failure during {operation}"

    def _fail_if_broken(self, operation: str) -> None:
        message = self._broken.pop(operation, "")
        if message:
            raise ProviderError(message)

    # --- the seam ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> CapabilitySnapshot:
        self._fail_if_broken("capabilities")
        return self._capabilities

    def create(
        self, workspace_id: str, environment: str, policy: EffectivePolicy
    ) -> WorkspaceDescriptor:
        workspace_id = require_workspace_id(workspace_id)
        self._fail_if_broken("create")
        if workspace_id in self._workspaces:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"workspace {workspace_id} already exists",
                remediation="choose a fresh workspace id, or remove the existing workspace",
            )

        # The provisioning walk is real: every hop is checked against the
        # lifecycle table, so a table change breaks the fake exactly as it
        # would break a real backend.
        state = State.REQUESTED
        for target in (State.PROVISIONING, State.READY):
            validate_transition(state, target)
            state = target

        record = _Workspace(
            workspace_id=workspace_id,
            environment_digest=environment_digest(environment),
            created_at=utc_now(),
            network_enabled=requested_limit(policy, "network") == "enabled",
            token=uuid.uuid4().hex,
            state=state,
        )
        self._workspaces[workspace_id] = record
        return self._describe(record)

    def run(
        self,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        *,
        job_id: str,
        env: Mapping[str, str] = EMPTY_ENV,
    ) -> JobOutcome:
        """Run one scripted command, and record the ``env`` it observed.

        ``env`` defaults to :data:`EMPTY_ENV` rather than an unset default:
        a caller that never sets an environment variable and a caller that
        explicitly passes ``{}`` must be indistinguishable to the provider,
        because that is what "no env" means at the seam. What the job
        actually saw is recorded onto ``record.last_job_env`` — nowhere
        else — so a test can assert on it without the env leaking into any
        field that a real backend could not populate the same way (output,
        usage, and so on are not places an environment variable belongs).

        A plan whose :attr:`JobPlan.stoppable` is set marks the workspace
        as having a job in flight for the duration of the call, which is
        what makes :meth:`stop` meaningful to call from inside
        :attr:`JobPlan.during`.
        """
        record = self._require(require_workspace_id(workspace_id))
        argv = require_command(command)
        self._fail_if_broken("run")
        plan = self._script.get(argv, self._default_plan)
        if plan.infrastructure_failure:
            raise ProviderError(plan.infrastructure_failure)

        # Record the env the job observed. This is the only place env touches
        # the workspace record: it must not leak into any other field.
        record.last_job_env = env

        wall_budget = float(requested_limit(policy, "wall_clock"))
        output_budget = int(requested_limit(policy, "output_bytes"))
        started_at = utc_now()

        record.active_jobs += 1
        if plan.stoppable:
            record.job_in_flight = True
        try:
            if plan.during is not None:
                plan.during()
            record.storage_bytes += plan.storage_bytes
            for path, content in plan.writes.items():
                record.files[require_workspace_path(path)] = bytes(content)
            output, produced, truncated = _capture(plan, output_budget)
            timed_out = plan.wall_time_seconds > wall_budget
        finally:
            record.active_jobs -= 1

        return JobOutcome(
            job_id=job_id,
            workspace_id=record.workspace_id,
            status=STATUS_TIMEOUT if timed_out else plan.status,
            exit_status=None if timed_out else plan.exit_status,
            output=output,
            truncated=truncated,
            # A job stopped by the clock never got far enough to be refused.
            command_refused=plan.command_refused and not timed_out,
            started_at=started_at,
            finished_at=utc_now(),
            usage=ResourceUsage(
                # A timed-out job is billed for exactly the budget it consumed
                # before being stopped, never for the duration it wanted.
                wall_time_seconds=wall_budget if timed_out else plan.wall_time_seconds,
                cpu_seconds=plan.cpu_seconds,
                max_memory_bytes=plan.max_memory_bytes,
                storage_bytes=_storage_bytes(record),
                output_bytes=produced,
            ),
        )

    def inspect(self, workspace_id: str) -> WorkspaceDescriptor:
        workspace_id = require_workspace_id(workspace_id)
        self._fail_if_broken("inspect")
        return self._describe(self._require(workspace_id))

    def read(
        self, workspace_id: str, path: str, *, chunk_size: int = DEFAULT_READ_CHUNK_BYTES
    ) -> ByteStream:
        """Stream one stored file back, in chunks no larger than ``chunk_size``.

        The path is normalised and bounded first, before the workspace is even
        looked up: a path that would leave the workspace is refused by the seam's
        own rule, so the fake and a live engine refuse the identical set.

        Nothing is held open, so there is genuinely nothing to release — but the
        stream still carries the full contract, because a caller must not have to
        know which backend it is talking to in order to know whether ``close``
        matters.
        """
        workspace_id = require_workspace_id(workspace_id)
        relative = require_workspace_path(path)
        size = require_chunk_size(chunk_size)
        self._fail_if_broken("read")
        record = self._require(workspace_id)
        content = record.files.get(relative)
        if content is None:
            raise missing_workspace_path(workspace_id, relative)
        return ByteStream(_chunked(content, size))

    def remove(self, workspace_id: str) -> RemovalDisposition:
        workspace_id = require_workspace_id(workspace_id)
        self._fail_if_broken("remove")
        record = self._require(workspace_id)

        # Refuses in-flight work, and walks only edges the table already
        # allows — the fake never invents the ready -> destroyed shortcut.
        for target in guard_removable(record.state, record.active_jobs):
            validate_transition(record.state, target)
            record.state = target

        del self._workspaces[workspace_id]
        return RemovalDisposition(workspace_id=workspace_id, removed=REMOVABLE_RESOURCES)

    def write(
        self,
        workspace_id: str,
        path: str,
        source: ByteSource,
        *,
        expected_sha256: str,
        overwrite: bool = False,
    ) -> None:
        """Copy inbound bytes into the workspace's storage — the inverse of :meth:`read`.

        A caller pushes bytes in so a later ``read`` (or a later job) can find
        them at ``path``. This is the real seam verb the conformance suite
        holds every backend to; ``write_file`` is a same-module shortcut for
        tests whose subject is something *other* than the write itself, and
        this method does not delegate to it — the two must be able to fail in
        different, independently-testable ways.

        Refusal order is deliberate, and the two refusals check different
        things for a reason. An occupied destination is checked *first*,
        before a single byte of ``source`` is touched: it is a fact about the
        workspace's own state that owes nothing to what the caller is trying
        to send, and it is cheap — a dict membership test — where consuming
        ``source`` is not. ``source`` may be a one-shot stream (a generator,
        a socket) that cannot be replayed; draining it just to discover the
        write was going to be refused anyway would be worse than refusing
        first and never touching it. The digest, by contrast, genuinely
        depends on the bytes, so it can only be checked once they have been
        consumed — and it is checked before anything is stored, so a
        mismatch (corruption in transit, or a caller's stale digest) never
        pollutes the workspace with the bad bytes.

        The digest follows this codebase's one convention for a sha256 value:
        a bare 64-character lowercase hex digest, with no algorithm prefix —
        see ``headspace.core.artifacts._normalise_digest`` and
        :attr:`~headspace.core.artifacts.ArtifactRecord.sha256`. That is a
        different value than :func:`~headspace.providers.base.environment_digest`'s
        ``sha256:``-prefixed form: that one is a content-addressed image
        reference, this one is a plain content digest, and the two must not
        be confused by sharing a format.

        Every chunk pulled from ``source`` is buffered into a list and joined
        once the source is exhausted. That is an honest simplification for a
        backend whose storage is a Python dict living entirely in this
        process's memory — it is not a claim to bounded-memory streaming the
        way :func:`headspace.core.artifacts.export_artifact` genuinely is,
        where a multi-gigabyte artifact must never sit whole in memory. The
        source is still consumed in ``source``-sized (or ``.read()``-sized)
        pieces rather than all at once, because that is the part of the
        contract that matters for conformance: a backend must not require the
        whole payload to already exist as one object before it can begin.
        """
        workspace_id = require_workspace_id(workspace_id)
        relative = require_workspace_path(path)
        record = self._require(workspace_id)

        # Cheap and content-independent: refused before touching `source`.
        if not overwrite and relative in record.files:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"destination {relative!r} already exists",
                remediation="pass overwrite=True to replace the existing file",
            )

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        for chunk in _iter_source(source):
            digest.update(chunk)
            chunks.append(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"digest mismatch: expected {expected_sha256}, got {actual}",
                remediation="check the source bytes and the digest you supplied",
            )

        record.files[relative] = b"".join(chunks)

    def stop(self, workspace_id: str) -> None:
        """End an in-flight job on ``workspace_id``.

        A stop is an action, not a no-op: it requires a job to be running.
        If nothing is running, the caller should know — a stop that silently
        succeeds would hide the fact that the caller stopped the wrong thing.

        The fake tracks in-flight jobs through :attr:`_Workspace.job_in_flight`,
        which is set by :meth:`run` when the plan is stoppable. There is
        nothing here for a real process to signal, so ending the job is
        just clearing that flag — the same event a live engine would report
        through a very different mechanism (killing a container). Returns
        nothing: a caller that wants confirmation calls :meth:`inspect`.
        """
        workspace_id = require_workspace_id(workspace_id)
        record = self._require(workspace_id)
        if not record.job_in_flight:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"no running job on workspace {workspace_id}",
                remediation="stop is only valid while a job is in flight",
            )
        record.job_in_flight = False

    # --- internals --------------------------------------------------------

    def _require(self, workspace_id: str) -> _Workspace:
        record = self._workspaces.get(workspace_id)
        if record is None:
            raise unknown_workspace(workspace_id)
        return record

    def _describe(self, record: _Workspace) -> WorkspaceDescriptor:
        return WorkspaceDescriptor(
            workspace_id=record.workspace_id,
            provider=self._name,
            state=record.state,
            environment_digest=record.environment_digest,
            created_at=record.created_at,
            network_enabled=record.network_enabled,
            storage_bytes=_storage_bytes(record),
            active_jobs=record.active_jobs,
            # A backend-private handle nothing above the seam may read. The
            # conformance suite proves it never appears anywhere else.
            ref=OpaqueRef(record.token),
        )


def _storage_bytes(record: _Workspace) -> int:
    """What the workspace occupies: what jobs claimed, plus what they left behind.

    A file in a workspace really does occupy it, so a fake that reported only
    the scripted figure would let a read-back test pass against a workspace that
    claimed to be empty — simulating the semantics, not stubbing them, is the
    whole reason this backend exists.
    """
    return record.storage_bytes + sum(len(content) for content in record.files.values())


def _chunked(content: bytes, chunk_size: int) -> Iterator[bytes]:
    """Yield ``content`` in bounded slices, lazily.

    Lazily even though it is already in memory: the point of the chunking is the
    *contract*, and a fake that handed back one blob would let a backend which
    ignored ``chunk_size`` look conformant by comparison.
    """
    for start in range(0, len(content), chunk_size):
        yield content[start : start + chunk_size]


def _iter_source(source: ByteSource) -> Iterator[bytes]:
    """Yield ``source`` as a sequence of byte chunks, whichever shape it arrived in.

    Three shapes are accepted. A bare ``bytes`` (or ``bytearray``) is handled
    first and specially: it satisfies ``Iterable`` structurally, but iterating
    it yields ``int`` — one per byte — which is both the wrong type and
    ruinously slow for anything but a trivial payload, so it is yielded whole
    rather than left to fall into the general iterable branch below. Anything
    with a callable ``.read()`` is read in bounded pieces, exactly as
    :func:`headspace.core.artifacts._iter_chunks` reads a file object — the
    ``.read()`` branch is checked before the plain-iterable branch for the
    same reason it is there: iterating a binary file object yields *lines*,
    which is not what a caller pushing bytes into a workspace means. Anything
    else — a generator, a list of chunks — is iterated directly.
    """
    if isinstance(source, (bytes, bytearray)):
        yield bytes(source)
        return
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(DEFAULT_READ_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    else:
        yield from source


def _capture(plan: JobPlan, budget: int) -> tuple[str, int, bool]:
    """Apply the output budget at capture time, and report the true volume.

    Cuts on a byte boundary but decodes leniently, so a multi-byte codepoint
    straddling the cut is dropped rather than emitted as mojibake — the same
    rule :mod:`headspace.core.result` uses when it excerpts.
    """
    raw = plan.output.encode("utf-8")
    produced = plan.produced_bytes if plan.produced_bytes is not None else len(raw)
    if len(raw) > budget:
        kept = raw[:budget].decode("utf-8", "ignore")
        return kept, max(produced, len(raw)), True
    return plan.output, max(produced, len(raw)), produced > len(raw)
