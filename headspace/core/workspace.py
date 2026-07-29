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

**4. A copy-in refuses rather than waits, and what it carries is not an artifact.**
:meth:`Orchestrator.put` is the one verb that puts host bytes *into* a
workspace, and it takes the workspace lock **non-blocking**, exactly as
``destroy`` does. The reason is :meth:`Orchestrator.run`: it holds that lock for
a job's entire duration, so a blocking ``put`` would not fail, it would *hang* —
for up to the wall-clock budget — with no output and nothing to act on. An
immediate refusal that names the job in flight and points at ``stop`` is the
honest answer to "the workspace is busy", and it is the same answer whether the
job's CLI still holds the lock or only the persisted ``running`` state survives
it. The refusal names the job by **id, never by command line**: an argv is
exactly the surface this copy-in path exists to stop leaking (issues #13/#14),
and a refusal message is still a surface.

What lands is recorded in an ``inputs`` ledger that is deliberately *not* the
artifact inventory. The destroy guard protects unexported **products** — work
that exists nowhere but inside the workspace and dies with it. A host-sourced
input is re-puttable by definition: its source still sits on the host, so
guarding it would refuse a teardown over a file the caller already has. The
ledger therefore renders separately, gates nothing, and leaves
:class:`~headspace.core.artifacts.ArtifactInventory` untouched.

Every one of those records carries the host path, the workspace destination, the
size and the sha256 — and never a byte of content. That is structural, not a
redaction pass: the only thing this module ever holds is an
:class:`~headspace.core.inputs.InputEntry`, and an ``InputEntry`` has no content
field to leak.

**5. A secret travels by name, and only the name is written down.**
:class:`JobEnvironment` is the other half of the same problem (issue #13). Before
it, argv was the only way to hand a job a value, and argv is recorded verbatim in
four places: ``outcome_summary``, ``provenance.inputs``, ``journal.jsonl`` and
``state.json``. A caller who needed to give a job an API key therefore had no
correct move. So ``run`` gained an environment channel whose *values* are read
from the caller's own environment, by name, and are handed to the provider and to
nothing else — while what is recorded is the key set of that very mapping. The
guarantee is again structural rather than a redaction pass: every recording
helper below reads :attr:`JobEnvironment.names`, and ``names`` is derived from
the mapping's keys, so there is no code path along which a value could be written
down even by accident.

Note what is deliberately *absent*: there is no redaction pass over argv. A
caller who types a secret into the command line still has it recorded verbatim,
because headspace cannot know which argv token is a secret, and a guessing
redactor that misses once is worse than a documented absence — a caller who
believes their transcript is clean stops checking. The answer to a secret in argv
is the channel that does not need argv, which is what this is.

The edge of the guarantee is stated rather than glossed: a *job* that prints its
own environment writes the value into its captured output, and captured output is
kept. That is the job's doing, not headspace's, and no recording discipline on
this side can unsee it — so ``run --help`` says so where the flags are.

**6. ``stop`` previews by default, and even when it acts it writes nothing.**
:meth:`Orchestrator.stop` is the verb the refusal above points at, and it is
shaped by the same fact that forced ``put`` to refuse rather than wait: ``run``
holds the workspace lock for a job's entire duration and is, for that duration,
the single writer of the workspace's state. A ``stop`` that took the lock would
not interrupt that run — it would queue behind it and arrive after the job it
was meant to end. A ``stop`` that wrote state would race the run's own
transition back to ``ready``. So this verb does neither: it reaches the engine
directly through :meth:`~headspace.providers.base.Provider.stop`, and the run
invocation still in flight observes the ending through the wait it was already
doing and journals the outcome itself. One verb ends the job, the other narrates
it, and the store keeps exactly one writer while both act on the same workspace.

Two consequences are worth stating rather than discovering. First, ``stop`` is
the **single exception to rule 2 above**: it does not reconcile, because
reconciliation takes locks and writes state, which is the pair of things this
verb exists not to do. What it gives up is the ability to release a workspace
stranded in ``running`` by a dead CLI — so when the engine reports nothing
running under a ``running`` record, ``stop`` says exactly that and names
reconciliation as what will fix it, rather than quietly fixing it under a lock
it should not be holding. Second, a preview answers from headspace's own
records and an ``--apply`` answers from the engine. Those two disagree in
exactly one situation — a job whose CLI died — and that disagreement is
information, not a bug to paper over.

Preview is the default because ending someone's computation is not recoverable
either: the partial work inside the workspace survives, but the run does not,
and a caller who typed the wrong workspace id cannot un-kill it. ``destroy``
takes the opposite default (act unless the guard refuses) because its guard can
*see* what would be lost; nothing here can see how far a job had got. So the
safe default is inverted: ``stop`` refuses to act until ``--apply`` says so, and
a preview touches nothing at all — not the store, not the lock, and not the
engine, which is not asked so much as a question.

Three deviations from the plan are implemented here
---------------------------------------------------
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

**d6 — ``export`` pulls its own bytes.** The seam originally had five verbs and
none of them could read a file *out* of a workspace, so :meth:`Orchestrator.export`
demanded the bytes from its caller. That was unimplementable for the one caller
that matters: ``headspace export`` knows an artifact's name, not its contents,
and the contents sit inside a volume no process outside the engine can open. The
seam grew a sixth verb (:meth:`headspace.providers.base.Provider.read`) and this
module now streams from it whenever no explicit ``source`` is given. Note what
did *not* move: the digest, the atomic temp-and-rename, and the ledger all still
live in :mod:`headspace.core.artifacts`. The provider became a source of bytes,
not a second opinion about durability.

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
    inputs                        the copy-in ledger: one row per destination
                                  ever written into the workspace from the host,
                                  carrying source path, destination, size and
                                  sha256 — never contents, and never consulted
                                  by the destroy guard
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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, BinaryIO

from headspace.cli._errors import (
    EXIT_CANCELLED,
    EXIT_COMPUTATION_FAILED,
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_POLICY_DENIED,
    EXIT_RESOURCE_EXHAUSTED,
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
from headspace.core.inputs import InputEntry, InputManifest, expand_input, precheck_storage_budget
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
    STATUS_RESOURCE_EXHAUSTED,
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
    StopOutcome,
    WorkspaceDescriptor,
    guard_removable,
    removal_path,
    requested_limit,
    require_workspace_path,
    utc_now,
)

# --- vocabularies -----------------------------------------------------------

#: The five things a verb can intend to do. Four of them touch an engine;
#: ``export`` is journalled too so an interrupted export is still visible to a
#: reader of the journal. ``put`` is the inbound counterpart of ``export`` and
#: the only intent whose payload names *host* paths, because it is the only one
#: whose recovery needs to know which host bytes were promised to a workspace.
INTENT_CREATE = "create"
INTENT_RUN = "run"
INTENT_PUT = "put"
INTENT_EXPORT = "export"
INTENT_REMOVE = "remove"
INTENTS: tuple[str, ...] = (
    INTENT_CREATE,
    INTENT_RUN,
    INTENT_PUT,
    INTENT_EXPORT,
    INTENT_REMOVE,
)

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

#: The optional provider hook reconciliation calls to clear a workspace's
#: copy-in staging area, looked up by name rather than declared on the
#: :class:`~headspace.providers.base.Provider` protocol.
#:
#: A backend that commits a copy-in by staging inside the workspace and then
#: renaming (the Docker one does, under a reserved prefix) can be killed between
#: those two steps, and what it leaves behind is a file only that backend knows
#: how to find. So the reap has to be the backend's own operation, and this
#: module can only ask for it. It is *optional* because a backend whose write
#: commits in one step has no staging area to clear and should not be forced to
#: implement an empty method — and because the honest report for a backend that
#: cannot reap is "residue was left in place", never "there was none". The hook
#: takes a workspace id and returns whatever it removed; reconciliation counts
#: it and reports the number.
STAGING_REAPER = "reap_staging"

#: How many copied-in files a single result package lists individually before it
#: summarises the rest. A directory copy-in can hold thousands of files, and the
#: package is bounded (see :mod:`headspace.core.result`) — so the cut is made
#: here, where the remainder can be *named as a remainder*, rather than by a
#: renderer that would silently drop the tail.
MAX_RENDERED_INPUTS = 10

#: ``O_NOFOLLOW`` where the platform has it, and 0 where it does not — the same
#: spelling :func:`headspace.core.inputs.expand_input` uses to measure a file,
#: so the measuring open and the streaming open ask the kernel for the same
#: thing. Degrading to 0 rather than failing is deliberate: on a platform
#: without the flag this is defense in depth that is simply unavailable, and the
#: digest re-verification downstream is the guarantee that never went away.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

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
    STATUS_RESOURCE_EXHAUSTED: EXIT_RESOURCE_EXHAUSTED,
}

#: The two exit statuses that mean the command never got to run its own logic,
#: and what each one tells the caller to change. POSIX's convention, not any
#: engine's — which is why it is written here rather than imported from a
#: backend, and why the finding built from it says "conventionally": a shell
#: *inside* the workspace relaying its own "command not found" produces the same
#: number, and headspace cannot tell the two apart from an exit status alone.
#:
#: The two are kept separate for the same reason the provider that produces them
#: keeps them separate: 127 says re-spell the name, 126 says the name was right
#: and something else is wrong. Collapsed, they would send an agent off to fix a
#: spelling that was already correct.
_UNRUNNABLE_COMMAND_DIAGNOSES: dict[int, str] = {
    126: "was found but is not executable",
    127: "was not found",
}

_STATE_KEY = "state"

#: The state record's copy-in ledger. A sibling of ``artifacts`` and never a
#: part of it — see the module docstring's fourth rule.
_INPUTS_KEY = "inputs"

#: "This job forwards nothing." An empty :class:`~types.MappingProxyType` built
#: once at import, never a ``{}`` literal in a signature default: a mutable
#: default is created once and shared by every caller that omits the argument,
#: so one caller's incidental mutation of "no env" would become every other
#: caller's "no env" for the rest of the process. The same reasoning, and the
#: same constant, as :data:`headspace.providers.fake.EMPTY_ENV` — the closed
#: default is structural on both sides of the seam or it is a convention.
_NO_ENV: Mapping[str, str] = MappingProxyType({})

#: The characters a variable *name* may never contain, whatever grammar the
#: caller-facing flag enforces on top. ``=`` would make the assignment
#: ambiguous to every engine that renders an environment as ``NAME=VALUE``
#: lines, and a null byte cannot survive ``execve`` at all. Checked here, at
#: the type, so a name that reached the orchestrator through some path other
#: than the CLI is still refused.
_UNUSABLE_IN_ENV_NAME = ("=", "\x00")


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
class InputRequest:
    """One host payload a job wants copied in first: where it lives, where it lands.

    The destination is put through
    :func:`~headspace.providers.base.require_workspace_path` in ``__post_init__``,
    which is what makes "validated before any engine contact" a property of the
    *type* rather than of the order some caller happened to write its statements
    in. A CLI that parses its flags into these objects before it constructs a
    provider has satisfied the rule structurally, and a caller that reaches
    :meth:`Orchestrator.run` directly gets the same boundary for free.

    ``host_path`` is not validated here on purpose. Whether it exists, whether it
    is a file or a directory, whether it holds a symlink — those are questions
    :func:`~headspace.core.inputs.expand_input` answers by *looking*, and
    answering them twice would mean answering them two subtly different ways.
    """

    host_path: str | os.PathLike[str]
    destination: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "destination", require_workspace_path(self.destination))


@dataclass(frozen=True, repr=False)
class JobEnvironment:
    """The environment one job runs with: values for the engine, names for the record.

    The split is the whole point (issue #13). ``values`` is handed to the
    provider and to nothing else; ``names`` — derived from ``values``' own keys,
    never stored separately — is what every recording surface is given. Because
    the recorded form is *computed from* the mapping rather than maintained
    alongside it, the two cannot drift, and there is no code path along which a
    value could be written down: a helper that wanted to record one would have to
    reach past :attr:`names` to do it.

    ``sources`` carries the host paths an ``--env-file`` was read from. A path is
    lineage, not payload — it says where a name came from without saying what the
    name is worth — so it is recorded, and the file's contents never are.

    ``__repr__`` is overridden for the same reason the error messages around this
    feature name jobs by id and never by command line: a debugger, a log line, an
    ``f"{environment}"`` written in haste, or a ``pytest`` assertion diff are all
    surfaces, and a dataclass's generated repr would print every value on all of
    them. Immutability is enforced rather than promised — the mapping is wrapped
    in :class:`~types.MappingProxyType` — so a value cannot be added to a
    ``JobEnvironment`` after the names it reports were taken from it.
    """

    values: Mapping[str, str] = field(default_factory=lambda: _NO_ENV)
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in self.values.items():
            self._require_usable(name, value)
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "sources", tuple(self.sources))

    @staticmethod
    def _require_usable(name: str, value: str) -> None:
        """Refuse a name or value no engine could carry — naming neither value."""
        if not name.strip():
            raise CliError(
                code=EXIT_USER_ERROR,
                message="an environment variable must have a name",
                remediation="name every variable that is forwarded into a job",
            )
        for character in _UNUSABLE_IN_ENV_NAME:
            if character in name:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"environment variable name {name!r} is not usable",
                    remediation=(
                        "a name may not contain '=' or a null byte; pass --env NAME and let "
                        "headspace read the value out of your environment by that name"
                    ),
                )
        if "\x00" in value:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"the value of environment variable '{name}' contains a null byte",
                remediation=(
                    "a null byte cannot survive execve, so no job could receive this value; "
                    "the value itself is not quoted here, because an error message is a "
                    "recording surface like any other"
                ),
            )

    @property
    def names(self) -> tuple[str, ...]:
        """The recorded form: the key set of the mapping the job actually gets.

        In declaration order, because that is the order the caller wrote and the
        order a reader comparing a journal entry against a command will expect.
        """
        return tuple(self.values)

    def provider_kwargs(self) -> dict[str, Mapping[str, str]]:
        """The ``env=`` keyword for :meth:`~headspace.providers.base.Provider.run`.

        Always present, now that ``env`` is part of the seam every backend
        implements. It was briefly conditional — omitted when there was nothing
        to forward — while this feature's halves were being built in parallel
        and one backend's ``run`` had not grown the parameter yet. That is a
        scaffolding shape, not a design: passing the empty mapping explicitly is
        exactly what the seam's own default already means, and a caller that
        forwards nothing now produces the same call as one that forwards an
        empty environment, because they are the same request.
        """
        return {"env": self.values}

    def __bool__(self) -> bool:
        return bool(self.values) or bool(self.sources)

    def __repr__(self) -> str:
        return f"JobEnvironment(names={list(self.names)}, sources={list(self.sources)})"


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


@dataclass(frozen=True)
class _InFlightJob:
    """The job a workspace's journal says has not finished yet, as ``stop`` sees it.

    Private on purpose: nothing public returns one. It is a reading of the
    journal that :meth:`Orchestrator.stop` renders into a result package, and
    the package is the surface.

    Assembled from the journal rather than from the state record, because the
    journal is where the fact lives: :meth:`Orchestrator.run` writes its run
    intent — argv, job id — before the engine is called and only records an
    outcome once the job is over, so an *open* run intent is precisely the
    signature of a job still going. The envelope's own ``recorded_at`` is
    therefore the closest thing to a start time that exists outside the engine,
    and :attr:`elapsed_seconds` is measured from it.

    ``elapsed_seconds`` is ``None``, never ``0.0``, when the timestamp cannot be
    read. An operator deciding whether to kill a job decides largely on how long
    it has been going; rendering an unreadable clock as zero would hand them a
    number headspace invented, and a job that has been burning CPU for an hour
    is exactly the one that would look freshly started.

    ``command`` is the caller's own argv, journalled verbatim by ``run``. It is
    carried here because a preview whose entire purpose is "should I end this?"
    is unanswerable without it — an id names a job but does not say what it is —
    and because this repeats a fact ``run`` already wrote to ``journal.jsonl``,
    ``state.json`` and its own outcome summary rather than exposing a new one.
    That is the opposite call from :meth:`Orchestrator._inflight_refusal`, which
    names a job by id only, and deliberately so: that refusal is handed to a
    caller who asked about something else entirely and never asked to see a
    command line.
    """

    job_id: str
    command: tuple[str, ...]
    started_at: str
    elapsed_seconds: float | None

    def named(self) -> str:
        """The job, named the way every message about it names it."""
        return f"job {self.job_id}" if self.job_id else "an unnamed job"

    def elapsed(self) -> str:
        """The wall clock it has spent, or an admission that it is unknown."""
        if self.elapsed_seconds is None:
            return "an unknown duration (its journal timestamp is unreadable)"
        return f"{self.elapsed_seconds:.1f}s"


# --- the orchestrator -------------------------------------------------------


class Orchestrator:
    """The lifecycle flows, over any :class:`~headspace.providers.base.Provider`.

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
                _INPUTS_KEY: [],
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
        inputs: Sequence[InputRequest] = (),
        environment: JobEnvironment | None = None,
        job_id: str | None = None,
    ) -> ResultPackage:
        """Execute one command in an existing workspace: ``ready -> running -> ready``.

        The lock is held for the whole job, which is what stops two invocations
        double-starting one workspace (c24). A concurrent destroy does not queue
        behind it — it reads the persisted ``running`` state and refuses.

        ``inputs`` are copied in *first*, through :meth:`_copy_in` — the same
        method :meth:`put` drives, called rather than re-implemented, because a
        second copy of the journal-then-engine-then-state ordering would be a
        second place for it to drift. The lock is already held here, which is
        precisely what ``_copy_in`` is factored to allow: it takes none itself.

        Order inside the lock is not incidental:

        1. the transition and every declaration are validated, but neither is
           written anywhere — a rejected move or a duplicate declaration is a
           caller's mistake, not a death, and must not leave an open intent;
        2. the payload is expanded and budget-checked on the host, which is the
           last point at which a refusal costs nothing;
        3. the copy-in runs, journalling and settling its own ``put`` intent, so
           a CLI killed mid-copy leaves a workspace still in ``ready`` with an
           open ``put`` intent — the shape reconciliation already knows;
        4. only then are the declarations persisted and the run intent opened.

        ``environment`` is the secret channel (issue #13). Its *values* go to the
        provider and nowhere else; its names, and the paths any ``--env-file``
        was read from, are what the journal, the state record, the summary and
        the provenance carry. See :class:`JobEnvironment` for why that is a
        structural property rather than a discipline, and for the edge it does
        not cover: a job that prints its own environment.
        """
        attention = self._take_attention()
        job_id = job_id or new_job_id()
        environment = environment if environment is not None else JobEnvironment()

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

            landed = self._copy_inputs(workspace_id, record, inputs, policy=effective)
            # Persisted only now: a copy-in that failed part-way already wrote
            # the ledger for what landed, and folding a declaration into that
            # write would leave a workspace owing an artifact from a job that
            # never started — and a destroy refusing over it.
            record["artifacts"] = inventory.to_list()

            intent_id = self._intend(
                workspace_id, INTENT_RUN, _run_intent(command, job_id, environment)
            )
            record = self._advance(workspace_id, record, State.RUNNING)

            try:
                outcome = self._provider.run(
                    workspace_id,
                    command,
                    effective,
                    job_id=job_id,
                    **environment.provider_kwargs(),
                )
            except CliError as err:
                # Infrastructure failure, raised — never returned as a job
                # status (NFR-07). The workspace is released first so a broken
                # engine does not also strand the session.
                record = self._advance(workspace_id, record, State.READY)
                self._close(workspace_id, INTENT_RUN, intent_id, PHASE_ABANDONED, err)
                raise

            record = _append_job(record, outcome, environment)
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

        warnings = (
            _measured_warnings(effective)
            + _job_warnings(record, outcome)
            + _env_warnings(environment)
        )
        return ResultPackage(
            outcome_summary=_run_summary(
                workspace_id, job_id, command, outcome, landed, environment
            ),
            status=outcome.status,
            key_findings=(
                _job_findings(
                    outcome,
                    policy=effective,
                    profile=str(record.get("profile", "")),
                    command=command,
                )
                + _copied_findings(landed)
                + _env_findings(environment)
            ),
            evidence=[
                Evidence(
                    label="captured output",
                    kind="excerpt",
                    excerpt=outcome.output,
                    # A bare ref, not a rendered command: result._excerpt_marker
                    # calls inspect_path() itself when it truncates. Passing an
                    # already-rendered path made every truncated result print
                    # `headspace inspect headspace inspect job-x --logs --logs`.
                    source=job_id,
                )
            ],
            artifacts=_artifact_section(record),
            warnings=warnings,
            resource_usage=outcome.usage,
            provenance=self._provenance(
                record,
                effective,
                None,
                outcome=outcome,
                command=command,
                inputs=_input_provenance(landed) + _env_provenance(environment),
            ),
            attention=(
                attention
                + _resource_attention(outcome, effective)
                + _pending_artifact_attention(record)
            ),
        )

    def _copy_inputs(
        self,
        workspace_id: str,
        record: dict[str, Any],
        inputs: Sequence[InputRequest],
        *,
        policy: EffectivePolicy,
    ) -> list[dict[str, Any]]:
        """Expand, budget-check and copy in a job's ``--input`` payloads.

        The host-side half of :meth:`put`, hoisted so ``run`` performs it in the
        same order and with the same refusals, and then hands the result to the
        same :meth:`_copy_in`. What ``put`` does that this does not is take a
        lock and read state: ``run``'s caller is already inside both.

        ``overwrite`` is not offered. A job's inputs arrive into a workspace that
        may already hold a previous job's outputs, and silently replacing one of
        those would destroy work with no record that anything was lost —
        replacement is a deliberate act, and it belongs to the verb a caller can
        deliberately give permission to.
        """
        if not inputs:
            return []
        manifest = _expand_inputs(inputs)
        # Budget before journal, and before the engine: an over-budget payload
        # costs nothing and leaves nothing. Checked against the whole payload
        # rather than one --input at a time, because the budget is a property of
        # the workspace and the caller asked for all of them at once.
        precheck_storage_budget(
            manifest, storage_bytes_remaining=_storage_remaining(record, policy)
        )
        return self._copy_in(workspace_id, record, _bounded_entries(manifest), overwrite=False)

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
        findings.extend(_ledger_findings(record))
        evidence: list[Evidence] = []
        if jobs:
            last = jobs[-1]
            evidence.append(
                Evidence(
                    label=f"captured output of job {last.get('job_id', '')}",
                    kind="excerpt",
                    excerpt=str(last.get("output", "")),
                    # Bare ref — the renderer applies inspect_path(). See run().
                    source=str(last.get("job_id", workspace_id)),
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
        source: ByteSource | None = None,
        destination: str | os.PathLike[str] | None = None,
        *,
        expected_sha256: str | None = None,
        path: str | None = None,
    ) -> ResultPackage:
        """Publish a declared artifact atomically and record what left the workspace.

        Two ways to name the bytes, and the default is the one that matters
        (deviation d6, issue #3):

        * **Omit ``source``** and the artifact is pulled through the provider's
          ``read`` verb from ``path`` — defaulting to ``name``, because an
          artifact is usually declared under the name the job wrote it as. This
          is the path the CLI takes, and the only one it *can* take: the bytes
          live inside a workspace volume that no process outside the engine can
          open, so "hand me the source" was a request the caller could not
          satisfy.
        * **Pass ``source``** and it is used verbatim, with no read at all, for
          the caller that already holds the bytes.

        Either way this method owns the durability boundary and nothing else
        moved: :func:`~headspace.core.artifacts.export_artifact` still computes
        the digest during the copy, still publishes by atomic rename, and still
        writes the ledger entry. The provider became *a source of bytes*, not a
        second place where durability is decided.

        The stream is released on every exit — a digest that fails to verify
        must not also strand whatever the backend was holding open.
        """
        attention = self._take_attention()
        if destination is None:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"no destination given for artifact '{name}'",
                remediation=(
                    "pass the path the artifact should be published to; an export that "
                    "lands nowhere durable is not an export"
                ),
            )
        workspace_path = path if path is not None else name

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
                {
                    "name": name,
                    "destination": os.fspath(destination),
                    "path": "" if source is not None else workspace_path,
                },
            )
            try:
                # The read is inside the try for the same reason the copy is: a
                # missing artifact and a dead engine are both failures of *this*
                # invocation, and both have to close the intent on their way out
                # or reconciliation will later read them as a crash.
                with self._bytes(workspace_id, source, workspace_path) as bytes_source:
                    exported = export_artifact(
                        bytes_source,
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

    def put(
        self,
        workspace_id: str,
        host_path: str | os.PathLike[str],
        destination: str,
        *,
        overwrite: bool = False,
    ) -> ResultPackage:
        """Copy host bytes *into* a workspace, and record the paths — never the bytes.

        ``export``'s mirror image, and modelled on it deliberately: take the
        lock, validate, journal an intent that names paths, call the engine, and
        settle or abandon that intent on every way out. Three things about this
        direction are different, and each is a decision rather than an accident.

        **The lock is taken non-blocking, and it is taken first.**
        :meth:`run` holds a workspace's lock for the entire duration of a job, so
        a blocking acquisition here would not report a conflict — it would sit
        silently for up to the wall-clock budget and then behave as if nothing
        had happened. What the caller needs instead is the fact: a job is in
        flight, here is its id, end it with ``headspace stop`` or wait.

        It is taken *first* for a separate reason. Reading state and then locking
        is the shape of the open TOCTOU in :meth:`destroy` (issue #11), where the
        window between the guard's read and the engine call belongs to whoever
        else is running. Every guard below is instead evaluated against state
        read *inside* the critical section, so what the guard saw is what the
        engine is asked about — and a destroy racing a copy-in either waits for
        this lock or refuses, with no interleaving that leaves an unjournalled
        write or a workspace destroyed mid-copy.

        **The intent names host paths and digests, and nothing else.** This whole
        path exists because argv-smuggled payloads leak their contents into
        ``outcome_summary``, ``provenance.inputs``, ``journal.jsonl`` and
        ``state.json`` (issues #13/#14). Recording a digest instead of content is
        what makes those surfaces clean structurally: everything this method
        holds is an :class:`~headspace.core.inputs.InputEntry`, and an
        ``InputEntry`` has no content field. The digest is measured on the host
        by :func:`~headspace.core.inputs.expand_input`, handed to the provider as
        ``expected_sha256``, and re-verified engine-side against what actually
        landed — so a file swapped underneath us between the hash and the copy is
        caught by the engine rather than by trust.

        **A partial copy is reported as a partial copy.** A directory expands to
        many files and the engine takes them one at a time, so a failure at file
        three of five is a real state: three files are in the workspace. The
        ledger is written with exactly those three before the intent is abandoned
        and the error re-raised — a ledger that claimed nothing landed would be a
        lie about a workspace the caller is about to reuse.

        ``overwrite`` is passed through, not decided here: refusing to replace a
        destination is the seam's boundary contract (a job's output must not be
        silently clobbered by a copy-in), and the backend is where the
        destination's existence can actually be checked.
        """
        attention = self._take_attention()

        # Lock first, and never wait for it. Everything below reads state, and
        # state read outside the lock is state that may have changed by the time
        # the engine is asked about it.
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(self._store.lock(workspace_id, blocking=False))
        except CliError as err:
            raise self._busy_refusal(workspace_id, err) from err

        with stack:
            record = self._read(workspace_id)
            state = _state_of(record)
            if state is State.RUNNING:
                # The lock was free, so the job's invocation is gone — but the
                # engine still reports the job in flight (reconciliation would
                # have released the workspace otherwise). Same situation as a
                # held lock, so the caller gets the same sentence.
                raise self._inflight_refusal(workspace_id)
            if state is not State.READY:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=(
                        f"refusing to copy into workspace {workspace_id}: it is "
                        f"'{state.value}', and nothing was copied"
                    ),
                    remediation=(
                        "a copy-in feeds a job that has not run yet, and only a 'ready' "
                        "workspace can host one — create a fresh workspace and copy into that"
                    ),
                )

            effective = self._effective_policy(record)
            manifest = expand_input(host_path, destination)
            if not manifest.entries:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"host input '{os.fspath(host_path)}' holds no regular files",
                    remediation=(
                        "point the copy-in at a file, or at a directory that contains one; "
                        "a copy that moves nothing is a mistake, not an empty success"
                    ),
                )
            # Budget before journal, and before the engine: an over-budget
            # payload costs nothing and leaves nothing. See _storage_remaining
            # for why the number it is checked against is the one it is.
            precheck_storage_budget(
                manifest, storage_bytes_remaining=_storage_remaining(record, effective)
            )
            entries = _bounded_entries(manifest)
            landed = self._copy_in(workspace_id, record, entries, overwrite=overwrite)

        return ResultPackage(
            outcome_summary=_copy_in_summary(workspace_id, host_path, landed),
            status=STATUS_SUCCESS,
            key_findings=_copied_findings(landed),
            artifacts=_artifact_section(record),
            warnings=_measured_warnings(effective),
            resource_usage=ResourceUsage(storage_bytes=_landed_bytes(landed)),
            provenance=self._provenance(record, effective, None, inputs=_input_provenance(landed)),
            attention=attention + _pending_artifact_attention(record),
        )

    def _copy_in(
        self,
        workspace_id: str,
        record: dict[str, Any],
        entries: Sequence[InputEntry],
        *,
        overwrite: bool,
    ) -> list[dict[str, Any]]:
        """Journal, stream, record — the copy-in proper, under a lock the caller holds.

        Factored out of :meth:`put` because it is the *single implementation
        site* for getting host bytes into a workspace: ``run``'s input flag
        drives this same method rather than a second, subtly different copy of
        the ordering rules. Callers must already hold the workspace lock; this
        method takes none, which is what lets it be called from inside a verb
        that is already holding one.

        Returns the ledger rows for what actually landed. On failure it writes
        those same rows first, *then* abandons the intent, then re-raises: state
        before journal on the way out, for the same reason the journal comes
        before the engine on the way in — the settling entry must never be the
        thing that survives a crash the state write did not.
        """
        intent_id = self._intend(
            workspace_id,
            INTENT_PUT,
            {
                "inputs": [entry.to_dict() for entry in entries],
                "total_bytes": sum(entry.size_bytes for entry in entries),
                "overwrite": overwrite,
            },
        )
        landed: list[dict[str, Any]] = []
        try:
            for entry in entries:
                with _open_input(entry) as source:
                    self._provider.write(
                        workspace_id,
                        entry.destination,
                        source,
                        expected_sha256=entry.sha256,
                        overwrite=overwrite,
                    )
                landed.append(_ledger_row(entry))
        except CliError as err:
            self._record_inputs(workspace_id, record, landed)
            self._close(
                workspace_id,
                INTENT_PUT,
                intent_id,
                PHASE_ABANDONED,
                err,
                {"landed": len(landed), "of": len(entries)},
            )
            raise

        self._record_inputs(workspace_id, record, landed)
        self._close(
            workspace_id,
            INTENT_PUT,
            intent_id,
            PHASE_SETTLED,
            None,
            {"landed": len(landed), "total_bytes": _landed_bytes(landed)},
        )
        return landed

    def _record_inputs(
        self, workspace_id: str, record: dict[str, Any], landed: Sequence[Mapping[str, Any]]
    ) -> None:
        """Merge landed rows into the ledger and persist it, keyed by destination.

        One destination is one row however often it is written: the ledger
        answers "what does this workspace hold, and from where", not "how many
        times was it copied". Appending instead would double-count a re-put
        against the storage budget, which is the one number the ledger is read
        back for.
        """
        if not landed:
            return
        record[_INPUTS_KEY] = _merged_ledger(record, landed)
        self._store.write_state(workspace_id, record)

    def _busy_refusal(self, workspace_id: str, err: CliError) -> CliError:
        """Turn "the lock is held" into a sentence naming what holds it.

        The store's own refusal is accurate but generic. Whether the holder is a
        job matters enormously to the caller — a job has a ``stop`` verb and a
        finish time, another verb has neither — so the journal is read (lock-free,
        which is safe: the store's writes are atomic and its readers take no
        lock) to see whether a run intent is open. The exit codes differ for the
        same reason: an in-flight job is the caller's decision to make (exit 1,
        as a destroy during a run already reports), while another verb holding
        the lock is transient contention and keeps the store's own exit 2.
        """
        job_id = self._inflight_job(workspace_id)
        if job_id:
            return self._inflight_refusal(workspace_id, job_id)
        return CliError(
            code=err.code,
            message=f"{err.message}; nothing was copied",
            remediation=(
                "another headspace invocation is working on this workspace — wait for it to "
                "finish and retry; a copy-in never interrupts work in progress"
            ),
        )

    def _inflight_refusal(self, workspace_id: str, job_id: str = "") -> CliError:
        """The refusal for a job in flight, naming it by id and never by command.

        The command line is exactly the surface this feature exists to keep
        payloads out of (issues #13/#14), and an error message is a surface like
        any other — so the job is identified by the id headspace minted, which
        carries no caller input at all.
        """
        job_id = job_id or self._inflight_job(workspace_id)
        named = f"job {job_id}" if job_id else "a job"
        return CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"refusing to copy into workspace {workspace_id}: {named} is in flight, "
                "and nothing was copied"
            ),
            remediation=(
                f"wait for {named} to finish and retry, or end it with `headspace stop "
                f"{workspace_id}` first — a copy-in feeds a job that has not started, so it "
                "never interrupts one that has"
            ),
        )

    def _inflight_job(self, workspace_id: str) -> str:
        """The id of the job the journal says is still running, or ``""``.

        Read from the journal rather than from state because that is where the
        fact lives: :meth:`run` journals its job id before the engine is called
        and only records the outcome once the job is over, so an *open* run
        intent is precisely the signature of a job that has not finished. Any
        failure to read is answered with "no id" rather than an exception — this
        is called while producing an error message, and an error message that
        raises is worse than one that is vague.
        """
        try:
            entries = self._store.read_journal(workspace_id)
        except CliError:
            return ""
        runs = [entry for entry in _open_intents(entries) if entry.get("intent") == INTENT_RUN]
        if not runs:
            return ""
        return str(dict(runs[-1].get("detail", {})).get("job_id", ""))

    def stop(self, workspace_id: str, *, apply: bool = False) -> ResultPackage:
        """End the job a workspace is running — or, by default, only say which one.

        The verb the copy-in refusal above points at, and the only one that acts
        on a workspace another invocation is holding. Five decisions make it what
        it is, and each is load-bearing.

        **It takes no lock and writes no state.** :meth:`run` holds the workspace
        lock for a job's entire duration, which is what makes it the single
        writer of that workspace's stored state for as long as the job lasts. A
        ``stop`` that took the lock would not interrupt that run — it would queue
        behind it and arrive after the job it was sent to end; a ``stop`` that
        wrote state would race the run's own transition back to ``ready``. So
        this reaches :meth:`~headspace.providers.base.Provider.stop` directly and
        touches nothing under the store, and the run invocation still blocked in
        the provider discovers the ending through the wait it was already doing
        and journals the outcome itself. The store keeps one writer throughout.

        **It does not reconcile**, which makes it the single exception to this
        module's own rule that every verb reconciles at entry. Reconciliation
        takes locks and writes state; those are the two things above. The cost is
        stated rather than hidden: a workspace stranded in ``running`` by a dead
        CLI is *reported* as such — the engine holds no job, the record says one
        is in flight, and the next verb's reconciliation is what releases it.

        **The default previews, and a preview is inert.** Without ``apply`` this
        reads the state record and the journal, renders what it *would* end, and
        returns — no engine call, not even a graceful signal, and nothing
        written. That is the opposite default from :meth:`destroy`, which acts
        unless its guard refuses, and the asymmetry is the point: destroy's guard
        can see what would be lost, while nothing here can see how far a job had
        got. A caller who typed the wrong workspace id can re-export an artifact
        they nearly discarded; they cannot un-kill a computation.

        **Nothing running is a fact, not a failure.** An operator racing a job
        that finished a moment earlier made no mistake, so
        :class:`~headspace.providers.base.StopOutcome` reports it (``job_id`` is
        ``None``, ``stopped`` is ``False``) and this reports it onward as a
        successful statement about the workspace. Raising there would turn the
        ordinary case into an error every caller has to special-case.

        **Exit 5 means a job really ended.** A stop that signalled a live job
        returns :data:`~headspace.core.result.STATUS_CANCELLED`, which
        :func:`exit_code_for_status` maps to the taxonomy's long-defined
        ``cancelled`` slot — the caller asked for it to stop, and it stopped. A
        preview and a stop that found nothing return ``success``, because
        nothing was cancelled and a process exit that said otherwise would be
        reporting an act that did not happen.

        An unknown workspace is refused (exit 1) before the engine is asked, as
        every other id-keyed verb here refuses one. An engine object the *store*
        has no record of is reconciliation's business, not this verb's.
        """
        record = self._read(workspace_id)
        state = _state_of(record)
        job = self._inflight_job_view(workspace_id)

        if not apply:
            status = STATUS_SUCCESS
            summary = _preview_summary(workspace_id, state, job)
            findings = _inflight_findings(state, job) + list(_PREVIEW_FINDINGS)
            attention: list[str] = []
        else:
            outcome = self._provider.stop(workspace_id)
            status = STATUS_CANCELLED if outcome.stopped else STATUS_SUCCESS
            summary = _stop_summary(workspace_id, job, outcome)
            findings = _inflight_findings(state, job) + _stopped_findings(outcome)
            attention = _stranded_attention(state, outcome)

        return ResultPackage(
            outcome_summary=summary,
            status=status,
            key_findings=findings,
            artifacts=_artifact_section(record),
            # The job's wall clock, not this verb's, and measured from the
            # journal rather than the engine — the findings say so, which is what
            # keeps a floor from reading as a measurement.
            resource_usage=ResourceUsage(wall_time_seconds=_elapsed_or_zero(job)),
            provenance=self._provenance(record, self._effective_policy(record), None),
            attention=attention + _pending_artifact_attention(record),
        )

    def _inflight_job_view(self, workspace_id: str) -> _InFlightJob | None:
        """What the journal says is still running, or ``None`` — never an exception.

        A journal this cannot read is answered with "no job", for the same reason
        :meth:`_inflight_job` answers that way: ``stop`` is the verb an operator
        reaches for when something has already gone wrong, and a corrupt journal
        must not be the thing standing between them and a runaway job. What is
        lost is the *description*, never the act — ``apply`` still asks the
        engine, and the engine is the authority on what is running.
        """
        try:
            entries = self._store.read_journal(workspace_id)
        except CliError:
            return None
        journalled = _open_run_entry(entries)
        if journalled is None:
            return None
        detail = dict(journalled.entry.get("detail", {}))
        return _InFlightJob(
            job_id=str(detail.get("job_id", "")),
            command=tuple(str(part) for part in detail.get("command", [])),
            started_at=str(journalled.recorded_at),
            elapsed_seconds=_elapsed_since(journalled.recorded_at),
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

        findings = _removal_findings(disposition, path)
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
            # The destruction report has to say what survives elsewhere, not
            # only what went away (doc section 11: "Destruction reports which
            # workspace data was removed and which exported artifacts remain").
            # `record` still holds the inventory updated just above, which is
            # why this reads it before delete_workspace() drops the record.
            # Only exported artifacts render here; anything force-discarded is
            # named in the warning instead, where its loss can be stated plainly.
            artifacts=_artifact_section(record),
            warnings=warnings,
            resource_usage=ResourceUsage(),
            provenance=self._provenance(record, None, descriptor),
            attention=attention + [_discarded_attention(item) for item in discarded],
        )

    @contextlib.contextmanager
    def _bytes(
        self,
        workspace_id: str,
        source: ByteSource | None,
        path: str,
    ) -> Iterator[ByteSource]:
        """The artifact's bytes, and the guarantee that whatever opened them closes.

        A context manager rather than a plain lookup because the two cases have
        different ownership and both are correct: a caller-supplied source
        belongs to the caller and must not be closed here, while a stream this
        method pulled through the seam belongs to this method and must be
        released however the export ends — verified, refused, or interrupted.
        """
        if source is not None:
            yield source
            return
        with self._provider.read(workspace_id, path) as stream:
            yield stream

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

        if intent == INTENT_PUT:
            return self._reconcile_put(workspace_id, latest, open_intents)

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

    def _reconcile_put(
        self,
        workspace_id: str,
        latest: Mapping[str, Any],
        open_intents: Sequence[Mapping[str, Any]],
    ) -> Reconciliation:
        """An interrupted copy-in made no lifecycle move; settle it and clear its residue.

        It needs its own branch for the same reason an export does, and then one
        more. Like an export, a copy-in never changes the workspace's lifecycle
        state, so the generic path below would read an open put intent as an
        interrupted *lifecycle* transition and rebuild the record from engine
        truth — adopting a workspace that was never in doubt, or worse, reaping a
        perfectly good one because the engine happens not to hold something the
        put was never about.

        The extra part is the residue. A backend that commits a copy-in by
        staging inside the workspace and renaming can be killed between those two
        steps, and the staged bytes then sit in the workspace spending its
        storage budget with nothing pointing at them. Only the backend can find
        them, so this asks (see :data:`STAGING_REAPER`) and reports what came
        back — including "this backend has no reaper", which is a different fact
        from "there was nothing to reap" and is reported as itself.

        The reap happens under the workspace lock, taken non-blocking, and that
        is not politeness: staging is reaped *by prefix*, so doing it while
        another invocation is mid-copy would delete the bytes that invocation is
        about to rename into place. A busy workspace is deferred with its intent
        left open, exactly as the generic path defers.

        Only put intents are settled here. A workspace that also carries an open
        intent of another kind still has a real orphan, and that orphan is left
        for the next verb's reconciliation to settle through the path built for
        it, rather than being quietly closed by this one.
        """
        detail = dict(latest.get("detail", {}))
        destinations = [str(row.get("destination", "")) for row in detail.get("inputs", []) if row]
        named = ", ".join(destinations[:MAX_RENDERED_INPUTS]) or "an unnamed file"
        if len(destinations) > MAX_RENDERED_INPUTS:
            named += f" and {len(destinations) - MAX_RENDERED_INPUTS} more"

        stack = contextlib.ExitStack()
        try:
            stack.enter_context(self._store.lock(workspace_id, blocking=False))
        except CliError:
            return Reconciliation(
                workspace_id,
                INTENT_PUT,
                DISPOSITION_REPORTED,
                f"an interrupted copy-in of {named} is unsettled, but the workspace is busy in "
                "another headspace invocation, so nothing was reaped under it and "
                "reconciliation was deferred rather than made to wait",
            )
        with stack:
            reaped, residue = self._reap_staging(workspace_id)
            with contextlib.suppress(CliError):
                for entry in open_intents:
                    if str(entry.get("intent", "")) != INTENT_PUT:
                        continue
                    self._close(
                        workspace_id,
                        INTENT_PUT,
                        str(entry.get("intent_id", "")),
                        PHASE_RECONCILED,
                        None,
                        {"reaped": reaped},
                    )
        return Reconciliation(
            workspace_id,
            INTENT_PUT,
            DISPOSITION_RESOLVED if reaped else DISPOSITION_REPORTED,
            f"a copy-in of {named} was interrupted; each destination holds either the complete "
            f"file or nothing, and the inputs ledger lists only what actually landed — "
            f"{residue}; verify the workspace and re-run the copy-in",
        )

    def _reap_staging(self, workspace_id: str) -> tuple[int, str]:
        """Ask the backend to clear its staging area, and say plainly what happened.

        Three outcomes, three sentences, and the distinction between them is the
        point: residue reaped, no residue to reap, and *no way to tell* — a
        backend without the hook is reported as leaving its residue in place,
        never as being clean, because this module has no way to know which it is.
        A reaper that fails is reported too, and never raised: reconciliation
        runs inside somebody else's verb, and failing that verb over a stale
        staging file trades a small mess for a large one.
        """
        reaper = getattr(self._provider, STAGING_REAPER, None)
        if not callable(reaper):
            return 0, (
                f"backend '{self._provider.name}' exposes no {STAGING_REAPER} hook, so any "
                "staging residue it holds was left in place rather than assumed absent"
            )
        try:
            reaped = list(reaper(workspace_id) or ())
        except CliError as err:
            return 0, f"its staging residue could not be reaped: {err.message}"
        if not reaped:
            return 0, "it left no staging residue behind"
        return len(reaped), f"{len(reaped)} staged file(s) it left behind were reaped"

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
            disposition, detail, record = self._reap(record)
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

    def _reap(self, record: dict[str, Any] | None) -> tuple[str, str, dict[str, Any] | None]:
        """The engine does not hold it. Keep the record only if work was at stake.

        Takes no workspace id, unlike its :meth:`_adopt` sibling: with no engine
        object left to interrogate, the record is the whole of the evidence.
        """
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
        inputs: Sequence[str] = (),
    ) -> Provenance:
        """Identity and lineage for one result package.

        ``inputs`` is the copy-in's contribution to ``provenance.inputs``:
        already-rendered ``path (sha256:...)`` descriptors, appended after the
        command rather than replacing it, so a verb that both copies files in and
        runs a command reports both without either hiding the other.
        """
        stored = descriptor or _stored_descriptor(record)
        return Provenance(
            workspace_id=str(record.get("workspace_id", "")),
            job_id=outcome.job_id if outcome is not None else "",
            profile=str(record.get("profile", "")),
            image_digest=stored.environment_digest if stored is not None else "",
            started_at=outcome.started_at if outcome is not None else str(record.get("created_at")),
            finished_at=outcome.finished_at if outcome is not None else utc_now(),
            policy_summary=_policy_summary(effective) if effective is not None else "",
            inputs=[*command, *inputs],
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


def _open_run_entry(entries: Sequence[JournalEntry]) -> JournalEntry | None:
    """The journal envelope of the newest unsettled ``run`` intent, or ``None``.

    Layered on :func:`_open_intents` rather than written beside it: that function
    owns the crash signature, and a second walk of the same rule would eventually
    disagree with it about what "open" means. What this adds is the *envelope* —
    :attr:`~headspace.core.store.JournalEntry.recorded_at` lives there rather
    than in the entry, and it is the only start time a job has outside the
    engine.
    """
    open_ids = {
        str(entry.get("intent_id", ""))
        for entry in _open_intents(entries)
        if entry.get("intent") == INTENT_RUN
    }
    if not open_ids:
        return None
    for journalled in reversed(entries):
        entry = journalled.entry
        if entry.get("phase") != PHASE_INTENDED:
            continue
        if str(entry.get("intent_id", "")) in open_ids:
            return journalled
    return None


def _elapsed_since(timestamp: str) -> float | None:
    """Seconds from an ISO-8601 journal timestamp until now, or ``None``.

    ``None`` rather than ``0.0`` for anything unparseable. This number is the
    main input to "should I kill this?", and a clock headspace could not read but
    rendered as zero would make a job that has been running for an hour look as
    though it had just started — the one error that would change the operator's
    answer. A naive timestamp is read as UTC, matching
    :func:`~headspace.providers.base.utc_now`, which is what wrote it.
    """
    try:
        started = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def _elapsed_or_zero(job: _InFlightJob | None) -> float:
    """The job's wall clock for :class:`~headspace.core.result.ResourceUsage`.

    Zero covers both "no job" and "a clock that could not be read", because the
    usage block is a set of numbers and has nowhere to say "unknown". The prose
    does say it, which is why the distinction is kept there and collapsed here.
    """
    return 0.0 if job is None or job.elapsed_seconds is None else job.elapsed_seconds


#: What a preview says about itself, on every preview. Written once, as a
#: constant, because "nothing happened" is the claim the whole default mode
#: rests on: a caller who is told a job would be ended must be told, in the same
#: breath and in the same words every time, that it was not.
_PREVIEW_FINDINGS: tuple[str, ...] = (
    "preview only: the engine was not contacted, no signal was sent, and nothing was written",
    f"pass --apply to end the job — its own outcome then reads '{STATUS_CANCELLED}', and this "
    f"verb exits {EXIT_CANCELLED}",
)


def _preview_summary(workspace_id: str, state: State, job: _InFlightJob | None) -> str:
    """One sentence for a preview: what is running, and that nothing was done."""
    if job is None:
        return (
            f"workspace {workspace_id} is '{state.value}' and holds no job in flight, so there "
            "is nothing for stop to end; nothing was changed"
        )
    return (
        f"workspace {workspace_id} is running {job.named()}, {job.elapsed()} into its wall "
        "clock; nothing was stopped, because this was a preview"
    )


def _stop_summary(workspace_id: str, job: _InFlightJob | None, outcome: StopOutcome) -> str:
    """One sentence for a stop that acted, whether or not it found anything."""
    if not outcome.stopped:
        return (
            f"workspace {workspace_id} holds no job the engine is running, so nothing was "
            "stopped"
        )
    named = f"job {outcome.job_id}" if outcome.job_id else "the job"
    spent = f" after {job.elapsed()}" if job is not None else ""
    return f"{named} was stopped in workspace {workspace_id}{spent}"


def _inflight_findings(state: State, job: _InFlightJob | None) -> list[str]:
    """What headspace's own records say is running — the half no engine supplied.

    Rendered identically for a preview and for a stop that acted, because it is
    the same reading of the same two files; only what follows it differs.
    """
    findings = [f"lifecycle state: {state.value}"]
    if job is None:
        findings.append(
            "no run intent is open in this workspace's journal, so headspace knows of no job "
            "to end"
        )
        if state is State.RUNNING:
            findings.append(
                "the stored state still reads 'running', which an interrupted job leaves "
                "behind; the next verb's reconciliation is what releases it"
            )
        return findings
    findings.append(f"{job.named()} has been running for {job.elapsed()}")
    if job.command:
        findings.append("command: " + " ".join(job.command))
    findings.append(
        f"started at {job.started_at}, measured from the journal rather than the engine"
    )
    return findings


def _stopped_findings(outcome: StopOutcome) -> list[str]:
    """What the engine did when it was asked, including when the answer was nothing."""
    if not outcome.stopped:
        return [
            "the engine holds no live job for this workspace, so nothing was signalled",
            "a job that finished in the moment before this call is the ordinary case, not a "
            "mistake — so it is reported rather than raised",
        ]
    return [
        f"the engine was asked to end job {outcome.job_id}: signalled first, and killed only "
        "if it ignored that",
        "the run invocation that started the job observes the ending itself and records the "
        "outcome; this verb took no lock and wrote no state",
    ]


def _stranded_attention(state: State, outcome: StopOutcome) -> list[str]:
    """The one case where a stop leaves a decision behind: a record the engine outlived.

    A ``running`` record the engine holds no job for is a workspace whose job's
    CLI died. Releasing it needs the workspace lock and a state write, which is
    exactly the pair this verb does not take — so it is named for the caller
    instead, along with what will actually fix it.
    """
    if outcome.stopped or state is not State.RUNNING:
        return []
    return [
        "the stored state says a job is in flight but the engine is running none, so the job's "
        "invocation died; stop writes no state, and the next headspace verb's reconciliation is "
        "what releases this workspace back to 'ready'"
    ]


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
        # Empty for the same reason ``artifacts`` is: a record rebuilt from the
        # journalled create intent knows what the workspace *was asked to be*,
        # not what later verbs put in it. Claiming a ledger we cannot
        # reconstruct would be worse than admitting we lost one.
        _INPUTS_KEY: [],
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


def _append_job(
    record: dict[str, Any], outcome: JobOutcome, environment: JobEnvironment
) -> dict[str, Any]:
    """Record one job outcome, plus the *names* the job's environment carried.

    ``state.json`` is one of the four surfaces issue #13 measured, so the names
    belong here — a reader who wants to know why a job behaved differently from
    an identical-looking one needs to see that it was handed something. The keys
    are omitted entirely when a job forwarded nothing, so a record written for a
    job with no environment is byte-identical to one written before this feature
    existed: absence is how the closed default reads back.
    """
    jobs = list(record.get("jobs", []))
    jobs.append({**outcome.to_dict(), **_env_record(environment)})
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


def _ledger(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The copy-in ledger, defaulted rather than required.

    A record written before this key existed is a perfectly good record of a
    workspace that has had nothing copied into it, and reading it as one is what
    keeps an older store usable by a newer binary (plan risk r2). The reverse
    direction holds too, and for free: every verb reads the whole state record
    and writes the whole record back, so an older binary carries a ledger it does
    not understand rather than dropping it.
    """
    return [dict(row) for row in record.get(_INPUTS_KEY, [])]


def _ledger_total(record: Mapping[str, Any]) -> int:
    """Bytes this workspace has been handed from the host, by the ledger's count."""
    return sum(int(row.get("size_bytes", 0)) for row in _ledger(record))


def _ledger_row(entry: InputEntry) -> dict[str, Any]:
    """One ledger row: where it came from, where it went, how big, and its digest.

    ``InputEntry.to_dict`` is the whole payload — there is no content field to
    accidentally include — plus the time headspace recorded it, which is what
    lets a reader tell a ledger row from before a job's last measurement from one
    written after it.
    """
    return {**entry.to_dict(), "recorded_at": utc_now()}


def _merged_ledger(
    record: Mapping[str, Any], landed: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The ledger with ``landed`` merged in, one row per destination, sorted.

    Sorted by destination for the same reason
    :func:`~headspace.core.inputs.expand_input` sorts its manifest: two runs over
    the same payload should produce identical records, so a diff between two
    state files is a diff of the workspace rather than of iteration order.
    """
    rows = {str(row.get("destination", "")): dict(row) for row in _ledger(record)}
    for row in landed:
        rows[str(row.get("destination", ""))] = dict(row)
    return [rows[destination] for destination in sorted(rows)]


def _storage_remaining(record: Mapping[str, Any], policy: EffectivePolicy) -> int:
    """Storage budget left for a copy-in, from the two numbers that are honest.

    The engine's measurement is the truth about the volume, but it is only
    refreshed when a job runs — so a workspace that has never run one measures
    zero however much has been copied into it. The ledger is headspace's own
    record of what it pushed in, but says nothing about what jobs wrote. Neither
    alone is the consumption; the larger of the two is the best lower bound
    available without asking the engine, and asking the engine here would mean an
    engine call before the journal, which is the one ordering this module does
    not allow itself.

    That makes this a fail-fast guard, not the enforcement boundary — storage is
    the canonical *measured* limit (see :mod:`headspace.core.policy`), and a
    result package says so in its warnings. What it does buy is the case that
    actually happens: two large copy-ins into a fresh workspace, where the second
    is refused on the host instead of half-filling a volume no one is capping.
    """
    budget = int(requested_limit(policy, "storage"))
    descriptor = _stored_descriptor(record)
    measured = descriptor.storage_bytes if descriptor is not None else 0
    return max(0, budget - max(measured, _ledger_total(record)))


def _expand_inputs(requests: Sequence[InputRequest]) -> InputManifest:
    """Expand every ``--input`` into one manifest, refusing two that collide.

    Each request is expanded on its own — :func:`~headspace.core.inputs.expand_input`
    is what decides whether a host path is a file, a directory or something a
    copy-in refuses — and the results are merged and re-sorted by destination, so
    a caller who writes the same two flags in the other order gets the same
    manifest, the same journal entry and the same ledger.

    Two requests that land at one destination are refused *here*, before the
    journal. The copy-in would refuse them too, at the second file, having
    already written the first — a half-done copy for a mistake that was fully
    visible before anything moved. The refusal names the destination, which is
    a path the caller typed, and neither host file's contents.
    """
    entries: list[InputEntry] = []
    claimed: set[str] = set()
    for request in requests:
        manifest = expand_input(request.host_path, request.destination)
        if not manifest.entries:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"input '{os.fspath(request.host_path)}' holds no regular files",
                remediation=(
                    "point --input at a file, or at a directory that contains one; a copy "
                    "that moves nothing is a mistake, not an empty success"
                ),
            )
        for entry in manifest:
            if entry.destination in claimed:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=(
                        f"two inputs both land at '{entry.destination}', and nothing " "was copied"
                    ),
                    remediation=(
                        "give each --input its own destination; headspace refuses rather "
                        "than letting one payload silently overwrite the other"
                    ),
                )
            claimed.add(entry.destination)
            entries.append(entry)
    entries.sort(key=lambda entry: entry.destination)
    return InputManifest(tuple(entries))


def _run_intent(command: Sequence[str], job_id: str, environment: JobEnvironment) -> dict[str, Any]:
    """The ``run`` intent's journal payload: argv, job id, and env *names*.

    ``command`` is journalled verbatim, as it always was — see the module
    docstring on why there is no redaction pass over argv. The environment is
    journalled by :attr:`JobEnvironment.names` and by the paths any values were
    read from, and by nothing else.
    """
    return {"command": list(command), "job_id": job_id, **_env_record(environment)}


def _env_record(environment: JobEnvironment) -> dict[str, Any]:
    """The persistable form of a job's environment: names and source paths.

    One function, used by both durable surfaces (the journal entry and the state
    record's job row), so the two cannot come to disagree about what a job was
    handed. Empty for a job that forwarded nothing, which is what keeps a
    no-environment record identical to one written before the flag existed.
    """
    record: dict[str, Any] = {}
    if environment.names:
        record["env"] = list(environment.names)
    if environment.sources:
        record["env_files"] = list(environment.sources)
    return record


def _env_findings(environment: JobEnvironment) -> list[str]:
    """What the caller is told about the environment their job ran with."""
    if not environment:
        return []
    findings = []
    if environment.names:
        findings.append(
            f"the job was handed {len(environment.names)} environment variable(s) by name: "
            + ", ".join(environment.names)
            + " — headspace records the names and never the values"
        )
    findings.extend(
        f"names were read from the env-file {path}, whose contents are not recorded"
        for path in environment.sources
    )
    return findings


def _env_provenance(environment: JobEnvironment) -> list[str]:
    """The environment's lineage lines: one per name, one per file it came from."""
    return [f"env {name} (name only; no value is recorded)" for name in environment.names] + [
        f"env-file {path} (names only; no value is recorded)" for path in environment.sources
    ]


def _env_warnings(environment: JobEnvironment) -> list[str]:
    """The edge of the no-value guarantee, stated on every run that relies on it.

    headspace records no value of its own. It cannot make the same promise about
    the job: a command that prints its environment — ``env``, ``printenv``, a
    traceback that dumps ``os.environ`` — writes the value into its captured
    output, and captured output is kept, in the result package and in the job's
    row in ``state.json``. A guarantee that did not say so would be read as
    covering a case it does not cover, which is worse than the gap itself.
    """
    if not environment.names:
        return []
    return [
        "headspace records the names of forwarded environment variables and never their "
        "values — but a job that prints its own environment (env, printenv, a traceback) "
        "writes the value into its captured output, which is kept"
    ]


def _bounded_entries(manifest: InputManifest) -> list[InputEntry]:
    """Re-express every destination in the seam's own path grammar, before the journal.

    :func:`~headspace.core.inputs.expand_input` already refuses an absolute or
    escaping destination, but :func:`~headspace.providers.base.require_workspace_path`
    is the boundary every backend is held to, and normalising here means the
    path written into the journal and the ledger is the same string the engine is
    asked for — not one that a backend silently canonicalised on the way in.
    """
    return [
        dataclasses.replace(entry, destination=require_workspace_path(entry.destination))
        for entry in manifest
    ]


def _open_input(entry: InputEntry) -> BinaryIO:
    """Open one input's bytes, turning a host I/O failure into a named error.

    The file was read once already, to measure it. If it cannot be read now, the
    host changed underneath the copy — an environment fact, exit 2 — and it has
    to arrive as a :class:`CliError` so the copy-in's caller closes its intent on
    the way out rather than unwinding as an unhandled ``OSError``.

    Opened ``O_NOFOLLOW``, matching how
    :func:`headspace.core.inputs.expand_input` measured it. The two opens have to
    agree: measuring with no-follow and then streaming through a plain open would
    leave a window where the path becomes a symlink between the digest and the
    read, and the bytes that travel would not be the bytes that were hashed. The
    digest is re-verified downstream, so a swap is *caught* rather than
    published — but "caught" is the wrong place to rely on when refusing to
    follow the link costs one flag, and a refusal names the real problem instead
    of surfacing as a digest mismatch that blames the transfer.
    """
    try:
        # Not `Path.open()`: it has no way to ask for O_NOFOLLOW.
        return os.fdopen(os.open(entry.source, os.O_RDONLY | _O_NOFOLLOW), "rb")
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot read host input '{entry.source}': {err.strerror or err}",
            remediation=(
                "the file was readable when it was measured and is not now — check it still "
                "exists and is readable, then re-run the copy-in"
            ),
        ) from err


def _landed_bytes(landed: Sequence[Mapping[str, Any]]) -> int:
    return sum(int(row.get("size_bytes", 0)) for row in landed)


def _copy_in_summary(
    workspace_id: str, host_path: str | os.PathLike[str], landed: Sequence[Mapping[str, Any]]
) -> str:
    """What a copy-in reports it did, in paths and digests.

    A single file names its digest here, because a one-file copy-in is the common
    case and the digest is the fact a caller checks. A multi-file copy-in names
    the count and total instead and leaves the per-file digests to the key
    findings — a summary that listed a thousand digests would be a transcript,
    which is the thing this whole package exists not to be.
    """
    if len(landed) == 1:
        row = landed[0]
        return (
            f"copied {row['source']} into workspace {workspace_id} as '{row['destination']}' "
            f"({row['size_bytes']} bytes, sha256:{row['sha256']})"
        )
    return (
        f"copied {len(landed)} file(s) totalling {_landed_bytes(landed)} bytes from "
        f"{os.fspath(host_path)} into workspace {workspace_id}; every destination and digest "
        "is recorded in the workspace's inputs ledger"
    )


def _run_summary(
    workspace_id: str,
    job_id: str,
    command: Sequence[str],
    outcome: JobOutcome,
    landed: Sequence[Mapping[str, Any]],
    environment: JobEnvironment,
) -> str:
    """One sentence for a job: what ran, what it was given, and how it ended.

    The command is rendered verbatim, as it always has been. The copy-in is named
    by count and volume — the per-file digests are in the key findings, and a
    summary that listed a thousand of them would be a transcript. The environment
    is named by :attr:`JobEnvironment.names`, in brackets, so that the *shape* a
    caller grepping this line is looking for (``env: [COLLEAGUE_API_KEY]``) is
    unmistakably a list of names and could not be mistaken for an assignment.
    """
    summary = (
        f"job {job_id} ran {' '.join(command)} in workspace {workspace_id} "
        f"and reported {outcome.status}"
    )
    if landed:
        summary += (
            f"; {len(landed)} file(s) totalling {_landed_bytes(landed)} bytes "
            "were copied in first"
        )
    if environment.names:
        summary += "; env: [" + ", ".join(environment.names) + "]"
    return summary


def _copied_findings(landed: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per copied file, bounded, with the remainder named as a remainder.

    Empty for a verb that copied nothing — ``run`` calls this unconditionally, and
    a job with no ``--input`` must not be told anything about inputs, least of all
    the note about what a destroy does with them.
    """
    if not landed:
        return []
    findings = [
        f"{row['destination']} <- {row['source']} ({row['size_bytes']} bytes, "
        f"sha256:{row['sha256']})"
        for row in landed[:MAX_RENDERED_INPUTS]
    ]
    if len(landed) > MAX_RENDERED_INPUTS:
        findings.append(
            f"and {len(landed) - MAX_RENDERED_INPUTS} more file(s), each recorded in the "
            "inputs ledger with its own path and digest"
        )
    findings.append("copied-in files are inputs, not artifacts: they do not gate a destroy")
    return findings


def _input_provenance(landed: Sequence[Mapping[str, Any]]) -> list[str]:
    """The copy-in's lineage line per file: destination, digest, size, host source."""
    return [
        f"{row['destination']} (sha256:{row['sha256']}, {row['size_bytes']} bytes, "
        f"from {row['source']})"
        for row in landed
    ]


def _ledger_findings(record: Mapping[str, Any]) -> list[str]:
    """The one-line ledger summary a read-only verb reports, distinct from artifacts."""
    ledger = _ledger(record)
    if not ledger:
        return []
    return [
        f"{len(ledger)} copied-in input file(s) recorded, {_ledger_total(record)} bytes — "
        "inputs are not artifacts and do not gate a destroy"
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


def _removal_findings(disposition: RemovalDisposition, path: Sequence[State]) -> list[str]:
    """The destruction report's buckets, with an empty one named rather than blank.

    Every bucket is rendered even when nothing fell into it. A line that just
    stops after the colon reads as "not checked", and the whole point of the
    report is that each resource named was verified one way or the other.
    """
    buckets = (
        ("removed", disposition.removed),
        ("retained", disposition.retained),
        ("unverified", disposition.unverified),
    )
    findings = [f"{bucket}: {', '.join(names) or '(none)'}" for bucket, names in buckets]
    findings.append("lifecycle path: " + " -> ".join(target.value for target in path))
    return findings


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


def _memory_ceiling(policy: EffectivePolicy) -> int:
    """The memory limit the workspace was created under, in bytes.

    Read from the effective policy — the ceiling headspace asked the host to
    *enforce* — and never from a measurement. The two are different numbers and
    the whole point of naming this one is that it is the one the job hit.
    """
    return int(requested_limit(policy, "memory"))


def _job_findings(
    outcome: JobOutcome,
    *,
    policy: EffectivePolicy,
    profile: str,
    command: Sequence[str],
) -> list[str]:
    """How the job ended, in the terms that change what the caller does next.

    Four shapes, in the order a reader needs them:

    * no exit status at all — the command was stopped, and silence is not
      success;
    * stopped for exceeding a ceiling — name the ceiling, in bytes, because
      "exit status 137" alone leaves an agent to rerun an identical job;
    * a command the environment could not run — name the profile and the
      caller's own ``argv[0]``, built from what headspace knows rather than
      from whatever sentence the engine produced (which carries engine handles
      and is exactly the context pollution headspace exists to prevent);
    * anything else — an ordinary computation reporting its own answer, which
      needs no interpretation and gets none.
    """
    if outcome.exit_status is None:
        return [
            f"the job reported {outcome.status} and never produced an exit status",
            "no exit status means the command was stopped, not that it succeeded",
        ]
    if outcome.status == STATUS_RESOURCE_EXHAUSTED:
        return [
            "the job was killed for exceeding the workspace's enforced memory ceiling of "
            f"{_memory_ceiling(policy)} bytes",
            f"exit status {outcome.exit_status} reports that kill, not an answer the "
            "command chose",
        ]
    # Gated on what the backend OBSERVED, never on the number alone. A command
    # that really ran can return 126 or 127 itself — `/bin/sh -c missing-tool`
    # runs the shell perfectly and returns 127 about a name the shell looked
    # for — and naming argv[0] there sends the caller to fix the wrong thing.
    diagnosis = (
        _UNRUNNABLE_COMMAND_DIAGNOSES.get(outcome.exit_status) if outcome.command_refused else None
    )
    if diagnosis is not None:
        argv0 = command[0] if command else ""
        return [
            f"the {profile} profile could not run '{argv0}': the command "
            f"{diagnosis} (exit status {outcome.exit_status})"
        ]
    return [f"the command completed with exit status {outcome.exit_status}"]


def _resource_attention(outcome: JobOutcome, policy: EffectivePolicy) -> list[str]:
    """The decision a job killed at its memory ceiling leaves for its caller.

    Empty for every other outcome. A ``resource_exhausted`` result with an empty
    attention section is the defect this exists to close: the caller is told the
    job died, given nothing to change, and reruns a job that dies identically.

    The remedy names the flag rather than gesturing at it, and says where the
    flag lives — ``--memory-bytes`` is a ``create`` flag, because a workspace
    runs under the contract it was created with (see cli/_commands/run.py), so
    "raise it and retry here" would be advice that cannot be followed.
    """
    if outcome.status != STATUS_RESOURCE_EXHAUSTED:
        return []
    return [
        "raise the memory ceiling — create a workspace with --memory-bytes above "
        f"{_memory_ceiling(policy)} — or reduce the job's working set; this workspace "
        "keeps the ceiling it was created under, so an unchanged rerun here dies the same way"
    ]


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
