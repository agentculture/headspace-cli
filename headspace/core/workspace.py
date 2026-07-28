"""Workspace orchestration: the layer that turns the pieces below it into the product.

WHY this module exists
----------------------
Everything underneath is a correct piece with no opinion about how it is used.
The store knows how to persist a record but not when one should exist; the
lifecycle table knows which moves are legal but never makes one; the inventory
knows what was declared but not what that obliges anyone to do; the provider
knows how to create and destroy but is forbidden, by its own contract, from
deciding whether destroying is a good idea. Those decisions have to live
somewhere, and if they were spread across five CLI verbs they would be made
five slightly different ways. They are made here, once.

Three of them are load-bearing enough to state outright.

**1. Ordering: journal, then engine, then state.**
headspace is daemonless, so between two invocations the only thing that
remembers anything is the store. A CLI killed mid-verb therefore has exactly
one chance to leave a recoverable trace, and it has to leave it *before* the
irreversible act. So every engine mutation is preceded by an intent record
written durably to the journal, and only settled once the state write lands.
That yields a three-way partition of what a crash can leave behind:

* no intent — the engine was never asked; nothing exists, nothing to do;
* an intent with no settle — the engine may hold something the store does not
  know about; this is the orphan, and the intent carries what is needed to
  adopt it;
* intent and settle — the invocation completed.

Reverse the order and the trace is written after the thing it was supposed to
describe, which is to say: exactly never, in the only case it was for.

**2. Reconciliation at entry, and it always reports.**
Every verb reconciles before it does its own work, because an orphan is
discovered by using the system, not by running a repair tool nobody runs. What
it finds it *reports*, in the result package's attention items — a workspace
silently adopted is a workspace the caller does not know it is paying for, and
a silently reaped one is work that vanished without a receipt. Reconciliation
also never blocks (it takes locks non-blocking and defers when busy) and never
fails a verb: an engine it cannot reach yields an "unverified" report, not an
exception, because failing an unrelated command over a stale orphan trades a
small mess for a large one.

Note what makes reconciliation *possible* at all through a five-verb seam with
no ``list``: because the intent is journalled before the engine is called,
there is no engine object headspace created that has no journal line naming
its workspace id — and a workspace id is all ``inspect`` needs. An engine
object with no journal entry was not created by headspace.

**3. Destroy refuses before it removes.**
Destroying unexported work is the one mistake this product cannot walk back.
So destroy diffs the declared artifacts against the export log *before* it
journals anything, and refuses without ``force`` — which makes "removes
nothing" structural rather than careful: the refusal happens upstream of the
first byte written and the first engine call made. With ``force`` it proceeds
and names every artifact it discarded, with the digest field present and empty,
because an artifact that was never exported has no digest and inventing one
would be the lie the guard exists to prevent.

Two deviations from the plan are implemented here
-------------------------------------------------
**d4 — ``running`` is a state workspaces genuinely occupy.** The lifecycle
table originally had no ``running -> ready`` edge, so a workspace that entered
``running`` could never serve a second job; the seam worked around it by never
entering ``running`` at all, leaving a state the spec names unreachable. This
module drives ``ready -> running -> ready`` around every job against a table
that now has the return edge. ``running -> destroyed`` is still absent, and
that omission is what produces the destroy-during-run refusal — in the table's
own words rather than a synthesized message. The edge pays twice: it is also
the edge reconciliation walks to release a workspace stranded in ``running`` by
a crash mid-job.

A workspace's state is not a job's status, and conflating them is what burnt
the original table: a failed job leaves a perfectly good workspace, which
returns to ``ready`` and hosts the next one. ``completed``/``failed`` describe
a workspace whose life ended that way, not a command that exited non-zero.

**d3 — the artifact mapping lives here.** The storage-side
:class:`~headspace.core.artifacts.ArtifactRecord` and the wire-side
:class:`~headspace.core.result.Artifact` diverge for a good reason: one carries
retention status for a guard that runs locally, the other carries a retrieval
reference for a caller that will never see the workspace. :func:`result_artifact`
is the explicit translation, :data:`ARTIFACT_FIELD_MAP` is its tested
specification, and the function refuses any record that was never exported —
a wire artifact promises retrievability and integrity, and a declared one can
keep neither promise.

What this module may import
---------------------------
``headspace/core/__init__.py`` bars this package from importing *a backend* or
the docker SDK, so that the lifecycle and result contracts stay
backend-neutral. This module imports :mod:`headspace.providers.base` — the
seam itself, which is backend-neutral by construction and is what makes
"depends only on the Protocol" checkable — and nothing else from that package.
It is the same sanctioned narrow exception :mod:`headspace.core.states` takes
on ``cli._errors``, and a test asserts the narrowness at the source level.

The persisted state record
--------------------------
One JSON object per workspace, inside the store's schema envelope::

    workspace_id, state           headspace's authoritative lifecycle state
    provider, profile,            which backend holds it, under what runtime
    environment, created_at
    policy, capabilities          the caller's declaration and the host fact it
                                  was resolved against — the effective policy is
                                  re-derived from these on every verb, so a
                                  workspace is always run under the contract it
                                  was created under
    descriptor                    the engine's own view, as last observed
    artifacts,                    the inventory, and where each export landed
    artifact_references
    jobs, jobs_dropped            budget-bounded outcomes, newest last

``state`` and ``descriptor["state"]`` are deliberately *not* the same field.
The descriptor is what the engine reports (a container that is up is "ready" to
it, even mid-command); ``state`` is headspace's lifecycle position, which reads
``running`` for the duration of a job. Persisting the descriptor never
overwrites the lifecycle state, or a concurrent destroy would stop seeing the
job it must refuse to interrupt.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_COMPUTATION_FAILED,
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_POLICY_DENIED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core import profiles
from headspace.core.artifacts import (
    DEFAULT_CONTENT_TYPE,
    RETENTION_EXPORTED,
    ArtifactInventory,
    ArtifactRecord,
    ByteSource,
    export_artifact,
)
from headspace.core.policy import (
    CapabilitySnapshot,
    EffectivePolicy,
    FilesystemScope,
    LimitStatus,
    NetworkPosture,
    Policy,
    ResourceBudget,
    resolve,
)
from headspace.core.result import (
    STATUS_CANCELLED,
    STATUS_FAILURE,
    STATUS_INFRASTRUCTURE_FAILURE,
    STATUS_PARTIAL_SUCCESS,
    STATUS_POLICY_DENIED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    STATUSES,
    Artifact,
    Evidence,
    Provenance,
    ResourceUsage,
    ResultPackage,
    inspect_path,
)
from headspace.core.states import State, validate_transition
from headspace.core.store import JournalEntry, Store
from headspace.providers.base import (
    REMOVABLE_RESOURCES,
    JobOutcome,
    Provider,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
    guard_removable,
    removal_path,
    utc_now,
)

# --- vocabularies -----------------------------------------------------------

#: The four things a verb can intend to do. Only the first, second and fourth
#: touch an engine; ``export`` is journalled too so an interrupted export is
#: still visible to a reader of the journal.
INTENT_CREATE = "create"
INTENT_RUN = "run"
INTENT_EXPORT = "export"
INTENT_REMOVE = "remove"
INTENTS: tuple[str, ...] = (INTENT_CREATE, INTENT_RUN, INTENT_EXPORT, INTENT_REMOVE)

#: An intent's phase. ``intended`` with no later entry carrying the same
#: ``intent_id`` is the crash signature, and the only one — a *handled* failure
#: writes ``abandoned`` on its way out, precisely so that an open intent means
#: "this invocation died", never "this invocation failed".
PHASE_INTENDED = "intended"
PHASE_SETTLED = "settled"
PHASE_ABANDONED = "abandoned"
PHASE_RECONCILED = "reconciled"
PHASES: tuple[str, ...] = (PHASE_INTENDED, PHASE_SETTLED, PHASE_ABANDONED, PHASE_RECONCILED)

#: What reconciliation did about an orphan. A closed set: every one of these is
#: reported to the caller, so they are part of the surface, not internal notes.
#:
#: ``adopted``      the engine holds it; the store record was completed from
#:                  engine truth plus the journalled intent.
#: ``reaped``       nothing of value survived; the leftovers were removed.
#: ``quarantined``  the engine object is gone but the record names work nobody
#:                  can recover; the record is kept so the loss is legible.
#: ``resolved``     the workspace was fine; a stale in-flight state or a lost
#:                  journal settle was cleared.
#: ``reported``     nothing was changed — the caller is told and left to decide.
DISPOSITION_ADOPTED = "adopted"
DISPOSITION_REAPED = "reaped"
DISPOSITION_QUARANTINED = "quarantined"
DISPOSITION_RESOLVED = "resolved"
DISPOSITION_REPORTED = "reported"
DISPOSITIONS: tuple[str, ...] = (
    DISPOSITION_ADOPTED,
    DISPOSITION_REAPED,
    DISPOSITION_QUARANTINED,
    DISPOSITION_RESOLVED,
    DISPOSITION_REPORTED,
)

#: How many job outcomes a workspace keeps. Each is already bounded by the
#: policy's output budget, but a long-lived session is otherwise unbounded in
#: count. Dropped outcomes are counted and surfaced as a warning: compression
#: may hide volume, never its own existence (the same clause the result package
#: keeps with its truncation markers).
MAX_RETAINED_JOBS = 50

#: Deviation d3, as data: storage-side field -> wire-side field, ``None`` for a
#: field that deliberately does not cross. ``reference`` appears on the wire
#: only and has no source here, which is why :func:`result_artifact` takes it as
#: an argument — the orchestrator performed the export, so the orchestrator is
#: what knows where the artifact landed.
ARTIFACT_FIELD_MAP: dict[str, str | None] = {
    "name": "name",
    "content_type": "media_type",
    "purpose": "purpose",
    "retention": None,
    "size_bytes": "size_bytes",
    "sha256": "digest",
}

#: Result status -> process exit code. The status vocabulary and the failure
#: taxonomy were built to mirror each other one-to-one; this is that mirror,
#: written down once so five CLI verbs cannot each get it subtly wrong.
_STATUS_EXIT_CODES: dict[str, int] = {
    STATUS_SUCCESS: EXIT_SUCCESS,
    STATUS_PARTIAL_SUCCESS: EXIT_SUCCESS,
    STATUS_FAILURE: EXIT_COMPUTATION_FAILED,
    STATUS_TIMEOUT: EXIT_TIMEOUT,
    STATUS_CANCELLED: EXIT_CANCELLED,
    STATUS_POLICY_DENIED: EXIT_POLICY_DENIED,
    STATUS_INFRASTRUCTURE_FAILURE: EXIT_INFRASTRUCTURE_FAILURE,
}

_STATE_KEY = "state"


# --- small public helpers ---------------------------------------------------


def new_workspace_id(prefix: str = "hs") -> str:
    """Mint a workspace id that is also a safe directory name in the store."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def new_job_id(prefix: str = "job") -> str:
    """Mint a job id. Distinct from the workspace id: a session outlives a job."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def exit_code_for_status(status: str) -> int:
    """The documented exit code for a result status.

    Refuses an unknown status rather than defaulting: a status with no code
    would surface as a mysterious process exit, which is the one thing the
    failure taxonomy exists to prevent.
    """
    try:
        return _STATUS_EXIT_CODES[status]
    except KeyError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown result status {status!r}",
            remediation="statuses are: " + ", ".join(STATUSES),
        ) from None


def result_artifact(record: ArtifactRecord, *, reference: str = "") -> Artifact:
    """Translate an inventory record into the wire form (deviation d3).

    Refuses anything that is not exported. The wire form promises a caller that
    the artifact can be *retrieved* and its integrity *checked*; a declared or
    discarded record can back neither promise, and filling ``digest`` with ""
    and ``size_bytes`` with 0 would render that as a zero-byte artifact rather
    than as the absence it is. Unexported work is reported where its status can
    be stated honestly — attention items — not smuggled into this section.
    """
    if record.retention != RETENTION_EXPORTED:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"artifact '{record.name}' is {record.retention}, not exported, "
                "so it cannot be rendered as a result-package artifact"
            ),
            remediation=(
                "export it first; a result-package artifact carries a digest and a "
                "retrieval reference, and an unexported artifact has neither"
            ),
        )
    return Artifact(
        name=record.name,
        purpose=record.purpose,
        media_type=record.content_type,
        digest=record.sha256 or "",
        size_bytes=record.size_bytes or 0,
        reference=reference,
    )


@dataclass(frozen=True)
class ArtifactDeclaration:
    """An output a job promises to produce, declared before it exists.

    Declaration is separate from export on purpose: the gap between the two is
    exactly the set the destroy guard protects. A workspace that could only
    declare an artifact by exporting it would have nothing to guard.
    """

    name: str
    purpose: str
    content_type: str = DEFAULT_CONTENT_TYPE


@dataclass(frozen=True)
class Reconciliation:
    """What reconciliation found, and what it did about it.

    Returned to the caller and rendered into attention items. Carries the
    intent that was left open so a reader can tell an interrupted create from
    an interrupted teardown — they mean very different things about what is
    still running and what is still owed.
    """

    workspace_id: str
    intent: str
    disposition: str
    detail: str

    def summary(self) -> str:
        """One attention-item line naming the workspace, the verdict and why."""
        return f"reconciled workspace {self.workspace_id}: {self.disposition} — {self.detail}"

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


# --- the orchestrator -------------------------------------------------------


class Orchestrator:
    """The five lifecycle flows, over any :class:`~headspace.providers.base.Provider`.

    One instance per CLI invocation. It reconciles once, on the first verb it
    serves, and hands the dispositions to that verb's result package — so the
    caller learns about recovered orphans on the command they actually ran.
    """

    def __init__(self, provider: Provider, store: Store | None = None) -> None:
        self._provider = provider
        self._store = store if store is not None else Store()
        self._pending: list[Reconciliation] | None = None

    @property
    def provider(self) -> Provider:
        """The backend this orchestrator drives. Read-only; never swapped."""
        return self._provider

    @property
    def store(self) -> Store:
        """The state store this orchestrator persists through."""
        return self._store

    # --- verbs ------------------------------------------------------------

    def create(
        self,
        *,
        profile: str = profiles.DEFAULT_PROFILE,
        policy: Policy | None = None,
        workspace_id: str | None = None,
    ) -> ResultPackage:
        """Provision a workspace: probe, resolve, journal, create, record.

        The policy is resolved against the host's own capability snapshot
        *before* the journal is touched, so a policy the host cannot enforce
        costs nothing and leaves nothing — failing closed is only meaningful if
        it fails early.

        An engine that fails *during* creation leaves the record behind in
        ``failed`` rather than deleting it. The provider contract does not
        promise that a failed ``create`` cleaned up after itself, so a deleted
        record would abandon whatever half-built thing the engine does hold —
        the exact leak the journal exists to prevent. A ``failed`` workspace is
        destroyable (``failed -> destroyed``), and destroying it is what asks
        the engine to remove or account for the remains.
        """
        attention = self._take_attention()
        declared = policy if policy is not None else Policy()
        environment = profiles.resolve(profile)
        workspace_id = workspace_id or new_workspace_id()

        snapshot = self._provider.capabilities()
        effective = resolve(declared, snapshot)

        with self._store.lock(workspace_id):
            if self._store.exists(workspace_id):
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"workspace {workspace_id} already exists",
                    remediation="choose a fresh id, or destroy the existing workspace first",
                )
            detail = {
                "profile": profile,
                "environment": environment,
                "policy": _policy_to_dict(declared),
                "capabilities": _snapshot_to_dict(snapshot),
            }
            intent_id = self._intend(workspace_id, INTENT_CREATE, detail)
            record: dict[str, Any] = {
                "workspace_id": workspace_id,
                _STATE_KEY: State.REQUESTED.value,
                "provider": self._provider.name,
                "profile": profile,
                "environment": environment,
                "created_at": utc_now(),
                "policy": detail["policy"],
                "capabilities": detail["capabilities"],
                "descriptor": None,
                "artifacts": [],
                "artifact_references": {},
                "jobs": [],
                "jobs_dropped": 0,
            }
            self._store.write_state(workspace_id, record)
            record = self._advance(workspace_id, record, State.PROVISIONING)

            try:
                descriptor = self._provider.create(workspace_id, environment, effective)
            except CliError as err:
                self._advance(workspace_id, record, State.FAILED)
                self._close(workspace_id, INTENT_CREATE, intent_id, PHASE_ABANDONED, err)
                raise

            record["descriptor"] = descriptor.to_dict()
            record = self._advance(workspace_id, record, State.READY)
            self._close(workspace_id, INTENT_CREATE, intent_id, PHASE_SETTLED, None)

        return ResultPackage(
            outcome_summary=(
                f"workspace {workspace_id} is ready on {self._provider.name}, "
                f"running the {profile} profile"
            ),
            status=STATUS_SUCCESS,
            key_findings=_descriptor_findings(State.READY, descriptor),
            warnings=_measured_warnings(effective),
            resource_usage=ResourceUsage(storage_bytes=descriptor.storage_bytes),
            provenance=self._provenance(record, effective, descriptor),
            attention=attention,
        )

    def declare(
        self,
        workspace_id: str,
        name: str,
        *,
        purpose: str,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> ArtifactRecord:
        """Register an output the workspace promises, before anything writes it.

        Returns the inventory record rather than a result package: declaring is
        a step inside a verb (usually :meth:`run`), not a verb of its own.
        """
        with self._store.lock(workspace_id):
            record = self._read(workspace_id)
            inventory = _inventory(record)
            declared = inventory.declare(name, purpose=purpose, content_type=content_type)
            record["artifacts"] = inventory.to_list()
            self._store.write_state(workspace_id, record)
        return declared

    def run(
        self,
        workspace_id: str,
        command: Sequence[str],
        *,
        declares: Iterable[ArtifactDeclaration] = (),
        job_id: str | None = None,
    ) -> ResultPackage:
        """Execute one command in an existing workspace: ``ready -> running -> ready``.

        The lock is held for the whole job, which is what stops two invocations
        double-starting one workspace (c24). A concurrent destroy does not queue
        behind it — it reads the persisted ``running`` state and refuses.
        """
        attention = self._take_attention()
        job_id = job_id or new_job_id()

        with self._store.lock(workspace_id):
            record = self._read(workspace_id)
            effective = self._effective_policy(record)
            # Everything that can fail locally fails before the journal is
            # touched: an open intent must mean "this invocation died", and a
            # rejected transition or a duplicate declaration is not a death.
            validate_transition(_state_of(record), State.RUNNING)
            inventory = _inventory(record)
            for declaration in declares:
                inventory.declare(
                    declaration.name,
                    purpose=declaration.purpose,
                    content_type=declaration.content_type,
                )
            record["artifacts"] = inventory.to_list()

            intent_id = self._intend(
                workspace_id, INTENT_RUN, {"command": list(command), "job_id": job_id}
            )
            record = self._advance(workspace_id, record, State.RUNNING)

            try:
                outcome = self._provider.run(workspace_id, command, effective, job_id=job_id)
            except CliError as err:
                # Infrastructure failure, raised — never returned as a job
                # status (NFR-07). The workspace is released first so a broken
                # engine does not also strand the session.
                record = self._advance(workspace_id, record, State.READY)
                self._close(workspace_id, INTENT_RUN, intent_id, PHASE_ABANDONED, err)
                raise

            record = _append_job(record, outcome)
            if outcome.usage.storage_bytes and record.get("descriptor"):
                record["descriptor"]["storage_bytes"] = outcome.usage.storage_bytes
            record = self._advance(workspace_id, record, State.READY)
            self._close(
                workspace_id,
                INTENT_RUN,
                intent_id,
                PHASE_SETTLED,
                None,
                {"job_id": job_id, "status": outcome.status},
            )

        warnings = _measured_warnings(effective) + _job_warnings(record, outcome)
        return ResultPackage(
            outcome_summary=(
                f"job {job_id} ran {' '.join(command)} in workspace {workspace_id} "
                f"and reported {outcome.status}"
            ),
            status=outcome.status,
            key_findings=_job_findings(outcome),
            evidence=[
                Evidence(
                    label="captured output",
                    kind="excerpt",
                    excerpt=outcome.output,
                    source=inspect_path(job_id),
                )
            ],
            artifacts=_artifact_section(record),
            warnings=warnings,
            resource_usage=outcome.usage,
            provenance=self._provenance(record, effective, None, outcome=outcome, command=command),
            attention=attention + _pending_artifact_attention(record),
        )

    def inspect(self, workspace_id: str) -> ResultPackage:
        """Report headspace's lifecycle view and the engine's, side by side."""
        attention = self._take_attention()
        record = self._read(workspace_id)
        effective = self._effective_policy(record)

        warnings = _measured_warnings(effective)
        descriptor = self._describe(workspace_id)
        if descriptor is None:
            warnings.append(
                f"the engine no longer holds workspace {workspace_id}; the facts below are "
                "the last ones headspace observed, not current ones"
            )
            descriptor = _stored_descriptor(record)

        jobs = list(record.get("jobs", []))
        findings = _descriptor_findings(_state_of(record), descriptor)
        findings.append(f"{len(jobs)} job(s) recorded in this session")
        evidence: list[Evidence] = []
        if jobs:
            last = jobs[-1]
            evidence.append(
                Evidence(
                    label=f"captured output of job {last.get('job_id', '')}",
                    kind="excerpt",
                    excerpt=str(last.get("output", "")),
                    source=inspect_path(str(last.get("job_id", workspace_id))),
                )
            )

        return ResultPackage(
            outcome_summary=(
                f"workspace {workspace_id} is {_state_of(record).value} on "
                f"{record.get('provider', 'unknown')}"
            ),
            status=STATUS_SUCCESS,
            key_findings=findings,
            evidence=evidence,
            artifacts=_artifact_section(record),
            warnings=warnings + _dropped_job_warnings(record),
            resource_usage=_session_usage(record, descriptor),
            provenance=self._provenance(record, effective, descriptor),
            attention=attention + _pending_artifact_attention(record),
        )

    def export(
        self,
        workspace_id: str,
        name: str,
        source: ByteSource,
        destination: str | os.PathLike[str],
        *,
        expected_sha256: str | None = None,
    ) -> ResultPackage:
        """Publish a declared artifact atomically and record what left the workspace.

        ``source`` is supplied by the caller rather than pulled through the
        provider: the seam's five verbs contain no way to read bytes out of a
        workspace, and inventing a sixth here would put a backend concern in
        the orchestration layer. Whoever can produce the bytes hands them over;
        this method owns the durability boundary, the digest, and the ledger.
        """
        attention = self._take_attention()
        with self._store.lock(workspace_id):
            record = self._read(workspace_id)
            inventory = _inventory(record)
            declaration = inventory.get(name)
            if declaration is None:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"artifact '{name}' was never declared by workspace {workspace_id}",
                    remediation=(
                        "declare an artifact when the job that writes it runs; only declared "
                        "outputs can be exported, and only declared outputs are guarded"
                    ),
                )

            intent_id = self._intend(
                workspace_id,
                INTENT_EXPORT,
                {"name": name, "destination": os.fspath(destination)},
            )
            try:
                exported = export_artifact(
                    source,
                    destination,
                    purpose=declaration.purpose,
                    name=name,
                    content_type=declaration.content_type,
                    expected_sha256=expected_sha256,
                )
            except CliError as err:
                self._close(workspace_id, INTENT_EXPORT, intent_id, PHASE_ABANDONED, err)
                raise

            inventory.mark_exported(
                name,
                size_bytes=int(exported.size_bytes or 0),
                sha256=str(exported.sha256 or ""),
            )
            record["artifacts"] = inventory.to_list()
            references = dict(record.get("artifact_references", {}))
            references[name] = os.fspath(destination)
            record["artifact_references"] = references
            self._store.write_state(workspace_id, record)
            self._close(
                workspace_id,
                INTENT_EXPORT,
                intent_id,
                PHASE_SETTLED,
                None,
                {"name": name, "sha256": exported.sha256},
            )

        return ResultPackage(
            outcome_summary=(
                f"artifact '{name}' left workspace {workspace_id} and is durable at "
                f"{os.fspath(destination)}"
            ),
            status=STATUS_SUCCESS,
            key_findings=[
                f"{exported.size_bytes} bytes verified as sha256:{exported.sha256}",
                "the artifact now outlives its workspace",
            ],
            artifacts=_artifact_section(record),
            resource_usage=ResourceUsage(storage_bytes=int(exported.size_bytes or 0)),
            provenance=self._provenance(record, self._effective_policy(record), None),
            attention=attention + _pending_artifact_attention(record),
        )

    def destroy(self, workspace_id: str, *, force: bool = False) -> ResultPackage:
        """Tear a workspace down, or refuse — and when it refuses, remove nothing.

        Both guards run before the journal, the lock and the engine, so a
        refusal cannot have a partial effect. The in-flight guard is asked of
        the lifecycle table (``running -> destroyed``, the edge it deliberately
        omits) so the caller gets the table's own words, and it is checked from
        persisted state so it holds across processes and needs no engine.
        """
        attention = self._take_attention()
        record = self._read(workspace_id)
        state = _state_of(record)

        inventory = _inventory(record)
        unexported = inventory.unexported()
        if unexported and not force:
            names = ", ".join(sorted(item.name for item in unexported))
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(
                    f"refusing to destroy workspace {workspace_id}: "
                    f"{len(unexported)} declared artifact(s) were never exported ({names})"
                ),
                remediation=(
                    "export them first — nothing has been removed — or pass force to discard "
                    "them deliberately; discarded artifacts cannot be recovered"
                ),
            )

        if state is State.RUNNING:
            # Refuse in the table's own words rather than a message of ours.
            validate_transition(State.RUNNING, State.DESTROYED)

        descriptor = self._describe(workspace_id)
        path = guard_removable(state, descriptor.active_jobs if descriptor else 0)

        with self._store.lock(workspace_id, blocking=False):
            intent_id = self._intend(
                workspace_id,
                INTENT_REMOVE,
                {"force": force, "path": [target.value for target in path]},
            )
            try:
                if descriptor is None:
                    # The engine lost it before we did. Nothing was removed by
                    # headspace, and nothing can be verified — which is exactly
                    # what the report's third bucket is for.
                    disposition = RemovalDisposition(
                        workspace_id=workspace_id, unverified=REMOVABLE_RESOURCES
                    )
                else:
                    disposition = self._provider.remove(workspace_id)
            except CliError as err:
                self._close(workspace_id, INTENT_REMOVE, intent_id, PHASE_ABANDONED, err)
                raise

            discarded = [inventory.mark_discarded(item.name) for item in unexported]
            record["artifacts"] = inventory.to_list()
            for target in path:
                record = self._advance(workspace_id, record, target)
            self._close(
                workspace_id,
                INTENT_REMOVE,
                intent_id,
                PHASE_SETTLED,
                None,
                {"path": [target.value for target in path], "discarded": len(discarded)},
            )
            self._store.delete_workspace(workspace_id)

        findings = [
            "removed: " + (", ".join(disposition.removed) or "(none)"),
            "retained: " + (", ".join(disposition.retained) or "(none)"),
            "unverified: " + (", ".join(disposition.unverified) or "(none)"),
            "lifecycle path: " + " -> ".join(target.value for target in path),
        ]
        warnings: list[str] = []
        if discarded:
            warnings.append(
                f"force discarded {len(discarded)} declared artifact(s) that were never "
                "exported; this cannot be undone"
            )
        return ResultPackage(
            outcome_summary=f"workspace {workspace_id} was destroyed on {self._provider.name}",
            status=STATUS_SUCCESS,
            key_findings=findings,
            warnings=warnings,
            resource_usage=ResourceUsage(),
            provenance=self._provenance(record, None, descriptor),
            attention=attention + [_discarded_attention(item) for item in discarded],
        )

    # --- reconciliation ---------------------------------------------------

    def reconcile(self) -> list[Reconciliation]:
        """Adopt or reap every orphan the journal and the engine's labels reveal.

        Cheap on a healthy store: a workspace whose journal has no open intent
        is skipped without an engine call, so the common path costs one small
        file read per workspace.
        """
        found: list[Reconciliation] = []
        for workspace_id in self._store.list_workspaces():
            disposition = self._reconcile_one(workspace_id)
            if disposition is not None:
                found.append(disposition)
        self._pending = found
        return found

    def _reconcile_one(self, workspace_id: str) -> Reconciliation | None:
        try:
            entries = self._store.read_journal(workspace_id)
        except CliError as err:
            return Reconciliation(
                workspace_id, "", DISPOSITION_REPORTED, f"its journal is unreadable: {err.message}"
            )

        open_intents = _open_intents(entries)
        if not open_intents:
            return None
        latest = open_intents[-1]
        intent = str(latest.get("intent", ""))

        owner = str(latest.get("provider", ""))
        if owner != self._provider.name:
            return Reconciliation(
                workspace_id,
                intent,
                DISPOSITION_REPORTED,
                f"it belongs to backend '{owner}', not '{self._provider.name}', "
                "so this invocation left it alone",
            )

        try:
            record: dict[str, Any] | None = (
                dict(self._store.read_state(workspace_id).state)
                if self._store.exists(workspace_id)
                else None
            )
        except CliError as err:
            return Reconciliation(
                workspace_id,
                intent,
                DISPOSITION_REPORTED,
                f"its state is unreadable: {err.message}",
            )

        if intent == INTENT_EXPORT:
            return self._reconcile_export(workspace_id, latest, open_intents)

        try:
            descriptor = self._provider.inspect(workspace_id)
        except ProviderError as err:
            return Reconciliation(
                workspace_id,
                intent,
                DISPOSITION_REPORTED,
                f"the engine could not be reached, so it could not be verified: {err.message}",
            )
        except CliError:
            descriptor = None  # the engine does not hold it

        stack = contextlib.ExitStack()
        try:
            stack.enter_context(self._store.lock(workspace_id, blocking=False))
        except CliError:
            return Reconciliation(
                workspace_id,
                intent,
                DISPOSITION_REPORTED,
                "it is busy in another headspace invocation, so reconciliation was deferred "
                "rather than made to wait",
            )
        with stack:
            return self._settle_orphan(workspace_id, intent, open_intents, record, descriptor)

    def _reconcile_export(
        self,
        workspace_id: str,
        latest: Mapping[str, Any],
        open_intents: Sequence[Mapping[str, Any]],
    ) -> Reconciliation:
        """An interrupted export touched no engine object; report it and move on.

        The published file is atomic either way (the export either committed or
        left nothing), and the inventory still says *declared* — which keeps
        the destroy guard protecting the artifact. So there is nothing to undo:
        the caller is told once, loudly, and the intent is closed rather than
        left to report itself forever.
        """
        detail = dict(latest.get("detail", {}))
        with contextlib.suppress(CliError):
            for entry in open_intents:
                self._close(
                    workspace_id,
                    INTENT_EXPORT,
                    str(entry.get("intent_id", "")),
                    PHASE_RECONCILED,
                    None,
                )
        return Reconciliation(
            workspace_id,
            INTENT_EXPORT,
            DISPOSITION_REPORTED,
            f"an export of '{detail.get('name', '')}' to {detail.get('destination', '')} was "
            "interrupted; the destination holds either a complete artifact or nothing, and the "
            "inventory still lists it as unexported — verify it and re-export",
        )

    def _settle_orphan(
        self,
        workspace_id: str,
        intent: str,
        open_intents: Sequence[Mapping[str, Any]],
        record: dict[str, Any] | None,
        descriptor: WorkspaceDescriptor | None,
    ) -> Reconciliation:
        """Decide an orphan's fate from engine truth and the record, and act."""
        if descriptor is not None:
            disposition, detail, record = self._adopt(
                workspace_id, record, descriptor, open_intents
            )
            if record is not None:
                self._store.write_state(workspace_id, record)
        else:
            disposition, detail, record = self._reap(workspace_id, record)
            if record is not None:
                self._store.write_state(workspace_id, record)

        if disposition == DISPOSITION_REAPED:
            # Nothing survives to journal against; the report is the receipt.
            self._store.delete_workspace(workspace_id)
            return Reconciliation(workspace_id, intent, disposition, detail)

        if disposition != DISPOSITION_REPORTED:
            # State first, journal second: settling before the fix landed would
            # erase the recovery signal while the problem still existed.
            for entry in open_intents:
                self._close(
                    workspace_id,
                    str(entry.get("intent", "")),
                    str(entry.get("intent_id", "")),
                    PHASE_RECONCILED,
                    None,
                )
        return Reconciliation(workspace_id, intent, disposition, detail)

    def _adopt(
        self,
        workspace_id: str,
        record: dict[str, Any] | None,
        descriptor: WorkspaceDescriptor,
        open_intents: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str, dict[str, Any] | None]:
        """The engine holds it. Complete the record rather than destroy the work."""
        if record is None:
            rebuilt = _record_from_intent(workspace_id, open_intents[-1], descriptor)
            return (
                DISPOSITION_ADOPTED,
                "the engine holds a workspace the store had no record of; the record was "
                "rebuilt from the journalled intent and engine truth",
                rebuilt,
            )

        state = _state_of(record)
        record["descriptor"] = descriptor.to_dict()
        if state in (State.REQUESTED, State.PROVISIONING):
            for target in (State.PROVISIONING, State.READY):
                if _state_of(record) is not target:
                    record = _move(record, target)
            return (
                DISPOSITION_ADOPTED,
                f"it was interrupted in '{state.value}' after the engine had already provisioned "
                "it; the workspace was adopted and is ready",
                record,
            )
        if state is State.RUNNING:
            if descriptor.active_jobs > 0:
                return (
                    DISPOSITION_REPORTED,
                    f"a job is genuinely in flight ({descriptor.active_jobs} active); "
                    "nothing was changed",
                    None,
                )
            record = _move(record, State.READY)
            return (
                DISPOSITION_RESOLVED,
                "it was left in 'running' by an interrupted job the engine is no longer "
                "executing; the session was released back to ready",
                record,
            )
        return (
            DISPOSITION_RESOLVED,
            "the workspace and the engine agree; only the journal's settling record was lost",
            record,
        )

    def _reap(
        self, workspace_id: str, record: dict[str, Any] | None
    ) -> tuple[str, str, dict[str, Any] | None]:
        """The engine does not hold it. Keep the record only if work was at stake."""
        if record is None:
            return (
                DISPOSITION_REAPED,
                "the engine was never asked, or never answered; a journalled intent was the only "
                "trace and nothing was ever provisioned",
                None,
            )

        lost = _inventory(record).unexported()
        state = _state_of(record)
        if lost:
            names = ", ".join(sorted(item.name for item in lost))
            if state is not State.CANCELLED:
                record = _move(record, State.CANCELLED)
            return (
                DISPOSITION_QUARANTINED,
                f"its engine object is gone but it declared artifacts nobody exported ({names}); "
                "the record was kept so the loss is legible, and can be destroyed deliberately",
                record,
            )

        for target in _reap_path(state):
            validate_transition(state, target)
            state = target
        return (
            DISPOSITION_REAPED,
            "its engine object is gone and nothing was declared but unexported, so the leftover "
            "record was removed",
            None,
        )

    # --- internals --------------------------------------------------------

    def _take_attention(self) -> list[str]:
        """Reconcile on first use, and hand each disposition to exactly one verb."""
        if self._pending is None:
            self.reconcile()
        pending, self._pending = self._pending or [], []
        return [item.summary() for item in pending]

    def _read(self, workspace_id: str) -> dict[str, Any]:
        record = dict(self._store.read_state(workspace_id).state)
        if _STATE_KEY not in record:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"the stored record for workspace {workspace_id} has no lifecycle state",
                remediation=(
                    "the record was not written by headspace; move it aside and let headspace "
                    "recreate the workspace"
                ),
            )
        return record

    def _describe(self, workspace_id: str) -> WorkspaceDescriptor | None:
        """Engine truth, or ``None`` when the engine simply does not hold it.

        :class:`ProviderError` is deliberately *not* swallowed: an engine that
        broke is not an engine that answered "no".
        """
        try:
            return self._provider.inspect(workspace_id)
        except ProviderError:
            raise
        except CliError:
            return None

    def _effective_policy(self, record: Mapping[str, Any]) -> EffectivePolicy:
        """Re-derive the effective policy from what the workspace was created under.

        Resolved against the *stored* capability snapshot, not a fresh probe: a
        workspace is run under the contract it was created under, so a host that
        quietly gained or lost a capability cannot change an existing
        workspace's budget behind the caller's back.
        """
        return resolve(
            _policy_from_dict(record.get("policy", {})),
            _snapshot_from_dict(record.get("capabilities", {})),
        )

    def _advance(self, workspace_id: str, record: dict[str, Any], target: State) -> dict[str, Any]:
        """Validate the transition, then write it. In that order, always."""
        record = _move(record, target)
        self._store.write_state(workspace_id, record)
        return record

    def _intend(self, workspace_id: str, intent: str, detail: Mapping[str, Any]) -> str:
        """Journal an intent and return its id. Called before the engine, never after."""
        intent_id = uuid.uuid4().hex[:12]
        self._store.append_journal(
            workspace_id,
            {
                "intent": intent,
                "intent_id": intent_id,
                "phase": PHASE_INTENDED,
                "provider": self._provider.name,
                "detail": dict(detail),
            },
        )
        return intent_id

    def _close(
        self,
        workspace_id: str,
        intent: str,
        intent_id: str,
        phase: str,
        error: CliError | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Pair an intent with its outcome, so an open intent only ever means a crash."""
        entry: dict[str, Any] = {
            "intent": intent,
            "intent_id": intent_id,
            "phase": phase,
            "provider": self._provider.name,
            "detail": dict(detail or {}),
        }
        if error is not None:
            entry["detail"]["error"] = error.message
        self._store.append_journal(workspace_id, entry)

    def _provenance(
        self,
        record: Mapping[str, Any],
        effective: EffectivePolicy | None,
        descriptor: WorkspaceDescriptor | None,
        *,
        outcome: JobOutcome | None = None,
        command: Sequence[str] = (),
    ) -> Provenance:
        stored = descriptor or _stored_descriptor(record)
        return Provenance(
            workspace_id=str(record.get("workspace_id", "")),
            job_id=outcome.job_id if outcome is not None else "",
            profile=str(record.get("profile", "")),
            image_digest=stored.environment_digest if stored is not None else "",
            started_at=outcome.started_at if outcome is not None else str(record.get("created_at")),
            finished_at=outcome.finished_at if outcome is not None else utc_now(),
            policy_summary=_policy_summary(effective) if effective is not None else "",
            inputs=list(command),
            trace_id=str(record.get("workspace_id", "")),
        )


# --- module-level helpers ---------------------------------------------------


def _state_of(record: Mapping[str, Any]) -> State:
    try:
        return State(record[_STATE_KEY])
    except (KeyError, ValueError) as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"the stored record carries an unusable lifecycle state: {err}",
            remediation="lifecycle states are: " + ", ".join(state.value for state in State),
        ) from err


def _move(record: dict[str, Any], target: State) -> dict[str, Any]:
    """Validate ``current -> target`` and apply it. Never applies an illegal move."""
    validate_transition(_state_of(record), target)
    record[_STATE_KEY] = target.value
    return record


def _reap_path(state: State) -> tuple[State, ...]:
    """The edges reconciliation walks to retire a record whose engine object is gone.

    ``running`` is absent from :data:`~headspace.providers.base.REMOVAL_PATHS`
    so that a *caller* cannot destroy a live job. Here the engine has already
    confirmed it holds nothing, so the job is not live — and the walk taken is
    the ``running -> cancelled -> destroyed`` one the lifecycle table prescribes
    for exactly this case.
    """
    if state is State.DESTROYED:
        return ()
    if state is State.RUNNING:
        return (State.CANCELLED, State.DESTROYED)
    return removal_path(state)


def _open_intents(entries: Sequence[JournalEntry]) -> list[dict[str, Any]]:
    """Journalled intents with no later entry closing them — the crash signature."""
    closed: set[str] = set()
    intended: list[dict[str, Any]] = []
    for journalled in entries:
        entry = dict(journalled.entry)
        if entry.get("phase") == PHASE_INTENDED:
            intended.append(entry)
        else:
            closed.add(str(entry.get("intent_id", "")))
    return [entry for entry in intended if str(entry.get("intent_id", "")) not in closed]


def _record_from_intent(
    workspace_id: str, intent: Mapping[str, Any], descriptor: WorkspaceDescriptor
) -> dict[str, Any]:
    """Rebuild a lost state record from the journalled intent plus engine truth.

    This is what the intent's ``detail`` payload is *for*: profile, environment,
    declared policy and host snapshot are the four facts no engine can tell us
    back, so they are written down before the engine is called.
    """
    detail = dict(intent.get("detail", {}))
    return {
        "workspace_id": workspace_id,
        _STATE_KEY: descriptor.state.value,
        "provider": descriptor.provider,
        "profile": str(detail.get("profile", "")),
        "environment": str(detail.get("environment", "")),
        "created_at": descriptor.created_at,
        "policy": dict(detail.get("policy", {})),
        "capabilities": dict(detail.get("capabilities", {})),
        "descriptor": descriptor.to_dict(),
        "artifacts": [],
        "artifact_references": {},
        "jobs": [],
        "jobs_dropped": 0,
    }


def _inventory(record: Mapping[str, Any]) -> ArtifactInventory:
    return ArtifactInventory(
        ArtifactRecord.from_dict(entry) for entry in record.get("artifacts", [])
    )


def _stored_descriptor(record: Mapping[str, Any]) -> WorkspaceDescriptor | None:
    stored = record.get("descriptor")
    return WorkspaceDescriptor.from_dict(stored) if stored else None


def _append_job(record: dict[str, Any], outcome: JobOutcome) -> dict[str, Any]:
    jobs = list(record.get("jobs", []))
    jobs.append(outcome.to_dict())
    dropped = int(record.get("jobs_dropped", 0))
    if len(jobs) > MAX_RETAINED_JOBS:
        dropped += len(jobs) - MAX_RETAINED_JOBS
        jobs = jobs[-MAX_RETAINED_JOBS:]
    record["jobs"] = jobs
    record["jobs_dropped"] = dropped
    return record


def _artifact_section(record: Mapping[str, Any]) -> list[Artifact]:
    """The exported subset of the inventory, in wire form (deviation d3)."""
    references = record.get("artifact_references", {})
    return [
        result_artifact(entry, reference=str(references.get(entry.name, "")))
        for entry in _inventory(record)
        if entry.retention == RETENTION_EXPORTED
    ]


def _pending_artifact_attention(record: Mapping[str, Any]) -> list[str]:
    return [
        f"artifact '{entry.name}' is declared but not exported ({entry.purpose}); export it, or "
        "a destroy will refuse until you discard it with force"
        for entry in _inventory(record).unexported()
    ]


def _discarded_attention(entry: ArtifactRecord) -> str:
    digest = entry.sha256 or "none — it was never exported, so nothing can verify or recover it"
    return f"discarded declared artifact '{entry.name}' (digest: {digest})"


def _descriptor_findings(state: State, descriptor: WorkspaceDescriptor | None) -> list[str]:
    findings = [f"lifecycle state: {state.value}"]
    if descriptor is None:
        return findings
    findings.extend(
        [
            f"network: {'enabled' if descriptor.network_enabled else 'disabled'}",
            f"storage: {descriptor.storage_bytes} bytes measured",
            f"the engine reports {descriptor.active_jobs} job(s) in flight",
            f"environment digest: {descriptor.environment_digest}",
        ]
    )
    return findings


def _job_findings(outcome: JobOutcome) -> list[str]:
    if outcome.exit_status is None:
        return [
            f"the job reported {outcome.status} and never produced an exit status",
            "no exit status means the command was stopped, not that it succeeded",
        ]
    return [f"the command completed with exit status {outcome.exit_status}"]


def _job_warnings(record: Mapping[str, Any], outcome: JobOutcome) -> list[str]:
    warnings = _dropped_job_warnings(record)
    if outcome.truncated:
        kept = len(outcome.output.encode("utf-8"))
        warnings.append(
            f"captured output was truncated to {kept} bytes of {outcome.usage.output_bytes} "
            f"produced — full text: `{inspect_path(outcome.job_id)}`"
        )
    return warnings


def _dropped_job_warnings(record: Mapping[str, Any]) -> list[str]:
    dropped = int(record.get("jobs_dropped", 0))
    if not dropped:
        return []
    return [
        f"{dropped} older job record(s) were dropped; this workspace keeps the most recent "
        f"{MAX_RETAINED_JOBS}"
    ]


def _measured_warnings(policy: EffectivePolicy) -> list[str]:
    return [
        f"the {limit.name} limit is measured, not enforced"
        + (f": {limit.detail}" if limit.detail else "")
        for limit in policy.limits
        if limit.status is LimitStatus.MEASURED
    ]


def _session_usage(
    record: Mapping[str, Any], descriptor: WorkspaceDescriptor | None
) -> ResourceUsage:
    """Aggregate a session's cost, so inspect answers "what has this cost so far"."""
    jobs = record.get("jobs", [])
    usages = [dict(job.get("usage", {})) for job in jobs]
    return ResourceUsage(
        wall_time_seconds=sum(float(usage.get("wall_time_seconds", 0.0)) for usage in usages),
        cpu_seconds=sum(float(usage.get("cpu_seconds", 0.0)) for usage in usages),
        max_memory_bytes=max(
            (int(usage.get("max_memory_bytes", 0)) for usage in usages), default=0
        ),
        storage_bytes=descriptor.storage_bytes if descriptor is not None else 0,
        output_bytes=sum(int(usage.get("output_bytes", 0)) for usage in usages),
    )


def _policy_summary(policy: EffectivePolicy) -> str:
    return ", ".join(
        f"{limit.name}={limit.requested}"
        + (" (measured)" if limit.status is LimitStatus.MEASURED else "")
        for limit in policy.limits
    )


def _policy_to_dict(policy: Policy) -> dict[str, Any]:
    return {
        "network": policy.network.value,
        "filesystem": {"host_paths": list(policy.filesystem.host_paths)},
        "budget": dataclasses.asdict(policy.budget),
    }


def _policy_from_dict(data: Mapping[str, Any]) -> Policy:
    try:
        return Policy(
            network=NetworkPosture(data["network"]),
            filesystem=FilesystemScope(host_paths=tuple(data["filesystem"]["host_paths"])),
            budget=ResourceBudget(**dict(data["budget"])),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"the stored policy declaration is malformed: {err}",
            remediation=(
                "the record was not written by this build of headspace; move it aside rather "
                "than run a job under a policy headspace cannot read"
            ),
        ) from err


def _snapshot_to_dict(snapshot: CapabilitySnapshot) -> dict[str, Any]:
    return dataclasses.asdict(snapshot)


def _snapshot_from_dict(data: Mapping[str, Any]) -> CapabilitySnapshot:
    try:
        return CapabilitySnapshot(**dict(data))
    except TypeError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"the stored capability snapshot is malformed: {err}",
            remediation=(
                "the record was not written by this build of headspace; move it aside rather "
                "than resolve a policy against a snapshot headspace cannot read"
            ),
        ) from err
