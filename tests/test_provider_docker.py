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

import functools
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import docker
import pytest

from headspace.cli._errors import EXIT_INFRASTRUCTURE_FAILURE
from headspace.core import profiles
from headspace.core.policy import (
    EffectivePolicy,
    LimitStatus,
    NetworkPosture,
    Policy,
    ResourceBudget,
)
from headspace.core.policy import resolve as resolve_policy
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
    LABEL_CAPABILITY_PREFIX,
    LABEL_ENVIRONMENT,
    LABEL_LIMIT_PREFIX,
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    LOG_FILE_COUNT,
    OBJECT_PREFIX,
    ROLE_JOB,
    ROLE_WORKSPACE,
    WORKSPACE_MOUNT_PATH,
    DockerProvider,
    engine_unavailable,
    log_cap_bytes,
)
from tests.conformance import ProviderCase, ProviderConformance

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
    """
    yield
    if _engine_reason():
        return
    client = docker.from_env()
    try:
        for container in client.containers.list(all=True, filters={"label": LABEL_WORKSPACE_ID}):
            if _owned(container.labels.get(LABEL_WORKSPACE_ID, "")):
                container.remove(force=True)
        for volume in client.volumes.list(filters={"label": LABEL_WORKSPACE_ID}):
            if _owned((volume.attrs.get("Labels") or {}).get(LABEL_WORKSPACE_ID, "")):
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
            slow_command=("sleep", "30"),
            slow_seconds=1,
            flooding_command=("/bin/sh", "-c", "yes headspace | head -c 200000"),
            flood_budget_bytes=4096,
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

    @pytest.mark.parametrize("verb", ["capabilities", "create", "run", "inspect", "remove"])
    def test_every_verb_raises_provider_error(self, unreachable: DockerProvider, verb: str) -> None:
        policy = resolve_policy(Policy(), _snapshot_without_an_engine())
        dead = f"{POSTURE_PREFIX}-unreachable"
        calls = {
            "capabilities": lambda: unreachable.capabilities(),
            "create": lambda: unreachable.create(dead, "python:3.12-slim", policy),
            "run": lambda: unreachable.run(dead, ("true",), policy, job_id="dead-job"),
            "inspect": lambda: unreachable.inspect(dead),
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
