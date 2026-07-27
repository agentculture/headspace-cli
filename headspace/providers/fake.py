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
output, an engine that breaks mid-run — would otherwise be untested until
production. This provider is therefore a small programmable execution engine:
:meth:`FakeProvider.script_command` maps an argv to a :class:`JobPlan`
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

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.core.policy import CapabilitySnapshot, EffectivePolicy
from headspace.core.result import STATUS_FAILURE, STATUS_SUCCESS, STATUS_TIMEOUT, ResourceUsage
from headspace.core.states import State, validate_transition
from headspace.providers.base import (
    REMOVABLE_RESOURCES,
    JobOutcome,
    OpaqueRef,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    environment_digest,
    guard_removable,
    requested_limit,
    require_command,
    require_workspace_id,
    unknown_workspace,
    utc_now,
)

#: A duration no realistic budget allows — how :meth:`JobPlan.timing_out`
#: expresses "runs forever" without knowing the budget it will be measured
#: against.
FOREVER_SECONDS = 10**9

#: The verbs :meth:`FakeProvider.break_next` can break.
BREAKABLE_OPERATIONS: tuple[str, ...] = ("capabilities", "create", "run", "inspect", "remove")

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
    during: Callable[[], None] | None = None

    @classmethod
    def succeeding(cls, output: str = "", **overrides: Any) -> JobPlan:
        return cls(status=STATUS_SUCCESS, exit_status=0, output=output, **overrides)

    @classmethod
    def failing(cls, exit_status: int = 1, output: str = "", **overrides: Any) -> JobPlan:
        return cls(status=STATUS_FAILURE, exit_status=exit_status, output=output, **overrides)

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
    """The fake's private record. Never crosses the seam; descriptors do."""

    workspace_id: str
    environment_digest: str
    created_at: str
    network_enabled: bool
    token: str
    state: State = State.READY
    storage_bytes: int = 0
    active_jobs: int = 0


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
    ) -> JobOutcome:
        record = self._require(require_workspace_id(workspace_id))
        argv = require_command(command)
        self._fail_if_broken("run")
        plan = self._script.get(argv, self._default_plan)
        if plan.infrastructure_failure:
            raise ProviderError(plan.infrastructure_failure)

        wall_budget = float(requested_limit(policy, "wall_clock"))
        output_budget = int(requested_limit(policy, "output_bytes"))
        started_at = utc_now()

        record.active_jobs += 1
        try:
            if plan.during is not None:
                plan.during()
            record.storage_bytes += plan.storage_bytes
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
            started_at=started_at,
            finished_at=utc_now(),
            usage=ResourceUsage(
                # A timed-out job is billed for exactly the budget it consumed
                # before being stopped, never for the duration it wanted.
                wall_time_seconds=wall_budget if timed_out else plan.wall_time_seconds,
                cpu_seconds=plan.cpu_seconds,
                max_memory_bytes=plan.max_memory_bytes,
                storage_bytes=record.storage_bytes,
                output_bytes=produced,
            ),
        )

    def inspect(self, workspace_id: str) -> WorkspaceDescriptor:
        workspace_id = require_workspace_id(workspace_id)
        self._fail_if_broken("inspect")
        return self._describe(self._require(workspace_id))

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
            storage_bytes=record.storage_bytes,
            active_jobs=record.active_jobs,
            # A backend-private handle nothing above the seam may read. The
            # conformance suite proves it never appears anywhere else.
            ref=OpaqueRef(record.token),
        )


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
