"""The Docker binding: the conformance suite, plus the posture it cannot see.

WHY this module exists
----------------------
Two jobs, and the split matters.

The first is the binding itself: :class:`TestDockerProviderConformance` hands
:class:`~tests.conformance.ProviderConformance` a live engine and inherits
every behavioural test unchanged. That is the whole point of a conformance
suite — the second implementation must be held to the *same executable*
description as the first, not to a friendlier restatement of it. Nothing here
overrides a ``test_*`` method; the suite fails the run if it did.

The second is everything the suite is deliberately blind to. The suite refuses
to know what a container is, so it cannot check that this backend actually
closed the box: that ``NetworkMode`` is ``none``, that the memory / cpu / pids
ceilings on the engine object are the ones the caller's budget asked for, that
the only mount is the workspace volume, that no device and no engine socket
came along, and that a job container carries a log-driver size cap. Those are
acceptance criteria, and a docstring promising them is worth nothing — so they
are asserted here against ``HostConfig`` as the engine reports it, using the
SDK directly. This file and ``headspace/providers/docker.py`` are the only two
places in the repo that may import it.

Two of those blind spots belong to the read-back verb, and both are the reason
it exists at all. The suite cannot stop a workspace's runtime, so it cannot
prove that an artifact survives a workspace whose container has exited — which
is precisely the case a caller needs the verb for. And it cannot measure
memory, so "streamed, not buffered" would be an assertion about an
implementation rather than an observation of one: here a 16 MiB artifact is
read back under :mod:`tracemalloc`, and the peak is the evidence.

The third job is the security regressions on the copy-in path
-------------------------------------------------------------
``tests/test_provider_docker_inbound.py`` proves that ``write`` works and that
its named refusals fire. The last section of *this* file asks a narrower and
more adversarial question: what a job sharing the workspace volume can do to a
copy-in in flight, and what it cannot. Those tests borrow that module's
``HostBackedAnchor`` — a real shell over a real directory, with only the daemon
faked — because a boundary check written in ``sh`` can only be tested by running
``sh``. Borrowed rather than copied: a second stand-in for the same container
would drift from the first, and a security test that pins a *replica* of the
implementation's environment pins nothing.

Running without an engine
-------------------------
The unit lane has no daemon and coverage must never require one, so engine
reachability is probed **once** (:func:`_engine_reason`) and every test that
needs a container skips inside its fixture. The tests that do *not* need one —
the infrastructure-failure taxonomy, and the proof that the skip path really
skips — run everywhere, which is exactly where NFR-07's "engine breakage is
never a computational failure" is cheapest to keep honest.

Leaving nothing behind
----------------------
Every engine object this module creates carries the workspace id in a label and
a recognisable name prefix, and a module-scoped reaper force-removes anything
still wearing one of this file's prefixes when the module finishes — including
after a failure, and including the workspace the engine-breakage test
deliberately strands. A leaked container is a real cost on a developer's
machine, so cleanup is a teardown, never a trailing call.

The prefixes carry the xdist worker id because CI runs ``pytest -n auto``: a
reaper that recognised every worker's objects would tear down containers a
sibling worker was still running jobs in, and the failure would look like a
flaky engine rather than a test bug. One namespace per worker makes the
reaper's authority exactly as wide as its knowledge.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import os
import shutil
import threading
import time
import tracemalloc
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import NotFound

from headspace.cli._errors import (
    EXIT_ENV_ERROR,
    EXIT_INFRASTRUCTURE_FAILURE,
    EXIT_POLICY_DENIED,
    EXIT_USER_ERROR,
    CliError,
)
from headspace.core import profiles
from headspace.core.policy import (
    EffectivePolicy,
    FilesystemScope,
    LimitStatus,
    NetworkPosture,
    Policy,
    PolicyError,
    ResourceBudget,
)
from headspace.core.policy import resolve as resolve_policy
from headspace.core.states import State
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import ArtifactDeclaration, Orchestrator
from headspace.providers.base import ProviderError, requested_limit
from headspace.providers.docker import (
    EXIT_COMMAND_NOT_EXECUTABLE,
    EXIT_COMMAND_NOT_FOUND,
    IDLE_COMMAND,
    LABEL_CAPABILITY_PREFIX,
    LABEL_ENVIRONMENT,
    LABEL_LIMIT_PREFIX,
    LABEL_PROVIDER,
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    LOG_FILE_COUNT,
    OBJECT_PREFIX,
    REQUIRED_REAP_TOOLS,
    REQUIRED_WRITE_TOOLS,
    ROLE_JOB,
    ROLE_WORKSPACE,
    STAGED_FILE_NAME,
    STAGING_DIR_NAME,
    WORKSPACE_MOUNT_PATH,
    WRITE_SHELL,
    DockerProvider,
    engine_unavailable,
    log_cap_bytes,
)
from tests.conformance import ProviderCase, ProviderConformance
from tests.test_provider_docker_inbound import HostBackedAnchor, StubEngine

#: A socket nothing is listening on. Pointing ``DOCKER_HOST`` here is how the
#: engine gets broken on demand: the provider opens its connection per verb, so
#: the next call genuinely fails at the transport rather than being faked.
DEAD_ENGINE = "unix:///nonexistent/headspace-no-such-engine.sock"

#: This process's slice of the engine's namespace. ``PYTEST_XDIST_WORKER`` is
#: set per worker under ``-n auto`` and absent otherwise, so the prefixes below
#: are disjoint between concurrent workers and stable for a serial run.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")

#: Workspace-id prefixes this module owns. Every engine object it creates is
#: named and labelled with one, so the reaper below can recognise a stray
#: without guessing, and so a human reading ``docker ps`` can too.
CONFORMANCE_PREFIX = f"conf-docker-{WORKER}"
POSTURE_PREFIX = f"hs-posture-{WORKER}"
OWNED_PREFIXES = (CONFORMANCE_PREFIX, POSTURE_PREFIX)

#: The artifact the read-back tests fetch, and the job that writes it. ``yes``
#: generates it, so the expected content is a Python expression rather than a
#: fixture file — the test asserts the exact bytes, not just the size.
ARTIFACT_PATH = "artifact.bin"
ARTIFACT_BYTES = (b"headspace\n" * 820)[:8192]
WRITING_COMMAND = (
    "/bin/sh",
    "-c",
    f"yes headspace | head -c {len(ARTIFACT_BYTES)} > {WORKSPACE_MOUNT_PATH}/{ARTIFACT_PATH}",
)

#: The conformance suite's two "this environment cannot run that" commands, on
#: this engine. Both are refused by the container's init step rather than by a
#: shell, so ``/bin/sh -c`` is deliberately absent: a shell would run, print its
#: own diagnostic and exit 127 itself, which would prove the *shell* classifies
#: correctly and say nothing about the provider. The second is a real file in
#: the image with no execute bit, which is the 126 half — the two must not
#: collapse to one number.
ABSENT_COMMAND = ("definitely-not-a-binary",)
UNRUNNABLE_COMMAND = ("/etc/hostname",)

#: The conformance suite's memory case: an allocation four times the ceiling
#: declared beneath it, so the kernel stops it well before any other budget
#: bites. The ceiling is comfortably above what the environment needs to start,
#: so a failure here is the allocation and never the interpreter's own startup.
MEMORY_CEILING_BYTES = 128 * 1024 * 1024
MEMORY_HUNGRY_COMMAND = ("python", "-c", f"x = bytearray({4 * MEMORY_CEILING_BYTES})")


@functools.cache
def _engine_reason() -> str:
    """Why this host has no engine, once. ``""`` when one answered a ping.

    Cached because the answer cannot change usefully within a run, and because
    a per-test probe against a dead socket is the slowest possible way to
    discover the same thing thirty times.
    """
    return engine_unavailable()


def _skip_without_engine() -> None:
    """Skip — never fail, never hang — when no engine is reachable."""
    reason = _engine_reason()
    if reason:
        pytest.skip(f"no reachable docker engine: {reason}")


def _default_policy(provider: DockerProvider, **budget: object) -> EffectivePolicy:
    return resolve_policy(
        Policy(budget=ResourceBudget(**budget)),  # type: ignore[arg-type]
        provider.capabilities(),
    )


# --- teardown: nothing survives this module --------------------------------


@pytest.fixture(scope="module", autouse=True)
def _reap_engine_strays() -> Iterator[None]:
    """Force-remove every object still wearing one of this module's prefixes.

    A safety net under the per-test teardowns, not a replacement for them: the
    engine-breakage test strands a workspace on purpose (its provider is
    pointed at a dead socket, so the suite's own cleanup cannot reach it), and
    a test that dies mid-``create`` can strand one by accident.

    ``ignore_removed`` is load-bearing under ``-n auto``. The filter selects on
    the label *key*, so the listing spans every worker's objects, and the SDK
    re-inspects each id it listed — an object a sibling worker removed in
    between therefore raises ``NotFound`` from the listing itself, aborting this
    reaper before it reaches its own strays. The one thing a safety net must not
    do is fail on someone else's tidiness.

    The same sentence has to cover the *removals*, and until now it did not.
    ``volumes.list`` takes no ``ignore_removed``, and a ``remove`` of something
    that has already gone raises ``NotFound`` in both loops — so a concurrent
    run tidying up between this listing and this removal turned a successful
    module into a teardown error, reported against whichever test happened to
    be last. "Already gone" is this reaper's goal state, not its failure, so it
    is suppressed per object rather than per pass: a stray that genuinely will
    not die still raises, which is the report worth keeping.
    """
    yield
    if _engine_reason():
        return
    client = docker.from_env()
    try:
        with contextlib.suppress(NotFound):
            for container in client.containers.list(
                all=True, filters={"label": LABEL_WORKSPACE_ID}, ignore_removed=True
            ):
                if _owned(container.labels.get(LABEL_WORKSPACE_ID, "")):
                    with contextlib.suppress(NotFound):
                        container.remove(force=True)
        with contextlib.suppress(NotFound):
            for volume in client.volumes.list(filters={"label": LABEL_WORKSPACE_ID}):
                if _owned((volume.attrs.get("Labels") or {}).get(LABEL_WORKSPACE_ID, "")):
                    with contextlib.suppress(NotFound):
                        volume.remove(force=True)
    finally:
        client.close()


def _owned(workspace_id: str) -> bool:
    return any(workspace_id.startswith(f"{prefix}-") for prefix in OWNED_PREFIXES)


# --- the binding ------------------------------------------------------------


class TestDockerProviderConformance(ProviderConformance):
    """The whole suite, unchanged, against a live engine."""

    @pytest.fixture
    def provider_case(self, monkeypatch: pytest.MonkeyPatch) -> ProviderCase:
        _skip_without_engine()
        provider = DockerProvider()
        # Warm the capability probe while the engine is still reachable: the
        # snapshot is a create-time fact, so breaking the engine afterwards
        # must break the *job*, not the policy resolution that precedes it.
        provider.capabilities()

        def break_engine() -> None:
            monkeypatch.setenv("DOCKER_HOST", DEAD_ENGINE)

        return ProviderCase(
            provider=provider,
            environment=profiles.resolve(profiles.DEFAULT_PROFILE),
            succeeding_command=("/bin/sh", "-c", "echo headspace-conformance"),
            echo_text="headspace-conformance",
            failing_command=("/bin/sh", "-c", "exit 7"),
            failing_exit_status=7,
            absent_command=ABSENT_COMMAND,
            absent_exit_status=EXIT_COMMAND_NOT_FOUND,
            unrunnable_command=UNRUNNABLE_COMMAND,
            unrunnable_exit_status=EXIT_COMMAND_NOT_EXECUTABLE,
            memory_hungry_command=MEMORY_HUNGRY_COMMAND,
            memory_ceiling_bytes=MEMORY_CEILING_BYTES,
            slow_command=("sleep", "30"),
            slow_seconds=1,
            flooding_command=("/bin/sh", "-c", "yes headspace | head -c 200000"),
            flood_budget_bytes=4096,
            writing_command=WRITING_COMMAND,
            artifact_path=ARTIFACT_PATH,
            artifact_bytes=ARTIFACT_BYTES,
            break_engine=break_engine,
            workspace_prefix=CONFORMANCE_PREFIX,
        )


# --- acceptance criterion 2: the box is closed, as the engine reports it ----


@pytest.fixture
def engine() -> Iterator[docker.DockerClient]:
    """A raw SDK client, for reading back what the provider actually built."""
    _skip_without_engine()
    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def provider() -> DockerProvider:
    _skip_without_engine()
    return DockerProvider()


@pytest.fixture
def workspace(provider: DockerProvider) -> Iterator[Callable[..., str]]:
    """Make workspaces; remove them in teardown, however the test ended."""
    created: list[str] = []

    def make(policy: EffectivePolicy | None = None) -> str:
        workspace_id = f"{POSTURE_PREFIX}-{uuid.uuid4().hex[:12]}"
        created.append(workspace_id)
        provider.create(
            workspace_id,
            profiles.resolve(profiles.DEFAULT_PROFILE),
            policy if policy is not None else _default_policy(provider),
        )
        return workspace_id

    yield make

    for workspace_id in reversed(created):
        try:
            provider.remove(workspace_id)
        except Exception:  # noqa: BLE001 - teardown must never mask the failure
            pass


def _anchor(client: docker.DockerClient, workspace_id: str) -> docker.models.containers.Container:
    found = client.containers.list(
        all=True,
        filters={
            "label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_WORKSPACE}"]
        },
    )
    assert len(found) == 1, f"expected exactly one workspace container, got {len(found)}"
    return found[0]


def _assert_sealed(host_config: dict[str, object], workspace_id: str) -> None:
    """Every property the closed-by-default posture claims, read off the engine."""
    mounts = host_config.get("Mounts") or []
    assert len(mounts) == 1, f"expected exactly one mount, got {mounts}"
    only = mounts[0]
    assert only["Type"] == "volume", "a workspace mounts a volume, never a host path"
    assert only["Source"] == f"{OBJECT_PREFIX}{workspace_id}"
    assert only["Target"] == WORKSPACE_MOUNT_PATH

    # No host bind mounts at all — not the engine socket, not anything else.
    assert not host_config.get("Binds")
    assert not host_config.get("VolumesFrom")
    serialised = repr(host_config)
    assert "docker.sock" not in serialised
    assert "/var/run" not in serialised

    assert not host_config.get("Devices")
    assert not host_config.get("DeviceRequests")
    assert host_config.get("Privileged") is False
    assert "no-new-privileges" in (host_config.get("SecurityOpt") or [])


class TestClosedByDefaultPosture:
    """Acceptance criterion 2, asserted against the engine rather than promised."""

    def test_the_workspace_container_is_closed_and_budget_bound(
        self,
        engine: docker.DockerClient,
        workspace: Callable[..., str],
    ) -> None:
        budget = ResourceBudget(
            memory_bytes=384 * 1024 * 1024, cpu_limit=0.75, pids_limit=64, output_bytes=4096
        )
        policy = resolve_policy(Policy(budget=budget), DockerProvider().capabilities())
        workspace_id = workspace(policy)

        host_config = _anchor(engine, workspace_id).attrs["HostConfig"]
        assert host_config["NetworkMode"] == "none"
        assert host_config["Memory"] == budget.memory_bytes
        # Swap is pinned to the memory ceiling: a container allowed to swap has
        # not really been given a memory limit.
        assert host_config["MemorySwap"] == budget.memory_bytes
        assert host_config["NanoCpus"] == int(budget.cpu_limit * 1_000_000_000)
        assert host_config["PidsLimit"] == budget.pids_limit
        _assert_sealed(host_config, workspace_id)

    def test_the_workspace_container_joins_only_the_none_network(
        self, engine: docker.DockerClient, workspace: Callable[..., str]
    ) -> None:
        anchor = _anchor(engine, workspace())
        assert list(anchor.attrs["NetworkSettings"]["Networks"]) == ["none"]

    def test_an_enabled_network_posture_is_the_only_way_out(
        self, engine: docker.DockerClient, provider: DockerProvider, workspace: Callable[..., str]
    ) -> None:
        opened = resolve_policy(Policy(network=NetworkPosture.ENABLED), provider.capabilities())
        anchor = _anchor(engine, workspace(opened))
        assert anchor.attrs["HostConfig"]["NetworkMode"] != "none"

    def test_a_job_container_carries_the_same_posture_and_a_log_size_cap(
        self,
        engine: docker.DockerClient,
        provider: DockerProvider,
        workspace: Callable[..., str],
    ) -> None:
        """Acceptance criterion 3: output is capped at capture, on the job itself.

        The job container is torn down the moment the job settles, so it is
        caught mid-flight: the job sleeps, the assertions read the live object.
        """
        budget = ResourceBudget(
            memory_bytes=256 * 1024 * 1024, cpu_limit=0.5, pids_limit=32, output_bytes=4096
        )
        policy = resolve_policy(Policy(budget=budget), provider.capabilities())
        workspace_id = workspace(policy)

        runner = threading.Thread(
            target=provider.run,
            args=(workspace_id, ("sleep", "5"), policy),
            kwargs={"job_id": "posture-probe"},
            daemon=True,
        )
        runner.start()
        try:
            job = _await_job_container(engine, workspace_id)
            host_config = job.attrs["HostConfig"]
            assert host_config["NetworkMode"] == "none"
            assert host_config["Memory"] == budget.memory_bytes
            assert host_config["NanoCpus"] == int(budget.cpu_limit * 1_000_000_000)
            assert host_config["PidsLimit"] == budget.pids_limit
            _assert_sealed(host_config, workspace_id)

            log_config = host_config["LogConfig"]
            assert log_config["Type"] == "json-file"
            assert log_config["Config"]["max-file"] == str(LOG_FILE_COUNT)
            expected_kib = log_cap_bytes(budget.output_bytes) // 1024
            assert log_config["Config"]["max-size"] == f"{expected_kib}k"
            assert job.labels[LABEL_ROLE] == ROLE_JOB
            assert job.labels[LABEL_WORKSPACE_ID] == workspace_id
        finally:
            runner.join(60)
        assert not runner.is_alive()

    def test_the_job_container_does_not_outlive_the_job(
        self, engine: docker.DockerClient, provider: DockerProvider, workspace: Callable[..., str]
    ) -> None:
        workspace_id = workspace()
        provider.run(
            workspace_id, ("/bin/sh", "-c", "echo done"), _default_policy(provider), job_id="tidy"
        )
        leftovers = engine.containers.list(
            all=True,
            filters={"label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_JOB}"]},
        )
        assert leftovers == []


def _await_job_container(
    client: docker.DockerClient, workspace_id: str, timeout: float = 30.0
) -> docker.models.containers.Container:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_JOB}"]},
        )
        if found:
            return found[0]
        time.sleep(0.02)
    raise AssertionError(f"no job container appeared for {workspace_id}")


# --- reading artifacts back out, where the suite cannot look ----------------


class TestReadingArtifactsBackOut:
    """The two properties the backend-agnostic suite structurally cannot check."""

    def test_an_artifact_outlives_a_workspace_whose_runtime_has_exited(
        self,
        engine: docker.DockerClient,
        provider: DockerProvider,
        workspace: Callable[..., str],
    ) -> None:
        """The case the verb exists for: the box died, the work must not die with it.

        A workspace whose container has exited reports ``failed`` — the runtime
        is gone. Its *storage* is not, and the artifacts are in the storage, so
        a read that needed a live container would lose exactly the work a caller
        most needs back.
        """
        workspace_id = workspace()
        provider.run(workspace_id, WRITING_COMMAND, _default_policy(provider), job_id="writer")

        _anchor(engine, workspace_id).stop(timeout=10)
        assert provider.inspect(workspace_id).state is State.FAILED

        with provider.read(workspace_id, ARTIFACT_PATH) as stream:
            assert b"".join(stream) == ARTIFACT_BYTES

    def test_reading_creates_no_engine_object_of_its_own(
        self,
        engine: docker.DockerClient,
        provider: DockerProvider,
        workspace: Callable[..., str],
    ) -> None:
        """Nothing to leak, because nothing is created: the read is a plain fetch.

        A read that spun up a helper container to reach the volume would have to
        be trusted to reap it on every failure path. This backend reads through
        the workspace container it already owns, so the strongest guarantee is
        available: the engine's object count does not move.
        """
        workspace_id = workspace()
        provider.run(workspace_id, WRITING_COMMAND, _default_policy(provider), job_id="writer")

        def objects() -> tuple[int, int]:
            # Scope the count to THIS workspace. Filtering on the label key alone
            # counts every headspace object on the engine, so under `pytest -n
            # auto` a sibling worker creating or reaping its own workspace moves
            # the number and fails this assertion for reasons that have nothing
            # to do with reading. Scoping loses nothing: the provider labels
            # every object it creates with the workspace id — that ownership
            # invariant is what crash reconciliation relies on, and other tests
            # assert it — so a helper object created to serve this read would
            # carry this label and still be counted.
            selector = {"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
            containers = engine.containers.list(all=True, filters=selector)
            volumes = engine.volumes.list(filters=selector)
            return len(containers), len(volumes)

        before = objects()
        with provider.read(workspace_id, ARTIFACT_PATH) as stream:
            assert b"".join(stream) == ARTIFACT_BYTES
        # ...and again, abandoned half way, which is the path a failed digest takes.
        abandoned = provider.read(workspace_id, ARTIFACT_PATH, chunk_size=64)
        next(iter(abandoned))
        abandoned.close()
        assert objects() == before

    def test_a_large_artifact_is_streamed_rather_than_materialised(
        self, provider: DockerProvider, workspace: Callable[..., str]
    ) -> None:
        """Criterion: streamed, measured rather than asserted.

        16 MiB is written into the workspace and read back while
        :mod:`tracemalloc` watches every Python allocation. A backend that
        buffered the artifact anywhere — the transfer archive, the HTTP
        response, a ``b"".join`` — would show a peak at least as large as the
        artifact itself. The ceiling asserted here is a quarter of it.
        """
        megabytes = 16
        workspace_id = workspace()
        provider.run(
            workspace_id,
            (
                "/bin/sh",
                "-c",
                f"dd if=/dev/zero of={WORKSPACE_MOUNT_PATH}/big.bin bs=1M count={megabytes}",
            ),
            _default_policy(provider),
            job_id="big-writer",
        )

        tracemalloc.start()
        try:
            total = 0
            widest = 0
            with provider.read(workspace_id, "big.bin", chunk_size=256 * 1024) as stream:
                for chunk in stream:
                    total += len(chunk)
                    widest = max(widest, len(chunk))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert total == megabytes * 1024 * 1024
        assert widest <= 256 * 1024
        assert peak < 4 * 1024 * 1024, (
            f"peak allocation was {peak} bytes reading a {total}-byte artifact: "
            "it was buffered, not streamed"
        )

    def test_a_directory_and_a_symlink_are_refused_rather_than_half_exported(
        self, provider: DockerProvider, workspace: Callable[..., str]
    ) -> None:
        """An artifact is one file's bytes; anything else is refused as a user error.

        The symlink half is a boundary check, not a nicety. A job can plant a
        link to a path outside the workspace volume, and the engine's own
        archive endpoint resolves such a link within the container's filesystem
        — so the refusal is what keeps a read to the storage it was asked for.
        """
        workspace_id = workspace()
        provider.run(
            workspace_id,
            (
                "/bin/sh",
                "-c",
                f"mkdir -p {WORKSPACE_MOUNT_PATH}/sub && "
                f"ln -s /etc/hostname {WORKSPACE_MOUNT_PATH}/escape",
            ),
            _default_policy(provider),
            job_id="odd-writer",
        )

        for path in ("sub", "escape"):
            with pytest.raises(CliError) as caught:
                provider.read(workspace_id, path)
            assert caught.value.code == EXIT_USER_ERROR
            assert not isinstance(caught.value, ProviderError)
            assert caught.value.remediation


class TestExportPullsBytesOutOfTheEngine:
    """Criterion 1, end to end: a job's file becomes a durable, verified artifact."""

    def test_export_recovers_the_artifact_by_digest_with_no_caller_supplied_source(
        self,
        provider: DockerProvider,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "headspace-home"))
        orchestrator = Orchestrator(provider, Store())
        workspace_id = f"{POSTURE_PREFIX}-{uuid.uuid4().hex[:12]}"
        destination = tmp_path / ARTIFACT_PATH

        try:
            orchestrator.create(workspace_id=workspace_id)
            orchestrator.run(
                workspace_id,
                WRITING_COMMAND,
                declares=[ArtifactDeclaration(ARTIFACT_PATH, "the artifact under test")],
            )
            package = orchestrator.export(workspace_id, ARTIFACT_PATH, destination=destination)
        finally:
            with contextlib.suppress(Exception):
                provider.remove(workspace_id)

        assert destination.read_bytes() == ARTIFACT_BYTES
        (artifact,) = package.artifacts
        assert artifact.digest == hashlib.sha256(ARTIFACT_BYTES).hexdigest()
        assert artifact.size_bytes == len(ARTIFACT_BYTES)
        assert artifact.reference == str(destination)


# --- acceptance criterion 3: the probe is measured, and it is persisted -----


class TestCapabilityProbe:
    """The snapshot must be interrogated from the engine, not asserted hopefully."""

    def test_every_enforceable_flag_matches_what_the_engine_reports(
        self, engine: docker.DockerClient, provider: DockerProvider
    ) -> None:
        info = engine.info()
        snapshot = provider.capabilities()

        assert snapshot.engine.startswith("docker ")
        assert snapshot.api_version == engine.version()["ApiVersion"]
        assert snapshot.memory_enforceable is bool(info["MemoryLimit"])
        assert snapshot.cpu_enforceable is bool(info["CpuCfsQuota"] and info["CpuCfsPeriod"])
        assert snapshot.pids_enforceable is bool(info["PidsLimit"])
        assert snapshot.network_disable_supported is ("null" in info["Plugins"]["Network"])
        # The canonical measured-not-enforced case: the local volume driver
        # reports a volume's size and caps nothing.
        assert snapshot.storage_enforceable is False
        # No host bind mount is ever offered, so no filesystem scope can be
        # honoured — reported honestly rather than optimistically.
        assert snapshot.filesystem_scope_enforceable is False

    def test_the_snapshot_and_the_resolved_limits_are_persisted_on_the_workspace(
        self,
        engine: docker.DockerClient,
        provider: DockerProvider,
        workspace: Callable[..., str],
    ) -> None:
        """A crashed CLI leaves the probe behind, readable by label alone."""
        policy = _default_policy(provider)
        workspace_id = workspace(policy)
        labels = _anchor(engine, workspace_id).labels

        snapshot = provider.capabilities()
        assert labels[f"{LABEL_CAPABILITY_PREFIX}engine"] == snapshot.engine
        assert labels[f"{LABEL_CAPABILITY_PREFIX}api_version"] == snapshot.api_version
        assert labels[f"{LABEL_CAPABILITY_PREFIX}memory_enforceable"] == "true"
        assert labels[f"{LABEL_CAPABILITY_PREFIX}storage_enforceable"] == "false"

        for limit in policy.limits:
            assert labels[f"{LABEL_LIMIT_PREFIX}{limit.name}"] == limit.status.value
        assert policy.status_of("storage") is LimitStatus.MEASURED
        assert labels[f"{LABEL_LIMIT_PREFIX}storage"] == "measured"
        assert labels[f"{LABEL_LIMIT_PREFIX}memory"] == "enforced"

        assert labels[LABEL_ENVIRONMENT] == profiles.resolve(profiles.DEFAULT_PROFILE)

    def test_storage_is_measured_after_a_job_writes_to_the_workspace(
        self, provider: DockerProvider, workspace: Callable[..., str]
    ) -> None:
        """Measured, not capped — but measured for real, not reported as zero."""
        workspace_id = workspace()
        provider.run(
            workspace_id,
            ("/bin/sh", "-c", "dd if=/dev/zero of=/workspace/blob bs=1024 count=512"),
            _default_policy(provider),
            job_id="storage-probe",
        )
        assert provider.inspect(workspace_id).storage_bytes >= 512 * 1024


# --- the log cap is derived, bounded, and host-safe -------------------------


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (0, 64 * 1024),  # floored: a cap below the smallest useful log is useless
        (4096, 64 * 1024),
        (10 * 1024 * 1024, 16 * 1024 * 1024),  # ceilinged: the host's disk is not the caller's
        (1024 * 1024 * 1024, 16 * 1024 * 1024),
    ],
)
def test_the_log_cap_is_clamped_between_a_floor_and_a_host_safe_ceiling(
    budget: int, expected: int
) -> None:
    assert log_cap_bytes(budget) == expected
    assert log_cap_bytes(budget) % 1024 == 0


# --- NFR-07, without needing an engine to break -----------------------------


class TestUnreachableEngineIsInfrastructureFailure:
    """Runs in the no-daemon lane: a broken engine is never a failed computation."""

    @pytest.fixture
    def unreachable(self, monkeypatch: pytest.MonkeyPatch) -> DockerProvider:
        monkeypatch.setenv("DOCKER_HOST", DEAD_ENGINE)
        monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
        monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
        return DockerProvider()

    @pytest.mark.parametrize("verb", ["capabilities", "create", "run", "inspect", "read", "remove"])
    def test_every_verb_raises_provider_error(self, unreachable: DockerProvider, verb: str) -> None:
        policy = resolve_policy(Policy(), _snapshot_without_an_engine())
        dead = f"{POSTURE_PREFIX}-unreachable"
        calls = {
            "capabilities": lambda: unreachable.capabilities(),
            "create": lambda: unreachable.create(dead, "python:3.12-slim", policy),
            "run": lambda: unreachable.run(dead, ("true",), policy, job_id="dead-job"),
            "inspect": lambda: unreachable.inspect(dead),
            "read": lambda: unreachable.read(dead, ARTIFACT_PATH),
            "remove": lambda: unreachable.remove(dead),
        }
        with pytest.raises(ProviderError) as caught:
            calls[verb]()
        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert caught.value.remediation
        assert caught.value.category == "infrastructure_failure"

    def test_the_provider_constructor_touches_no_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructing a provider must not need a daemon — ``--help`` must work."""
        monkeypatch.setenv("DOCKER_HOST", DEAD_ENGINE)
        assert DockerProvider().name == "docker"


def _snapshot_without_an_engine() -> object:
    from headspace.core.policy import CapabilitySnapshot

    return CapabilitySnapshot(engine="docker (unprobed)", api_version="1.44")


# --- the skip path itself, proven rather than assumed -----------------------


def test_engine_unavailable_reports_a_reason_for_an_unreachable_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", DEAD_ENGINE)
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    reason = engine_unavailable()
    assert reason, "an unreachable engine must report why, so a skip can say so"
    assert "unreachable" in reason


def test_the_binding_skips_rather_than_fails_when_no_engine_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-negotiable one: no daemon means skipped, never failed or hung."""
    monkeypatch.setattr(
        "tests.test_provider_docker.engine_unavailable", lambda: "engine unreachable: probed"
    )
    _engine_reason.cache_clear()
    try:
        with pytest.raises(pytest.skip.Exception) as caught:
            _skip_without_engine()
        assert "no reachable docker engine" in str(caught.value)
    finally:
        _engine_reason.cache_clear()


# --- the copy-in boundary, under a job that is actively hostile -------------
#
# What separates this section from tests/test_provider_docker_inbound.py is the
# threat model, not the machinery. That module asks whether copy-in works and
# whether its refusals fire; these tests assume a job container that shares the
# workspace volume and is trying to get bytes out of it — or to get headspace to
# put bytes somewhere it was never asked to. Every one of them is a regression:
# the property it pins is one the implementation currently claims, and the point
# of writing it down is that the claim then cannot be lost silently.

#: The workspace the copy-in regressions act on. Its own id, distinct from the
#: inbound module's, so a fixture leaking between the two files is a visible
#: mistake rather than a shared secret.
COPY_IN_WORKSPACE = "posture-copy-in-ws"
COPY_IN_PAYLOAD = b"headspace copy-in regression\n" * 32
COPY_IN_DIGEST = hashlib.sha256(COPY_IN_PAYLOAD).hexdigest()

#: What the fixture's shell needs of *this* host to stand in for the container's.
#: ``ln`` is the extra one: several tests here are a job planting a symlink, and
#: one of them plants it from inside the container's own shell.
_HOST_TOOLS_NEEDED = ("sh", "ln", *REQUIRED_WRITE_TOOLS)


def _absent_host_tools() -> list[str]:
    return [tool for tool in _HOST_TOOLS_NEEDED if shutil.which(tool) is None]


#: Applied per class rather than as a module ``pytestmark``: everything above
#: this line runs on any host, and a module-wide skip would take the engine
#: posture assertions with it.
requires_a_host_shell = pytest.mark.skipif(
    bool(_absent_host_tools()),
    reason=f"the host shell lacks {', '.join(_absent_host_tools())}",
)


def _tree(root: Path) -> list[str]:
    """Every path under ``root``, relative and sorted — links never followed.

    A missing ``root`` is an empty tree rather than an error, because half the
    assertions below are "this directory was never created".
    """
    found: list[str] = []
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        for name in list(subdirectories) + files:
            found.append(str(Path(directory, name).relative_to(root)))
    return sorted(found)


def _staging_of(anchor: HostBackedAnchor) -> Path:
    return anchor.root / STAGING_DIR_NAME


def _copy_in(
    provider: DockerProvider, path: str, payload: bytes = COPY_IN_PAYLOAD, **kwargs: Any
) -> None:
    kwargs.setdefault("expected_sha256", hashlib.sha256(payload).hexdigest())
    provider.write(COPY_IN_WORKSPACE, path, io.BytesIO(payload), **kwargs)


def _holds_the_payload(root: Path) -> list[str]:
    """Every file under ``root`` carrying the payload — the assertion that matters.

    Stated as "which files leaked" rather than "did anything leak" so a failure
    names the escape rather than merely reporting that one happened.
    """
    leaked = []
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            candidate = Path(directory, name)
            if not candidate.is_symlink() and candidate.read_bytes() == COPY_IN_PAYLOAD:
                leaked.append(str(candidate.relative_to(root)))
    return sorted(leaked)


@pytest.fixture
def volume_root(tmp_path: Path) -> Path:
    """The directory standing in for the workspace volume."""
    root = tmp_path / "volume"
    root.mkdir()
    return root


@pytest.fixture
def escape_root(tmp_path: Path) -> Path:
    """Where a job would like the caller's bytes to end up. It must stay empty."""
    escape = tmp_path / "outside"
    escape.mkdir()
    return escape


@pytest.fixture
def copy_in_anchor(volume_root: Path) -> HostBackedAnchor:
    return HostBackedAnchor(
        volume_root,
        {
            LABEL_WORKSPACE_ID: COPY_IN_WORKSPACE,
            LABEL_PROVIDER: "docker",
            LABEL_ROLE: ROLE_WORKSPACE,
            LABEL_ENVIRONMENT: profiles.resolve(profiles.DEFAULT_PROFILE),
        },
    )


@pytest.fixture
def copy_in_engine(copy_in_anchor: HostBackedAnchor) -> StubEngine:
    return StubEngine(copy_in_anchor)


@pytest.fixture
def copy_in_provider(copy_in_engine: StubEngine) -> DockerProvider:
    return DockerProvider(connect=lambda: copy_in_engine)


@requires_a_host_shell
class TestAnAncestorSymlinkNeverBecomesAnEscape:
    """Issue #10's shape, on the write path, at every depth it can occur.

    The read path's open bug is that it validates only the *final* tar entry, so
    ``link -> /etc`` followed by a read of ``link/hostname`` escapes: the entry
    named ``hostname`` is an ordinary file, and nothing ever looked at ``link``.
    Fixing that is a recorded non-goal of the copy-in change, which makes it all
    the more important to know whether the new direction inherited the same hole
    — so these tests plant the link at an *intermediate* component and never at
    the destination itself.

    The destination-is-a-link case is the inbound module's
    (``test_a_symlinked_destination_is_replaced_rather_than_written_through``)
    and is not repeated here; the immediate-parent case is
    ``test_a_parent_that_resolves_outside_the_volume_is_refused``. What is new is
    everything above the parent, where the finalize script's walk-up loop is the
    only thing looking.
    """

    def test_a_job_planted_ancestor_symlink_refuses_the_next_copy_in(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
        escape_root: Path,
    ) -> None:
        """The sequence, not the snapshot: it worked, a job intervened, it refuses.

        A link planted before the first copy-in would be caught by any check at
        all. The case worth pinning is the one where a workspace has already
        received files at that very path — so the caller has every reason to
        believe the destination is settled — and a job swaps the directory for a
        link out of the volume in between.
        """
        _copy_in(copy_in_provider, "results/first.bin")
        assert (volume_root / "results/first.bin").read_bytes() == COPY_IN_PAYLOAD

        # The job: keep the real directory, point the name somewhere else.
        (volume_root / "results").rename(volume_root / "results.real")
        (volume_root / "results").symlink_to(escape_root)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "results/deep/second.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert caught.value.remediation
        assert _tree(escape_root) == [], "a copy-in wrote through a link out of the volume"
        assert (volume_root / "results.real/first.bin").read_bytes() == COPY_IN_PAYLOAD
        assert _tree(_staging_of(copy_in_anchor)) == []

    @pytest.mark.parametrize(
        ("link_at", "destination", "prepared_beyond"),
        [
            # The walk-up: nothing below the link exists, so the loop has to
            # climb past two missing components before it finds a directory.
            ("link", "link/deep/planted.bin", None),
            # The far side already has the shape, so the loop stops *on the far
            # side of the link* and the resolve is the only thing that notices.
            ("link", "link/deep/planted.bin", "deep"),
            # Deeper in an otherwise ordinary tree: the link is neither the
            # first component nor the parent.
            ("results/link", "results/link/deep/planted.bin", None),
            # Several missing levels, so a check that only looked one component
            # up from the destination would see nothing at all.
            ("link", "link/a/b/c/planted.bin", None),
        ],
    )
    def test_an_intermediate_ancestor_symlink_is_refused_wherever_it_sits(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
        escape_root: Path,
        link_at: str,
        destination: str,
        prepared_beyond: str | None,
    ) -> None:
        """Four positions for one link, and the same refusal from every one.

        Parametrised over *position* rather than over target, because position
        is what the finalize script's walk-up loop actually varies over: how many
        components it climbs before it finds something to resolve, and whether it
        stops on the near or the far side of the link.
        """
        if prepared_beyond is not None:
            (escape_root / prepared_beyond).mkdir()
        planted = volume_root / link_at
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.symlink_to(escape_root)
        before = _tree(escape_root)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, destination)

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert COPY_IN_WORKSPACE in caught.value.message
        assert _holds_the_payload(escape_root) == []
        assert _tree(escape_root) == before, "the copy-in created something outside the volume"
        assert _tree(_staging_of(copy_in_anchor)) == []

    def test_the_volume_boundary_is_a_path_boundary_and_not_a_string_prefix(
        self,
        copy_in_provider: DockerProvider,
        volume_root: Path,
        tmp_path: Path,
    ) -> None:
        """A neighbour whose name merely starts with the volume root's is outside it.

        The check is a shell ``case`` against ``$root`` and ``$root/*``, and the
        classic way to get that wrong is to compare prefixes: ``/workspace-evil``
        starts with ``/workspace`` and is a different directory entirely. A job
        cannot create the container's ``/workspace-evil``, but it does not have
        to — the pattern is what is being pinned, and a rewrite that reached for
        a plain ``case "$resolved" in "$root"*`` would ship this hole.
        """
        next_door = tmp_path / f"{volume_root.name}-next-door"
        next_door.mkdir()
        (volume_root / "link").symlink_to(next_door)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "link/deep/planted.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert _tree(next_door) == []

    def test_a_relative_ancestor_symlink_out_of_the_volume_is_refused_too(
        self, copy_in_provider: DockerProvider, volume_root: Path, escape_root: Path
    ) -> None:
        """``../`` in a link target is resolved, not read — so it must be caught.

        ``require_workspace_path`` refuses ``..`` in the caller's own path, which
        is the seam's answer to a textual escape. A link's *target* never passes
        through that check: it is a job's string, interpreted by the kernel. Only
        the in-container resolve sees it.
        """
        (volume_root / "link").symlink_to(f"../{escape_root.name}")

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "link/deep/planted.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert _holds_the_payload(escape_root) == []
        assert _tree(escape_root) == []

    def test_an_ancestor_symlink_that_stays_inside_the_volume_is_honoured(
        self, copy_in_provider: DockerProvider, volume_root: Path
    ) -> None:
        """The control, and the reason every refusal above means something.

        A backend that refused *every* symlinked ancestor would pass all four
        escape tests and be wrong: the rule is "resolves inside the volume", not
        "contains no links". Without this case the suite could not tell the two
        apart, and the cheapest way to make a security test green — refuse more —
        would go unnoticed.
        """
        (volume_root / "results").mkdir()
        (volume_root / "inside").symlink_to(volume_root / "results")

        _copy_in(copy_in_provider, "inside/deep/final.csv")

        assert (volume_root / "results/deep/final.csv").read_bytes() == COPY_IN_PAYLOAD

    def test_a_parent_planted_inside_the_race_window_costs_a_directory_never_a_payload(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
        escape_root: Path,
        tmp_path: Path,
    ) -> None:
        """The race the implementation says it narrowed but could not close.

        The finalize script resolves the deepest existing ancestor, runs
        ``mkdir -p``, then resolves the parent again. Between the first resolve
        and the ``mkdir`` there is a window, and no POSIX shell can close it. So
        the guarantee is not "the race cannot be lost" — it is that losing it
        costs an empty directory and never a byte of the caller's payload,
        because the second resolve gates the rename.

        That is a claim about what happens *after* the window, which means it can
        only be tested by actually losing the race. A shim ``realpath`` on the
        script's ``PATH`` plants the link at the one instant that matters: it
        answers the first resolve truthfully and creates the link on its way out,
        so the script proceeds on an answer that was correct when it was given
        and false by the time it is used. The assertions are then the whole
        point — the escape directory exists (the race really was lost) and holds
        nothing (the guarantee held anyway).
        """
        shim = tmp_path / "shimmed-bin"
        shim.mkdir()
        for tool in REQUIRED_WRITE_TOOLS:
            if tool != "realpath":
                (shim / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        (shim / "realpath").write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "{volume_root}" ]; then\n'
            f'    {shutil.which("ln")} -s "{escape_root}" "{volume_root}/late" || true\n'
            "fi\n"
            f'exec {shutil.which("realpath")} "$@"\n'
        )
        (shim / "realpath").chmod(0o755)
        copy_in_anchor.path_override = str(shim)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "late/deep/planted.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        # The race was genuinely lost: the link is there, and ``mkdir -p`` ran
        # through it. Without this the test could pass by catching the plant at
        # the first resolve, which would prove nothing about the window.
        assert (volume_root / "late").is_symlink()
        assert _tree(escape_root) == ["deep"], "the lost race cost more than an empty directory"
        assert _holds_the_payload(escape_root) == []
        assert _tree(_staging_of(copy_in_anchor)) == []


@requires_a_host_shell
class TestOnlyVerifiedBytesAreCommitted:
    """What a job can do to the staged file between the transfer and the rename."""

    def test_a_swap_leaves_an_existing_destination_untouched_even_under_overwrite(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
    ) -> None:
        """The destructive half of the digest check, which the fresh case cannot show.

        The inbound module proves a swap is caught and that nothing appears at a
        destination that was empty. The dangerous case is the other one: a caller
        replacing a file it already has. ``overwrite=True`` is a licence to
        destroy something, and a finalize that unlinked the destination before it
        verified — or that renamed and then checked — would lose the old bytes to
        buy nothing. The refusal has to leave the workspace exactly as it was.
        """
        existing = b"a job produced this, and nothing has exported it\n"
        (volume_root / "out.bin").write_bytes(existing)

        def a_job_rewrites_the_staged_file(root: Path) -> None:
            for staged in (root / STAGING_DIR_NAME).glob(f"*/{STAGED_FILE_NAME}"):
                staged.write_bytes(b"swapped by a job that shares the volume\n")

        copy_in_anchor.after_put = a_job_rewrites_the_staged_file

        with pytest.raises(ProviderError) as caught:
            _copy_in(copy_in_provider, "out.bin", overwrite=True)

        assert caught.value.code == EXIT_INFRASTRUCTURE_FAILURE
        assert (volume_root / "out.bin").read_bytes() == existing
        assert not (volume_root / "out.bin").is_symlink()
        assert _tree(_staging_of(copy_in_anchor)) == []

    def test_a_staged_payload_swapped_for_a_link_is_never_committed(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
        escape_root: Path,
    ) -> None:
        """The swap the digest check does not see, because it certifies the wrong thing.

        A job does not need to know the payload to do this: it copies the staged
        bytes out of the volume and links back to them, so ``sha256sum`` computes
        exactly the digest the caller declared. What it has bought is that the
        verified bytes and the committed path are no longer the same object — the
        digest certifies a file the job still owns, and can rewrite the moment the
        copy-in returns.

        The property asserted is the narrow one that survives either outcome: a
        copy-in may refuse, or it may land the caller's bytes, but the one thing
        it may never do is commit a path that resolves outside the workspace
        volume. That is the same rule the ancestor-symlink tests above enforce
        for the destination's parents, applied to the destination itself.
        """
        smuggled = escape_root / "job-controlled"

        def a_job_swaps_the_staged_file_for_a_link(root: Path) -> None:
            for staged in (root / STAGING_DIR_NAME).glob(f"*/{STAGED_FILE_NAME}"):
                smuggled.write_bytes(staged.read_bytes())
                staged.unlink()
                staged.symlink_to(smuggled)

        copy_in_anchor.after_put = a_job_swaps_the_staged_file_for_a_link

        with contextlib.suppress(CliError):
            _copy_in(copy_in_provider, "out.bin")

        landed = volume_root / "out.bin"
        assert not landed.is_symlink(), (
            "the copy-in committed a symlink resolving outside the workspace volume: "
            f"{landed} -> {landed.readlink() if landed.is_symlink() else ''}"
        )


@requires_a_host_shell
class TestPuttingOntoAPathTheWorkspaceAlreadyHolds:
    """Refusing by default is the protection; naming the path is what makes it usable."""

    def test_an_existing_destination_is_refused_with_the_path_the_caller_named(
        self,
        copy_in_provider: DockerProvider,
        copy_in_engine: StubEngine,
        copy_in_anchor: HostBackedAnchor,
        volume_root: Path,
    ) -> None:
        """The path in the refusal is the caller's own, not the container's.

        Two halves, and the second is the one that rots. A refusal has to name
        *which* path was already taken — with several files copied into one
        workspace, "something already exists" is not actionable — and it has to
        name it the way the caller wrote it. ``/workspace/results/deep/final.csv``
        is an engine-side layout headspace promises nobody, and a caller that
        pasted it back into the next command would be refused for a new reason.
        """
        relative = "results/deep/final.csv"
        existing = b"a job produced this\n"
        (volume_root / "results/deep").mkdir(parents=True)
        (volume_root / relative).write_bytes(existing)
        before = copy_in_engine.objects()

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, relative)

        assert caught.value.code == EXIT_USER_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert relative in caught.value.message
        assert "overwrite" in caught.value.remediation
        rendered = f"{caught.value.message} {caught.value.remediation}"
        assert WORKSPACE_MOUNT_PATH not in rendered
        assert (volume_root / relative).read_bytes() == existing
        assert _tree(_staging_of(copy_in_anchor)) == []
        assert copy_in_engine.objects() == before
        assert copy_in_engine.created == []

    def test_a_dangling_link_at_the_destination_is_something_the_workspace_already_holds(
        self, copy_in_provider: DockerProvider, volume_root: Path, escape_root: Path
    ) -> None:
        """A broken link is not an empty slot, and the difference is the caller's to make.

        ``[ -e ]`` is false for a link whose target does not exist, so a check
        written with that test alone would treat a job's planted link as free
        space and silently consume it. The refusal has to be driven by ``[ -L ]``
        as well — and then ``overwrite`` has to genuinely replace the link rather
        than write through it, which is the same rename-the-link property the
        live destination case rests on.
        """
        target = escape_root / "never-created"
        (volume_root / "out.bin").symlink_to(target)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "out.bin")

        assert caught.value.code == EXIT_USER_ERROR
        assert "out.bin" in caught.value.message
        assert (volume_root / "out.bin").is_symlink()
        assert not target.exists()

        _copy_in(copy_in_provider, "out.bin", overwrite=True)

        assert not (volume_root / "out.bin").is_symlink()
        assert (volume_root / "out.bin").read_bytes() == COPY_IN_PAYLOAD
        assert not target.exists(), "overwrite wrote through the link instead of replacing it"


@requires_a_host_shell
class TestAnImageThatCannotFinishTheJobSaysSo:
    """Acceptance criterion 3, over the whole toolset rather than one member.

    The image is modelled by a ``PATH`` with one tool removed rather than by a
    second image: what is under test is the provider's preflight and its wording,
    not the engine's ability to run a smaller image. The inbound module pins the
    shape of that refusal for ``realpath``; what is pinned here is that the
    preflight actually covers *every* tool the finalize step goes on to use — a
    list that is easy to add to and easy to forget to check.
    """

    def _crippled_path(self, bin_dir: Path, without: str) -> str:
        bin_dir.mkdir()
        for tool in REQUIRED_WRITE_TOOLS:
            if tool != without:
                (bin_dir / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        return str(bin_dir)

    @pytest.mark.parametrize("missing", REQUIRED_WRITE_TOOLS)
    def test_every_tool_a_copy_in_needs_is_named_when_the_image_lacks_it(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        tmp_path: Path,
        missing: str,
    ) -> None:
        """One case per tool, because a preflight is only as wide as its list.

        Parametrising over the constant rather than restating it is what makes
        this hold: adding a tool to ``REQUIRED_WRITE_TOOLS`` without teaching the
        prepare script to check for it fails here immediately, instead of
        surfacing later as an unexplained engine failure on a distroless image.
        """
        copy_in_anchor.path_override = self._crippled_path(tmp_path / "bin", without=missing)

        with pytest.raises(CliError) as caught:
            _copy_in(copy_in_provider, "out.bin")

        assert caught.value.code == EXIT_ENV_ERROR
        assert not isinstance(caught.value, ProviderError)
        assert missing in caught.value.message
        assert profiles.DEFAULT_PROFILE in caught.value.message
        assert COPY_IN_WORKSPACE in caught.value.message
        # The remediation has to be answerable: the whole toolset and the shell,
        # so an operator can fix the profile in one pass rather than one refusal
        # at a time.
        assert all(tool in caught.value.remediation for tool in REQUIRED_WRITE_TOOLS)
        assert "shell" in caught.value.remediation
        assert copy_in_anchor.put_targets == [], "bytes moved before the image was checked"
        assert _tree(_staging_of(copy_in_anchor)) == []

    def test_an_image_that_can_no_longer_write_can_still_be_reaped(
        self, copy_in_provider: DockerProvider, copy_in_anchor: HostBackedAnchor, tmp_path: Path
    ) -> None:
        """The subset claim, executed rather than compared.

        The inbound module asserts ``REQUIRED_REAP_TOOLS < REQUIRED_WRITE_TOOLS``
        as a set relation. That is the design, but the behaviour it is supposed to
        buy is this: an image that has lost the ability to *write* is exactly the
        image most likely to be holding residue from a copy-in that failed, and
        reconciliation still has to clear it. A reaper that reused the wider list
        would refuse precisely where cleaning up matters most, and the set
        comparison alone would not notice.
        """
        residue = _staging_of(copy_in_anchor) / ("b" * 32)
        residue.mkdir(parents=True)
        (residue / STAGED_FILE_NAME).write_bytes(COPY_IN_PAYLOAD)

        crippled = tmp_path / "bin"
        crippled.mkdir()
        for tool in REQUIRED_REAP_TOOLS:
            (crippled / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        copy_in_anchor.path_override = str(crippled)

        reaped = copy_in_provider.reap_staging(COPY_IN_WORKSPACE)

        assert reaped == (f"{STAGING_DIR_NAME}/{'b' * 32}",)
        assert _tree(_staging_of(copy_in_anchor)) == []

    @pytest.mark.parametrize("missing", REQUIRED_REAP_TOOLS)
    def test_a_reap_an_image_cannot_perform_says_so_and_leaves_the_residue(
        self,
        copy_in_provider: DockerProvider,
        copy_in_anchor: HostBackedAnchor,
        tmp_path: Path,
        missing: str,
    ) -> None:
        """Residue left in place is the honest report; a silent empty result is not.

        A reconciler that could not tell "nothing was there" from "nothing could
        be done" would report a clean workspace over residue that is still
        sitting in the caller's volume — so the refusal is named, and the residue
        is asserted still present rather than assumed to be.
        """
        residue = _staging_of(copy_in_anchor) / ("c" * 32)
        residue.mkdir(parents=True)
        (residue / STAGED_FILE_NAME).write_bytes(COPY_IN_PAYLOAD)

        crippled = tmp_path / "bin"
        crippled.mkdir()
        for tool in REQUIRED_REAP_TOOLS:
            if tool != missing:
                (crippled / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        copy_in_anchor.path_override = str(crippled)

        with pytest.raises(CliError) as caught:
            copy_in_provider.reap_staging(COPY_IN_WORKSPACE)

        assert caught.value.code == EXIT_ENV_ERROR
        assert missing in caught.value.message
        assert profiles.DEFAULT_PROFILE in caught.value.message
        assert _tree(_staging_of(copy_in_anchor)) != []


# --- the mount posture the copy-in was built *not* to weaken ----------------


class _MaximallyCapableEngine:
    """An engine that says yes to everything, so a ``False`` cannot be its fault.

    The point of answering every probe optimistically is that
    ``filesystem_scope_enforceable`` must come back ``False`` anyway: this
    backend offers no bind mount to restrict, so the honest answer is a property
    of the *provider*, not a shortfall of the host it happens to run on. A stub
    that answered nothing would prove the same ``False`` for the wrong reason.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def version(self) -> dict[str, str]:
        self.touched.append("version")
        return {"Version": "99.0.0", "ApiVersion": "1.99"}

    def info(self) -> dict[str, object]:
        self.touched.append("info")
        return {
            "Plugins": {"Network": ["bridge", "host", "null", "overlay"], "Volume": ["local"]},
            "MemoryLimit": True,
            "CpuCfsQuota": True,
            "CpuCfsPeriod": True,
            "PidsLimit": True,
        }

    def close(self) -> None:
        self.touched.append("close")


class TestAllowHostPathStillFailsClosed:
    """The boundary the copy-in exists to make unnecessary, and must not have moved.

    Issue #14 was filed because ``--allow-host-path`` fails closed on docker, and
    the filer endorsed that refusal rather than asking for it to be lifted. The
    copy-in fills the gap the refusal leaves. These tests are the statement that
    it did not also widen the hole it was routed around: the refusal is still the
    same one, and the container configuration still carries no bind mount of any
    kind. Neither needs an engine, which is the point — a posture assertion that
    only ran where a daemon was reachable would be absent from exactly the lane
    that runs on every commit.
    """

    def test_no_filesystem_scope_is_enforceable_however_capable_the_engine_claims_to_be(
        self,
    ) -> None:
        engine = _MaximallyCapableEngine()
        snapshot = DockerProvider(connect=lambda: engine).capabilities()

        assert snapshot.filesystem_scope_enforceable is False
        # ...and the stub really was maximal, so the False above is structural.
        assert snapshot.network_disable_supported is True
        assert snapshot.memory_enforceable is True
        assert snapshot.cpu_enforceable is True
        assert snapshot.pids_enforceable is True
        assert engine.touched == ["version", "info", "close"]

    def test_a_declared_host_path_is_refused_as_policy_denied_before_anything_is_made(
        self,
    ) -> None:
        """Exit 3, the taxonomy's own slot, and the paths named in the refusal."""
        snapshot = DockerProvider(connect=_MaximallyCapableEngine).capabilities()
        declared = Policy(filesystem=FilesystemScope(host_paths=("/etc", "/srv/data")))

        with pytest.raises(PolicyError) as caught:
            resolve_policy(declared, snapshot)

        assert caught.value.code == EXIT_POLICY_DENIED
        assert caught.value.to_dict()["category"] == "policy_denied"
        assert isinstance(caught.value, CliError)
        rendered = f"{caught.value.message} {caught.value.remediation}"
        assert "filesystem" in rendered
        assert "/etc" in rendered and "/srv/data" in rendered

    def test_the_only_filesystem_scope_that_survives_resolution_is_the_empty_one(self) -> None:
        """Why ``_sealed_kwargs`` can be trusted with no host-path branch at all.

        The builder below has nowhere to put a bind mount because no policy
        carrying one ever reaches it: ``resolve`` refuses every non-empty scope
        against this backend's snapshot. Pinning that here is what makes the
        absence in ``_sealed_kwargs`` a guarantee rather than an oversight nobody
        has exercised yet.
        """
        snapshot = DockerProvider(connect=_MaximallyCapableEngine).capabilities()

        assert requested_limit(resolve_policy(Policy(), snapshot), "filesystem") == ()
        for scope in (("/",), ("/etc",), ("/home/agent", "/srv")):
            with pytest.raises(PolicyError):
                resolve_policy(Policy(filesystem=FilesystemScope(host_paths=scope)), snapshot)

    @pytest.mark.parametrize("network_enabled", [False, True])
    def test_the_sealed_container_configuration_carries_no_bind_of_any_kind(
        self, network_enabled: bool
    ) -> None:
        """Asserted on the kwargs handed to the SDK, upstream of any engine.

        ``TestClosedByDefaultPosture`` reads the same properties back off a live
        ``HostConfig``, which is the stronger evidence and needs a daemon. This
        reads the dictionary the provider composed, which needs nothing — and it
        catches the case the engine reading cannot: a key added here that the
        engine would silently accept, on a host where nobody ran the live lane.
        """
        provider = DockerProvider(connect=_MaximallyCapableEngine)
        policy = resolve_policy(Policy(), provider.capabilities())

        kwargs = provider._sealed_kwargs(policy, network_enabled, COPY_IN_WORKSPACE)

        for forbidden in ("binds", "volumes_from", "devices", "device_requests", "cap_add"):
            assert forbidden not in kwargs, f"the sealed configuration grew a {forbidden}"
        assert kwargs["privileged"] is False
        assert "no-new-privileges" in kwargs["security_opt"]
        assert kwargs["network_mode"] == ("bridge" if network_enabled else "none")

        (only,) = kwargs["mounts"]
        assert only["Type"] == "volume"
        assert only["Source"] == f"{OBJECT_PREFIX}{COPY_IN_WORKSPACE}"
        assert only["Target"] == WORKSPACE_MOUNT_PATH

        serialised = repr(kwargs)
        assert "docker.sock" not in serialised
        assert "/var/run" not in serialised
        assert "bind" not in serialised.lower()


def test_the_copy_in_scripts_run_under_the_shell_every_workspace_image_already_needs() -> None:
    """No new demand on a profile: the copy-in's shell is the anchor's own.

    A workspace that could be created can always be written to — that is the
    whole reason the tool preflight is about ``sha256sum`` and friends rather
    than about the interpreter. If the two ever drifted, a profile could satisfy
    ``create`` and then refuse every copy-in, which is the confusing kind of
    failure this repo's taxonomy exists to prevent.
    """
    assert IDLE_COMMAND[0] == WRITE_SHELL
