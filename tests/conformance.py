"""The provider conformance suite: one behavioural contract, any backend.

WHY this module exists
----------------------
headspace's central promise is that a caller gets identical lifecycle and
result semantics no matter what executes the work. Docker is merely the first
backend. A promise like that cannot live in prose — every backend has to be
held to the *same executable* description of the seam, or the second backend
quietly redefines it. This module is that description: a pytest mixin whose
``test_*`` methods exercise :class:`headspace.providers.base.Provider` through
its six verbs and assert the normalized structures that come back, without
ever naming a backend.

It is deliberately **not** collected on its own. The file is named
``conformance.py`` rather than ``test_conformance.py`` and the class is named
``ProviderConformance`` rather than ``TestConformance``, so pytest ignores it
until a per-backend module subclasses it. The suite runs once per binding.

Binding a provider to this suite — follow this literally
--------------------------------------------------------
Create ``tests/test_provider_<backend>.py``. **Do not edit this module.**

    from __future__ import annotations

    import pytest

    from headspace.core import profiles
    from headspace.providers.docker import DockerProvider
    from tests.conformance import ProviderCase, ProviderConformance


    class TestDockerProviderConformance(ProviderConformance):
        @pytest.fixture
        def provider_case(self) -> ProviderCase:
            provider = DockerProvider()          # or pytest.skip(...) if no engine
            return ProviderCase(
                provider=provider,
                environment=profiles.resolve(profiles.DEFAULT_PROFILE),
                succeeding_command=("/bin/sh", "-c", "echo headspace-conformance"),
                echo_text="headspace-conformance",
                failing_command=("/bin/sh", "-c", "exit 7"),
                failing_exit_status=7,
                absent_command=("definitely-not-a-binary",),
                absent_exit_status=127,
                unrunnable_command=("/etc/hostname",),
                unrunnable_exit_status=126,
                memory_hungry_command=("python", "-c", "x = bytearray(512 * 1024 * 1024)"),
                memory_ceiling_bytes=128 * 1024 * 1024,
                slow_command=("sleep", "30"),
                slow_seconds=1,
                flooding_command=("/bin/sh", "-c", "yes headspace | head -c 200000"),
                flood_budget_bytes=4096,
                writing_command=(
                    "/bin/sh", "-c", "yes headspace | head -c 8192 > /workspace/artifact.bin"
                ),
                artifact_path="artifact.bin",
                artifact_bytes=(b"headspace\n" * 820)[:8192],
                break_engine=None,               # or a callable, see ProviderCase
                workspace_prefix="conf-docker",
            )

The four binding rules:

1. The subclass MUST be named ``Test*`` — pytest only collects ``Test``-prefixed
   classes, and the suite's methods are inherited, not copied.
2. The subclass MUST override exactly one fixture, ``provider_case``, returning
   a :class:`ProviderCase`. Nothing else is overridable.
3. The subclass MUST NOT redefine any ``test_*`` method.
   :meth:`ProviderConformance.test_suite_methods_are_not_overridden` fails the
   run if it does — "reuse the suite unchanged" is enforced, not requested.
4. To skip a whole binding (no engine on this host), call :func:`pytest.skip`
   *inside* the ``provider_case`` fixture. Skipping individual behaviours is
   done by leaving the optional :attr:`ProviderCase.break_engine` hook at
   ``None``; every other field is required because every other behaviour is
   mandatory.

The backend-neutrality scanner
------------------------------
Acceptance criterion 2 ("no provider-specific field appears in any structure
the interface returns") is checked mechanically, not promised.
:func:`walk_structure` walks every returned structure and
:meth:`ProviderConformance.test_returned_structures_are_backend_neutral`
asserts:

* every leaf is a JSON primitive — a docker SDK object cannot survive the walk;
* no key contains a backend-mechanics word (:data:`BANNED_KEY_WORDS`) or
  compound name (:data:`BANNED_KEY_COMPOUNDS`);
* no string value headspace itself filled in contains a backend-mechanics
  substring (:data:`BANNED_VALUE_SUBSTRINGS`) and none is a bare 64-hex engine
  id — caller payload passing through (:data:`PAYLOAD_KEYS`) is exempt, since a
  job that prints a socket path leaked nothing but its own output;
* the one algorithm-prefixed content digest allowed anywhere,
  ``environment_digest``, matches
  :data:`~headspace.providers.base.ENVIRONMENT_DIGEST_RE` and appears nowhere
  else;
* whatever a backend hides in its :class:`~headspace.providers.base.OpaqueRef`
  appears in no other position of any structure — an opaque handle that is also
  echoed into a readable field is not opaque.

Two deliberate non-bans, because a reviewer will ask:

* The *name* of a backend is not a leak. ``CapabilitySnapshot.engine`` exists
  precisely to record it, and ``WorkspaceDescriptor.provider`` routes
  reconciliation. The scanner bans docker's *data model* (``HostConfig``,
  ``NetworkMode``, socket paths, container ids), never the word "docker" on its
  own.
* :class:`~headspace.core.policy.CapabilitySnapshot` is a core-owned frozen
  dataclass whose two free-form fields exist to name the engine and its API
  version. It is shape-checked rather than value-scanned; a backend cannot add
  a field to it, and naming itself is its job.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from headspace.cli._errors import EXIT_INFRASTRUCTURE_FAILURE, EXIT_USER_ERROR, CliError
from headspace.core.policy import (
    CapabilitySnapshot,
    EffectivePolicy,
    NetworkPosture,
    Policy,
    ResourceBudget,
)
from headspace.core.policy import resolve as resolve_policy
from headspace.core.result import (
    STATUS_FAILURE,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
)
from headspace.core.states import State
from headspace.providers.base import (
    ENVIRONMENT_DIGEST_RE,
    JOB_STATUSES,
    OPAQUE_KEY,
    REMOVABLE_RESOURCES,
    ByteStream,
    JobOutcome,
    Provider,
    ProviderError,
    RemovalDisposition,
    WorkspaceDescriptor,
)

#: The chunk size the read-back tests ask for. Deliberately small, and
#: deliberately not any backend's natural granularity: a backend that ignored it
#: and handed back one object would fail the bounded-chunk assertion rather than
#: pass it by accident. :attr:`ProviderCase.artifact_bytes` must be larger than
#: this, or "it arrived in more than one chunk" would be unprovable.
READ_CHUNK_BYTES = 1024

# --- the binding value object ----------------------------------------------


@dataclass(frozen=True)
class ProviderCase:
    """Everything the suite needs to exercise one backend.

    The suite states *behaviours*; a backend supplies the commands that realise
    them on its own runtime. That split is what lets one suite run against an
    in-memory fake and a live engine without a single ``if backend ==`` branch.
    """

    #: The implementation under test. Must satisfy :class:`Provider`.
    provider: Provider
    #: A resolvable environment reference — what ``profiles.resolve()`` returns.
    environment: str
    #: A command that exits 0 and writes :attr:`echo_text` to its output.
    succeeding_command: Sequence[str]
    #: The text :attr:`succeeding_command` emits, asserted in captured output.
    echo_text: str
    #: A command that exits with :attr:`failing_exit_status` (non-zero).
    failing_command: Sequence[str]
    failing_exit_status: int
    #: A command nothing in this environment provides — a typo, or a tool that
    #: was never installed. It never runs at all, which is the caller's mistake
    #: and never the engine's, so it has an exit status like any other failure.
    absent_command: Sequence[str]
    absent_exit_status: int
    #: A path this environment *does* hold but cannot execute — no execute bit,
    #: a directory, a data file. Supplied separately from :attr:`absent_command`
    #: and asserted to report a different status, because the two send a caller
    #: to different fixes: only one of them is a spelling to correct.
    unrunnable_command: Sequence[str]
    unrunnable_exit_status: int
    #: A command that tries to hold more memory than :attr:`memory_ceiling_bytes`
    #: at once, and is therefore stopped at the ceiling instead of finishing.
    memory_hungry_command: Sequence[str]
    #: The memory ceiling to declare so that happens. Must be low enough that
    #: :attr:`memory_hungry_command` really exceeds it and high enough that the
    #: environment can start at all.
    memory_ceiling_bytes: int
    #: A command that runs longer than :attr:`slow_seconds` wall-clock.
    slow_command: Sequence[str]
    slow_seconds: int
    #: A command whose output exceeds :attr:`flood_budget_bytes`.
    flooding_command: Sequence[str]
    flood_budget_bytes: int
    #: A command that writes :attr:`artifact_bytes` into the workspace, at
    #: :attr:`artifact_path`. Read-back is exercised against what a *job* wrote,
    #: never against content a test placed behind the backend's back — an
    #: artifact nobody produced proves nothing about artifacts leaving.
    writing_command: Sequence[str]
    #: Where :attr:`writing_command` leaves it, relative to the workspace root.
    artifact_path: str
    #: The exact bytes it writes. MUST be longer than :data:`READ_CHUNK_BYTES`,
    #: because "it arrived in bounded chunks" is unprovable for an artifact that
    #: fits in one.
    artifact_bytes: bytes
    #: Optional hook that arms the backend so the NEXT engine call — the suite
    #: uses it for ``run`` and for ``read`` — raises :class:`ProviderError`.
    #: ``None`` skips the taxonomy tests: a live engine cannot always be broken
    #: on demand, and faking the break would test the fake, not the provider.
    break_engine: Callable[[], None] | None = None
    #: Prefix for generated workspace ids, so a failed run is traceable.
    workspace_prefix: str = "conformance"

    def workspace_id(self) -> str:
        """A fresh workspace id; store-compatible characters only."""
        return f"{self.workspace_prefix}-{uuid.uuid4().hex[:12]}"


# --- the mechanical backend-neutrality scanner ------------------------------

#: Words that must not appear as a WORD in any key of a returned structure.
#: These name engine mechanics, not engines: a field called ``container_id``
#: forces every layer above to learn docker's data model, which is exactly the
#: leak this seam exists to prevent.
#:
#: Matched per word rather than per substring, because raw substring matching
#: is wrong in both directions — it flags ``truncated`` for containing "runc"
#: while still missing ``HostConfig``. :func:`key_words` does the splitting,
#: snake_case and camelCase alike.
BANNED_KEY_WORDS: frozenset[str] = frozenset(
    {
        "attach",
        "bind",
        "binds",
        "cgroup",
        "client",
        "container",
        "containerd",
        "containers",
        "daemon",
        "docker",
        "entrypoint",
        "hostconfig",
        "image",
        "label",
        "labels",
        "layer",
        "layers",
        "mount",
        "mounts",
        "namespace",
        "oci",
        "runc",
        "sdk",
        "sock",
        "socket",
        "tty",
        "volume",
        "volumes",
    }
)

#: Compound names checked against the key with separators stripped, so
#: ``HostConfig``, ``host_config`` and ``hostconfig`` are all one rule.
BANNED_KEY_COMPOUNDS: tuple[str, ...] = (
    "containerid",
    "dockersock",
    "execid",
    "graphdriver",
    "hostconfig",
    "imageid",
    "logconfig",
    "networkmode",
    "portbinding",
    "repotag",
    "restartpolicy",
    "securityopt",
)

#: Substrings that must not appear in any string VALUE headspace itself fills
#: in. Every entry is a piece of a backend's *implementation surface* — a path,
#: a wire format, a config fragment. The bare vendor name is absent on purpose
#: (see the module docstring).
BANNED_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "/containers/",
    "/proc/self/",
    "/var/lib/docker",
    "/var/run/docker",
    "apparmor",
    "cgroup",
    "containerd",
    "docker.io",
    "docker.sock",
    "hostconfig",
    "networkmode",
    "npipe://",
    "overlay2",
    "seccomp",
    "unix://",
)

#: Keys whose value is the caller's own payload passing through, not a fact
#: headspace stated about the backend. A job that prints ``/var/run/docker.sock``
#: to stdout has leaked nothing — it printed its own text — so these are
#: type-checked and opacity-checked but never substring-scanned. Scanning them
#: would make the criterion-2 test fail on job content, which is both a false
#: positive and an invitation to weaken the real rules to silence it.
PAYLOAD_KEYS: frozenset[str] = frozenset({"output"})

#: A bare 64-hex string is an engine object id (container id, image id) with the
#: algorithm prefix stripped off — the most common way a backend id sneaks
#: through a "just a string" field.
_BARE_ENGINE_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: The only key allowed to hold an algorithm-prefixed content digest.
_DIGEST_KEY = "environment_digest"

_JSON_LEAF_TYPES = (str, int, float, bool)


@dataclass
class StructureWalk:
    """What :func:`walk_structure` found: readable scalars and opaque tokens."""

    #: ``(dotted path, value)`` for every leaf outside an opaque subtree.
    scalars: list[tuple[str, Any]] = field(default_factory=list)
    #: Every value hidden behind an :data:`OPAQUE_KEY`, never interpreted.
    opaque: list[str] = field(default_factory=list)
    #: Human-readable violations; empty means the structure is neutral.
    violations: list[str] = field(default_factory=list)


def key_words(key: str) -> set[str]:
    """Split a field name into words, snake_case and camelCase alike."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return {word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word}


def walk_structure(
    node: Any, where: str, walk: StructureWalk | None = None, *, payload: bool = False
) -> StructureWalk:
    """Recursively inspect one returned structure for backend leakage.

    Collects rather than asserts, so a single test can report every violation
    across every structure at once instead of stopping at the first.
    """
    walk = walk if walk is not None else StructureWalk()
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                walk.violations.append(f"{where}: non-string key {key!r}")
                continue
            path = f"{where}.{key}"
            if key == OPAQUE_KEY:
                # The sanctioned escape hatch. Not scanned, because the caller
                # never interprets it — but recorded, because it must not be
                # echoed anywhere a caller *would* read.
                walk.opaque.append(str(value))
                continue
            for word in sorted(key_words(key) & BANNED_KEY_WORDS):
                walk.violations.append(f"{path}: field name carries backend word {word!r}")
            collapsed = re.sub(r"[^a-z0-9]+", "", key.lower())
            for compound in BANNED_KEY_COMPOUNDS:
                if compound in collapsed:
                    walk.violations.append(f"{path}: field name carries backend name {compound!r}")
            walk_structure(value, path, walk, payload=payload or key in PAYLOAD_KEYS)
        return walk
    if isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            walk_structure(item, f"{where}[{index}]", walk, payload=payload)
        return walk
    if node is None or isinstance(node, _JSON_LEAF_TYPES):
        walk.scalars.append((where, node))
        if not payload:
            _check_scalar(where, node, walk)
        return walk
    walk.violations.append(f"{where}: leaf of non-JSON type {type(node).__name__}")
    return walk


def _check_scalar(path: str, value: Any, walk: StructureWalk) -> None:
    if not isinstance(value, str):
        return
    lowered = value.lower()
    for banned in BANNED_VALUE_SUBSTRINGS:
        if banned in lowered:
            walk.violations.append(f"{path}: value carries backend substring {banned!r}")
    if _BARE_ENGINE_ID_RE.match(lowered):
        walk.violations.append(f"{path}: value looks like a bare engine object id")
    is_digest_field = path.endswith(f".{_DIGEST_KEY}")
    if ":" in value and _looks_like_digest(value) and not is_digest_field:
        walk.violations.append(f"{path}: content digest outside {_DIGEST_KEY!r}")
    if is_digest_field and value and not ENVIRONMENT_DIGEST_RE.match(value):
        walk.violations.append(f"{path}: {value!r} is not an algorithm-prefixed content digest")


def _looks_like_digest(value: str) -> bool:
    algorithm, _, rest = value.partition(":")
    return bool(algorithm) and len(rest) >= 32 and all(c in "0123456789abcdef" for c in rest)


# --- policy helper ----------------------------------------------------------


def effective_policy(
    provider: Provider,
    *,
    network: NetworkPosture = NetworkPosture.DISABLED,
    **budget: Any,
) -> EffectivePolicy:
    """Resolve a declared policy against the provider's OWN capability probe.

    Using the provider's snapshot rather than a hand-built one means the suite
    also proves a backend's self-report can actually host a default policy: a
    provider that claims it cannot enforce memory fails here, loudly, instead of
    silently running unlimited.
    """
    declared = Policy(network=network, budget=ResourceBudget(**budget))
    return resolve_policy(declared, provider.capabilities())


# --- the suite --------------------------------------------------------------


class ProviderConformance:
    """Inherit this from a ``Test*`` class and override ``provider_case`` only."""

    # --- fixtures ---------------------------------------------------------

    @pytest.fixture
    def provider_case(self) -> ProviderCase:
        """Override in a ``Test*`` subclass; see this module's docstring."""
        raise NotImplementedError(
            "bind a provider by subclassing ProviderConformance in a Test* class "
            "and overriding the `provider_case` fixture to return a ProviderCase"
        )

    @pytest.fixture
    def provider(self, provider_case: ProviderCase) -> Provider:
        return provider_case.provider

    @pytest.fixture
    def workspaces(
        self, provider_case: ProviderCase
    ) -> Iterator[Callable[..., WorkspaceDescriptor]]:
        """Factory for throwaway workspaces, torn down even when a test fails.

        Teardown suppresses :class:`CliError` on purpose: a test that already
        removed its workspace, or one that failed before creating it, must not
        turn cleanup into a second, misleading failure.
        """
        created: list[str] = []

        def make(policy: EffectivePolicy | None = None) -> WorkspaceDescriptor:
            workspace_id = provider_case.workspace_id()
            created.append(workspace_id)
            return provider_case.provider.create(
                workspace_id,
                provider_case.environment,
                policy if policy is not None else effective_policy(provider_case.provider),
            )

        yield make

        for workspace_id in reversed(created):
            with contextlib.suppress(CliError):
                provider_case.provider.remove(workspace_id)

    # --- the seam itself --------------------------------------------------

    def test_suite_methods_are_not_overridden(self) -> None:
        """A binding that redefines a conformance test is not conforming."""
        suite_tests = {name for name in vars(ProviderConformance) if name.startswith("test_")}
        for klass in type(self).__mro__:
            if klass is ProviderConformance:
                break
            clashes = sorted(suite_tests & set(vars(klass)))
            assert not clashes, (
                f"{klass.__name__} redefines conformance test(s) {clashes}; "
                "bind by overriding the `provider_case` fixture only"
            )

    def test_provider_satisfies_the_protocol(self, provider: Provider) -> None:
        assert isinstance(provider, Provider)
        assert isinstance(provider.name, str) and provider.name

    # --- capability probe -------------------------------------------------

    def test_capabilities_returns_a_core_capability_snapshot(self, provider: Provider) -> None:
        snapshot = provider.capabilities()
        assert isinstance(snapshot, CapabilitySnapshot)
        # Shape-checked, not value-scanned: naming the engine is this
        # structure's whole job (see the module docstring).
        assert {f.name for f in dataclasses.fields(snapshot)} == {
            "engine",
            "api_version",
            "network_disable_supported",
            "filesystem_scope_enforceable",
            "memory_enforceable",
            "cpu_enforceable",
            "pids_enforceable",
            "storage_enforceable",
        }
        assert snapshot.engine.strip()
        assert snapshot.api_version.strip()

    def test_capability_probe_is_stable(self, provider: Provider) -> None:
        """Two probes of one unchanged host agree — the snapshot is a fact, not a guess."""
        assert provider.capabilities() == provider.capabilities()

    # --- create -----------------------------------------------------------

    def test_create_returns_a_ready_descriptor(
        self, provider_case: ProviderCase, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        descriptor = workspaces()
        assert isinstance(descriptor, WorkspaceDescriptor)
        assert descriptor.provider == provider_case.provider.name
        assert descriptor.state is State.READY
        assert descriptor.active_jobs == 0
        assert descriptor.created_at
        assert ENVIRONMENT_DIGEST_RE.match(descriptor.environment_digest)

    def test_create_refuses_a_duplicate_workspace_id(
        self, provider_case: ProviderCase, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        descriptor = workspaces()
        policy = effective_policy(provider_case.provider)
        with pytest.raises(CliError) as caught:
            provider_case.provider.create(
                descriptor.workspace_id,
                provider_case.environment,
                policy,
            )
        assert caught.value.code == EXIT_USER_ERROR
        assert caught.value.remediation

    def test_create_honours_the_declared_network_posture(
        self, provider_case: ProviderCase, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        """Closed by default; open only when the policy actually said so."""
        closed = workspaces()
        assert closed.network_enabled is False

        opened = workspaces(
            effective_policy(provider_case.provider, network=NetworkPosture.ENABLED)
        )
        assert opened.network_enabled is True

    # --- inspect ----------------------------------------------------------

    def test_inspect_reports_the_same_workspace(
        self, provider: Provider, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        created = workspaces()
        seen = provider.inspect(created.workspace_id)
        assert seen.workspace_id == created.workspace_id
        assert seen.provider == created.provider
        assert seen.environment_digest == created.environment_digest
        assert seen.created_at == created.created_at
        assert seen.network_enabled == created.network_enabled
        assert seen.state is State.READY

    def test_inspect_of_an_unknown_workspace_is_a_user_error(
        self, provider: Provider, provider_case: ProviderCase
    ) -> None:
        unknown_workspace_id = provider_case.workspace_id()
        with pytest.raises(CliError) as caught:
            provider.inspect(unknown_workspace_id)
        assert caught.value.code == EXIT_USER_ERROR
        assert caught.value.remediation

    def test_descriptor_round_trips_through_its_serialised_form(
        self, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        """The store persists descriptors between daemonless invocations."""
        descriptor = workspaces()
        payload = descriptor.to_dict()
        assert set(payload) == {f.name for f in dataclasses.fields(WorkspaceDescriptor)}
        assert WorkspaceDescriptor.from_dict(json.loads(json.dumps(payload))) == descriptor

    # --- run --------------------------------------------------------------

    def test_run_reports_a_successful_job(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        descriptor = workspaces()
        outcome = provider.run(
            descriptor.workspace_id,
            provider_case.succeeding_command,
            effective_policy(provider),
            job_id="job-success",
        )
        assert isinstance(outcome, JobOutcome)
        assert outcome.workspace_id == descriptor.workspace_id
        assert outcome.job_id == "job-success"
        assert outcome.status == STATUS_SUCCESS
        assert outcome.exit_status == 0
        assert provider_case.echo_text in outcome.output
        assert outcome.truncated is False
        assert outcome.started_at and outcome.finished_at
        assert outcome.usage.wall_time_seconds >= 0.0
        assert outcome.usage.output_bytes >= len(outcome.output.encode("utf-8"))

    def test_run_reports_a_failing_job_without_calling_it_infrastructure(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """A job that ran and lost is a computational failure — code 6, not 7."""
        descriptor = workspaces()
        outcome = provider.run(
            descriptor.workspace_id,
            provider_case.failing_command,
            effective_policy(provider),
            job_id="job-failure",
        )
        assert outcome.status == STATUS_FAILURE
        assert outcome.exit_status == provider_case.failing_exit_status
        assert outcome.exit_status != 0

    def _ran(
        self,
        provider: Provider,
        workspace_id: str,
        command: Sequence[str],
        policy: EffectivePolicy,
        job_id: str,
    ) -> JobOutcome:
        """Run a job whose failure belongs to the *job*, and say so if it is raised.

        The taxonomy's whole load-bearing distinction is between a result and a
        fault: a returned outcome means "this is what your work did", a raised
        :class:`ProviderError` means "ask again later, the engine is broken".
        A backend that raises for one of the failures below would otherwise fail
        these tests as an error with a stack trace; caught here, it fails as the
        taxonomy violation it actually is.
        """
        try:
            return provider.run(workspace_id, command, policy, job_id=job_id)
        except ProviderError as raised:
            pytest.fail(
                f"{tuple(command)} was reported as an infrastructure failure "
                f"({raised.message}); nothing about the engine broke, so a caller "
                "would retry this forever"
            )

    def test_a_command_the_environment_cannot_run_is_the_callers_failure(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Same slot as a job that lost, different cause: it never ran at all.

        A backend learns this the hard way — the engine reports a command it
        cannot exec through the same channel it reports its own breakage — so
        the pull towards exit 7 is real, and this is what resists it. The two
        POSIX answers are asserted apart rather than together, because
        collapsing them would send a caller off to re-spell a command that was
        sitting exactly where they said it was.
        """
        assert provider_case.absent_exit_status != provider_case.unrunnable_exit_status, (
            "an absent command and an unrunnable one must not report the same status: "
            "only one of the two is a spelling to fix"
        )
        descriptor = workspaces()
        policy = effective_policy(provider)
        for job_id, command, expected in (
            ("job-absent", provider_case.absent_command, provider_case.absent_exit_status),
            (
                "job-unrunnable",
                provider_case.unrunnable_command,
                provider_case.unrunnable_exit_status,
            ),
        ):
            outcome = self._ran(provider, descriptor.workspace_id, command, policy, job_id)
            assert outcome.status == STATUS_FAILURE, f"{job_id}: {outcome.output!r}"
            assert outcome.exit_status == expected

    def test_a_job_stopped_at_its_memory_ceiling_is_resource_exhausted(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Exhaustion is its own status: the ceiling held, and it says which one.

        Not ``failure``, because the job did not compute a wrong answer — it was
        stopped, and a caller's next move is to raise the ceiling or shrink the
        work. Not ``infrastructure_failure``, because retrying an unchanged job
        against an unchanged budget cannot succeed. Unlike a timeout the command
        did produce an exit status on its way out, which is exactly why the two
        statuses are separate.
        """
        policy = effective_policy(provider, memory_bytes=provider_case.memory_ceiling_bytes)
        descriptor = workspaces(policy)
        outcome = self._ran(
            provider,
            descriptor.workspace_id,
            provider_case.memory_hungry_command,
            policy,
            "job-exhausted",
        )
        assert outcome.status == STATUS_RESOURCE_EXHAUSTED, f"job-exhausted: {outcome.output!r}"
        assert outcome.exit_status is not None, "a job stopped at a ceiling still exited"
        assert outcome.exit_status != 0

    def test_run_status_never_claims_infrastructure_or_policy(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        descriptor = workspaces()
        for job_id, command in (
            ("job-vocab-ok", provider_case.succeeding_command),
            ("job-vocab-bad", provider_case.failing_command),
        ):
            outcome = provider.run(
                descriptor.workspace_id, command, effective_policy(provider), job_id=job_id
            )
            assert outcome.status in JOB_STATUSES

    def test_run_of_an_unknown_workspace_is_a_user_error(
        self, provider: Provider, provider_case: ProviderCase
    ) -> None:
        unknown_workspace_id = provider_case.workspace_id()
        policy = effective_policy(provider)
        with pytest.raises(CliError) as caught:
            provider.run(
                unknown_workspace_id,
                provider_case.succeeding_command,
                policy,
                job_id="job-nowhere",
            )
        assert caught.value.code == EXIT_USER_ERROR

    def test_a_workspace_hosts_several_jobs_and_stays_ready(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Jobs share one workspace, so a job must not consume its lifecycle."""
        descriptor = workspaces()
        policy = effective_policy(provider)
        for index in range(3):
            outcome = provider.run(
                descriptor.workspace_id,
                provider_case.succeeding_command,
                policy,
                job_id=f"job-shared-{index}",
            )
            assert outcome.status == STATUS_SUCCESS
        after = provider.inspect(descriptor.workspace_id)
        assert after.state is State.READY
        assert after.active_jobs == 0

    def test_captured_output_is_capped_and_says_so(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Bounded at capture, and honest about the volume it stood in for."""
        policy = effective_policy(provider, output_bytes=provider_case.flood_budget_bytes)
        descriptor = workspaces(policy)
        outcome = provider.run(
            descriptor.workspace_id, provider_case.flooding_command, policy, job_id="job-flood"
        )
        captured = len(outcome.output.encode("utf-8"))
        assert captured <= provider_case.flood_budget_bytes
        assert outcome.truncated is True
        assert outcome.usage.output_bytes > provider_case.flood_budget_bytes

    def test_a_job_over_its_wall_clock_budget_times_out(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Timeout is its own status with no exit status — it never produced one."""
        policy = effective_policy(provider, wall_clock_seconds=provider_case.slow_seconds)
        descriptor = workspaces(policy)
        outcome = provider.run(
            descriptor.workspace_id, provider_case.slow_command, policy, job_id="job-slow"
        )
        assert outcome.status == STATUS_TIMEOUT
        assert outcome.exit_status is None

    def test_engine_breakage_raises_infrastructure_failure(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """NFR-07: a broken engine is never reported as a failed computation."""
        if provider_case.break_engine is None:
            pytest.skip("this backend cannot be broken on demand (ProviderCase.break_engine)")
        descriptor = workspaces()
        provider_case.break_engine()
        policy = effective_policy(provider)
        with pytest.raises(ProviderError) as caught:
            provider.run(
                descriptor.workspace_id,
                provider_case.succeeding_command,
                policy,
                job_id="job-broken",
            )
        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert isinstance(caught.value, CliError)
        assert caught.value.remediation

    # --- read -------------------------------------------------------------

    def _written(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
        job_id: str,
    ) -> str:
        """A workspace holding the case's artifact, put there by a real job."""
        descriptor = workspaces()
        outcome = provider.run(
            descriptor.workspace_id,
            provider_case.writing_command,
            effective_policy(provider),
            job_id=job_id,
        )
        assert (
            outcome.status == STATUS_SUCCESS
        ), f"the writing command did not succeed: {outcome.status} / {outcome.output!r}"
        return descriptor.workspace_id

    def test_read_streams_back_exactly_what_a_job_wrote(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """The product's whole promise: what a workspace produced can leave it."""
        workspace_id = self._written(provider, provider_case, workspaces, "job-writes")

        stream = provider.read(workspace_id, provider_case.artifact_path)
        assert isinstance(stream, ByteStream)
        with stream:
            content = b"".join(stream)

        assert content == provider_case.artifact_bytes
        assert (
            hashlib.sha256(content).hexdigest()
            == hashlib.sha256(provider_case.artifact_bytes).hexdigest()
        ), "the artifact that left is not the artifact the job wrote"

    def test_read_delivers_bounded_chunks_rather_than_one_object(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Streamed, observably: an artifact may be larger than the host's memory."""
        workspace_id = self._written(provider, provider_case, workspaces, "job-writes-chunked")
        with provider.read(
            workspace_id, provider_case.artifact_path, chunk_size=READ_CHUNK_BYTES
        ) as stream:
            chunks = list(stream)

        assert len(chunks) > 1, "the artifact arrived as one object, not as a stream"
        assert all(isinstance(chunk, bytes) for chunk in chunks)
        assert max(len(chunk) for chunk in chunks) <= READ_CHUNK_BYTES
        assert b"".join(chunks) == provider_case.artifact_bytes

    def test_an_abandoned_read_releases_whatever_it_opened(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """A consumer that stops half way must strand nothing behind the seam.

        The export path abandons a stream whenever a digest fails to verify or a
        destination fills up, so "read the whole thing or leak" would be a real
        failure mode, not a theoretical one.
        """
        workspace_id = self._written(provider, provider_case, workspaces, "job-writes-abandoned")

        stream = provider.read(
            workspace_id, provider_case.artifact_path, chunk_size=READ_CHUNK_BYTES
        )
        first = next(iter(stream))
        assert len(first) == READ_CHUNK_BYTES
        stream.close()
        stream.close()  # idempotent: the release runs once, not once per call
        assert list(stream) == [], "a closed stream must not keep yielding"

        # The proof the release actually released: the same path reads again.
        with provider.read(workspace_id, provider_case.artifact_path) as again:
            assert b"".join(again) == provider_case.artifact_bytes

    def test_read_of_a_path_the_workspace_does_not_hold_is_a_user_error(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """A missing artifact is the caller's mistake — never a broken engine."""
        descriptor = workspaces()  # nothing has written anything into it
        with pytest.raises(CliError) as caught:
            provider.read(descriptor.workspace_id, provider_case.artifact_path)
        assert caught.value.code == EXIT_USER_ERROR
        assert caught.value.remediation
        assert not isinstance(caught.value, ProviderError)

    def test_read_of_an_unknown_workspace_is_a_user_error(
        self, provider: Provider, provider_case: ProviderCase
    ) -> None:
        unknown_workspace_id = provider_case.workspace_id()
        with pytest.raises(CliError) as caught:
            provider.read(unknown_workspace_id, provider_case.artifact_path)
        assert caught.value.code == EXIT_USER_ERROR

    @pytest.mark.parametrize("escape", ["../etc/passwd", "/etc/passwd", "a/../../b", "", "   "])
    def test_read_refuses_a_path_that_leaves_the_workspace(
        self,
        provider: Provider,
        workspaces: Callable[..., WorkspaceDescriptor],
        escape: str,
    ) -> None:
        """The workspace is a boundary; a read that steps outside it is refused here.

        Refused at the seam rather than at each backend, because the engine
        underneath may well resolve ``..`` happily — a Docker archive request
        for ``/workspace/../etc/hostname`` returns the container's own file.
        """
        descriptor = workspaces()
        with pytest.raises(CliError) as caught:
            provider.read(descriptor.workspace_id, escape)
        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)

    def test_read_reports_engine_breakage_as_infrastructure_failure(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """NFR-07 again, on the path that carries the artifacts out.

        An artifact that cannot be fetched because the engine died is exit 7: a
        caller must retry the fetch, not conclude its work was lost.
        """
        if provider_case.break_engine is None:
            pytest.skip("this backend cannot be broken on demand (ProviderCase.break_engine)")
        workspace_id = self._written(provider, provider_case, workspaces, "job-writes-broken")
        provider_case.break_engine()
        with pytest.raises(ProviderError) as caught:
            provider.read(workspace_id, provider_case.artifact_path)
        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert caught.value.remediation

    # --- remove -----------------------------------------------------------

    def test_remove_reports_what_it_actually_removed(
        self, provider: Provider, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        descriptor = workspaces()
        disposition = provider.remove(descriptor.workspace_id)
        assert isinstance(disposition, RemovalDisposition)
        assert disposition.workspace_id == descriptor.workspace_id
        assert set(disposition.removed) == set(REMOVABLE_RESOURCES)
        # Every named resource is accounted for exactly once across the three
        # buckets — "what was removed, retained, and unverified" is a partition,
        # not three independent lists that can double-count or drop a resource.
        named = disposition.removed + disposition.retained + disposition.unverified
        assert len(named) == len(set(named))
        assert set(named) <= set(REMOVABLE_RESOURCES)

    def test_remove_works_after_jobs_have_run(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        descriptor = workspaces()
        provider.run(
            descriptor.workspace_id,
            provider_case.succeeding_command,
            effective_policy(provider),
            job_id="job-before-remove",
        )
        assert provider.remove(descriptor.workspace_id).removed

    def test_a_removed_workspace_is_gone(
        self, provider: Provider, workspaces: Callable[..., WorkspaceDescriptor]
    ) -> None:
        descriptor = workspaces()
        provider.remove(descriptor.workspace_id)
        for call in (
            lambda: provider.inspect(descriptor.workspace_id),
            lambda: provider.remove(descriptor.workspace_id),
        ):
            with pytest.raises(CliError) as caught:
                call()
            assert caught.value.code == EXIT_USER_ERROR

    # --- acceptance criterion 2, mechanically -----------------------------

    def test_returned_structures_are_backend_neutral(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """Walk everything the seam returns; a leaked backend concept fails here."""
        descriptor = workspaces()
        outcome = provider.run(
            descriptor.workspace_id,
            provider_case.succeeding_command,
            effective_policy(provider),
            job_id="job-neutral",
        )
        inspected = provider.inspect(descriptor.workspace_id)
        disposition = provider.remove(descriptor.workspace_id)

        walk = StructureWalk()
        for label, structure in (
            ("create", descriptor),
            ("run", outcome),
            ("inspect", inspected),
            ("remove", disposition),
        ):
            payload = structure.to_dict()
            # Serialisable by construction: a docker SDK object cannot survive.
            json.dumps(payload)
            walk_structure(payload, label, walk)

        assert not walk.violations, "backend leakage:\n" + "\n".join(walk.violations)

        # An opaque handle that is also echoed into a readable field is not
        # opaque — the caller could start depending on it.
        readable = [value for _, value in walk.scalars if isinstance(value, str)]
        for token in walk.opaque:
            if not token:
                continue
            leaked = [text for text in readable if token in text]
            assert not leaked, f"opaque backend handle {token!r} echoed into {leaked}"

    def test_serialised_structures_cover_every_declared_field(
        self,
        provider: Provider,
        provider_case: ProviderCase,
        workspaces: Callable[..., WorkspaceDescriptor],
    ) -> None:
        """``to_dict`` cannot quietly drop a field the dataclass declares."""
        descriptor = workspaces()
        outcome = provider.run(
            descriptor.workspace_id,
            provider_case.succeeding_command,
            effective_policy(provider),
            job_id="job-fields",
        )
        disposition = provider.remove(descriptor.workspace_id)
        for structure in (descriptor, outcome, disposition):
            declared = {f.name for f in dataclasses.fields(structure)}
            assert set(structure.to_dict()) == declared
