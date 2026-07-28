"""Docker integration suite: the orchestrated path, against a live engine.

WHY this module exists, and what it is NOT
-------------------------------------------
``tests/test_provider_docker.py`` (with ``tests/conformance.py`` behind it)
already proves the Docker *provider* in isolation: ``HostConfig`` assertions
against the raw engine, the capability probe, the log-driver cap, the
read-back verb streaming under ``tracemalloc``. None of that is restated
here. This module calls :class:`~headspace.core.workspace.Orchestrator`
instead of :class:`~headspace.providers.docker.DockerProvider` directly, so
every test below also exercises the journal, the state store, the lifecycle
table and policy resolution that sit in front of the provider — the layer a
caller (the CLI, an agent) actually goes through. Where a behaviour is
already pinned at the provider level, this module verifies it survives the
orchestration layer's own translation (a *declared* ``Policy`` resolved by
``Orchestrator.create`` rather than an ``EffectivePolicy`` a test built by
hand) or checks a claim the provider-level suite structurally cannot make at
all (a *real* crash between the engine call and the state write; *real*
threads racing one workspace; a *real* gigabyte-scale flood measured against
what lands in headspace's own persisted record).

The six honesty conditions, one test group each
------------------------------------------------
h6
    egress from a default workspace fails (a job really tries and really
    cannot reach the network — not "NetworkMode is none", which
    ``test_provider_docker.py`` already checks); a caller's *declared*
    ``Policy`` budget lands on the engine's own workspace container by way of
    ``Orchestrator.create``'s policy resolution; a policy the host cannot
    back is refused before ``create`` ever calls the engine.
h15
    a host that cannot enforce a requested limit fails ``create`` rather than
    silently running the workspace unlimited (checked by proving no engine
    object exists afterwards, not just that an exception was raised); the
    effective-policy view the caller actually sees —
    ``ResultPackage.provenance.policy_summary``, not a hand-inspected
    ``EffectiveLimit`` — labels storage ``measured`` against the *real*
    engine's real answer.
h16
    a crash between the engine call and the state write (a ``KeyboardInterrupt``
    raised from inside ``Store.write_state``, exactly as ``tests/test_workspace.py``
    injects it — see that module's docstring) leaves a real orphan container on
    the real engine; a fresh ``Orchestrator`` over a fresh ``Store`` and a fresh
    ``DockerProvider`` — sharing nothing with the crashed invocation but the
    store root on disk and the still-live engine — adopts it and reports it.
h18
    genuinely concurrent threads, each with its own ``Store`` and
    ``DockerProvider`` (the in-process analogue of independent CLI
    invocations racing the same on-disk lock), running jobs against one
    workspace: the store never corrupts and a live poll of the engine proves
    at most one job container is ever alive at once. Separately, destroying a
    workspace while a job is genuinely running (polled from the store, not
    assumed) is refused in the lifecycle table's own words, because
    ``running -> destroyed`` is the edge the table deliberately omits.
h19
    a job that really emits a large, bounded-log-cap-busting volume of output
    leaves only the capped capture in headspace's own persisted state file —
    not gigabytes of it — with ``truncated`` set, and the overflow is named in
    the result package's warnings.
h20
    a job launched through the orchestrator — not through the provider
    directly — carries no socket mount, no host bind mount, ``NetworkMode
    none`` by default, and exactly one writable mount: the workspace volume.

The two defects, pinned where they were found
----------------------------------------------
The last group is not an honesty condition. It is the pair of failures that
were found by hand against a live engine, and it lives here — rather than only
in ``tests/test_docker_classification.py``, where a stub engine scripts both —
because a stub can only produce the engine sentence a test author already
believed in. These reproductions get the sentence from the engine itself:
``definitely-not-a-binary`` really is absent from a real image's ``PATH``, and
a real 512 MiB allocation really is killed under a real 128 MiB cgroup ceiling.
If Docker ever changes the wording, the shape of its OOM state, or the status
it reports for an unrunnable command, these fail and the stub-backed suite does
not.

They assert at the **process** boundary — argv in, exit code and ``--json`` on
stdout out — because that is what a subprocess consumer actually reads, and
because both defects were failures of that surface: an exit code that sent an
agent down the wrong recovery path, and a result package that gave it nothing
to change. Every number they assert is written as a literal with the constant
it mirrors named in a comment, never imported: these tests are the fail-first
evidence for the fix, so they must stay runnable against a revision where
``EXIT_RESOURCE_EXHAUSTED`` does not exist yet. An import of a symbol the
pre-fix tree lacks turns a *failing* test into a collection *error*, and a
collection error proves nothing about behaviour.

Running without an engine, and leaving nothing behind
-------------------------------------------------------
Same discipline as ``test_provider_docker.py``: :func:`_skip_without_engine`
probes once (cached) and every fixture that needs a container skips inside
itself, so ``DOCKER_HOST=unix:///nonexistent/docker.sock uv run pytest -q``
must pass with skips, never a failure or a hang. Every workspace id this
module mints carries :data:`WORKSPACE_PREFIX`, a module-scoped reaper sweeps
anything still wearing it when the module finishes, and the ``workspace``
fixture removes what it made in every test's own teardown regardless of how
the test ended — so a leak survives neither a passing run nor a failing one.

``HEADSPACE_HOME`` is pointed at a fresh ``tmp_path`` in every test (the
``home`` fixture) — never the operator's real ``~/.headspace``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

from headspace.cli._errors import CliError
from headspace.core.policy import Policy, PolicyError, ResourceBudget
from headspace.core.profiles import DEFAULT_PROFILE
from headspace.core.result import STATUS_FAILURE, STATUS_SUCCESS
from headspace.core.states import State
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import DISPOSITION_ADOPTED, PHASE_INTENDED, Orchestrator
from headspace.providers.base import ProviderError
from headspace.providers.docker import (
    LABEL_ROLE,
    LABEL_WORKSPACE_ID,
    ROLE_JOB,
    ROLE_WORKSPACE,
    DockerProvider,
    engine_unavailable,
)

# --- engine reachability, probed once --------------------------------------

#: This process's slice of the engine's namespace, disjoint between
#: ``pytest -n auto`` workers and stable for a serial run — see
#: ``tests/test_provider_docker.py``'s module docstring for the rationale.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "solo")

#: Every workspace id this module mints starts with this. The reaper and the
#: per-test teardown both key off it, so a human reading ``docker ps`` (or a
#: test in this file failing) can tell this module's objects apart from any
#: other's.
WORKSPACE_PREFIX = f"hs-integ-{WORKER}"

#: Slow by unit-test standards (real containers, real polling, a real flood) —
#: identifies this file to anyone selecting or excluding by marker. Not
#: registered in ``pyproject.toml`` (out of scope for this task), which is
#: harmless: the suite runs without ``--strict-markers``.
pytestmark = pytest.mark.integration


@functools.cache
def _engine_reason() -> str:
    """Why this host has no engine, probed once; ``""`` when one answered."""
    return engine_unavailable()


def _skip_without_engine() -> None:
    reason = _engine_reason()
    if reason:
        pytest.skip(f"no reachable docker engine: {reason}")


@pytest.fixture(scope="module", autouse=True)
def _reap_engine_strays() -> Iterator[None]:
    """Force-remove anything still wearing :data:`WORKSPACE_PREFIX` when the module ends.

    A safety net under the per-test ``workspace`` teardown, not a replacement
    for it — the h16 crash test deliberately leaves an orphan mid-test, and a
    test that dies before its own teardown runs can strand one by accident.
    """
    yield
    if _engine_reason():
        return
    client = docker.from_env()
    try:
        for container in client.containers.list(
            all=True, filters={"label": LABEL_WORKSPACE_ID}, ignore_removed=True
        ):
            if container.labels.get(LABEL_WORKSPACE_ID, "").startswith(f"{WORKSPACE_PREFIX}-"):
                container.remove(force=True)
        for volume in client.volumes.list(filters={"label": LABEL_WORKSPACE_ID}):
            workspace_id = (volume.attrs.get("Labels") or {}).get(LABEL_WORKSPACE_ID, "")
            if workspace_id.startswith(f"{WORKSPACE_PREFIX}-"):
                volume.remove(force=True)
    finally:
        client.close()


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway store root. Never the operator's real ``~/.headspace``."""
    root = tmp_path / "headspace-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    return root


@pytest.fixture
def provider(home: Path) -> DockerProvider:
    _skip_without_engine()
    return DockerProvider()


@pytest.fixture
def engine(home: Path) -> Iterator[docker.DockerClient]:
    """A raw SDK client, for reading back what the orchestrated path actually built."""
    _skip_without_engine()
    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def orch(provider: DockerProvider) -> Orchestrator:
    return Orchestrator(provider, Store())


@pytest.fixture
def workspace(provider: DockerProvider) -> Iterator[Any]:
    """Mint prefixed workspace ids; force-remove their engine objects on teardown.

    Deliberately independent of ``Orchestrator.destroy`` and of whatever state
    the store thinks a workspace is in — a crash test or a refused-destroy test
    can leave either in an unusual place, and cleanup must not depend on the
    very machinery a test exists to interrupt.
    """
    minted: list[str] = []

    def make() -> str:
        workspace_id = f"{WORKSPACE_PREFIX}-{uuid.uuid4().hex[:10]}"
        minted.append(workspace_id)
        return workspace_id

    yield make

    if _engine_reason():
        return
    client = docker.from_env()
    try:
        for workspace_id in minted:
            for container in client.containers.list(
                all=True, filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
            ):
                with contextlib.suppress(Exception):
                    container.remove(force=True)
            for volume in client.volumes.list(
                filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
            ):
                with contextlib.suppress(Exception):
                    volume.remove(force=True)
    finally:
        client.close()


# --- small shared helpers ----------------------------------------------------


@dataclasses.dataclass(eq=False)
class _CrashingStore(Store):
    """A store that dies exactly where a killed CLI would.

    Identical technique to ``tests/test_workspace.py``'s ``CrashingStore``
    (read that module's docstring for the full rationale): ``KeyboardInterrupt``
    — a ``BaseException`` — is raised from inside :meth:`Store.write_state` at
    the write the caller names, before the write happens. Nothing in
    :mod:`headspace.core.workspace` catches anything but :class:`CliError`, so
    the flow unwinds with no compensating journal entry, precisely as a
    ``SIGINT``-killed process would leave it. Reimplemented locally (rather
    than imported from the sibling test module) so this file depends on
    nothing outside its own fixtures and the product code.
    """

    crash_before_state: str = ""

    def write_state(self, workspace_id: str, state: dict[str, Any]) -> Any:
        if state.get("state") == self.crash_before_state:
            raise KeyboardInterrupt(f"simulated kill before writing '{self.crash_before_state}'")
        return super().write_state(workspace_id, state)


def _open_intents(store: Store, workspace_id: str) -> list[dict[str, Any]]:
    """Journalled intents with no settling entry — the crash signature."""
    entries = [entry.entry for entry in store.read_journal(workspace_id)]
    settled = {e["intent_id"] for e in entries if e["phase"] != PHASE_INTENDED}
    return [e for e in entries if e["phase"] == PHASE_INTENDED and e["intent_id"] not in settled]


def _workspace_container(
    engine: docker.DockerClient, workspace_id: str
) -> docker.models.containers.Container:
    found = engine.containers.list(
        all=True,
        filters={
            "label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_WORKSPACE}"]
        },
    )
    assert len(found) == 1, f"expected exactly one workspace container, got {len(found)}"
    return found[0]


def _await_job_container(
    engine: docker.DockerClient, workspace_id: str, timeout: float = 30.0
) -> docker.models.containers.Container:
    """Poll for a job container while it is alive — the same technique
    ``test_provider_docker.py`` uses to catch a job mid-flight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = engine.containers.list(
            all=True,
            filters={"label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_JOB}"]},
        )
        if found:
            return found[0]
        time.sleep(0.02)
    raise AssertionError(f"no job container appeared for {workspace_id}")


def _wait_for_lifecycle_state(
    store: Store, workspace_id: str, state: State, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store.read_state(workspace_id).state.get("state") == state.value:
            return
        time.sleep(0.02)
    raise AssertionError(f"workspace {workspace_id} never reached '{state.value}' in {timeout}s")


# --- h6: closed by default, budgets round-trip, unsupported policy refused --


def test_h6_egress_from_a_workspace_created_by_the_orchestrator_fails(
    home: Path, orch: Orchestrator, workspace: Any
) -> None:
    """Not "NetworkMode is none" (already proven at the provider level) — a job
    run through the orchestrator genuinely cannot reach the network."""
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)

    package = orch.run(
        workspace_id,
        ("python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"),
        job_id="egress-attempt",
    )

    assert package.status == STATUS_FAILURE
    assert package.resource_usage is not None
    # A real attempt, not a job that never ran: it produced a traceback on
    # stderr naming exactly the failure a network-less container gives.
    assert "unreachable" in package.evidence[0].excerpt.lower()


def test_h6_a_callers_declared_budget_lands_on_the_engines_own_workspace_container(
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any
) -> None:
    """The caller only ever declares a ``Policy`` — this proves the number it
    wrote reaches the real engine after passing through ``Orchestrator.create``'s
    own resolution, not through an ``EffectivePolicy`` a test built by hand
    (that path is ``test_provider_docker.py``'s job)."""
    workspace_id = workspace()
    declared = Policy(
        budget=ResourceBudget(memory_bytes=333 * 1024 * 1024, cpu_limit=0.6, pids_limit=77)
    )

    orch.create(workspace_id=workspace_id, policy=declared)

    host_config = _workspace_container(engine, workspace_id).attrs["HostConfig"]
    assert host_config["Memory"] == 333 * 1024 * 1024
    assert host_config["NanoCpus"] == 600_000_000
    assert host_config["PidsLimit"] == 77


def test_h6_a_policy_the_host_cannot_enforce_fails_before_create_touches_the_engine(
    home: Path,
    provider: DockerProvider,
    workspace: Any,
    engine: docker.DockerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed-network policy the host claims it cannot back is refused by
    ``resolve()`` before ``Orchestrator.create`` journals anything or calls the
    engine — so no job, and no engine object, ever exists for it."""
    workspace_id = workspace()
    real = provider.capabilities()
    cannot_disable_network = dataclasses.replace(real, network_disable_supported=False)
    monkeypatch.setattr(provider, "capabilities", lambda: cannot_disable_network)
    orch = Orchestrator(provider, Store())

    with pytest.raises(PolicyError):
        orch.create(workspace_id=workspace_id)

    assert not orch.store.exists(workspace_id)
    assert not orch.store.read_journal(workspace_id)
    assert (
        engine.containers.list(all=True, filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"})
        == []
    )


# --- h15: fail closed rather than run unlimited; the honest effective view --


def test_h15_create_fails_rather_than_running_unlimited_when_a_limit_cannot_be_enforced(
    home: Path,
    provider: DockerProvider,
    workspace: Any,
    engine: docker.DockerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host claiming it cannot enforce memory must not silently create a
    memory-unlimited workspace — ``create`` fails, and nothing was made."""
    workspace_id = workspace()
    real = provider.capabilities()
    unenforceable = dataclasses.replace(real, memory_enforceable=False)
    monkeypatch.setattr(provider, "capabilities", lambda: unenforceable)
    orch = Orchestrator(provider, Store())

    with pytest.raises(PolicyError):
        orch.create(workspace_id=workspace_id)

    assert not orch.store.exists(workspace_id)
    assert (
        engine.containers.list(all=True, filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"})
        == []
    ), "a policy the host cannot back must leave no engine object behind, unlimited or otherwise"


def test_h15_the_effective_policy_view_in_the_result_package_labels_storage_measured(
    home: Path, orch: Orchestrator, workspace: Any
) -> None:
    """Against the real engine's real answer (the local volume driver reports
    usage without capping it): the caller-facing effective-policy view —
    ``provenance.policy_summary``, the surface a caller actually reads — must
    say so, and must not say it about a limit the real engine really enforces."""
    workspace_id = workspace()

    package = orch.create(workspace_id=workspace_id)

    summary = package.provenance.policy_summary
    assert "(measured)" in summary
    storage_clause = next(
        (clause for clause in summary.split(", ") if clause.startswith("storage=")), None
    )
    assert storage_clause is not None and "(measured)" in storage_clause
    memory_clause = next(
        (clause for clause in summary.split(", ") if clause.startswith("memory=")), None
    )
    assert memory_clause is not None and "(measured)" not in memory_clause
    measured_warnings = [w for w in package.warnings if "storage" in w and "measured" in w]
    assert measured_warnings, "a measured limit must also surface as a warning, not only a label"


# --- h16: a real crash leaves a real orphan; a fresh invocation adopts it ---


def test_h16_a_crash_between_the_engine_call_and_the_state_write_leaves_a_real_orphan_that_a_fresh_orchestrator_adopts(  # noqa: E501
    home: Path, provider: DockerProvider, workspace: Any, engine: docker.DockerClient
) -> None:
    workspace_id = workspace()
    store = Store()
    crashing = Orchestrator(
        provider, _CrashingStore(store.root, crash_before_state=State.READY.value)
    )

    with pytest.raises(KeyboardInterrupt):
        crashing.create(workspace_id=workspace_id)

    # The crash is real: the engine really holds a running container (the
    # engine call landed and was never undone), while the store's last
    # successful write left the lifecycle stale at 'provisioning', with a
    # journalled create intent that nothing closed.
    assert provider.inspect(workspace_id).state is State.READY
    assert _workspace_container(engine, workspace_id).status == "running"
    assert store.read_state(workspace_id).state["state"] == State.PROVISIONING.value
    (intent,) = _open_intents(store, workspace_id)
    assert intent["intent"] == "create"

    # A fresh Orchestrator, over a fresh Store, with a fresh DockerProvider —
    # sharing nothing with the crashed invocation but the store root on disk
    # and the still-live engine, the in-memory analogue of a new CLI process
    # against a running daemon.
    revived = Orchestrator(DockerProvider(), Store())
    package = revived.inspect(workspace_id)

    assert store.read_state(workspace_id).state["state"] == State.READY.value
    reported = [item for item in package.attention if workspace_id in item]
    assert reported, "silent adoption is as bad as a leak"
    assert DISPOSITION_ADOPTED in reported[0]
    assert not _open_intents(store, workspace_id), "reconciliation settles the intent it acted on"

    # Adoption completed the existing record; it did not mint a second engine
    # object for the same workspace id.
    assert (
        len(
            engine.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{LABEL_WORKSPACE_ID}={workspace_id}",
                        f"{LABEL_ROLE}={ROLE_WORKSPACE}",
                    ]
                },
            )
        )
        == 1
    )


# --- h18: genuine concurrency never corrupts state, and destroy refuses ----


def test_h18_concurrent_runs_against_one_workspace_never_double_start_a_job_or_corrupt_state(
    home: Path, orch: Orchestrator, workspace: Any, engine: docker.DockerClient
) -> None:
    """Real threads, each with its own ``Store`` and ``DockerProvider`` — the
    in-process analogue of independent CLI invocations — race one workspace's
    lock. A background poll of the live engine is the genuine evidence that at
    most one job container is ever alive at once, not an inference from the
    lock's existence."""
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)

    thread_count = 4
    barrier = threading.Barrier(thread_count)
    errors: list[BaseException] = []
    max_concurrent = 0
    concurrency_lock = threading.Lock()
    stop_watching = threading.Event()

    def watch() -> None:
        nonlocal max_concurrent
        watcher_client = docker.from_env()
        try:
            while not stop_watching.is_set():
                seen = len(
                    watcher_client.containers.list(
                        filters={
                            "label": [
                                f"{LABEL_WORKSPACE_ID}={workspace_id}",
                                f"{LABEL_ROLE}={ROLE_JOB}",
                            ]
                        }
                    )
                )
                with concurrency_lock:
                    max_concurrent = max(max_concurrent, seen)
                time.sleep(0.01)
        finally:
            watcher_client.close()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=15)
            Orchestrator(DockerProvider(), Store()).run(
                workspace_id, ("/bin/sh", "-c", "sleep 0.3"), job_id=f"race-{index}"
            )
        except BaseException as err:  # noqa: BLE001 - collected, never swallowed
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    stop_watching.set()
    watcher.join(5)

    assert not errors, f"concurrent runs raised: {errors!r}"
    assert max_concurrent <= 1, (
        f"observed {max_concurrent} job containers alive at the same time — "
        "a job was double-started"
    )

    record = Store().read_state(workspace_id).state
    assert record["state"] == State.READY.value
    job_ids = {job["job_id"] for job in record["jobs"]}
    assert job_ids == {f"race-{i}" for i in range(thread_count)}, (
        "the store must hold exactly one settled record per concurrent job — "
        "a lost or duplicated one is corruption"
    )


def test_h18_destroy_refuses_while_a_job_is_genuinely_running_because_running_to_destroyed_is_absent(  # noqa: E501
    home: Path, provider: DockerProvider, workspace: Any
) -> None:
    """A real job, genuinely in flight (polled from the store, not assumed),
    makes destroy refuse in the lifecycle table's own words — never a bespoke
    message — because ``running -> destroyed`` is the edge the table
    deliberately omits (see ``headspace/core/states.py``)."""
    workspace_id = workspace()
    store = Store()
    Orchestrator(provider, store).create(workspace_id=workspace_id)

    job_done = threading.Event()

    def run_long_job() -> None:
        Orchestrator(DockerProvider(), Store()).run(workspace_id, ("sleep", "3"), job_id="long-job")
        job_done.set()

    runner = threading.Thread(target=run_long_job, daemon=True)
    runner.start()
    _wait_for_lifecycle_state(store, workspace_id, State.RUNNING, timeout=10)

    contender = Orchestrator(DockerProvider(), Store())
    with pytest.raises(CliError) as caught:
        contender.destroy(workspace_id)

    assert not isinstance(caught.value, ProviderError)
    assert "running" in caught.value.message and "destroyed" in caught.value.message

    # The refusal removed nothing: the job is still genuinely running.
    assert store.read_state(workspace_id).state["state"] == State.RUNNING.value
    assert provider.inspect(workspace_id).workspace_id == workspace_id

    runner.join(30)
    assert job_done.is_set(), "the job must have been left to finish, never interrupted"

    # And once it settles, the very same workspace destroys cleanly — the
    # refusal was about the state, not a permanent defect.
    settled = Orchestrator(DockerProvider(), Store()).destroy(workspace_id)
    assert settled.status == STATUS_SUCCESS


# --- h19: a genuine flood is bounded on disk, with a truncation marker -----


def test_h19_a_flood_of_output_leaves_only_a_bounded_capture_on_disk_with_a_truncation_marker(
    home: Path, orch: Orchestrator, workspace: Any
) -> None:
    """The job really emits hundreds of megabytes — read through the same
    live-attach capture path a caller's job always goes through, at the
    ~10 MB/s this engine sustains over it, which is why this stops short of a
    full gigabyte rather than because a smaller flood would prove less. What
    survives headspace's own persisted state file, and what the result
    package's warnings say about the rest, is what h19 is actually about."""
    workspace_id = workspace()
    flood_bytes = 300 * 1024 * 1024  # a real, sustained flood — 36,000x the output budget
    output_budget = 8192
    orch.create(
        workspace_id=workspace_id, policy=Policy(budget=ResourceBudget(output_bytes=output_budget))
    )

    package = orch.run(
        workspace_id,
        ("/bin/sh", "-c", f"yes headspace | head -c {flood_bytes}"),
        job_id="flood-job",
    )

    assert package.status == STATUS_SUCCESS
    assert (
        package.resource_usage.output_bytes >= flood_bytes * 0.99
    ), "the job did not really produce a large volume of output — the flood was not genuine"
    truncation_notes = [w for w in package.warnings if "truncat" in w]
    assert truncation_notes, "the overflow must surface in the result package's warnings"
    assert str(output_budget) in truncation_notes[0]

    record = Store().read_state(workspace_id).state
    kept = record["jobs"][-1]["output"]
    assert len(kept.encode("utf-8")) <= output_budget
    assert record["jobs"][-1]["truncated"] is True

    state_file = home / "workspaces" / workspace_id / "state.json"
    on_disk_bytes = state_file.stat().st_size
    assert on_disk_bytes < 1_000_000, (
        f"the persisted state file is {on_disk_bytes} bytes after a "
        f"{flood_bytes} byte flood — the bounded capture did not stay bounded on disk"
    )


# --- h20: every job container the orchestrator launches is sealed ----------


def test_h20_a_job_run_through_the_orchestrator_has_no_socket_no_host_binds_no_network_and_only_the_workspace_volume_writable(  # noqa: E501
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any
) -> None:
    """Caught mid-flight, the way ``test_provider_docker.py`` catches a job
    container: this one is launched through ``Orchestrator.run``, so what is
    asserted is that the posture survives the caller's declared ``Policy``
    being resolved and journalled by the orchestration layer, not only that
    the provider builds a sealed ``HostConfig`` when called directly."""
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)

    runner = threading.Thread(
        target=orch.run,
        args=(workspace_id, ("sleep", "5")),
        kwargs={"job_id": "sealed-probe"},
        daemon=True,
    )
    runner.start()
    try:
        job = _await_job_container(engine, workspace_id)
        host_config = job.attrs["HostConfig"]

        assert host_config["NetworkMode"] == "none"
        mounts = host_config.get("Mounts") or []
        assert len(mounts) == 1, f"expected exactly one mount, got {mounts}"
        (only,) = mounts
        assert only["Type"] == "volume"
        assert only["Target"] == "/workspace"
        assert not only.get("ReadOnly"), "the workspace volume must be writable"

        assert not host_config.get("Binds")
        assert not host_config.get("VolumesFrom")
        serialised = repr(host_config)
        assert "docker.sock" not in serialised
        assert "/var/run" not in serialised
        assert not host_config.get("Devices")
        assert not host_config.get("DeviceRequests")
        assert host_config.get("Privileged") is False
    finally:
        runner.join(60)
    assert not runner.is_alive()


# --- the two defects from live testing, pinned at the process boundary ------

#: The exit codes asserted below, written as literals on purpose — see this
#: module's docstring. Each mirrors the like-named constant in
#: ``headspace.cli._errors``; the comment is the only link, and that is the
#: point, because importing the constant would make these tests uncollectable
#: on the revision they exist to fail against.
_EXIT_COMPUTATION_FAILED = 6  # mirrors _errors.EXIT_COMPUTATION_FAILED
_EXIT_INFRASTRUCTURE_FAILURE = 7  # mirrors _errors.EXIT_INFRASTRUCTURE_FAILURE
_EXIT_RESOURCE_EXHAUSTED = 8  # mirrors _errors.EXIT_RESOURCE_EXHAUSTED

#: Likewise for the status this change introduced. ``STATUS_FAILURE`` is
#: imported at the top of this module because it predates the change; this one
#: cannot be, for the same reason the exit codes cannot.
_STATUS_RESOURCE_EXHAUSTED = "resource_exhausted"  # mirrors result.STATUS_RESOURCE_EXHAUSTED

#: The command's own exit statuses for the two shapes an image cannot run.
#: Both are POSIX shell convention, not headspace's invention, and both map to
#: the *same* process exit code — which is exactly why
#: :func:`test_regression_process_exit_six_and_the_commands_own_exit_status_stay_two_numbers`
#: exists.
_STATUS_COMMAND_NOT_FOUND = 127
_STATUS_NOT_EXECUTABLE = 126

#: The ceiling from the original reproduction, and an allocation four times
#: over it — a decisive breach, not a near-miss another host might let through.
_MEMORY_CEILING_BYTES = 134217728  # 128 MiB
_OVERSHOOT_BYTES = 512 * 1024 * 1024

#: A ``DOCKER_HOST`` that resolves to nothing, so the fourth outcome below is a
#: genuinely broken engine rather than a simulated one. Same address the
#: no-engine run named in this module's docstring uses.
_UNREACHABLE_ENGINE = "unix:///nonexistent/docker.sock"

#: What the first defect leaked into a caller's context: the raw engine handles
#: from a ``docker.errors.APIError``, and the retry hint an autonomous agent
#: trusted into an endless loop on a deterministic failure. Literals rather
#: than imports of the product's own wording — a test that quotes the code it
#: guards agrees with it by construction and guards nothing.
_ENGINE_HANDLE_MARKERS = ("http+docker", "400 Client Error")
_RETRY_HINT_FRAGMENT = "the execution engine failed, not the job"

#: Generous: a cold ``create`` pulls the profile's pinned image, and every
#: invocation below pays a fresh interpreter start. A bound rather than no
#: bound so a wedged engine fails the test instead of hanging the suite.
_CLI_TIMEOUT_SECONDS = 300.0


def _cli(
    *argv: str, home: Path, docker_host: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Drive the CLI out of process — argv in, exit code and streams out.

    Not ``headspace.cli.main`` in-process: the exit code is the whole subject
    of these tests, and in-process that number is a return value a test could
    read even if ``main`` never propagated it. Here it is the real thing the
    kernel reports, read the way a subprocess consumer reads it.

    ``env`` is inherited rather than minimised (the idiom
    ``tests/test_cli_verbs.py`` uses for its no-SDK-import proof) because these
    tests need the operator's real engine settings — everything except
    ``HEADSPACE_HOME``, which is forced to the per-test throwaway root, and
    ``DOCKER_HOST`` when a caller deliberately breaks it.
    """
    env = dict(os.environ)
    env[HOME_ENV_VAR] = str(home)
    if docker_host is not None:
        env["DOCKER_HOST"] = docker_host
    return subprocess.run(
        [sys.executable, "-m", "headspace", *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=_CLI_TIMEOUT_SECONDS,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _create_workspace(home: Path, workspace_id: str, *extra: str) -> None:
    created = _cli("create", "--workspace-id", workspace_id, "--json", *extra, home=home)
    assert created.returncode == 0, f"create failed: {created.stderr}"


def _package(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The ``--json`` result package a consumer parses off stdout."""
    assert result.stdout, f"nothing on stdout to parse; stderr was: {result.stderr}"
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _findings(package: dict[str, Any]) -> str:
    return " | ".join(package["key_findings"])


def _recorded_job(home: Path, job_id: str) -> dict[str, Any]:
    """The job record ``headspace inspect <job> --logs --json`` hands back.

    The one caller-facing surface carrying the *command's* own exit status as a
    number rather than inside a sentence, which is what lets the pairing below
    be asserted rather than substring-matched.
    """
    logs = _cli("inspect", job_id, "--logs", "--json", home=home)
    assert logs.returncode == 0, f"inspect --logs failed: {logs.stderr}"
    (job,) = json.loads(logs.stdout)["jobs"]
    recorded: dict[str, Any] = job
    return recorded


def test_regression_a_command_the_image_cannot_run_exits_six_with_a_full_result_package(
    home: Path, workspace: Any
) -> None:
    """Defect 1, at the surface it was found on.

    ``headspace run <ws> definitely-not-a-binary`` used to exit 7
    (``infrastructure_failure``), hint "check the engine is running and
    reachable, then retry", and hand the caller a raw Docker API string. An
    agent that believed the hint retried a deterministic failure forever. It is
    a *computation* that failed — nothing about the engine was wrong — so it
    exits 6 and reports a full result package instead of an error, and that
    package names the profile that was asked and the executable it lacked,
    which is what makes the next attempt different from this one.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)

    result = _cli(
        "run",
        "--json",
        "--job-id",
        "job-not-runnable",
        workspace_id,
        "definitely-not-a-binary",
        home=home,
    )

    assert result.returncode == _EXIT_COMPUTATION_FAILED, (
        f"exit {result.returncode}, expected {_EXIT_COMPUTATION_FAILED}; "
        f"stderr: {result.stderr}"
    )
    package = _package(result)
    assert package["status"] == STATUS_FAILURE
    # A full result package, not an error payload: the profile that was asked,
    # and the name the caller typed, both from what headspace already knew.
    assert package["provenance"]["profile"] == DEFAULT_PROFILE
    findings = _findings(package)
    assert DEFAULT_PROFILE in findings
    assert "definitely-not-a-binary" in findings
    assert str(_STATUS_COMMAND_NOT_FOUND) in findings

    combined = result.stdout + result.stderr
    for marker in _ENGINE_HANDLE_MARKERS:
        assert marker not in combined, f"the engine handle {marker!r} reached the caller"
    assert _RETRY_HINT_FRAGMENT not in combined, "the hint that caused the retry loop is back"
    assert "infrastructure_failure" not in combined


def test_regression_an_allocation_over_the_ceiling_exits_eight_naming_the_ceiling_and_a_remedy(
    home: Path, workspace: Any
) -> None:
    """Defect 2, at the surface it was found on.

    A 512 MiB allocation under a 128 MiB ceiling used to exit 6 saying only
    "the command completed with exit status 137", with empty warnings and empty
    attention — indistinguishable from an ordinary failing computation, so the
    caller's only sane next move was to rerun the identical job. Three things
    have to hold for that to be fixed, and all three are asserted here: the
    exit code separates it from an ordinary failure, a key finding names the
    number that was breached, and attention names something the caller can
    actually change.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id, "--memory-bytes", str(_MEMORY_CEILING_BYTES))

    result = _cli(
        "run",
        "--json",
        "--job-id",
        "job-killed",
        workspace_id,
        "python3",
        "-c",
        f"x = bytearray({_OVERSHOOT_BYTES})",
        home=home,
    )

    assert result.returncode == _EXIT_RESOURCE_EXHAUSTED, (
        f"exit {result.returncode}, expected {_EXIT_RESOURCE_EXHAUSTED}; "
        f"stderr: {result.stderr}"
    )
    package = _package(result)
    assert package["status"] == _STATUS_RESOURCE_EXHAUSTED

    findings = _findings(package)
    assert str(_MEMORY_CEILING_BYTES) in findings, "the ceiling that was breached is not named"
    assert (
        "the command completed with exit status 137" not in findings
    ), "the uninterpreted sentence from the defect is back"

    assert package["attention"], "an exhausted result with empty attention IS the defect"
    remedies = [item for item in package["attention"] if "--memory-bytes" in item]
    assert remedies, f"attention names no remedy the caller can act on: {package['attention']}"
    assert (
        str(_MEMORY_CEILING_BYTES) in remedies[0]
    ), "a remedy that does not say what the ceiling is now cannot be acted on"


def test_regression_one_workspace_tells_four_outcomes_apart(home: Path, workspace: Any) -> None:
    """One workspace, four ways for a job to end, and what a caller can tell apart.

    Driven from a single workspace on purpose: the four are separated by *what
    happened*, never by how the workspace was configured, so no assertion here
    can be satisfied by a difference the test itself introduced.

    Three exit codes for four outcomes, which is the honest count. A command
    the image cannot run and a command that ran and failed share exit 6,
    because both are the caller's computation reporting a result — that is the
    fix, not a gap in it. They separate one level down, on the command's own
    exit status, which is asserted here and pinned as a contract by the test
    below. The two that must never collapse into 6 do not: the budget kill
    (8, "your ceiling stopped this") and the broken engine (7, "nothing about
    your job was wrong"). And a job that deliberately exits 137 stays an
    ordinary failure — the OOM verdict comes from the engine's kill flag, not
    from a number a command is free to choose.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id, "--memory-bytes", str(_MEMORY_CEILING_BYTES))

    def run(job_id: str, *command: str, docker_host: str | None = None) -> Any:
        return _cli(
            "run",
            "--json",
            "--job-id",
            job_id,
            workspace_id,
            *command,
            home=home,
            docker_host=docker_host,
        )

    not_runnable = run("job-not-runnable", "definitely-not-a-binary")
    killed = run("job-killed", "python3", "-c", f"x = bytearray({_OVERSHOOT_BYTES})")
    failed = run("job-failed", "python3", "-c", "raise SystemExit(137)")
    engine_broke = run("job-engine", "python3", "-c", "print(1)", docker_host=_UNREACHABLE_ENGINE)

    codes = [
        not_runnable.returncode,
        killed.returncode,
        failed.returncode,
        engine_broke.returncode,
    ]
    assert codes == [
        _EXIT_COMPUTATION_FAILED,
        _EXIT_RESOURCE_EXHAUSTED,
        _EXIT_COMPUTATION_FAILED,
        _EXIT_INFRASTRUCTURE_FAILURE,
    ], f"the four outcomes came back as {codes}"

    assert _package(not_runnable)["status"] == STATUS_FAILURE
    assert _package(killed)["status"] == _STATUS_RESOURCE_EXHAUSTED
    assert _package(failed)["status"] == STATUS_FAILURE
    # The broken engine is raised, never returned as a job status (NFR-07):
    # a structured error on stderr, and no result package at all on stdout.
    assert not engine_broke.stdout
    assert json.loads(engine_broke.stderr)["category"] == "infrastructure_failure"

    # The three jobs that really ran, as headspace recorded them. The broken
    # engine recorded nothing, because no job of the caller's ever started.
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    assert set(recorded) == {"job-not-runnable", "job-killed", "job-failed"}
    assert recorded["job-not-runnable"]["exit_status"] == _STATUS_COMMAND_NOT_FOUND
    assert recorded["job-killed"]["status"] == _STATUS_RESOURCE_EXHAUSTED
    # Same 137 the kernel's kill reports, chosen by the command itself — and
    # still an ordinary failure, because nothing exceeded a ceiling.
    assert recorded["job-failed"]["exit_status"] == 137
    assert recorded["job-failed"]["status"] == STATUS_FAILURE

    # Four outcomes, three taxonomy slots, and still four answers a caller can
    # tell apart — the pair sharing exit 6 separates on the command's own exit
    # status, and the pair sharing exit status 137 separates on the code. This
    # is the claim that actually holds; "four distinct exit codes" does not,
    # and asserting it would mean bending the taxonomy to fit a test.
    signatures = {
        (not_runnable.returncode, recorded["job-not-runnable"]["exit_status"]),
        (killed.returncode, recorded["job-killed"]["exit_status"]),
        (failed.returncode, recorded["job-failed"]["exit_status"]),
        (engine_broke.returncode, None),  # no job of the caller's ever ran
    }
    assert len(signatures) == 4, f"two of the four outcomes are indistinguishable: {signatures}"


def test_regression_process_exit_six_and_the_commands_own_exit_status_stay_two_numbers(
    home: Path, workspace: Any
) -> None:
    """Exit 6 and exit status 127/126 answer different questions. Both, together.

    6 is headspace's taxonomy category — *this was your computation, not our
    engine*. 127 and 126 are the command's own status — *absent from PATH* and
    *found but not executable*. A later change that made the process exit 127
    "because that is what the command returned" would lose the category, and
    one that dropped 127 from the record would lose the diagnosis. Asserting
    them in the same breath is what stops either from happening quietly. Both
    not-runnable shapes are covered here rather than one, because they are the
    two that share a process exit code and so are the pair most likely to be
    collapsed into each other.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)

    for job_id, command, command_status in (
        ("job-absent-from-path", "definitely-not-a-binary", _STATUS_COMMAND_NOT_FOUND),
        ("job-not-executable", "/tmp", _STATUS_NOT_EXECUTABLE),
    ):
        result = _cli("run", "--json", "--job-id", job_id, workspace_id, command, home=home)

        assert result.returncode == _EXIT_COMPUTATION_FAILED, (
            f"{command!r} exited {result.returncode}, expected "
            f"{_EXIT_COMPUTATION_FAILED}; stderr: {result.stderr}"
        )
        assert f"exit status {command_status}" in _findings(_package(result))

        recorded = _recorded_job(home, job_id)
        assert recorded["exit_status"] == command_status
        assert recorded["exit_status"] != result.returncode, (
            "the command's own exit status and headspace's taxonomy code have "
            "been collapsed into one number"
        )
