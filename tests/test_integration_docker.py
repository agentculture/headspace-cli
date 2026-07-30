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
This group is not an honesty condition. It is the pair of failures that
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

Issues #13 and #14, live: the consumer's own case and the deferred probes
-------------------------------------------------------------------------
The last group is not an honesty condition either. ``agentculture/embodiment``
filed two issues against 0.9.0: there was no way to get a *file* into a
workspace (#14) and no way to pass a *secret* (#13). Their workaround for the
first was to stand up an HTTP server on the host bound to the docker bridge and
``run`` a fetch-and-exec — which needs ``--network enabled``, so a caller who
genuinely needed isolation had no path at all. ``headspace put``,
``run --input``, ``run --env``, ``run --env-file`` and ``headspace stop`` are
the answer, and the first test below is the filer's case rebuilt: a
network-disabled workspace, a multi-file harness copied in, a job that runs it,
an artifact exported — no HTTP server, no argv payload, and the isolation
asserted from inside the job rather than inferred from a flag.

The rest of the group is the live evidence that could not be gathered anywhere
else, because a stub engine can only confirm what its author already believed:

* what ``put_archive`` *actually does* with a symlinked ancestor. This is
  recorded because it is worth knowing, not because anything depends on it —
  see :func:`test_put_archive_writes_straight_through_a_planted_ancestor_symlink`.
* whether a job container is genuinely gone on the normal path, and whether the
  label-reaping backstop genuinely works when the removal genuinely fails.
* whether ``stop --apply`` genuinely ends a live job in another process — and
  whether the still-blocked ``run`` now genuinely records that honestly:
  ``cancelled``/exit 5 with no ``exit_status`` at all, rather than the
  ``failure``/137/exit 6 the identical scenario produced before issue #16's
  two-phase cancellation channel existed. See
  :func:`test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled`.

The adversarial group: the same channel, with a job fighting it
----------------------------------------------------------------
The group above shows the cancellation channel working when nothing is
attacking it. The last group assumes the opposite, because the threat model
does: a job shares the workspace volume with headspace's two reserved marker
names, and a job is the caller's own code. Everything there is one direction of
a single asymmetry the provider's docstring claims out loud — *tampering can
cost a real cancellation its name; it can never manufacture one that did not
happen* — and it is tested live because a stub engine cannot produce the two
things that decide it: real signal delivery ordering, and what a real
``/proc`` shows a real process about itself.

Six probes, plus one that records the boundary rather than a guarantee: a
SIGTERM handler that deletes both markers; a job that catches and ignores
SIGTERM and dies to the escalation; an intent marker a job planted naming
itself; a job both operator-stopped and past its wall-clock budget in one
window; every identity surface a job can read, searched for its own job id;
symlinks planted at both reserved names by the job about to be stopped; and —
the seventh — what happens when a job *is* told its own id, which is the exact
hinge the unforgeability claim turns on. Two findings from writing them are
recorded in the tests themselves rather than here, because they are not
obvious: a marker a job plants is consumed by that job's *own* run before any
later job can meet it, and the countersignal is never within reach of the job
it names because it is written only after that job is dead.

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
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
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
from headspace.core.profiles import DEFAULT_PROFILE, REGISTRY
from headspace.core.result import STATUS_CANCELLED, STATUS_FAILURE, STATUS_SUCCESS
from headspace.core.states import State
from headspace.core.store import HOME_ENV_VAR, Store
from headspace.core.workspace import (
    DISPOSITION_ADOPTED,
    PHASE_INTENDED,
    ArtifactDeclaration,
    JobEnvironment,
    Orchestrator,
)
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


# --- issues #13 and #14, live: the consumer's case, and the deferred probes --

#: The taxonomy code a stop that really ended a job exits with. A literal for
#: the same reason the codes above are literals — see this module's docstring.
_EXIT_CANCELLED = 5  # mirrors _errors.EXIT_CANCELLED

#: The name the secret travels under. Never a value: this constant is the part
#: that is allowed to be recorded, and every test below generates the value it
#: pairs with at run time so no fixed string can drift into a committed file.
_API_KEY_NAME = "HEADSPACE_LIVE_PROBE_API_KEY"

#: Long enough for a ``sleep`` job to still be alive when a second process
#: decides to end it, and far under the 300s default wall-clock budget so the
#: job is ended by the stop and never by headspace's own enforcer.
_LONG_JOB_SECONDS = "60"

#: What ``container.stop()`` costs on this engine before it escalates. ``sleep``
#: runs as PID 1, and PID 1 ignores a signal it installed no handler for, so
#: SIGTERM is discarded and the job dies to the SIGKILL that follows the grace
#: period. Measured here, not assumed: 10.07s and exit 137 on docker 29.1.3.
_STOP_GRACE_SECONDS = 10  # mirrors docker.STOP_GRACE_SECONDS


def _surface(home: Path, workspace_id: str, filename: str) -> str:
    """One durable store file as raw on-disk text — what a grep would actually see."""
    path = home / "workspaces" / workspace_id / filename
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _engine_objects(engine: docker.DockerClient, workspace_id: str) -> tuple[set[str], set[str]]:
    """Every engine object wearing this workspace's label, right now.

    Scoped to one workspace id rather than to the whole daemon on purpose: this
    box runs several agents against one engine, so "the daemon's container list
    is unchanged" would be a claim about other people's work. The label scope is
    both deterministic and exactly the claim being made — a copy-in must not add
    a container or a volume *to the workspace it copies into*.
    """
    containers = {
        container.id
        for container in engine.containers.list(
            all=True, filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"}
        )
    }
    volumes = {
        volume.name
        for volume in engine.volumes.list(filters={"label": f"{LABEL_WORKSPACE_ID}={workspace_id}"})
    }
    return containers, volumes


def _job_containers(engine: docker.DockerClient, workspace_id: str) -> list[Any]:
    return engine.containers.list(
        all=True,
        filters={"label": [f"{LABEL_WORKSPACE_ID}={workspace_id}", f"{LABEL_ROLE}={ROLE_JOB}"]},
    )


def _await_running_job_container(
    engine: docker.DockerClient, workspace_id: str, timeout: float = 60.0
) -> Any:
    """A job container the engine has actually *started*.

    ``_await_job_container`` returns as soon as one exists, which for a stop
    test is too early: a ``created`` container is in ``LIVE_STATUSES`` and would
    be signalled before the job it is supposed to be interrupting ever ran.
    """
    container = _await_job_container(engine, workspace_id, timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()
        if container.status == "running":
            return container
        time.sleep(0.02)
    raise AssertionError(f"job container for {workspace_id} never started: {container.status}")


def _cli_background(*argv: str, home: Path) -> subprocess.Popen[str]:
    """``_cli``, but left running — the second process a ``stop`` needs to interrupt.

    ``stop`` exists precisely because ``run`` blocks and holds the workspace
    lock for a job's whole duration, so proving it works needs two genuinely
    separate processes: one blocked in ``run``, one arriving with no shared
    memory, no handle and nothing but the workspace id to go on.
    """
    env = dict(os.environ)
    env[HOME_ENV_VAR] = str(home)
    return subprocess.Popen(
        [sys.executable, "-m", "headspace", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


#: The harness's second module, so importing it proves more than one file landed
#: and landed byte-exact. The marker lives here rather than in the entrypoint
#: because the entrypoint is the file whose *name* appears in argv — a marker
#: there could not distinguish "the bytes arrived" from "the name was typed".
_COMPUTE_SOURCE = '''"""The harness's second module — imported, never named on the command line."""

MARKER = {marker!r}


def total(values):
    return sum(int(value) for value in values)
'''

#: The entrypoint. It reads a config file that arrived by a *different* channel
#: (``run --input``, not ``put``), imports its sibling, and makes its own
#: judgement about the network from inside the box rather than trusting a flag.
_REPORT_SOURCE = '''"""Runs inside a network-disabled workspace, importing its own package."""

import json
import os
import socket

from harness.compute import MARKER, total


def _network():
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=3).close()
    except OSError as err:
        return "unreachable: " + type(err).__name__
    return "reachable"


def main():
    with open("/workspace/config.json", encoding="utf-8") as handle:
        config = json.load(handle)
    report = {
        "label": config["label"],
        "total": total(config["values"]),
        "modules": sorted(
            name for name in os.listdir("/workspace/harness") if name.endswith(".py")
        ),
        "network": _network(),
        "marker": MARKER,
    }
    with open("/workspace/result.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True)
    print("harness wrote /workspace/result.json")


main()
'''


def _write_harness(root: Path, marker: str) -> tuple[Path, Path]:
    """Build the consumer's payload on the host: a package of three modules, and a config.

    Two host objects, because the case needs two *channels*: the package goes in
    with ``headspace put`` (a whole directory, before any job runs) and the
    config with ``run --input`` (alongside the job that consumes it).
    """
    package = root / "harness"
    package.mkdir()
    (package / "__init__.py").write_text(
        '"""Copied in whole, as a directory, by headspace put."""\n', encoding="utf-8"
    )
    (package / "compute.py").write_text(_COMPUTE_SOURCE.format(marker=marker), encoding="utf-8")
    (package / "report.py").write_text(_REPORT_SOURCE, encoding="utf-8")
    config = root / "config.json"
    config.write_text(
        json.dumps({"label": "embodiment-harness", "values": [2, 3, 5, 7]}), encoding="utf-8"
    )
    return package, config


def test_issue14_a_network_disabled_workspace_runs_a_copied_in_multi_file_harness_and_exports_it(
    home: Path, engine: docker.DockerClient, workspace: Any, tmp_path: Path
) -> None:
    """The filer's case, rebuilt end to end at the process boundary.

    ``agentculture/embodiment`` needed to run its own multi-file harness inside
    a workspace that could not reach the network. Before this change the only
    inbound channel was argv, so the payload had to be either smuggled through
    the command line (recorded verbatim in four durable places) or fetched over
    HTTP from the host — which requires ``--network enabled`` and therefore
    gives up the isolation that was the point. This test does neither.

    Both copy-in channels are used, because they are different verbs with
    different failure modes and the case needs both: ``headspace put`` moves a
    whole *directory* before any job exists, and ``run --input`` moves a file
    *with* the job that consumes it. What proves the harness really ran, rather
    than a single file being cat-ed back, is that the entrypoint imports a
    sibling module and reports the package's own file listing from inside the
    container.

    The isolation is asserted twice and neither is a flag echoed back: the job
    itself tries to open a socket to 1.1.1.1 and writes the failure into its
    report, and the engine's own record of the anchor is read back separately.
    """
    workspace_id = workspace()
    marker = f"harness-marker-{uuid.uuid4().hex}"
    package_dir, config_path = _write_harness(tmp_path, marker)
    compute_digest = hashlib.sha256((package_dir / "compute.py").read_bytes()).hexdigest()

    _create_workspace(home, workspace_id, "--network", "disabled")

    # 1. The package, as a directory, before any job runs.
    put = _cli("put", "--json", workspace_id, str(package_dir), "harness", home=home)
    assert put.returncode == 0, f"put failed: {put.stderr}"
    put_package = _package(put)
    assert "copied 3 file(s)" in put_package["outcome_summary"], (
        "one put moved the whole package directory, so the summary must say so: "
        f"{put_package['outcome_summary']}"
    )
    put_findings = _findings(put_package)
    for module in ("harness/__init__.py", "harness/compute.py", "harness/report.py"):
        assert module in put_findings, f"{module} is missing from the copy-in's findings"

    # 2. The job, with its config arriving through the other channel. The
    #    command line carries a module name and nothing else.
    run = _cli(
        "run",
        "--json",
        "--job-id",
        "harness-job",
        "--declare",
        "result.json=the harness's computed report, exported to the host",
        "--input",
        f"config.json={config_path}",
        workspace_id,
        "python3",
        "-m",
        "harness.report",
        home=home,
    )
    assert run.returncode == 0, f"run failed: {run.stderr}"
    run_package = _package(run)
    assert run_package["status"] == STATUS_SUCCESS
    assert "harness wrote /workspace/result.json" in run_package["evidence"][0]["excerpt"]

    # 3. The artifact leaves the workspace and outlives it.
    destination = tmp_path / "result.json"
    exported = _cli(
        "export", "--json", workspace_id, "result.json", "--to", str(destination), home=home
    )
    assert exported.returncode == 0, f"export failed: {exported.stderr}"

    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["total"] == 17, "the config that arrived by --input was not the one consumed"
    assert report["label"] == "embodiment-harness"
    assert report["marker"] == marker, "the harness's second module did not arrive byte-exact"
    assert report["modules"] == ["__init__.py", "compute.py", "report.py"], (
        "the whole package did not land — a multi-file copy-in is the thing this "
        f"case needs, and the workspace held {report['modules']}"
    )
    # Isolation, judged from inside the box by a real connect attempt.
    assert report["network"].startswith("unreachable"), (
        "the workspace reached the network, so this run does not prove the "
        f"HTTP-server workaround is unnecessary: {report['network']}"
    )
    # ...and corroborated by what the engine actually configured.
    assert _workspace_container(engine, workspace_id).attrs["HostConfig"]["NetworkMode"] == "none"

    # No argv payload: the durable surfaces hold the file's digest and its
    # destination, and no byte of its contents.
    for filename in ("journal.jsonl", "state.json"):
        text = _surface(home, workspace_id, filename)
        assert marker not in text, f"the harness's contents reached {filename}"
        assert compute_digest in text, f"{filename} recorded no digest for the copied-in module"
    assert marker not in run.stdout + run.stderr


def test_issue13_an_api_key_reaches_the_job_by_name_and_no_recorded_surface_holds_its_value(
    home: Path, workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half of the filer's case: a credential, live, at the CLI boundary.

    The job proves it received the *value* by hashing it and printing only the
    digest — so the positive half ("it really arrived") is asserted without the
    test itself planting the secret in captured output, which is the one surface
    a job is allowed to leak into and which would make the negative half
    untestable.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id, "--network", "disabled")
    key = f"sk-live-{uuid.uuid4().hex}"
    monkeypatch.setenv(_API_KEY_NAME, key)

    result = _cli(
        "run",
        "--json",
        "--job-id",
        "keyed-job",
        "--env",
        _API_KEY_NAME,
        workspace_id,
        "python3",
        "-c",
        "import hashlib, os; "
        f"print(hashlib.sha256(os.environ[{_API_KEY_NAME!r}].encode()).hexdigest())",
        home=home,
    )

    assert result.returncode == 0, f"run failed: {result.stderr}"
    package = _package(result)
    assert package["status"] == STATUS_SUCCESS
    assert hashlib.sha256(key.encode()).hexdigest() in package["evidence"][0]["excerpt"], (
        "the job did not receive the value, so a clean grep below would prove "
        "nothing but that the feature does not work"
    )

    surfaces = {
        "outcome_summary": str(package["outcome_summary"]),
        "provenance.inputs": "\n".join(package["provenance"].get("inputs", [])),
        "journal.jsonl": _surface(home, workspace_id, "journal.jsonl"),
        "state.json": _surface(home, workspace_id, "state.json"),
    }
    for label, text in surfaces.items():
        assert key not in text, f"the secret reached {label}"
    # The name is the part that is meant to be there, in both durable surfaces.
    assert _API_KEY_NAME in surfaces["journal.jsonl"]
    assert _API_KEY_NAME in surfaces["state.json"]
    assert key not in result.stdout + result.stderr


def _env_names(text: str) -> set[str]:
    """The variable *names* in ``env`` output — never the values."""
    return {line.partition("=")[0] for line in text.splitlines() if "=" in line}


def test_issue13_a_run_with_no_env_flag_hands_the_job_only_the_images_own_defaults(
    home: Path, engine: docker.DockerClient, workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-flag probe, answered against the image itself rather than a list.

    Issue #13 measured "seven image defaults" on 0.9.0. Asserting that count, or
    those names, would pin this test to one revision of ``python:3.12-slim``.
    What is actually being claimed is *closed by default* — that headspace adds
    nothing — so the comparison is against what the very same image gives a bare
    ``docker run`` with headspace nowhere in the picture.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)
    monkeypatch.setenv(_API_KEY_NAME, f"sk-live-{uuid.uuid4().hex}")

    result = _cli("run", "--json", "--job-id", "bare-job", workspace_id, "env", home=home)
    assert result.returncode == 0, f"run failed: {result.stderr}"
    through_headspace = _env_names(_package(result)["evidence"][0]["excerpt"])

    bare = engine.containers.run(
        REGISTRY[DEFAULT_PROFILE].image, ["env"], network_mode="none", remove=True
    )
    image_defaults = _env_names(bare.decode("utf-8", "replace"))

    assert through_headspace == image_defaults, (
        "a job run through headspace with no --env sees a different set of "
        f"variables than the image's own: {through_headspace ^ image_defaults}"
    )
    assert _API_KEY_NAME not in through_headspace, "the caller's environment leaked into the job"


def test_issue13_the_forwarded_value_lives_in_the_job_containers_own_env_and_dies_with_it(
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any
) -> None:
    """Where the value *is*, on the engine, while the job runs — stated, not hidden.

    headspace's guarantee is about the four surfaces *it* composes. The engine
    is a fifth, and it is not clean: a forwarded value has to be in the job
    container's own ``Config.Env``, because that is how ``execve`` receives it,
    and anyone who can run ``docker inspect`` on that container can read it.
    That is inherent to the channel, so it is recorded here rather than argued
    away, together with the two properties that bound it — it reaches neither
    the labels nor the argv (the surfaces this provider treats as public), and
    the container carrying it is removed the moment the job settles, which is
    exactly what argv did not do.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    key = f"sk-live-{uuid.uuid4().hex}"

    runner = threading.Thread(
        target=orch.run,
        args=(workspace_id, ("sleep", "5")),
        kwargs={
            "job_id": "env-exposure-probe",
            "environment": JobEnvironment(values={_API_KEY_NAME: key}),
        },
        daemon=True,
    )
    runner.start()
    try:
        job = _await_running_job_container(engine, workspace_id)
        config = job.attrs["Config"]

        assert f"{_API_KEY_NAME}={key}" in config["Env"], (
            "the value did not reach the job's environment at all — this test's "
            "point is where it IS, and it has to be here for the job to work"
        )
        assert not any(key in part for part in config["Cmd"] or ())
        assert not any(key in str(value) for value in (config["Labels"] or {}).values())
        assert key not in repr(job.attrs["HostConfig"])
    finally:
        runner.join(60)

    # The exposure ends with the container, which is the whole difference from
    # a value typed into argv and kept in a journal for ever.
    assert _job_containers(engine, workspace_id) == []


def test_put_adds_no_engine_object_to_the_workspace_it_copies_into(
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any, tmp_path: Path
) -> None:
    """A copy-in is an exec against the anchor, not a container of its own.

    Worth pinning live because the obvious implementation of "get a file in" is
    a throwaway container with the volume mounted, and that container would be a
    second thing to leak, a second thing to seal, and a second thing a caller's
    ``docker ps`` would have to be told to ignore.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    payload = tmp_path / "payload.txt"
    payload.write_text("copied by exec, not by container\n", encoding="utf-8")

    before = _engine_objects(engine, workspace_id)
    assert len(before[0]) == 1, f"expected only the anchor before the copy-in, got {before[0]}"

    orch.put(workspace_id, payload, "payload.txt")

    assert _engine_objects(engine, workspace_id) == before, (
        "the copy-in changed this workspace's engine objects; a verified copy-in "
        "runs as an exec against the anchor and must create nothing"
    )
    # And the bytes really are in the volume, read back by a job rather than by
    # the same verb that put them there.
    read_back = orch.run(workspace_id, ("cat", "/workspace/payload.txt"), job_id="read-back")
    assert read_back.status == STATUS_SUCCESS
    assert "copied by exec, not by container" in read_back.evidence[0].excerpt


def test_a_copy_in_into_a_stopped_workspace_is_refused_by_name_while_reading_still_works(
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any, tmp_path: Path
) -> None:
    """The stopped-anchor classification, and the asymmetry its remediation promises.

    A verified copy-in has to hash and rename inside the workspace, which a
    stopped container cannot do — so it is refused, honestly, naming the state
    the engine reported. The refusal's remediation then makes a *second* claim,
    that reading artifacts back out still works on a workspace whose runtime has
    exited, and a remediation nobody checked is a remediation that can be wrong.
    Both halves are asserted against the same genuinely stopped container.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    orch.run(
        workspace_id,
        ("/bin/sh", "-c", "printf 'kept\\n' > /workspace/kept.txt"),
        declares=[ArtifactDeclaration(name="kept.txt", purpose="work the caller can still have")],
        job_id="writer",
    )

    _workspace_container(engine, workspace_id).stop(timeout=5)

    late = tmp_path / "late.txt"
    late.write_text("arrived too late\n", encoding="utf-8")
    with pytest.raises(ProviderError) as caught:
        orch.put(workspace_id, late, "late.txt")

    assert workspace_id in caught.value.message
    assert "not running" in caught.value.message
    assert "exited" in caught.value.message, (
        "the refusal must name the state the engine actually reported, not a "
        f"generic one: {caught.value.message}"
    )

    destination = tmp_path / "kept-out.txt"
    exported = orch.export(workspace_id, "kept.txt", destination=destination)
    assert exported.status == STATUS_SUCCESS
    assert destination.read_text(encoding="utf-8") == "kept\n"


def test_a_job_container_is_removed_on_the_normal_path_and_a_failed_removal_is_reaped_by_label(
    home: Path,
    engine: docker.DockerClient,
    orch: Orchestrator,
    workspace: Any,
) -> None:
    """Both halves of ``run``'s best-effort tidy-up, against the real engine.

    ``run`` removes the job container in a ``finally`` with exceptions
    suppressed, on purpose: a successful job must not be reported as an engine
    failure because the tidy-up lost a race. That design is only honest if the
    backstop is real, so the removal is made to genuinely fail — the engine's
    own ``remove`` raises for this workspace's job containers and for nothing
    else — and what is asserted afterwards is that the stray really is left
    behind, really wears the label, and really is reaped by ``destroy``.

    The patch is scoped with :meth:`pytest.MonkeyPatch.context` rather than
    taken from the ``monkeypatch`` fixture, because it has to be undone *inside*
    the test — and ``monkeypatch.undo()`` on the fixture would also undo the
    ``home`` fixture's ``HEADSPACE_HOME``, pointing everything after it at the
    operator's real ``~/.headspace``. A scoped context reverts exactly what it
    applied and nothing else.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)

    clean = orch.run(workspace_id, ("/bin/sh", "-c", "echo tidy"), job_id="tidy-job")
    assert clean.status == STATUS_SUCCESS
    assert _job_containers(engine, workspace_id) == [], "the normal path left a job container"

    real_remove = docker.models.containers.Container.remove

    def refuse_this_workspaces_job_removal(self: Any, **kwargs: Any) -> Any:
        labels = self.labels or {}
        if labels.get(LABEL_WORKSPACE_ID) == workspace_id and labels.get(LABEL_ROLE) == ROLE_JOB:
            raise docker.errors.APIError("simulated: the engine refused to remove the container")
        return real_remove(self, **kwargs)

    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(
            docker.models.containers.Container, "remove", refuse_this_workspaces_job_removal
        )
        stranded = orch.run(workspace_id, ("/bin/sh", "-c", "echo stranded"), job_id="stranded-job")

    # The job is still reported as the success it was: a lost tidy-up is not the
    # caller's failure, which is the whole reason the removal is suppressed.
    assert stranded.status == STATUS_SUCCESS
    strays = _job_containers(engine, workspace_id)
    assert len(strays) == 1, f"the simulated removal failure left {len(strays)} strays, wanted 1"
    assert strays[0].labels[LABEL_WORKSPACE_ID] == workspace_id
    assert strays[0].labels[LABEL_ROLE] == ROLE_JOB

    disposition = Orchestrator(DockerProvider(), Store()).destroy(workspace_id)
    assert disposition.status == STATUS_SUCCESS
    assert (
        _job_containers(engine, workspace_id) == []
    ), "the label-reaping backstop did not reap a stray it was the backstop for"


def test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled(
    home: Path, engine: docker.DockerClient, workspace: Any
) -> None:
    """Two processes, one job: the stop reports honestly, and now so does the run.

    ``run`` blocks and holds the workspace lock for a job's whole duration, so
    the only way to prove ``stop`` works is to have a genuinely separate process
    arrive with nothing but the workspace id. That half has always worked, and
    is asserted as it should be: ``cancelled``, exit 5.

    The other half used to be a known gap, tracked as issue #16: the
    still-blocked ``run`` sees a container the engine killed with SIGKILL —
    exit status 137, ``OOMKilled: false`` — and that is byte for byte what
    ``python -c "raise SystemExit(137)"`` leaves behind too, a fact pinned
    independently by ``test_regression_one_workspace_tells_four_outcomes_apart``.
    No amount of staring at the engine's own numbers can tell those two jobs
    apart, which is exactly what makes the assertions below meaningful rather
    than circular: if ``cancelled`` shows up here, it did not come from the
    exit code, the ``OOMKilled`` flag, or any other fact the engine can report
    about this container, because none of those facts differ between a job an
    operator stopped and a job that chose 137 for itself. It can only have come
    from the positive, out-of-band signal described in
    ``headspace/providers/docker.py``'s module docstring under "Recording that
    an operator ended it": the intent marker ``stop`` writes into the
    workspace volume before it signals, and the countersignal it writes
    through the anchor once the signalling has run its course — both read back
    by ``run`` and matched against the job id that just ran.

    ``exit_status`` being absent below is not this provider merely asserting
    that a stopped job carries none — it is
    :meth:`~headspace.providers.base.JobOutcome.__post_init__` refusing, at
    construction, to build a ``cancelled`` outcome that carries an exit status
    at all. So even a provider that classified the job correctly but also
    tried to report the 137 it observed at the engine would fail loudly right
    there, rather than quietly leaking a number the taxonomy says a cancelled
    job never produces.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)

    runner = _cli_background(
        "run",
        "--json",
        "--job-id",
        "stoppable-job",
        workspace_id,
        "sleep",
        _LONG_JOB_SECONDS,
        home=home,
    )
    try:
        _await_running_job_container(engine, workspace_id)

        began = time.monotonic()
        stopped = _cli("stop", "--json", "--apply", workspace_id, home=home)
        elapsed = time.monotonic() - began

        assert stopped.returncode == _EXIT_CANCELLED, (
            f"stop --apply exited {stopped.returncode}, expected {_EXIT_CANCELLED}; "
            f"stderr: {stopped.stderr}"
        )
        stop_package = _package(stopped)
        assert stop_package["status"] == STATUS_CANCELLED
        assert "stoppable-job" in _findings(stop_package) + stop_package["outcome_summary"]
        # Graceful first: the polite signal really was sent and really was given
        # its window before the kill, rather than the kill being sent outright.
        assert elapsed >= _STOP_GRACE_SECONDS - 1, (
            f"stop returned in {elapsed:.2f}s, too fast to have waited "
            f"{_STOP_GRACE_SECONDS}s for the job to end itself"
        )

        out, err = runner.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if runner.poll() is None:  # pragma: no cover - only on a wedged engine
            runner.kill()
            runner.communicate(timeout=30)

    # --- the job's own record, honest about who ended it (since issue #16) ---
    assert runner.returncode == _EXIT_CANCELLED, (
        f"the interrupted run exited {runner.returncode}, expected {_EXIT_CANCELLED}; "
        f"stderr: {err}"
    )
    run_package = json.loads(out)
    assert run_package["status"] == STATUS_CANCELLED  # the countersignal, not the exit code
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    assert recorded["stoppable-job"]["exit_status"] is None  # a cancelled outcome carries none
    assert recorded["stoppable-job"]["status"] == STATUS_CANCELLED  # since issue #16


def test_put_archive_writes_straight_through_a_planted_ancestor_symlink(
    home: Path, engine: docker.DockerClient, orch: Orchestrator, workspace: Any, tmp_path: Path
) -> None:
    """The deferred probe, run for real — and what it is *not* evidence of.

    The copy-in path guards against a job planting ``link -> /etc`` and a later
    copy-in writing through it, and that guard was deliberately written so as
    not to depend on how the engine itself resolves such a path. This test is
    the reason that decision was right: probed against docker 29.1.3, the
    archive endpoint follows a planted ancestor symlink without complaint.

    The first exploratory probe (a scratch container, the same engine, three
    shapes of link at once) reported::

        [out-link (ancestor -> /etc)]         put_archive(...) -> True
        [in-link  (ancestor -> /workspace/real)] put_archive(...) -> True
        [deep/a/esc (INTERMEDIATE -> /etc)]   put_archive(...) -> True
        # /etc/probe-out.txt:  PROBE-probe-out.txt
        # /etc/probe-deep.txt: PROBE-probe-deep.txt
        # find /workspace: l /workspace/out-link -> /etc   (nothing else new)

    So the bytes landed at each link's *target*, outside the workspace volume,
    with the endpoint reporting success in every case — a final component and an
    intermediate one behave identically, and no refusal is offered for either.
    The blast radius is bounded by the container's own root filesystem, because
    the daemon resolves the path in that scope, so nothing reaches the host; but
    "outside the volume" is already far enough to matter, and it is a fact about
    the engine rather than a promise from it. The body below re-runs the same
    shape inside a real headspace workspace, so the record stays live rather
    than becoming a comment nobody re-checks.

    This is defense-in-depth documentation, not a guard the product depends on.
    headspace never hands ``put_archive`` a caller-controlled path: every write
    is streamed into a fresh nonce directory under a staging root the prepare
    step resolved first, and the destination is bounded and committed by the
    finalize script's own ``realpath`` checks inside the container. The second
    half of this test says exactly that, by proving an ordinary ``put`` into the
    very workspace holding the planted link still lands where it should.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    probe_name = f"probe-{uuid.uuid4().hex}.txt"

    # A job plants the link, which is the realistic threat: the volume is shared
    # with every job the workspace runs, and a job is the caller's own code.
    planted = orch.run(
        workspace_id,
        ("/bin/sh", "-c", "ln -s /etc /workspace/planted-link"),
        job_id="plant-link",
    )
    assert planted.status == STATUS_SUCCESS

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo(probe_name)
        content = b"escaped the volume\n"
        member.size = len(content)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(content))

    anchor = _workspace_container(engine, workspace_id)
    accepted = anchor.put_archive("/workspace/planted-link", payload.getvalue())

    # Recorded, not asserted as desirable: this is what the engine does.
    assert accepted is True, "the engine refused the write — re-read this test's docstring"
    landed = anchor.exec_run(["/bin/sh", "-c", f"cat /etc/{probe_name}"])
    assert landed.exit_code == 0 and b"escaped the volume" in landed.output, (
        "put_archive did not follow the link after all; the recorded engine "
        f"behaviour has changed: {landed.output!r}"
    )
    inside = anchor.exec_run(["/bin/sh", "-c", "ls -A /workspace"])
    assert probe_name not in inside.output.decode(
        "utf-8", "replace"
    ), "the bytes stayed in the volume, which contradicts the probe above"
    # Bounded by the container's own rootfs: the host is untouched.
    assert not Path("/etc", probe_name).exists()

    # ...and none of that is load-bearing, because headspace's own copy-in never
    # hands the endpoint a path a job could have influenced.
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("landed where it was asked to\n", encoding="utf-8")
    orch.put(workspace_id, ordinary, "ordinary.txt")
    check = orch.run(workspace_id, ("cat", "/workspace/ordinary.txt"), job_id="check-ordinary")
    assert check.status == STATUS_SUCCESS
    assert "landed where it was asked to" in check.evidence[0].excerpt


# --- the adversarial wave: what a job inside the box can do to the channel ---
#
# Everything above proves the cancellation channel works when nobody is
# fighting it. This group assumes the opposite, because the threat model says
# so out loud: a job shares the workspace volume with headspace's own two
# reserved marker names, runs code the caller wrote rather than code headspace
# wrote, and is under no obligation to be polite about either. Every test below
# is one direction of a single asymmetry claimed in
# ``headspace/providers/docker.py``'s module docstring under "Recording that an
# operator ended it":
#
#     tampering can cost a real cancellation its name — it can never
#     manufacture one that did not happen.
#
# The claim rests on one fact and one ordering, and the tests are split along
# them. The fact is that nothing inside the box ever tells a job its own id, so
# a job cannot write the one string that would make a marker speak about
# itself (probe 5 below, and probe 7 which shows precisely what the guarantee
# costs if that fact ever stops being true). The ordering is that the
# countersignal — the only marker ``run`` will classify from — is written after
# the job is already dead, so it is never within reach of the job it names
# (probe 1). What a job *can* reach is everything else, and probes 3 and 6 show
# what that buys it: nothing, twice, in two different ways.
#
# Same single-writer constraint as the rest of this module: these drive real
# containers with deterministic ids and must not be run concurrently with each
# other.

#: The reserved names, written out rather than imported from the provider.
#: These two strings are a cross-process wire protocol — one process writes
#: them and a different one reads them — so a test that imported them would
#: agree with a rename by construction and prove nothing about the two halves
#: still meeting. The comment is the only link, exactly as it is for the exit
#: codes above.
_SIGNALLED_MARKER = "/workspace/.headspace-cancelled"  # mirrors docker.CANCELLATION_SIGNALLED_...
_INTENT_MARKER = "/workspace/.headspace-cancel-requested"  # mirrors docker.CANCELLATION_INTENT_...

#: How long ``run`` holds the door open when an intent marker names the job
#: that just settled. Asserted against a *measured* wall time below, which is
#: what makes "the wait really ran and still invented nothing" a fact rather
#: than a reading of the source.
_CANCELLATION_SETTLE_SECONDS = 2.0  # mirrors docker.CANCELLATION_SETTLE_SECONDS

#: The taxonomy name the wall-clock enforcer produces, and the process exit
#: code that goes with it. Present only so the precedence assertion below can
#: say which answer was *rejected* rather than only which was returned.
_STATUS_TIMEOUT = "timeout"  # mirrors result.STATUS_TIMEOUT
_EXIT_TIMEOUT = 4  # mirrors _errors.EXIT_TIMEOUT

#: A wall-clock budget short enough that headspace's own enforcer reliably
#: fires *before* a ``stop`` issued at the same moment escalates to SIGKILL —
#: six seconds against a ten-second grace period. That gap is what makes the
#: coincidence in the ``both_operator_stopped_and_past_its_wall_clock_budget``
#: test below real rather than asserted: the container demonstrably died to the
#: enforcer, and the operator's countersignal still won the classification.
_SHORT_WALL_CLOCK_SECONDS = 6


def _captured_output(package: dict[str, Any]) -> str:
    """Only the bytes the *job* produced — never headspace's narration about it.

    The distinction is the whole point of the leak probe and load-bearing in
    the tamper probes too. A result package legitimately names the job it
    describes in ``outcome_summary``, in every evidence item's ``source``, and
    in the findings; searching the serialised package for a job id would
    therefore always find one, and would say nothing at all about what the job
    could see from inside its container. This returns the excerpt bodies alone.
    """
    return "\n".join(
        item["excerpt"] for item in package["evidence"] if item.get("kind") == "excerpt"
    )


def _await_job_log(container: Any, needle: str, timeout: float = 60.0) -> None:
    """Block until a running job has said something specific on its own stream.

    ``_await_running_job_container`` answers "the engine has started this
    container", which for an adversarial test is still too early: a job that
    has to arm a signal handler, or plant an object at a reserved name, has not
    necessarily done so at the instant the engine calls it running. Gating on
    the job's *own* announcement removes that window, so the probes below test
    what they claim to test rather than occasionally racing their own setup.

    Read from the engine's retained log rather than from an attach stream:
    ``run`` is already attached in another process, and a second attach would
    change the capture this module asserts about elsewhere.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(docker.errors.APIError, OSError):
            if needle in container.logs(stdout=True, stderr=True).decode("utf-8", "replace"):
                return
        time.sleep(0.05)
    raise AssertionError(f"job container never emitted {needle!r} within {timeout}s")


def _volume_listing(engine: docker.DockerClient, workspace_id: str) -> str:
    """What actually stands in the workspace volume, read through the anchor.

    The anchor is the container headspace itself writes and reads the markers
    through, so this sees exactly what the provider would see. Reading it from
    the host would mean finding the volume's path under the engine's own state
    directory, which needs root and would be a claim about this machine's
    storage driver rather than about the workspace.
    """
    anchor = _workspace_container(engine, workspace_id)
    listing = anchor.exec_run(["/bin/sh", "-c", "ls -A /workspace"])
    return listing.output.decode("utf-8", "replace")


#: A job that fights back the moment it is asked to stop.
#:
#: On SIGTERM it reports what stands at each reserved marker name, deletes it,
#: reports again, and exits 137 — the number a SIGKILL would have left, chosen
#: so that every fact the engine can report about this container is identical
#: to a forceful stop's. The reports are the evidence: they are taken from
#: inside the box at the last instant the job is alive, which is the only
#: moment at which its tampering could possibly matter.
#:
#: ``sleep 60 & wait`` rather than a foreground ``sleep 60``, and the choice is
#: not cosmetic. A POSIX shell defers a trap until the foreground command it is
#: running finishes, and ``docker stop`` signals PID 1 alone — so a shell
#: blocked on a foreground ``sleep`` would never run this handler at all, would
#: die to the escalation ten seconds later with the trap unexecuted, and the
#: test would quietly become a duplicate of the plain-``sleep`` case above.
#: ``wait`` is interruptible by a trapped signal, which is what puts the
#: handler on the stack while the job still exists.
_MARKER_DELETING_TAMPER = f"""
trap '
for marker in {_SIGNALLED_MARKER} {_INTENT_MARKER}
do
  if [ -e "$marker" ] || [ -L "$marker" ]
  then echo "TAMPER-SAW $marker"
  else echo "TAMPER-ABSENT $marker"
  fi
  rm -f "$marker"
  if [ -e "$marker" ] || [ -L "$marker" ]
  then echo "TAMPER-KEPT $marker"
  else echo "TAMPER-GONE $marker"
  fi
done
echo TAMPER-DONE
exit 137
' TERM
echo TAMPER-ARMED
sleep 60 &
wait
"""


def test_a_tampering_job_can_delete_the_intent_marker_but_never_reaches_the_countersignal(
    home: Path, engine: docker.DockerClient, workspace: Any
) -> None:
    """The headline probe: a job deleting both marker names deletes the wrong one.

    The job installs a SIGTERM handler that unlinks both reserved names from
    the shared volume and then exits 137 — the exit status a SIGKILL leaves,
    so nothing the engine can report distinguishes this container from one an
    operator forcefully ended. It is then genuinely stopped by a genuinely
    separate process, and the handler's own reports say what it could see.

    Three of those reports are facts about the *ordering* of the two-phase
    channel, and they are the reason this test is not a race:

    * ``TAMPER-SAW`` at the intent name. ``stop`` writes intent *before* it
      signals, so by the time a SIGTERM handler runs the intent marker is
      necessarily already in the volume. The job therefore has the channel
      fully within reach — this is not a test of a job that failed to find
      what it was attacking.
    * ``TAMPER-GONE`` at the intent name. And it really deleted it: the volume
      is shared and writable, and headspace does nothing to prevent this.
      Deleting the intent marker is not harmless, either — it is exactly the
      warning ``run`` uses to know a countersignal is on its way, so the job
      has disarmed :data:`~headspace.providers.docker.CANCELLATION_SETTLE_SECONDS`
      before it ever runs.
    * ``TAMPER-ABSENT`` at the *countersignal* name. This is the whole
      guarantee, and it holds by construction rather than by timing: the
      countersignal is written only once the signalling has run its course,
      which is to say only once this container is dead. There is no instant at
      which a job and its own countersignal both exist, so there is no tamper
      a job can perform against the one marker that classifies.

    What the recorded outcome then is, is the interesting part, and the honest
    answer is that it depends on which of two processes gets to the volume
    first — so this test asserts the pair of answers that are honest and
    refuses everything else, rather than pretending to a determinism the
    channel does not claim. Measured against docker 29.1.3 on the reference
    host, eight consecutive rounds all recorded ``cancelled``: with the intent
    marker deleted, ``run`` does not wait, but it still spends a ``/system/df``
    call (:meth:`~headspace.providers.docker.DockerProvider._volume_bytes`,
    ~180ms on a daemon holding 135 images) between the job's death and its read
    of the volume, and ``stop`` lands the countersignal inside that window. On
    a leaner daemon that call is cheaper and ``run`` may read first, in which
    case the record is ``failure``/137 — the pre-#16 answer, which is the
    documented degradation and not a defect.

    So the assertion below is the one the design actually makes: whichever way
    the read lands, the record is either the truth (``cancelled``, carrying no
    exit status, because
    :meth:`~headspace.providers.base.JobOutcome.__post_init__` refuses one) or
    the strictly weaker pre-#16 truth (``failure``, carrying the job's own
    137). It is never ``success``, never ``timeout``, never
    ``resource_exhausted``, and — the direction that matters — the tamper never
    moved the record *towards* ``cancelled``, because a stop really did happen
    here. The manufacturing direction is probed separately by
    :func:`test_an_intent_marker_a_job_planted_for_itself_buys_it_nothing_at_all`.

    What this is NOT evidence of: that ``cancelled`` will be recorded on any
    given host. If a future change makes ``run`` read the volume sooner, this
    test keeps passing and the record degrades silently to ``failure`` — which
    is the correct direction to degrade in, and is why the disjunction here is
    a deliberate statement about the channel rather than a hedge.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)

    runner = _cli_background(
        "run",
        "--json",
        "--job-id",
        "tamper-job",
        workspace_id,
        "/bin/sh",
        "-c",
        _MARKER_DELETING_TAMPER,
        home=home,
    )
    try:
        container = _await_running_job_container(engine, workspace_id)
        # Gate on the job's own announcement, not on the engine calling the
        # container running: a handler that is not installed yet cannot tamper,
        # and this probe is worthless if it stops a job before it is armed.
        _await_job_log(container, "TAMPER-ARMED")

        stopped = _cli("stop", "--json", "--apply", workspace_id, home=home)
        assert stopped.returncode == _EXIT_CANCELLED, (
            f"stop --apply exited {stopped.returncode}, expected {_EXIT_CANCELLED}; "
            f"stderr: {stopped.stderr}"
        )
        out, err = runner.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if runner.poll() is None:  # pragma: no cover - only on a wedged engine
            runner.kill()
            runner.communicate(timeout=30)

    package = json.loads(out)
    tamper_report = _captured_output(package)
    assert "TAMPER-DONE" in tamper_report, (
        "the SIGTERM handler never ran, so this round tested nothing about "
        f"tampering; the job said: {tamper_report!r}"
    )
    # The channel was fully within the job's reach...
    assert f"TAMPER-SAW {_INTENT_MARKER}" in tamper_report, (
        "the intent marker was not in the volume when the job was signalled, "
        "which contradicts stop writing it before it signals"
    )
    assert f"TAMPER-GONE {_INTENT_MARKER}" in tamper_report, (
        "the job could not delete the intent marker; this probe assumes it can, "
        "and the guarantee it tests does not depend on it being unable to"
    )
    # ...and the one marker that classifies was still not in it.
    assert f"TAMPER-ABSENT {_SIGNALLED_MARKER}" in tamper_report, (
        "a countersignal existed while the job that it names was still alive — "
        "the ordering the whole two-phase channel rests on has been inverted"
    )

    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    tampered = recorded["tamper-job"]
    assert package["status"] == tampered["status"]
    if tampered["status"] == STATUS_CANCELLED:
        assert tampered["exit_status"] is None  # a cancelled outcome carries none, by construction
        assert runner.returncode == _EXIT_CANCELLED
    else:
        assert tampered["status"] == STATUS_FAILURE, (
            "a tampered stop was recorded as something other than the truth or "
            f"the documented degradation: {tampered['status']!r}"
        )
        assert tampered["exit_status"] == 137  # the job's own choice, preserved verbatim
        assert runner.returncode == _EXIT_COMPUTATION_FAILED


#: A job that hears the polite signal, says so, and carries on regardless.
#:
#: Deliberately different from the plain ``sleep`` the stop test above uses.
#: There, PID 1 discards SIGTERM because it installed no handler, so the signal
#: is never *delivered* and the escalation is the only thing that could have
#: ended the job. Here the signal is delivered, caught and ignored on purpose,
#: and the job says so on its own stream — which turns "the escalation ran"
#: from an inference about PID 1 semantics into something the job itself
#: witnessed.
_SIGTERM_IGNORING_JOB = """
trap 'echo IGNORED-SIGTERM' TERM
echo IGNORER-ARMED
count=0
while [ "$count" -lt 120 ]
do
  sleep 1 &
  wait
  count=$((count + 1))
done
"""


def test_a_job_that_catches_and_ignores_sigterm_is_recorded_cancelled_after_the_escalation(
    home: Path, engine: docker.DockerClient, workspace: Any
) -> None:
    """The slowest stop there is still gets its countersignal read.

    ``stop`` is graceful before it is forceful, so a job that refuses to end
    itself costs it the full :data:`STOP_GRACE_SECONDS` before the SIGKILL —
    and every one of those seconds is spent *before* the countersignal is
    written. That is the worst case the settle wait exists for, and it is worth
    a test of its own because the two processes are least synchronised exactly
    here: ``run`` is polling a container that dies the instant the escalation
    lands, while ``stop`` still owes a reload and an exec round trip.

    The job catching the signal rather than ignoring it by PID 1 default is
    what makes this distinct from
    :func:`test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled`.
    That test's ``sleep`` never receives SIGTERM at all — the kernel discards a
    signal PID 1 installed no handler for — so "the escalation ended it" is an
    inference from the elapsed time. Here the job prints ``IGNORED-SIGTERM``
    from inside its own handler, so delivery, refusal and escalation are three
    separate observed facts rather than one deduction.

    ``exit_status`` being absent is not this test being lenient about the 137
    the engine holds: a ``cancelled`` outcome that carried an exit status could
    not be constructed at all
    (:meth:`~headspace.providers.base.JobOutcome.__post_init__`), so the
    ``None`` below is a refusal, not an omission.

    What this is NOT evidence of: anything about a job that *does* end itself
    inside its grace window. Such a job is never killed, may exit 0, and is
    recorded ``success`` on purpose — the channel records that an operator
    asked and that the ask landed, never that the job was cut short.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)

    runner = _cli_background(
        "run",
        "--json",
        "--job-id",
        "stubborn-job",
        workspace_id,
        "/bin/sh",
        "-c",
        _SIGTERM_IGNORING_JOB,
        home=home,
    )
    try:
        container = _await_running_job_container(engine, workspace_id)
        _await_job_log(container, "IGNORER-ARMED")

        began = time.monotonic()
        stopped = _cli("stop", "--json", "--apply", workspace_id, home=home)
        elapsed = time.monotonic() - began

        assert stopped.returncode == _EXIT_CANCELLED, (
            f"stop --apply exited {stopped.returncode}, expected {_EXIT_CANCELLED}; "
            f"stderr: {stopped.stderr}"
        )
        assert elapsed >= _STOP_GRACE_SECONDS - 1, (
            f"stop returned in {elapsed:.2f}s, too fast to have given the job its "
            f"{_STOP_GRACE_SECONDS}s grace period before escalating"
        )
        out, err = runner.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if runner.poll() is None:  # pragma: no cover - only on a wedged engine
            runner.kill()
            runner.communicate(timeout=30)

    package = json.loads(out)
    assert "IGNORED-SIGTERM" in _captured_output(package), (
        "the job never reported catching SIGTERM, so nothing here shows the "
        "polite signal was delivered and refused rather than simply discarded"
    )
    assert runner.returncode == _EXIT_CANCELLED, (
        f"the interrupted run exited {runner.returncode}, expected {_EXIT_CANCELLED}; "
        f"stderr: {err}"
    )
    assert package["status"] == STATUS_CANCELLED
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    assert recorded["stubborn-job"]["status"] == STATUS_CANCELLED
    assert recorded["stubborn-job"]["exit_status"] is None


def test_an_intent_marker_a_job_planted_for_itself_buys_it_nothing_at_all(
    home: Path, workspace: Any, orch: Orchestrator
) -> None:
    """Not an ending, and since issue #20 not even a delay.

    Producing a genuine intent-only state is the hard part of this probe, and
    the two obvious routes are both dead ends. A completed ``stop`` always
    writes both phases, so it cannot leave one. And a job that plants an intent
    marker *for a later job* achieves nothing at all — a fact worth recording
    because it is not obvious: ``run`` clears both reserved names on every
    classification path including exit 0, so the planting job's own run consumes
    the plant before any other job can meet it (verified against docker 29.1.3
    while writing this suite, and relied on from the other direction by
    :func:`test_a_symlink_planted_at_a_marker_path_costs_a_cancellation_its_name_and_nothing_else`,
    which is why the job that plants there has to be the job that is stopped).

    So this test does the only remaining thing, and does it in the attacker's
    favour: it *tells the job its own job id*, by naming the job on the command
    line and writing the same string into the intent marker from inside the
    container. That is deliberately more than the threat model allows, so that
    the properties above it can be tested on their own.

    Two facts, and the second one moved. **The record is a failure** — intent
    means an operator asked, never that they succeeded — which was true before
    the workspace token existed and is unchanged by it. **And the run does not
    pay the settle wait**, which is new. Holding the correct job id used to be
    enough to satisfy ``_ended_by_an_operator``'s intent test and send ``run``
    into :meth:`~headspace.providers.docker.DockerProvider._await_countersignal`
    for two seconds; the job could not manufacture a cancellation that way, but
    it could make every one of its own runs burn the budget. Now the intent
    marker must carry the workspace's token too, so a job's self-planted intent
    fails to match and is simply cleared. Closing a forgery closed a small
    denial-of-service lever with it, which is worth pinning so a future change
    that loosens the intent test cannot quietly reopen both.

    The settle wait's *timeout* path — a real intent marker with a countersignal
    that never arrives — is no longer reachable from inside a container at all,
    which is the point. It is covered where it can now be staged honestly:
    ``test_the_wait_for_a_countersignal_ends`` in
    ``tests/test_docker_classification.py``.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    victim = "self-named-intent-job"

    package = orch.run(
        workspace_id,
        (
            "/bin/sh",
            "-c",
            # The job id is handed to the job in argv, which headspace itself
            # never does — see this test's docstring for why that is the point.
            f"printf '{victim}' > {_INTENT_MARKER}; echo planted-intent-for-myself; exit 9",
        ),
        job_id=victim,
    )

    assert package.status == STATUS_FAILURE, (
        "an intent marker naming the job that just failed was enough to record "
        "a cancellation; intent means an operator asked, never that they succeeded"
    )
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    planted = recorded[victim]
    assert planted["status"] == STATUS_FAILURE
    assert planted["exit_status"] == 9, "the job's own exit status was not preserved"
    assert planted["usage"]["wall_time_seconds"] < _CANCELLATION_SETTLE_SECONDS, (
        "the run paid the settle wait for an intent marker the job wrote itself, "
        "so the intent test is matching on something a job can produce — the "
        f"workspace token is supposed to prevent that: {planted['usage']['wall_time_seconds']}s"
    )


def test_a_countersignal_a_job_planted_for_itself_is_refused(
    home: Path, workspace: Any, orch: Orchestrator
) -> None:
    """The forgery this test used to record, now refused — live (issue #20).

    Its previous incarnation was committed deliberately as a *recorded
    weakness*, and it ended with an instruction to whoever closed it:

        If a future change makes this test fail — because the channel grew a
        provenance check, a nonce, or a write path a job cannot reach — that is
        an improvement, and this test should be rewritten to assert the refusal
        rather than deleted.

    That change arrived. What it recorded was the guarantee's real hinge: the
    provider called the channel unforgeable and justified it with "the one
    string it cannot write is its own job id", which is a conditional, and
    ``--job-id`` is the *caller's* to choose. A caller who picked predictable
    ids and ran code it did not trust had told the job its id by another route,
    and the job could then write its own countersignal and be believed —
    recorded ``cancelled``, with no operator anywhere, asserting that a human
    deliberately ended work that in truth broke by itself.

    The fix is not to make the write harder to reach — a job with a shell in
    the volume can always write these bytes. It is to make the *payload*
    contain something no job can obtain: a per-workspace token minted at
    ``create`` and carried as a label on the anchor container, which is engine
    metadata no process inside any container can read, on an object that is not
    even the job's own container. So the body below is unchanged from the
    version that forged successfully — same job, same id, same bytes at the
    same path — and only the verdict moved.

    That is what makes this evidence rather than assertion: the attack was not
    weakened to make the test pass.

    What this is still NOT evidence of: safety against anyone who can reach the
    Docker socket or ``~/.headspace``. They can read the label and edit the
    record respectively. The adversary here is the untrusted code inside the
    box, which is the one that cannot be reasoned with.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    forger = "self-named-countersignal-job"

    package = orch.run(
        workspace_id,
        (
            "/bin/sh",
            "-c",
            f"printf '{forger}' > {_SIGNALLED_MARKER}; echo planted-countersignal; exit 9",
        ),
        job_id=forger,
    )

    assert package.status == STATUS_FAILURE, (
        "a job forged a cancellation for itself using only its own job id — the "
        "workspace token is meant to make that impossible; check that create "
        "still writes the anchor label and that run still requires it"
    )
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    assert recorded[forger]["status"] == STATUS_FAILURE
    assert recorded[forger]["exit_status"] == 9, (
        "the job's own exit status was lost, so the refusal cost more than the "
        "forged cancellation it refused"
    )


def test_a_job_both_operator_stopped_and_past_its_wall_clock_budget_is_recorded_cancelled(
    home: Path, engine: docker.DockerClient, workspace: Any
) -> None:
    """Two ways to end at once, and the documented precedence decides — cancelled > timeout.

    ``_status`` ranks the three ways a job can be ended by something other than
    itself: ``cancelled`` beats ``timeout`` beats ``resource_exhausted``,
    heaviest deliberation first. Both of the top two are made true here in one
    window, against a real engine, rather than by passing two booleans to a
    static method: a workspace with a six-second wall-clock budget runs a
    ``sleep 60`` that nothing will end politely, and an operator stops it a
    fraction of a second after it starts.

    The two events then genuinely collide. ``stop`` writes its intent, sends a
    SIGTERM that PID 1 discards, and settles in to wait out its ten-second
    grace period — but headspace's own enforcer kills the container at six
    seconds, well inside that grace. So the container is killed by the timeout
    path and named by the cancellation path, and the assertions pin both halves
    independently:

    * ``stop`` returning in comfortably less than :data:`_STOP_GRACE_SECONDS`
      is the evidence that *something else* killed the container, because a
      stop that had to escalate could not have returned before its own grace
      period elapsed. The only other killer in the system is the wall-clock
      enforcer.
    * a recorded wall time at or past the budget is the evidence that the
      budget was genuinely exceeded rather than merely configured.
    * and the recorded status is ``cancelled`` — exit 5, no exit status — not
      ``timeout``/exit 4, which is what this job would have been called had the
      operator not been involved.

    That ordering is a claim about what a *caller* should do next, which is why
    it is worth a live test: an agent told ``timeout`` widens the budget and
    re-runs a job a human just deliberately ended.

    What this is NOT evidence of: the branch order inside ``_status``. A
    unit-level test pins each precedence pair with the conditions beneath it
    true at the same time; this one proves the two conditions can actually
    co-occur in a real workspace and that the live path agrees with the table.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id, "--wall-clock-seconds", str(_SHORT_WALL_CLOCK_SECONDS))

    runner = _cli_background(
        "run",
        "--json",
        "--job-id",
        "raced-job",
        workspace_id,
        "sleep",
        _LONG_JOB_SECONDS,
        home=home,
    )
    try:
        _await_running_job_container(engine, workspace_id)

        began = time.monotonic()
        stopped = _cli("stop", "--json", "--apply", workspace_id, home=home)
        elapsed = time.monotonic() - began

        assert stopped.returncode == _EXIT_CANCELLED, (
            f"stop --apply exited {stopped.returncode}, expected {_EXIT_CANCELLED}; "
            f"stderr: {stopped.stderr}"
        )
        assert elapsed < _STOP_GRACE_SECONDS, (
            f"stop took {elapsed:.2f}s, at least its own {_STOP_GRACE_SECONDS}s grace "
            "period — so the container died to the stop's escalation and the "
            "wall-clock enforcer never got there first; the two events did not coincide"
        )
        out, err = runner.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if runner.poll() is None:  # pragma: no cover - only on a wedged engine
            runner.kill()
            runner.communicate(timeout=30)

    package = json.loads(out)
    assert package["status"] == STATUS_CANCELLED, (
        f"a job that was both stopped and out of time was recorded "
        f"{package['status']!r}; the documented precedence is cancelled > timeout"
    )
    assert package["status"] != _STATUS_TIMEOUT
    assert runner.returncode == _EXIT_CANCELLED, (
        f"the interrupted run exited {runner.returncode} (timeout is {_EXIT_TIMEOUT}), "
        f"expected {_EXIT_CANCELLED}; stderr: {err}"
    )
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    raced = recorded["raced-job"]
    assert raced["status"] == STATUS_CANCELLED
    assert raced["exit_status"] is None
    assert raced["usage"]["wall_time_seconds"] >= _SHORT_WALL_CLOCK_SECONDS, (
        "the job did not actually outlive its wall-clock budget, so only one of "
        f"the two conditions was true: {raced['usage']['wall_time_seconds']}s "
        f"against a {_SHORT_WALL_CLOCK_SECONDS}s budget"
    )


#: Every surface a process can interrogate about its own identity, dumped from
#: inside a job. The section headers are asserted individually so a surface
#: that silently stops being readable — a ``/proc`` mount that changes shape, a
#: missing tool — fails the test instead of quietly narrowing what it covers.
#:
#: ``/proc/self/cmdline`` holds this script, so the script itself must never
#: name a job id, and it does not: the id is passed to ``headspace run`` on the
#: *host's* command line and asserted against from there.
_IDENTITY_SURFACE_DUMP = """
echo '--- env ---'
env
echo '--- hostname ---'
cat /proc/sys/kernel/hostname
cat /etc/hostname
echo '--- cgroup ---'
cat /proc/self/cgroup
echo '--- mountinfo ---'
cat /proc/self/mountinfo
echo '--- cmdline ---'
tr '\\0' ' ' < /proc/self/cmdline
echo
echo '--- pid1-cmdline ---'
tr '\\0' ' ' < /proc/1/cmdline
echo
echo '--- environ ---'
tr '\\0' '\\n' < /proc/self/environ
"""


def _dumped_sections(dump: str) -> dict[str, str]:
    """Split the surface dump above into ``{name: body}``."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in dump.splitlines():
        if line.startswith("--- ") and line.endswith(" ---"):
            current = line[4:-4]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(body) for name, body in sections.items()}


def test_no_surface_inside_a_job_container_carries_its_own_job_id(
    home: Path, workspace: Any, orch: Orchestrator
) -> None:
    """The fact the whole cancellation channel rests on, made into a standing test.

    A job shares the workspace volume with headspace's two reserved marker
    names and can write anything it likes at either. The only reason that does
    not make the channel forgeable is that a job cannot write the one string
    that would matter — its own job id — because nothing inside the container
    ever tells it one. That is a placement discipline, not a mechanism: the id
    travels as a container *name* and a ``headspace.job_id`` label, both of
    which live at the engine and neither of which a process can read from
    inside itself. A container's hostname is its engine id, not its name.

    Placement disciplines rot silently. A future change that put the job id in
    an environment variable for convenience, or set ``hostname`` to it for
    nicer logs, would leave every existing test passing and quietly hand every
    job half of what a forged countersignal needs. Only half, since issue #20:
    the payload also carries a per-workspace token a job cannot reach, and
    :func:`test_a_countersignal_a_job_planted_for_itself_is_refused`
    holds that line. Which is exactly why this test still earns its keep — the
    token is the guarantee, and this is the defence that keeps the token from
    being the *only* one. The job id is passed explicitly with ``--job-id`` so
    the exact string being hunted for is known rather than inferred.

    Every surface a process can interrogate about itself is dumped and searched:
    the whole environment, the kernel's idea of the hostname and the image's
    ``/etc/hostname``, ``/proc/self/cgroup``, ``/proc/self/mountinfo``, its own
    ``/proc/self/cmdline`` and PID 1's, and ``/proc/self/environ`` — the last
    because an environment variable unset after start still shows there. Each
    section is asserted to be non-empty, so a surface that stops being readable
    fails loudly instead of narrowing the search in silence.

    The contrast is the point of the last assertion, and it is why this is not
    an argument about obscurity. The *workspace* id is plainly visible: it is
    in the volume's name, and ``/proc/self/mountinfo`` shows the volume backing
    ``/workspace`` in full. So a job can name the box it is in without
    difficulty — headspace hides nothing on principle here. What it withholds
    is the one identifier that would let a job speak *as headspace about
    itself*, and it withholds it in exactly one place because that is where it
    matters. Recorded against docker 29.1.3: ``HOSTNAME`` was the container's
    12-hex engine id, ``/proc/self/cgroup`` read ``0::/``, and mountinfo named
    ``/var/lib/docker/volumes/headspace-<workspace id>/_data``.

    What this is NOT evidence of: that a job cannot obtain its id by some route
    outside the container — a caller who passes a predictable ``--job-id`` and
    then runs untrusted code has told it, and the forge test above records what
    follows.
    """
    workspace_id = workspace()
    orch.create(workspace_id=workspace_id)
    # Distinctive and unrelated to the workspace id, so neither string can
    # accidentally satisfy the other's assertion by being a substring of it.
    job_id = f"leakprobe-{uuid.uuid4().hex[:12]}"

    package = orch.run(workspace_id, ("/bin/sh", "-c", _IDENTITY_SURFACE_DUMP), job_id=job_id)
    assert package.status == STATUS_SUCCESS, f"the dump job failed: {package.outcome_summary}"

    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    dump = recorded[job_id]["output"]
    assert not recorded[job_id]["truncated"], "the dump was capped, so the search was partial"

    sections = _dumped_sections(dump)
    expected = {
        "env",
        "hostname",
        "cgroup",
        "mountinfo",
        "cmdline",
        "pid1-cmdline",
        "environ",
    }
    assert set(sections) == expected, f"a surface went missing from the dump: {sorted(sections)}"
    for name, body in sections.items():
        assert body.strip(), f"the {name} surface came back empty, so it was never searched"
        assert job_id not in body, (
            f"the job id leaked into the {name} surface a job can read about itself, "
            "which is the single fact the cancellation channel's unforgeability rests on"
        )

    # HOSTNAME is the engine's id for the container, never headspace's name for
    # the job — the specific confusion that would hand a job its own id.
    hostname = sections["hostname"].splitlines()[0].strip()
    # Two facts, two assertions: "the surface said something" and "what it said
    # was not the job id" fail for different reasons and deserve to be told
    # apart — an empty hostname would otherwise read as a leak that isn't one.
    assert hostname, "the container reported no hostname at all, so nothing was checked"
    assert hostname != job_id, "the container's hostname *is* the job id — the channel is open"
    assert re.fullmatch(r"[0-9a-f]{12}", hostname), (
        f"the container hostname was {hostname!r}, not an engine short id; if "
        "headspace has started setting a hostname, check it can never be the job id"
    )
    # ...while the workspace the job runs in is nameable from inside, plainly.
    assert workspace_id in sections["mountinfo"], (
        "the workspace volume was not visible in mountinfo, so this test's "
        "contrast — the box is nameable, the job is not — was never demonstrated"
    )


#: A job that plants hostile objects at both reserved names and then refuses to
#: die politely, so that the ``stop`` which follows meets them.
#:
#: Two links, two shapes, for two different assertions. The countersignal name
#: points *out of the volume* at the anchor's own ``/etc/passwd``, which is the
#: escape the refusal exists to prevent. The intent name points at a canary
#: *inside* the volume, which is the half that can be read back and compared
#: byte for byte afterwards — the anchor's ``/etc/passwd`` and the job's are
#: different files in different containers, so only the in-volume link gives a
#: target both a job and a later probe can see.
#:
#: A plain foreground ``sleep`` on purpose: the shell installs no handler, PID 1
#: discards a signal it has no handler for, and the job therefore has to be
#: killed by the escalation — which is the slow path, and the one that gives
#: ``stop`` the most opportunity to write a marker it should be refusing to.
_MARKER_LINK_PLANTING_JOB = f"""
printf 'canary-content-do-not-overwrite\\n' > /workspace/canary.txt
ln -s /etc/passwd {_SIGNALLED_MARKER}
ln -s /workspace/canary.txt {_INTENT_MARKER}
echo PLANTER-ARMED
sleep 60
"""


def test_a_symlink_planted_at_a_marker_path_costs_a_cancellation_its_name_and_nothing_else(
    home: Path, engine: docker.DockerClient, workspace: Any
) -> None:
    """A job can wedge its own cancellation. It cannot write a byte through the wedge.

    This is the same probe shape as
    :func:`test_put_archive_writes_straight_through_a_planted_ancestor_symlink`
    — a job plants the hostile object, and the real code path is then driven
    against it — applied to the two paths headspace writes on its own account
    rather than to a copy-in's destination. The threat is identical and so is
    the reason it is not theoretical: the volume is shared, a job is the
    caller's own code, and the engine resolves link targets within the
    container's filesystem quite happily.

    The job that plants is deliberately the job that is stopped, which is not
    the obvious arrangement and is the only one that works. A job that planted
    for some *later* job would achieve nothing: ``run`` clears both reserved
    names on every classification path including exit 0, so the planter's own
    run takes the plant away before anything else can meet it. So the attack
    has to be self-directed — a job protecting itself from being named — and
    that is exactly what this does.

    Four things are asserted, and the last two matter as much as the first two:

    * ``stop`` still ends the job. A marker that cannot be written must never
      withhold the signal; ending a runaway job is what the operator came for.
    * the classification degrades to ``failure``/137 — the pre-#16 answer, the
      documented direction to fail in. The job has successfully cost its own
      cancellation its name, and that is all it has done.
    * **nothing was written through either link.** The write stages to an
      unguessable nonce path and renames onto the marker's name rather than
      redirecting at it, so the planted link is replaced rather than followed —
      except that here it is refused outright before either. The canary inside
      the volume is unchanged and the anchor's ``/etc/passwd`` does not contain
      the job id, so neither the in-volume nor the out-of-volume link carried a
      byte.
    * **the channel recovers.** ``run`` clears whatever it found with ``rm -rf``,
      which takes a link and never its target, so the very next job in the very
      same workspace is stopped and recorded ``cancelled`` normally. Without
      that, one job could permanently poison a workspace's cancellation channel
      for every job that ever followed it, which would be a far worse outcome
      than the misreport it is defending itself against.

    What this is NOT evidence of: anything about a *directory* or a device node
    at a marker name. Those are refused on a separate branch of the same script
    (a rename over a directory relocates rather than fails), and are pinned by
    the provider-level suite.
    """
    workspace_id = workspace()
    _create_workspace(home, workspace_id)
    anchor = _workspace_container(engine, workspace_id)

    runner = _cli_background(
        "run",
        "--json",
        "--job-id",
        "planter-job",
        workspace_id,
        "/bin/sh",
        "-c",
        _MARKER_LINK_PLANTING_JOB,
        home=home,
    )
    try:
        container = _await_running_job_container(engine, workspace_id)
        _await_job_log(container, "PLANTER-ARMED")

        stopped = _cli("stop", "--json", "--apply", workspace_id, home=home)
        assert stopped.returncode == _EXIT_CANCELLED, (
            "stop refused to end a job because it could not write a marker; "
            f"naming the ending must never withhold it. stderr: {stopped.stderr}"
        )
        out, err = runner.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if runner.poll() is None:  # pragma: no cover - only on a wedged engine
            runner.kill()
            runner.communicate(timeout=30)

    package = json.loads(out)
    assert package["status"] == STATUS_FAILURE, (
        "a stop whose countersignal could not be written was still classified "
        f"{package['status']!r}; a refused write must fail towards silence"
    )
    assert runner.returncode == _EXIT_COMPUTATION_FAILED
    recorded = {job["job_id"]: job for job in Store().read_state(workspace_id).state["jobs"]}
    assert recorded["planter-job"]["status"] == STATUS_FAILURE
    assert recorded["planter-job"]["exit_status"] == 137  # the engine's number for the kill

    # Nothing crossed either link. The canary is inside the volume, so a later
    # probe can read the very bytes the planted link pointed at.
    canary = anchor.exec_run(["/bin/sh", "-c", "cat /workspace/canary.txt"])
    assert canary.output.decode("utf-8", "replace").strip() == "canary-content-do-not-overwrite", (
        "the marker write followed the in-volume link and overwrote its target: "
        f"{canary.output!r}"
    )
    escaped = anchor.exec_run(["/bin/sh", "-c", "grep -c planter-job /etc/passwd || true"])
    assert escaped.output.decode("utf-8", "replace").strip() == "0", (
        "a job id was written into /etc/passwd through the planted link, which "
        "is a write outside the workspace volume"
    )

    # ...and the channel is clean again, not wedged for every job that follows.
    listing = _volume_listing(engine, workspace_id)
    assert ".headspace-cancelled" not in listing, f"the planted link survived the run: {listing!r}"
    assert ".headspace-cancel-requested" not in listing, f"intent survived the run: {listing!r}"

    recovery = _cli_background(
        "run",
        "--json",
        "--job-id",
        "recovery-job",
        workspace_id,
        "sleep",
        _LONG_JOB_SECONDS,
        home=home,
    )
    try:
        _await_running_job_container(engine, workspace_id)
        again = _cli("stop", "--json", "--apply", workspace_id, home=home)
        assert again.returncode == _EXIT_CANCELLED, f"second stop failed: {again.stderr}"
        recovered_out, recovered_err = recovery.communicate(timeout=_CLI_TIMEOUT_SECONDS)
    finally:
        if recovery.poll() is None:  # pragma: no cover - only on a wedged engine
            recovery.kill()
            recovery.communicate(timeout=30)

    assert recovery.returncode == _EXIT_CANCELLED, (
        "the next job in this workspace could not be recorded cancelled, so the "
        f"planted links wedged the channel permanently; stderr: {recovered_err}"
    )
    assert json.loads(recovered_out)["status"] == STATUS_CANCELLED
