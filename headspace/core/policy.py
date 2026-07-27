"""Policy and budget model: closed-by-default access, enforced-vs-measured honesty.

WHY this exists: the product's security posture is closed-by-default (NFR-02)
-- a workspace starts with no network access and a small, explicit resource
budget, and access only expands through a caller's declared policy. But a
declared limit is only as real as the host's ability to apply it. Docker's
default local volume driver, for instance, *reports* disk usage without
*capping* it. Treating that report as a cap would be exactly the
silent-weakening bug the product exists to avoid -- discovered only when an
agent's job fills the disk. So this module keeps two things separate:

* :class:`Policy` -- what a caller *declares* they want: a network posture,
  a filesystem scope, and a resource budget (memory, cpu, pids, storage,
  wall-clock time, output bytes, concurrency). Every default is the CLOSED
  one: network disabled, no extra filesystem scope, and small explicit
  budget ceilings (never "unlimited").
* :class:`CapabilitySnapshot` -- plain data describing what the host engine
  can *actually* enforce. A sibling task (the Docker provider) probes the
  engine once at create time and hands one of these in; this module only
  ever consumes it as a value object. Per the layering rule in
  ``headspace/core/__init__.py``, this module never imports a provider or
  the docker SDK -- it has no idea Docker exists.

:func:`resolve` combines the two into an :class:`EffectivePolicy`: every
limit is labelled ``enforced`` (the host will really apply it) or
``measured`` (the host only observes/reports it). Storage on the default
local volume driver is the canonical ``measured`` case -- it is reported,
never a hard failure. Everything else that guards the isolation boundary
(network, filesystem scope, memory, cpu, pids) is different: if the snapshot
cannot really back a requested limit there, :func:`resolve` raises
:class:`PolicyError` before returning -- fail closed, never silently
degrade or weaken the requested limit to make it fit. Wall-clock time,
output bytes, and concurrency are enforced by headspace's own process
regardless of the engine, so they are always ``enforced``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from headspace.cli._errors import EXIT_POLICY_DENIED, CliError

# ---------------------------------------------------------------------------
# Declared policy -- what the caller asks for. Every default is CLOSED.
# ---------------------------------------------------------------------------


class NetworkPosture(enum.Enum):
    """Whether a workspace may reach the network. Closed default: DISABLED."""

    DISABLED = "disabled"
    ENABLED = "enabled"


@dataclass(frozen=True)
class FilesystemScope:
    """What a workspace may see on disk, beyond its own bounded workspace volume.

    Closed default: no extra host paths. The workspace volume itself is
    always present and is not part of this scope -- every entry here is an
    explicit policy expansion. For the MVP Docker provider no host mounts
    are supported at all (jobs never receive a host bind mount), so in
    practice any non-empty scope will fail closed against a real
    :class:`CapabilitySnapshot` -- that is intended, not a bug to work
    around by weakening the request.
    """

    host_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceBudget:
    """Numeric ceilings a workspace may consume, in canonical units.

    Every field is a hard cap, never ``None`` / "unlimited" -- the
    closed-by-default posture applies to budgets too: a caller that never
    asked for a limit still gets a small, explicit one instead of an
    unbounded workspace.
    """

    memory_bytes: int = 512 * 1024 * 1024  # 512 MiB
    cpu_limit: float = 1.0  # cores
    pids_limit: int = 128
    storage_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    wall_clock_seconds: int = 300  # 5 minutes
    output_bytes: int = 10 * 1024 * 1024  # 10 MiB of captured output
    concurrency: int = 1  # concurrent jobs


@dataclass(frozen=True)
class Policy:
    """A caller's declared access request. Defaults are the CLOSED ones."""

    network: NetworkPosture = NetworkPosture.DISABLED
    filesystem: FilesystemScope = field(default_factory=FilesystemScope)
    budget: ResourceBudget = field(default_factory=ResourceBudget)


# ---------------------------------------------------------------------------
# Capability snapshot -- pure data describing what the host can enforce.
# Produced and persisted by a sibling task (the Docker provider); this
# module only defines the shape and consumes it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySnapshot:
    """What the host engine can actually enforce, probed once at create time.

    Pure data -- a value object. A sibling task (the Docker provider) builds
    one of these from an engine version/info handshake (API version, cgroup
    controllers for memory/cpu/pids) and persists it in workspace state.
    This module never talks to Docker or any other provider; it treats the
    snapshot as an opaque fact about the host.

    Every ``*_enforceable`` field answers "can the host really apply this
    limit", not "did the caller ask for it". ``storage_enforceable``
    defaults to ``False`` because that is the canonical case today: Docker's
    default local volume driver reports storage usage without capping it.
    """

    engine: str
    api_version: str
    network_disable_supported: bool = True
    filesystem_scope_enforceable: bool = True
    memory_enforceable: bool = True
    cpu_enforceable: bool = True
    pids_enforceable: bool = True
    storage_enforceable: bool = False


# ---------------------------------------------------------------------------
# Effective-policy view -- every limit labelled enforced or measured.
# ---------------------------------------------------------------------------

#: Canonical set of limit names the effective-policy view always covers.
#: A test iterates this to make sure a future limit can't ship unlabelled.
LIMIT_NAMES: tuple[str, ...] = (
    "network",
    "filesystem",
    "memory",
    "cpu",
    "pids",
    "storage",
    "wall_clock",
    "output_bytes",
    "concurrency",
)


class LimitStatus(enum.Enum):
    """How the host will actually apply a limit."""

    ENFORCED = "enforced"  # the host will really apply this limit
    MEASURED = "measured"  # the host only observes/reports it, never caps it


@dataclass(frozen=True)
class EffectiveLimit:
    """One line of the effective-policy view: what was requested and how the
    host will actually apply it."""

    name: str
    requested: Any
    status: LimitStatus
    detail: str = ""


@dataclass(frozen=True)
class EffectivePolicy:
    """The resolved view of a :class:`Policy` against a
    :class:`CapabilitySnapshot`: every limit labelled enforced or measured.
    """

    limits: tuple[EffectiveLimit, ...]

    def __iter__(self) -> Any:
        return iter(self.limits)

    def __len__(self) -> int:
        return len(self.limits)

    def status_of(self, name: str) -> LimitStatus:
        for limit in self.limits:
            if limit.name == name:
                return limit.status
        raise KeyError(name)


# ---------------------------------------------------------------------------
# PolicyError -- fail closed, never silently degrade.
# ---------------------------------------------------------------------------


class PolicyError(CliError):
    """Raised when a declared :class:`Policy` cannot be satisfied by a
    :class:`CapabilitySnapshot`.

    A **subclass** of :class:`~headspace.cli._errors.CliError`, not a
    look-alike. ``main()`` catches ``CliError`` by name and wraps anything
    else as ``unexpected: ... file a bug`` — so a structurally-identical but
    unrelated type would surface a legitimate policy refusal as an internal
    defect, and route it through the wrong exit code. Inheriting removes
    that translation burden from every future caller instead of relying on
    each one to remember it.

    Importing ``_errors`` does not breach the core layering rule in
    ``headspace/core/__init__.py``: that rule bars providers and the docker
    SDK, and ``_errors`` imports nothing from the rest of the package, so no
    cycle is possible. :mod:`headspace.core.states` and
    :mod:`headspace.core.profiles` take the same import.

    ``code`` defaults to :data:`~headspace.cli._errors.EXIT_POLICY_DENIED`
    (3) — the failure taxonomy's own slot. A refused policy is precisely
    what that code exists to name, and reporting it as a generic user error
    would collapse the distinction the taxonomy was built to preserve.
    """

    def __init__(
        self,
        message: str,
        remediation: str = "",
        code: int = EXIT_POLICY_DENIED,
    ) -> None:
        super().__init__(code=code, message=message, remediation=remediation)


def resolve(policy: Policy, snapshot: CapabilitySnapshot) -> EffectivePolicy:
    """Resolve a declared :class:`Policy` against what the host can actually do.

    Fails closed: raises :class:`PolicyError` -- before returning anything,
    i.e. before any job runs -- if the snapshot cannot really back a
    requested limit that guards the isolation boundary (network, filesystem
    scope, memory, cpu, pids). Never weakens the requested limit to make it
    fit; the caller gets an explicit refusal with a remediation hint naming
    exactly what the host could not satisfy, not a quietly smaller budget.

    Storage is different by design: the default local volume driver reports
    usage without capping it, so storage is always reported as ``enforced``
    or ``measured`` -- never a resolution failure.

    Wall-clock time, output bytes, and concurrency are enforced by
    headspace's own process (a timeout around the job, a cap on captured
    output, a scheduling semaphore) independent of the engine, so they are
    always ``enforced`` regardless of what the snapshot says.
    """
    limits: list[EffectiveLimit] = []
    unsatisfied: list[str] = []

    # network: only *disabling* it is a claim the host has to back up --
    # enabling it has nothing to violate.
    if policy.network is NetworkPosture.DISABLED and not snapshot.network_disable_supported:
        unsatisfied.append(
            f"network disabled (engine {snapshot.engine} cannot guarantee network isolation)"
        )
    else:
        limits.append(EffectiveLimit("network", policy.network.value, LimitStatus.ENFORCED))

    # filesystem: an empty scope (the default) needs nothing from the host;
    # only an explicit expansion requires bind-mount support.
    if policy.filesystem.host_paths and not snapshot.filesystem_scope_enforceable:
        unsatisfied.append(
            f"filesystem scope {policy.filesystem.host_paths} "
            f"(engine {snapshot.engine} cannot restrict bind mounts)"
        )
    else:
        limits.append(
            EffectiveLimit("filesystem", policy.filesystem.host_paths, LimitStatus.ENFORCED)
        )

    # engine-enforced resource limits: fail closed if the host truly cannot
    # cap them -- an unenforced memory/cpu/pids ceiling is a security gap,
    # not a cosmetic shortfall.
    resource_checks = (
        ("memory", policy.budget.memory_bytes, snapshot.memory_enforceable),
        ("cpu", policy.budget.cpu_limit, snapshot.cpu_enforceable),
        ("pids", policy.budget.pids_limit, snapshot.pids_enforceable),
    )
    for name, requested, enforceable in resource_checks:
        if enforceable:
            limits.append(EffectiveLimit(name, requested, LimitStatus.ENFORCED))
        else:
            unsatisfied.append(
                f"{name} limit {requested} (engine {snapshot.engine} cannot enforce it)"
            )

    # storage: the canonical measured case -- reported, not capped, and
    # never a hard failure either way.
    if snapshot.storage_enforceable:
        storage_status = LimitStatus.ENFORCED
        storage_detail = ""
    else:
        storage_status = LimitStatus.MEASURED
        storage_detail = "reported by the engine, not capped (default local volume driver)"
    limits.append(
        EffectiveLimit("storage", policy.budget.storage_bytes, storage_status, storage_detail)
    )

    # CLI-side limits: headspace enforces these itself, independent of the
    # engine, so they never depend on the snapshot and never fail here.
    cli_detail = "enforced by headspace's own process, independent of the engine"
    for name, requested in (
        ("wall_clock", policy.budget.wall_clock_seconds),
        ("output_bytes", policy.budget.output_bytes),
        ("concurrency", policy.budget.concurrency),
    ):
        limits.append(EffectiveLimit(name, requested, LimitStatus.ENFORCED, cli_detail))

    if unsatisfied:
        joined = "; ".join(unsatisfied)
        raise PolicyError(
            message=f"policy cannot be satisfied by {snapshot.engine}: {joined}",
            remediation=(
                f"host cannot satisfy: {joined} -- request a lower limit or use a "
                "host/engine that supports it"
            ),
        )

    return EffectivePolicy(tuple(limits))
